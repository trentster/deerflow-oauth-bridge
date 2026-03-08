#!/usr/bin/env python3
"""Standalone pre-flight tests for DeerFlow OAuth Bridge proxy."""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, Tuple

import httpx

BASE_URL = "http://127.0.0.1:8462"
TIMEOUT = 60.0


def _print_header(name: str) -> None:
    print(f"\n=== {name} ===")


def _pass(name: str, detail: str = "") -> bool:
    print(f"PASS: {name}")
    if detail:
        print(detail)
    return True


def _fail(name: str, detail: str = "", response: httpx.Response | None = None) -> bool:
    print(f"FAIL: {name}")
    if detail:
        print(detail)
    if response is not None:
        print(f"HTTP {response.status_code}")
        try:
            print(json.dumps(response.json(), indent=2, ensure_ascii=False))
        except Exception:
            print(response.text)
    return False


def test_health(client: httpx.Client) -> bool:
    name = "Test 1 — Health check"
    _print_header(name)
    print(f"GET {BASE_URL}/health")

    try:
        resp = client.get(f"{BASE_URL}/health")
    except Exception as exc:
        return _fail(name, f"Request error: {exc}")

    if resp.status_code != 200:
        return _fail(name, "Expected HTTP 200", resp)

    try:
        body = resp.json()
    except Exception as exc:
        return _fail(name, f"Invalid JSON: {exc}", resp)

    auth_status = body.get("authenticated")
    return _pass(name, f"authenticated={auth_status}, token_expires={body.get('token_expires_iso')}")


def test_models(client: httpx.Client) -> bool:
    name = "Test 2 — Models list"
    _print_header(name)
    print(f"GET {BASE_URL}/v1/models")

    try:
        resp = client.get(f"{BASE_URL}/v1/models")
    except Exception as exc:
        return _fail(name, f"Request error: {exc}")

    if resp.status_code != 200:
        return _fail(name, "Expected HTTP 200", resp)

    try:
        body = resp.json()
    except Exception as exc:
        return _fail(name, f"Invalid JSON: {exc}", resp)

    data = body.get("data")
    if not isinstance(data, list) or len(data) == 0:
        return _fail(name, "Expected non-empty data[] model list", resp)

    model_ids = [m.get("id") for m in data if isinstance(m, dict)]
    return _pass(name, f"models={model_ids}")


def _chat_payload(stream: bool) -> Dict[str, Any]:
    return {
        "model": "gpt-5",
        "stream": stream,
        "messages": [
            {"role": "user", "content": "Say hello in exactly three words."}
        ],
    }


def test_non_stream(client: httpx.Client) -> bool:
    name = "Test 3 — Non-streaming completion"
    _print_header(name)

    payload = _chat_payload(stream=False)
    print(f"POST {BASE_URL}/v1/chat/completions")
    print("Sent payload:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    try:
        resp = client.post(f"{BASE_URL}/v1/chat/completions", json=payload)
    except Exception as exc:
        return _fail(name, f"Request error: {exc}")

    if resp.status_code != 200:
        return _fail(name, "Expected HTTP 200", resp)

    try:
        body = resp.json()
    except Exception as exc:
        return _fail(name, f"Invalid JSON: {exc}", resp)

    try:
        content = body["choices"][0]["message"]["content"]
    except Exception:
        return _fail(name, "Missing choices[0].message.content", resp)

    return _pass(name, f"Received content: {content!r}")


def test_tools_mapping(client: httpx.Client) -> bool:
    name = "Test 5 — Tools request mapping"
    _print_header(name)

    payload = {
        "model": "gpt-5",
        "stream": False,
        "messages": [
            {"role": "user", "content": "Say hello briefly."}
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the web",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"}
                        },
                        "required": ["query"],
                    },
                },
            }
        ],
    }

    print(f"POST {BASE_URL}/v1/chat/completions (tools included)")
    print("Sent payload:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    try:
        resp = client.post(f"{BASE_URL}/v1/chat/completions", json=payload)
    except Exception as exc:
        return _fail(name, f"Request error: {exc}")

    if resp.status_code != 200:
        return _fail(name, "Expected HTTP 200", resp)

    try:
        body = resp.json()
        _ = body["choices"][0]["message"]
    except Exception:
        return _fail(name, "Missing choices[0].message in response", resp)

    return _pass(name, "Tools-format request accepted")


def test_tool_call_streaming(client: httpx.Client) -> bool:
    name = "Test 6 — Tool-call streaming round-trip"
    _print_header(name)

    payload = {
        "model": "gpt-5",
        "stream": True,
        "tool_choice": "required",
        "messages": [
            {"role": "user", "content": "What is the weather today in Burnie Tasmania?"}
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the web.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            }
        ],
    }

    saw_tool_delta = False
    saw_tool_finish = False
    raw_lines = []

    try:
        with client.stream("POST", f"{BASE_URL}/v1/chat/completions", json=payload) as resp:
            if resp.status_code != 200:
                return _fail(name, "Expected HTTP 200", resp)

            for line in resp.iter_lines():
                if line is None:
                    continue
                line = line.strip()
                if not line:
                    continue
                raw_lines.append(line)

                if line == "data: [DONE]":
                    break
                if not line.startswith("data: "):
                    continue

                data_str = line[len("data: "):]
                try:
                    obj = json.loads(data_str)
                except Exception:
                    continue

                choices = obj.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue

                c0 = choices[0]
                delta = c0.get("delta", {})
                if isinstance(delta, dict) and isinstance(delta.get("tool_calls"), list) and delta["tool_calls"]:
                    saw_tool_delta = True
                if c0.get("finish_reason") == "tool_calls":
                    saw_tool_finish = True

    except Exception as exc:
        return _fail(name, f"Stream request error: {exc}")

    if not saw_tool_delta:
        return _fail(name, detail="No tool_calls delta detected\n" + "\n".join(raw_lines[-40:]))
    if not saw_tool_finish:
        return _fail(name, detail="No finish_reason=tool_calls detected\n" + "\n".join(raw_lines[-40:]))

    return _pass(name, "Tool-call deltas and finish_reason=tool_calls detected")


def test_stream(client: httpx.Client) -> bool:
    name = "Test 4 — Streaming completion"
    _print_header(name)

    payload = _chat_payload(stream=True)
    print(f"POST {BASE_URL}/v1/chat/completions (stream=true)")
    print("Sent payload:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    saw_delta = False
    saw_done = False
    raw_lines = []

    try:
        with client.stream("POST", f"{BASE_URL}/v1/chat/completions", json=payload) as resp:
            if resp.status_code != 200:
                return _fail(name, "Expected HTTP 200", resp)

            for line in resp.iter_lines():
                if line is None:
                    continue
                line = line.strip()
                if not line:
                    continue

                raw_lines.append(line)
                print(line)

                if line == "data: [DONE]":
                    saw_done = True
                    break

                if not line.startswith("data: "):
                    continue

                data_str = line[len("data: "):]
                try:
                    obj = json.loads(data_str)
                except Exception:
                    continue

                choices = obj.get("choices")
                if isinstance(choices, list) and choices:
                    delta = choices[0].get("delta", {})
                    if isinstance(delta, dict) and delta:
                        saw_delta = True
    except Exception as exc:
        return _fail(name, f"Stream request error: {exc}")

    if not saw_delta:
        return _fail(name, detail="No delta chunks detected in stream\n" + "\n".join(raw_lines[-20:]))
    if not saw_done:
        return _fail(name, "Stream did not end with data: [DONE]", detail="\n".join(raw_lines[-20:]))

    return _pass(name, "Received delta chunks and [DONE] terminator")


def main() -> int:
    print("DeerFlow OAuth Bridge pre-flight tests")
    print(f"Target: {BASE_URL}")

    results = []
    with httpx.Client(timeout=TIMEOUT) as client:
        results.append(test_health(client))
        results.append(test_models(client))
        results.append(test_non_stream(client))
        results.append(test_tools_mapping(client))
        results.append(test_tool_call_streaming(client))
        results.append(test_stream(client))

    passed = sum(1 for r in results if r)
    total = len(results)

    print("\n=== Summary ===")
    print(f"Passed {passed}/{total} tests")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
