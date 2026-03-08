# DeerFlow OAuth Bridge — Task 1 + Task 2 Findings

Generated: 2026-03-08 (Australia/Hobart)

---

## Final Implementation Status (2026-03-08)

- OAuth module, translator module, FastAPI proxy, packaging, and preflight harness are implemented.
- Confirmed Codex OAuth working models: `gpt-5`, `gpt-5.1`, `gpt-5.1-codex`, `gpt-5.2`, `gpt-5.2-codex`, `gpt-5.3-codex`, `gpt-5.4`.
- Preflight test suite (`test_bridge.py`) passes 6/6.
- Tool-call streaming translation is implemented (tool deltas + `finish_reason: "tool_calls"`).
- Known UI caveat: DeerFlow may show lingering “more steps” indicator after full response (cosmetic).

---

## Task 1 — OpenClaw OAuth Reconnaissance

### 1) Codex OAuth client ID
- `app_EMoamEEZ73f0CkXaXp7hrann`
- Source: `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/utils/oauth/openai-codex.js:19`

### 2) OAuth endpoints
- Authorization: `https://auth.openai.com/oauth/authorize`
- Token: `https://auth.openai.com/oauth/token`
- Source: `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/utils/oauth/openai-codex.js:20-21`

### 3) PKCE flow
- Verifier: 32 random bytes (`crypto.getRandomValues`) → base64url
- Challenge: `base64url(SHA-256(verifier))`
- Challenge method: `S256`
- Source:
  - `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/utils/oauth/pkce.js:19-29`
  - `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/utils/oauth/openai-codex.js:145,152-153`

### 4) System prompt requirement
- OpenClaw does not hardcode a single “magic auth validation prompt” in OAuth module.
- Request body uses runtime prompt: `instructions: context.systemPrompt`.
- Source: `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-codex-responses.js:201`
- Secondary reference (`numman-ali/opencode-openai-codex-auth`) dynamically fetches model-family prompt files from OpenAI Codex GitHub release tags, not one fixed literal.

### 5) Bearer token attachment and headers
- `Authorization: Bearer <token>`
- `chatgpt-account-id: <accountId>`
- `OpenAI-Beta: responses=experimental`
- `originator: pi`
- Source: `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-codex-responses.js:683-686`

### 6) Token refresh logic
- Refresh trigger: token expired (`Date.now() >= creds.expires`).
  - Source: `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/utils/oauth/index.js:121-125`
- Refresh endpoint: `https://auth.openai.com/oauth/token`
- Refresh payload (`application/x-www-form-urlencoded`):
  - `grant_type=refresh_token`
  - `refresh_token=<refresh_token>`
  - `client_id=app_EMoamEEZ73f0CkXaXp7hrann`
- Source: `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/utils/oauth/openai-codex.js:113-120`

### 7) Token storage format + location
- Canonical OpenClaw store: `auth-profiles.json` (not legacy `auth.json`).
- File names and path resolution:
  - `AUTH_PROFILE_FILENAME = "auth-profiles.json"`
  - `LEGACY_AUTH_FILENAME = "auth.json"`
  - `<agentDir>/auth-profiles.json`
- Source:
  - `/opt/homebrew/lib/node_modules/openclaw/dist/model-selection-Zb7eBzSY.js:161-162`
  - `/opt/homebrew/lib/node_modules/openclaw/dist/model-selection-Zb7eBzSY.js:484-486`
- Legacy migration support for `auth.json` exists.

### 8) Prompt injection cadence
- System prompt injected per request as `instructions`.
- Source: `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-codex-responses.js:193-203`

### Additional OAuth authorize parameters used
- `id_token_add_organizations=true`
- `codex_cli_simplified_flow=true`
- `originator=<value>`
- Source: `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/utils/oauth/openai-codex.js:155-157`

### Task 1 unknowns
1. No single immutable hardcoded “magic prompt” found in OpenClaw code.
2. OpenAI backend validation behavior cannot be fully inferred from client code alone.

---

## Task 2 — Protocol Gap Analysis + Interface Spec

### A) API format gap

#### Inbound (DeerFlow/LangChain)
- `POST /v1/chat/completions` (Chat Completions payload)

#### Outbound (Codex OAuth path in OpenClaw)
- Base URL: `https://chatgpt.com/backend-api`
- POST target: `/codex/responses`
- URL resolution logic:
  - if base ends with `/codex/responses` use as-is
  - if base ends with `/codex` append `/responses`
  - else append `/codex/responses`
- Source:
  - `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-codex-responses.js:16`
  - `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-codex-responses.js:233-241`
  - `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-codex-responses.js:116-119`

#### Compatibility decision
- Treat Codex OAuth as targeting ChatGPT backend Responses surface, not OpenAI Platform `/v1/chat/completions`.
- OpenClaw provider split confirms OAuth credentials are for `openai-codex`, not plain `openai` API-key provider.
  - Source: `/opt/homebrew/lib/node_modules/openclaw/dist/model-selection-Zb7eBzSY.js` (error text around `No API key found for provider "openai"... authenticated with OpenAI Codex OAuth`)

### B) Required header mapping
- `Authorization: Bearer <access_token>`
- `chatgpt-account-id: <chatgpt_account_id>`
- `OpenAI-Beta: responses=experimental`
- `originator: pi` (OpenClaw path)
- `accept: text/event-stream`
- `content-type: application/json`
- Source: `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-codex-responses.js:683-690`

#### account ID origin
- Extracted from JWT access token claim:
  - claim path: `https://api.openai.com/auth.chatgpt_account_id`
- Sources:
  - `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/utils/oauth/openai-codex.js:24`
  - `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/utils/oauth/openai-codex.js:233-237`
  - `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/utils/oauth/openai-codex.js:327-336,350-359`

### C) Request translation spec

#### Chat Completions → Responses mapping
- `chat.model` → `responses.model`
- `chat.stream` → `responses.stream`
- system message content → `responses.instructions`
- non-system messages → `responses.input`
- `chat.temperature` → `responses.temperature`
- tools (if present) → `responses.tools` (function schema shape)

#### Exact `responses.input` shape (from OpenClaw converter)
`input` is **not** a flat string. It is a heterogeneous array of Responses input items, including message-like objects and tool-call items.

Common shapes:
1) User text message:
```json
{
  "role": "user",
  "content": [{ "type": "input_text", "text": "..." }]
}
```
2) User multimodal message:
```json
{
  "role": "user",
  "content": [
    { "type": "input_text", "text": "..." },
    { "type": "input_image", "detail": "auto", "image_url": "data:<mime>;base64,<...>" }
  ]
}
```
3) Assistant prior message output item:
```json
{
  "type": "message",
  "role": "assistant",
  "content": [{ "type": "output_text", "text": "...", "annotations": [] }],
  "status": "completed",
  "id": "msg_xxx"
}
```
4) Assistant tool call item:
```json
{
  "type": "function_call",
  "id": "fc_xxx",
  "call_id": "call_xxx",
  "name": "tool_name",
  "arguments": "{...json string...}"
}
```
5) Tool result item:
```json
{
  "type": "function_call_output",
  "call_id": "call_xxx",
  "output": "..."
}
```

Source for shapes: `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-responses-shared.js:56-179`

#### OpenClaw request body scaffold
```json
{
  "model": "...",
  "store": false,
  "stream": true,
  "instructions": "...",
  "input": [ ... ],
  "text": { "verbosity": "medium" },
  "include": ["reasoning.encrypted_content"],
  "prompt_cache_key": "...",
  "tool_choice": "auto",
  "parallel_tool_calls": true
}
```
- Source: `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-codex-responses.js:197-208`

### D) Error contract (completed + continuation)

#### What is confirmed in OpenClaw handling
1) Retryable statuses:
- `429, 500, 502, 503, 504` with exponential backoff
- Source: `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-codex-responses.js:32-37,126-129`

2) Parsed structured error envelope:
- Parser expects JSON with optional:
```json
{
  "error": {
    "code": "...",
    "type": "...",
    "message": "...",
    "plan_type": "...",
    "resets_at": 1234567890
  }
}
```
- Source: `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-codex-responses.js:640-658`

3) Usage/rate-limit recognition logic:
- Recognizes `usage_limit_reached`, `usage_not_included`, `rate_limit_exceeded`, or status `429`
- Builds friendly guidance using `plan_type` and `resets_at` if present
- Source: `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-codex-responses.js:649-656`

4) Other surfaced error modes in provider stream handling:
- SSE event type `error` → throws `Codex error: ...`
- SSE event type `response.failed` → throws `response.error.message`
- WebSocket closed before completion → error
- Request abort → error
- Source:
  - `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-codex-responses.js:261-269`
  - `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-codex-responses.js:607`
  - `/opt/homebrew/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-codex-responses.js:141-143`

#### 401 expired/invalid token behavior (what backend returns)
- **Not explicitly hardcoded/typed in OpenClaw.**
- OpenClaw does not special-case 401 in parser; it forwards `error.message` (if JSON) or raw text/statusText.
- Therefore proxy should normalize:
  - status `401`/`403` with body passthrough into a stable error contract,
  - then attempt refresh once and retry once.
- Source: parser behavior at `/openai-codex-responses.js:640-661`

#### 429 shape + Retry-After headers
- OpenClaw does not inspect `Retry-After` header in this provider path.
- It relies on body fields (`error.code`, optional `resets_at`) and internal exponential backoff.
- Therefore `Retry-After` availability from backend is **unconfirmed from source**.

#### Proxy error normalization contract (implementation target)
- For DeerFlow-facing responses, normalize to:
```json
{
  "error": {
    "type": "auth_error|rate_limit_error|backend_error",
    "code": "<upstream_code_or_fallback>",
    "message": "<human readable>",
    "upstream_status": 401,
    "retry_after_seconds": 0,
    "raw": { "...": "optional upstream envelope" }
  }
}
```

---

## Checklist status
- [x] Task 1 — OpenClaw OAuth reconnaissance
- [x] Task 2 — Protocol gap analysis + interface spec
- [x] Task 3 — OAuth PKCE standalone module implemented (`~/workspace/deerflow-oauth-bridge/oauth.py`)
- [x] Task 4 — Protocol translator standalone module implemented (`~/workspace/deerflow-oauth-bridge/translator.py`)
- [x] Task 5 — FastAPI proxy implemented (`~/workspace/deerflow-oauth-bridge/server.py`)
- [ ] Task 5 verification — runtime E2E pending dependency install (`fastapi`, `uvicorn`, `httpx`) and live auth test
- [ ] Task 6+ — DeerFlow integration + packaging

---

## Remaining explicit unknowns
1. Definitive backend 401 payload variants for all auth failure modes (not explicitly documented in OpenClaw code).
2. Whether backend emits `Retry-After` headers consistently for rate limiting.
3. Direct Codex OAuth compatibility with OpenAI Platform `/v1/chat/completions` endpoint remains unconfirmed.
