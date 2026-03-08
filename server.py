#!/usr/bin/env python3
"""DeerFlow OAuth Bridge FastAPI proxy.

Bridges OpenAI Chat Completions requests to ChatGPT backend Responses API
using Codex OAuth credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional, Tuple

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from oauth import AUTH_FILE, get_valid_token, load_credentials, login, refresh_tokens
from translator import translate_request, translate_response, translate_stream_event

UPSTREAM_URL = "https://chatgpt.com/backend-api/codex/responses"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8462
ORIGINATOR = "deerflow-bridge"
LOG_PATH = Path(__file__).resolve().parent / "bridge.log"
DEBUG_LOG = False

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3

app = FastAPI(title="DeerFlow OAuth Bridge", version="0.1.0")


def _log_event(kind: str, payload: Dict[str, Any]) -> None:
    if not DEBUG_LOG:
        return
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": int(time.time()),
            "kind": kind,
            "payload": payload,
        }
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _build_headers(access_token: str, account_id: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": ORIGINATOR,
        "accept": "text/event-stream",
        "content-type": "application/json",
    }


def _extract_error_body(resp: httpx.Response) -> Dict[str, Any]:
    try:
        body = resp.json()
        if isinstance(body, dict):
            return body
        return {"raw": body}
    except Exception:
        raw = getattr(resp, "content", b"") or b""
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = str(raw)
        return {"raw": text}


def _parse_error_bytes(raw: bytes) -> Dict[str, Any]:
    if not raw:
        return {}
    text = raw.decode("utf-8", errors="replace")
    try:
        body = json.loads(text)
        if isinstance(body, dict):
            return body
        return {"raw": body}
    except Exception:
        return {"raw": text}


def _error_to_http(status: int, body: Dict[str, Any], fallback: str) -> HTTPException:
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        err = {"message": fallback}

    detail: Dict[str, Any] = {
        "error": {
            "type": err.get("type") or "backend_error",
            "code": err.get("code"),
            "message": err.get("message") or fallback,
            "upstream_status": status,
        }
    }

    if err.get("resets_at") is not None:
        detail["error"]["resets_at"] = err.get("resets_at")
    if err.get("plan_type") is not None:
        detail["error"]["plan_type"] = err.get("plan_type")
    if body.get("raw") is not None:
        detail["error"]["raw"] = body.get("raw")

    return HTTPException(status_code=status, detail=detail)


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _refresh_once_token() -> Dict[str, str]:
    creds = load_credentials()
    if not creds:
        return login()
    refreshed = refresh_tokens(creds)
    return {
        "access_token": refreshed["access_token"],
        "account_id": refreshed["account_id"],
    }


async def _call_json_with_retries(payload: Dict[str, Any]) -> httpx.Response:
    """Upstream non-stream call with auth refresh + retry policy."""
    timeout = httpx.Timeout(connect=30.0, read=180.0, write=30.0, pool=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        token_info = get_valid_token()
        auth_refreshed = False

        attempt = 0
        while True:
            headers = _build_headers(token_info["access_token"], token_info["account_id"])
            resp = await client.post(UPSTREAM_URL, headers=headers, json=payload)
            status = resp.status_code

            if status in (401, 403):
                if auth_refreshed:
                    return resp
                token_info = _refresh_once_token()
                auth_refreshed = True
                continue

            if status in RETRYABLE_STATUSES and attempt < MAX_RETRIES:
                delay = 2**attempt
                await _sleep(delay)
                attempt += 1
                continue

            return resp


async def _open_stream_with_retries(
    payload: Dict[str, Any],
) -> Tuple[httpx.AsyncClient, httpx.Response]:
    """Open upstream streaming response with retry policy.

    Returns an open (client, response) pair; caller must close both.
    """
    timeout = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)
    client = httpx.AsyncClient(timeout=timeout)

    try:
        token_info = get_valid_token()
        auth_refreshed = False
        attempt = 0

        while True:
            headers = _build_headers(token_info["access_token"], token_info["account_id"])
            req = client.build_request("POST", UPSTREAM_URL, headers=headers, json=payload)
            resp = await client.send(req, stream=True)
            status = resp.status_code

            if status in (401, 403):
                await resp.aclose()
                if auth_refreshed:
                    return client, resp
                token_info = _refresh_once_token()
                auth_refreshed = True
                continue

            if status in RETRYABLE_STATUSES and attempt < MAX_RETRIES:
                await resp.aclose()
                delay = 2**attempt
                await _sleep(delay)
                attempt += 1
                continue

            return client, resp
    except Exception:
        await client.aclose()
        raise


def _parse_sse_event(raw_event: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    event_type: Optional[str] = None
    data_lines = []

    for line in raw_event.splitlines():
        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())

    if not data_lines:
        return event_type, None

    data_text = "\n".join(data_lines)
    if data_text == "[DONE]":
        return "done", {"done": True}

    try:
        return event_type, json.loads(data_text)
    except Exception:
        return event_type, {"raw": data_text}


async def _collect_nonstream_chat_completion(
    upstream_client: httpx.AsyncClient, upstream_resp: httpx.Response
) -> Dict[str, Any]:
    """Consume upstream SSE and synthesize a non-stream chat completion payload."""
    try:
        if upstream_resp.status_code >= 400:
            raw = await upstream_resp.aread()
            body = _parse_error_bytes(raw)
            raise _error_to_http(upstream_resp.status_code, body, "Upstream request failed")

        buffer = ""
        fallback_content = ""
        completed_response: Optional[Dict[str, Any]] = None

        async for line in upstream_resp.aiter_lines():
            if line is None:
                continue

            if line == "":
                if not buffer.strip():
                    continue

                event_type, event_data = _parse_sse_event(buffer)
                _log_event("upstream_sse_event", {"event_type": event_type, "event_data": event_data})
                buffer = ""

                if event_type in {"error", "response.failed"}:
                    err = {}
                    if isinstance(event_data, dict):
                        if isinstance(event_data.get("error"), dict):
                            err = event_data["error"]
                        elif isinstance(event_data.get("response", {}).get("error"), dict):
                            err = event_data["response"]["error"]
                    raise _error_to_http(502, {"error": err or {"message": "Upstream SSE error"}}, "Upstream SSE error")

                if event_type == "response.output_text.delta" and isinstance(event_data, dict):
                    fallback_content += str(event_data.get("delta", ""))

                if event_type == "response.completed" and isinstance(event_data, dict):
                    resp_obj = event_data.get("response")
                    if isinstance(resp_obj, dict):
                        completed_response = resp_obj
                        break

                if event_type == "done":
                    break

                continue

            buffer += line + "\n"

        if completed_response is not None:
            translated = translate_response(completed_response)
            _log_event("translated_nonstream_response", {"response": translated})
            return translated

        # Fallback if completed response object was not present.
        fallback = {
            "id": f"chatcmpl_fallback_{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": None,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": fallback_content.strip()},
                    "finish_reason": "stop",
                }
            ],
        }
        _log_event("translated_nonstream_response", {"response": fallback})
        return fallback
    finally:
        await upstream_resp.aclose()
        await upstream_client.aclose()


async def _stream_translated_chunks(
    upstream_client: httpx.AsyncClient,
    upstream_resp: httpx.Response,
    requested_model: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    try:
        if upstream_resp.status_code >= 400:
            raw = await upstream_resp.aread()
            body = _parse_error_bytes(raw)
            err = body.get("error") if isinstance(body, dict) else None
            if not isinstance(err, dict):
                err = {
                    "type": "backend_error",
                    "message": f"Upstream HTTP {upstream_resp.status_code}",
                    "raw": body.get("raw") if isinstance(body, dict) else None,
                }
            err_line = "data: " + json.dumps({"error": err}, ensure_ascii=False) + "\n\n"
            _log_event("translated_chunk", {"chunk": err_line})
            yield err_line
            done_line = "data: [DONE]\n\n"
            _log_event("translated_chunk", {"chunk": done_line})
            yield done_line
            return

        stream_id = f"chatcmpl-{uuid.uuid4().hex}"
        stream_model = requested_model
        role_chunk_sent = False

        buffer = ""
        async for line in upstream_resp.aiter_lines():
            if line is None:
                continue

            if line == "":
                if not buffer.strip():
                    continue

                event_type, event_data = _parse_sse_event(buffer)
                _log_event("upstream_sse_event", {"event_type": event_type, "event_data": event_data})
                buffer = ""

                if isinstance(event_data, dict):
                    resp_meta = event_data.get("response")
                    if isinstance(resp_meta, dict) and resp_meta.get("model"):
                        stream_model = resp_meta.get("model")

                if event_type == "done":
                    done_line = "data: [DONE]\n\n"
                    _log_event("translated_chunk", {"chunk": done_line})
                    yield done_line
                    return

                if event_type is None:
                    continue

                translated = translate_stream_event(event_type, event_data or {})
                if translated:
                    payload = translated[len("data: ") :].strip()
                    if payload.endswith("\n\n"):
                        payload = payload[:-2]
                    try:
                        chunk_obj = json.loads(payload)
                    except Exception:
                        _log_event("translated_chunk", {"chunk": translated})
                        yield translated
                        continue

                    # Send assistant role once before first content/tool delta chunk.
                    if (not role_chunk_sent) and event_type == "response.output_text.delta":
                        role_chunk = {
                            "id": stream_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": stream_model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"role": "assistant"},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        role_line = "data: " + json.dumps(role_chunk, ensure_ascii=False) + "\n\n"
                        _log_event("translated_chunk", {"chunk": role_line})
                        yield role_line
                        role_chunk_sent = True

                    chunk_obj["id"] = stream_id
                    chunk_obj["model"] = stream_model
                    out_line = "data: " + json.dumps(chunk_obj, ensure_ascii=False) + "\n\n"
                    _log_event("translated_chunk", {"chunk": out_line})
                    yield out_line

                    if event_type in {"response.failed", "error"}:
                        done_line = "data: [DONE]\n\n"
                        _log_event("translated_chunk", {"chunk": done_line})
                        yield done_line
                        return
                continue

            buffer += line + "\n"

        if buffer.strip():
            event_type, event_data = _parse_sse_event(buffer)
            _log_event("upstream_sse_event", {"event_type": event_type, "event_data": event_data})
            if event_type:
                translated = translate_stream_event(event_type, event_data or {})
                if translated:
                    payload = translated[len("data: ") :].strip()
                    if payload.endswith("\n\n"):
                        payload = payload[:-2]
                    try:
                        chunk_obj = json.loads(payload)
                        chunk_obj["id"] = stream_id
                        chunk_obj["model"] = stream_model
                        out_line = "data: " + json.dumps(chunk_obj, ensure_ascii=False) + "\n\n"
                        _log_event("translated_chunk", {"chunk": out_line})
                        yield out_line
                    except Exception:
                        _log_event("translated_chunk", {"chunk": translated})
                        yield translated

        done_line = "data: [DONE]\n\n"
        _log_event("translated_chunk", {"chunk": done_line})
        yield done_line
    finally:
        await upstream_resp.aclose()
        await upstream_client.aclose()


@app.get("/health")
def health() -> Dict[str, Any]:
    creds = load_credentials()
    authenticated = bool(creds and creds.get("access_token"))
    expires = creds.get("expires") if isinstance(creds, dict) else None

    return {
        "ok": True,
        "authenticated": authenticated,
        "auth_file": str(AUTH_FILE),
        "token_expires": expires,
        "token_expires_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires)) if expires else None,
    }


@app.get("/v1/models")
def models() -> Dict[str, Any]:
    now = int(time.time())
    model_ids = [
        "gpt-5",
        "gpt-5.1",
        "gpt-5.1-codex",
        "gpt-5.2",
        "gpt-5.2-codex",
        "gpt-5.3-codex",
        "gpt-5.4",
    ]
    return {
        "object": "list",
        "data": [
            {"id": mid, "object": "model", "created": now, "owned_by": "openai"}
            for mid in model_ids
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    try:
        chat_body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "Invalid JSON body"}})

    if not isinstance(chat_body, dict):
        raise HTTPException(status_code=400, detail={"error": {"message": "JSON body must be an object"}})

    translated_request = translate_request(chat_body)
    stream = bool(chat_body.get("stream", True))

    _log_event(
        "outbound_request",
        {
            "stream": stream,
            "upstream_url": UPSTREAM_URL,
            "request": translated_request,
        },
    )

    if not stream:
        nonstream_upstream_request = dict(translated_request)
        nonstream_upstream_request["stream"] = True
        upstream_client, upstream_resp = await _open_stream_with_retries(nonstream_upstream_request)
        translated = await _collect_nonstream_chat_completion(upstream_client, upstream_resp)
        return JSONResponse(translated)

    upstream_client, upstream_resp = await _open_stream_with_retries(translated_request)
    return StreamingResponse(
        _stream_translated_chunks(upstream_client, upstream_resp, requested_model=translated_request.get("model")),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.on_event("startup")
def _startup_auth_warmup() -> None:
    creds = load_credentials()
    auth_status = "cached"

    if not creds or not creds.get("access_token"):
        auth_status = "login-required"
        token_info = login()
        creds = load_credentials() or {}
        auth_status = f"authenticated ({token_info.get('account_id', 'unknown-account')})"

    expires = creds.get("expires") if isinstance(creds, dict) else None
    expires_str = (
        time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(int(expires))) if expires else "unknown"
    )

    print("\nDeerFlow OAuth Bridge ready")
    print(f"- Listening: http://{LISTEN_HOST}:{LISTEN_PORT}")
    print(f"- Auth status: {auth_status}")
    print(f"- Token expiry: {expires_str}")
    print(f"- Auth file: {AUTH_FILE}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DeerFlow OAuth Bridge server")
    parser.add_argument("--debug", action="store_true", help="Enable detailed bridge.log debug logging")
    args = parser.parse_args()

    if args.debug:
        DEBUG_LOG = True
        print(f"Debug logging enabled: {LOG_PATH}")

    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT)
