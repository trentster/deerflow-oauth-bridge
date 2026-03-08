#!/usr/bin/env python3
"""DeerFlow OAuth Bridge protocol translator.

Standalone data transformers between:
- OpenAI Chat Completions format (DeerFlow side)
- ChatGPT backend Responses format (Codex endpoint side)
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

# Streaming function-call tracking: item_id -> metadata for tool-call delta emission.
_TOOL_CALL_STATE: Dict[str, Dict[str, Any]] = {}
_TOOL_CALL_COUNTER: int = 0


def _as_text(content: Any) -> str:
    """Normalize chat message content into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                item_type = item.get("type")
                if item_type in {"text", "input_text", "output_text"}:
                    parts.append(str(item.get("text", "")))
                elif item_type == "image_url":
                    # Keep a deterministic placeholder for non-text content.
                    parts.append("[image]")
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text", ""))
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _translate_tools(chat_tools: Any) -> List[Dict[str, Any]]:
    """Map Chat Completions tools format to Responses tools format.

    Chat Completions commonly provides:
      {"type":"function","function":{"name":"...","description":"...","parameters":{...}}}

    Responses expects function definitions at top level:
      {"type":"function","name":"...","description":"...","parameters":{...}}
    """
    if not isinstance(chat_tools, list):
        return []

    out: List[Dict[str, Any]] = []
    for tool in chat_tools:
        if not isinstance(tool, dict):
            continue

        tool_type = tool.get("type", "function")

        # Already in Responses-like shape
        if tool_type == "function" and isinstance(tool.get("name"), str):
            mapped = {
                "type": "function",
                "name": tool.get("name"),
                "description": tool.get("description"),
                "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
            }
            out.append(mapped)
            continue

        # Chat Completions shape: nested function object
        fn = tool.get("function")
        if tool_type == "function" and isinstance(fn, dict) and fn.get("name"):
            mapped = {
                "type": "function",
                "name": fn.get("name"),
                "description": fn.get("description"),
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            }
            out.append(mapped)
            continue

    return out


def translate_request(chat_body: Dict[str, Any]) -> Dict[str, Any]:
    """Translate Chat Completions request body into Responses request body."""
    model = chat_body.get("model")
    stream = chat_body.get("stream", True)
    messages = chat_body.get("messages", []) or []

    system_texts: List[str] = []
    input_items: List[Dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue

        role = msg.get("role")
        content_text = _as_text(msg.get("content"))

        if role == "system":
            if content_text:
                system_texts.append(content_text)
            continue

        if role == "assistant":
            # Emit function_call items BEFORE assistant message when tool_calls exist.
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id") or _new_id("call")
                fn = tc.get("function") or {}
                name = fn.get("name") or tc.get("name") or "unknown_tool"
                arguments = fn.get("arguments") if isinstance(fn, dict) else tc.get("arguments", "")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": tc_id,
                        "name": name,
                        "arguments": arguments,
                    }
                )

            input_items.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": content_text,
                            "annotations": [],
                        }
                    ],
                    "status": "completed",
                }
            )
            continue

        if role == "tool":
            call_id = msg.get("tool_call_id")
            if call_id:
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(call_id),
                        "output": content_text,
                    }
                )
            continue

        # Default non-system user-like message handling.
        if role == "user":
            input_items.append(
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": content_text}],
                }
            )
        else:
            # Preserve unknown roles as message records to avoid data loss.
            input_items.append(
                {
                    "type": "message",
                    "role": str(role or "user"),
                    "content": [
                        {
                            "type": "output_text",
                            "text": content_text,
                            "annotations": [],
                        }
                    ],
                    "status": "completed",
                }
            )

    out: Dict[str, Any] = {
        "model": model,
        "stream": stream,
        "store": False,
        "input": input_items,
        "text": {"format": {"type": "text"}},
    }

    instructions = "\n\n".join(t for t in system_texts if t).strip()
    if not instructions:
        instructions = "You are a helpful assistant."
    out["instructions"] = instructions

    if "temperature" in chat_body:
        out["temperature"] = chat_body["temperature"]

    if "tools" in chat_body:
        translated_tools = _translate_tools(chat_body["tools"])
        if translated_tools:
            out["tools"] = translated_tools

    if "tool_choice" in chat_body:
        out["tool_choice"] = chat_body["tool_choice"]

    return out


def translate_response(responses_body: Dict[str, Any]) -> Dict[str, Any]:
    """Translate non-streaming Responses body into Chat Completions body."""
    output = responses_body.get("output") or []

    content_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []

    for item in output:
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")

        if item_type == "function_call":
            call_id = item.get("call_id") or item.get("id") or _new_id("call")
            name = item.get("name") or "unknown_tool"
            arguments = item.get("arguments", "")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            tool_calls.append(
                {
                    "id": str(call_id),
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )
            continue

        # Responses often wraps text in message.content items.
        if item_type == "message":
            for c in item.get("content", []) or []:
                if isinstance(c, dict) and c.get("type") in {"output_text", "text", "input_text"}:
                    txt = c.get("text")
                    if txt:
                        content_parts.append(str(txt))
            continue

        # Some integrations may emit top-level output_text items.
        if item_type in {"output_text", "text"}:
            txt = item.get("text")
            if txt:
                content_parts.append(str(txt))

    message: Dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(content_parts).strip(),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    usage = responses_body.get("usage")
    out: Dict[str, Any] = {
        "id": responses_body.get("id") or _new_id("chatcmpl"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": responses_body.get("model"),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "stop",
            }
        ],
    }
    if usage is not None:
        out["usage"] = usage

    return out


def translate_stream_event(event_type: str, event_data: Dict[str, Any]) -> Optional[str]:
    """Translate one Responses SSE event to one Chat Completions SSE chunk string.

    Returns a "data: {...}\n\n" chunk string, or None to skip unsupported events.
    """
    global _TOOL_CALL_COUNTER

    base = {
        "id": event_data.get("response", {}).get("id") or event_data.get("id") or _new_id("chatcmpl"),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": event_data.get("response", {}).get("model") or event_data.get("model"),
    }

    if event_type == "response.created":
        _TOOL_CALL_STATE.clear()
        _TOOL_CALL_COUNTER = 0
        return None

    if event_type == "response.output_text.delta":
        delta = event_data.get("delta", "")
        chunk = {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": str(delta)},
                    "finish_reason": None,
                }
            ],
        }
        return "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"

    if event_type == "response.output_item.added":
        item = event_data.get("item") if isinstance(event_data, dict) else None
        if isinstance(item, dict) and item.get("type") == "function_call":
            item_id = str(item.get("id") or "")
            call_id = str(item.get("call_id") or _new_id("call"))
            name = str(item.get("name") or "unknown_tool")
            tool_index = _TOOL_CALL_COUNTER
            _TOOL_CALL_COUNTER += 1
            if item_id:
                _TOOL_CALL_STATE[item_id] = {
                    "call_id": call_id,
                    "name": name,
                    "index": tool_index,
                }
            chunk = {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": tool_index,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": name, "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
            return "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"

    if event_type in {"response.function_call.arguments.delta", "response.function_call_arguments.delta"}:
        item_id = str(event_data.get("item_id") or "") if isinstance(event_data, dict) else ""
        state = _TOOL_CALL_STATE.get(item_id, {}) if item_id else {}
        output_index = int(event_data.get("output_index", 0)) if isinstance(event_data, dict) else 0
        tool_index = int(state.get("index", output_index))
        delta = event_data.get("delta", "") if isinstance(event_data, dict) else ""
        tool_call_obj: Dict[str, Any] = {
            "index": tool_index,
            "function": {"arguments": str(delta)},
        }
        if state.get("call_id"):
            tool_call_obj["id"] = state.get("call_id")
            tool_call_obj["type"] = "function"
        chunk = {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [tool_call_obj]},
                    "finish_reason": None,
                }
            ],
        }
        return "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"

    if event_type == "response.output_item.done":
        item = event_data.get("item") if isinstance(event_data, dict) else None
        if isinstance(item, dict) and item.get("type") == "function_call":
            item_id = str(item.get("id") or "")
            if item_id:
                _TOOL_CALL_STATE.pop(item_id, None)
        return None

    if event_type == "response.completed":
        finish_reason = "stop"
        _TOOL_CALL_STATE.clear()
        _TOOL_CALL_COUNTER = 0
        response_obj = event_data.get("response") if isinstance(event_data, dict) else None
        output_items = response_obj.get("output") if isinstance(response_obj, dict) else None
        if isinstance(output_items, list):
            if any(isinstance(it, dict) and it.get("type") == "function_call" for it in output_items):
                finish_reason = "tool_calls"
        chunk = {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": finish_reason,
                }
            ],
        }
        return "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"

    if event_type in {"response.failed", "error"}:
        error_obj = event_data.get("error") if isinstance(event_data, dict) else None
        if error_obj is None:
            error_obj = {
                "type": "backend_error",
                "message": event_data.get("message", "Unknown stream error") if isinstance(event_data, dict) else "Unknown stream error",
            }
        return "data: " + json.dumps({"error": error_obj}, ensure_ascii=False) + "\n\n"

    return None


if __name__ == "__main__":
    # Example 1: Chat -> Responses request translation
    chat_request = {
        "model": "gpt-5",
        "stream": True,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "You are concise."},
            {"role": "user", "content": "Summarize this repo."},
            {
                "role": "assistant",
                "content": "I will inspect files.",
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "list_files", "arguments": '{"path":"."}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_123", "content": "[\"README.md\", \"src/app.py\"]"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ],
    }

    translated_request = translate_request(chat_request)
    print("=== Chat -> Responses ===")
    print(json.dumps(translated_request, indent=2, ensure_ascii=False))

    # Example 2: Responses -> Chat non-stream response translation
    responses_reply = {
        "id": "resp_abc",
        "model": "gpt-5",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "Here is the summary.", "annotations": []}
                ],
                "status": "completed",
            },
            {
                "type": "function_call",
                "call_id": "call_123",
                "name": "list_files",
                "arguments": '{"path":"."}',
            },
        ],
        "usage": {"input_tokens": 10, "output_tokens": 6, "total_tokens": 16},
    }

    translated_response = translate_response(responses_reply)
    print("\n=== Responses -> Chat ===")
    print(json.dumps(translated_response, indent=2, ensure_ascii=False))

    # Example 3: SSE event translations
    print("\n=== SSE Event Translations ===")
    sse_examples = [
        ("response.output_text.delta", {"delta": "Hello ", "response": {"id": "resp_stream", "model": "gpt-5"}}),
        (
            "response.function_call.arguments.delta",
            {"call_id": "call_123", "name": "list_files", "delta": '{"path":"', "response": {"id": "resp_stream", "model": "gpt-5"}},
        ),
        ("response.completed", {"response": {"id": "resp_stream", "model": "gpt-5"}}),
        ("error", {"error": {"type": "backend_error", "message": "Something failed"}}),
    ]
    for etype, edata in sse_examples:
        print(f"\nEvent: {etype}")
        print(translate_stream_event(etype, edata))
