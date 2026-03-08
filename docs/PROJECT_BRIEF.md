# DeerFlow OAuth Bridge — Project Brief

## Purpose
A lightweight Python proxy service that enables DeerFlow 2.0 (ByteDance's open-source SuperAgent framework) to use ChatGPT subscription access via OpenAI's Codex OAuth flow — eliminating the need for a separate OpenAI API key.

## Why This Matters
DeerFlow 2.0 only supports static API keys. Many users already have ChatGPT Plus/Pro subscriptions but no API credits. This bridge lets them use their existing subscription with DeerFlow, and potentially any LangChain-based tool. No equivalent open-source solution exists yet.

## Architecture
```
DeerFlow (LangChain ChatOpenAI)
    ↓ POST /v1/chat/completions (localhost)
DeerFlow OAuth Bridge (FastAPI proxy)
    ↓ Translates Chat Completions → Responses format
    ↓ Injects OAuth bearer token + required headers
    ↓ POST /codex/responses
ChatGPT Backend API (chatgpt.com/backend-api)
    ↓ Response (SSE stream)
Bridge translates back → Chat Completions format
    ↓
DeerFlow receives standard response
```

## Key Architectural Decisions (confirmed via source code recon)

### OAuth Details
- **Client ID:** `app_EMoamEEZ73f0CkXaXp7hrann`
- **Authorization endpoint:** `https://auth.openai.com/oauth/authorize`
- **Token endpoint:** `https://auth.openai.com/oauth/token`
- **PKCE method:** S256 (32 random bytes base64url verifier, SHA-256 challenge)
- **Extra authorize params:** `codex_cli_simplified_flow=true`, `id_token_add_organizations=true`

### Token Management
- **Refresh trigger:** when `Date.now() >= creds.expires`
- **Refresh payload:** `grant_type=refresh_token`, `refresh_token=<token>`, `client_id=<client_id>` (x-www-form-urlencoded)
- **Account ID extraction:** JWT claim path `https://api.openai.com/auth` → `chatgpt_account_id`
- **Storage:** local JSON file (`~/.deerflow-bridge/auth.json`)

### API Protocol (CRITICAL)
- Codex OAuth tokens work against **`chatgpt.com/backend-api/codex/responses`** — NOT the standard OpenAI platform API at `api.openai.com`
- This means the proxy is a **full protocol translator**, not just a header injector
- DeerFlow sends Chat Completions format; proxy must translate to Responses format

### Required Outbound Headers (every request)
1. `Authorization: Bearer <access_token>`
2. `chatgpt-account-id: <account_id>`
3. `OpenAI-Beta: responses=experimental`
4. `originator: deerflow-bridge` (OpenClaw uses `pi`)
5. `accept: text/event-stream`
6. `content-type: application/json`

### Request Translation (Chat Completions → Responses)
- `chat.model` → `responses.model`
- `chat.stream` → `responses.stream`
- `chat.messages` split:
  - `role=="system"` content → `responses.instructions`
  - all non-system messages → `responses.input` (heterogeneous array)
- `chat.temperature` → `responses.temperature`
- `chat.tools` → `responses.tools`
- Additional: `store: false`, `text.verbosity: "medium"`

### Responses Input Array Shapes
- User message: `{ role: "user", content: [{ type: "input_text", text: "..." }] }`
- Assistant message: `{ type: "message", role: "assistant", content: [{ type: "output_text", text: "...", annotations: [] }], status: "completed" }`
- Tool call: `{ type: "function_call", id: "fc_...", call_id: "...", name: "...", arguments: "{...}" }`
- Tool result: `{ type: "function_call_output", call_id: "...", output: "..." }`

### System Prompt
- No "magic" validation prompt required — auth is token-based
- OpenClaw passes runtime system prompt as `instructions` on every API call
- The proxy should pass DeerFlow's system messages through as `instructions`

### Error Handling
- **401/403:** Bubble error message, trigger token refresh
- **429 / rate limit:** Recognized by status 429 or `error.code` in `{usage_limit_reached, usage_not_included, rate_limit_exceeded}`. May include `plan_type` and `resets_at`
- **Retryable statuses:** 429, 500, 502, 503, 504 — exponential backoff
- **SSE errors:** event `type: "error"` or `type: "response.failed"` in stream

## Current State (2026-03-08)

- End-to-end bridge is operational with OAuth auth, request/response translation, and SSE streaming.
- Confirmed model set for Codex OAuth in this project: `gpt-5`, `gpt-5.1`, `gpt-5.1-codex`, `gpt-5.2`, `gpt-5.2-codex`, `gpt-5.3-codex`, `gpt-5.4`.
- Preflight harness passes 6/6 tests.
- Known UI caveat in DeerFlow: occasional lingering “more steps” indicator after completed answer (cosmetic).

## Build Phases

### Phase 1: OAuth Module (`oauth.py`) — standalone
- PKCE flow (verifier/challenge generation)
- Browser-based authorization
- Token exchange, refresh, storage
- JWT parsing for account ID
- Public interface: `get_valid_token()` → `{access_token, account_id}`

### Phase 2: Protocol Translator (`translator.py`)
- Chat Completions → Responses request translation
- Responses → Chat Completions response translation
- SSE stream passthrough/translation
- Tool call format mapping

### Phase 3: Proxy Server (`server.py`) — FastAPI
- `/v1/chat/completions` endpoint matching OpenAI spec
- Header injection using OAuth module
- Request/response translation using translator
- Error handling + retry logic
- Health check endpoint

### Phase 4: DeerFlow Integration
- `conf.yaml` snippet pointing to localhost proxy
- Setup script / one-liner install
- End-to-end testing with DeerFlow research task

### Phase 5: Community Release
- README with install instructions
- YouTube walkthrough
- Reddit/GitHub distribution

## Team Workflow
- **Claude:** Architect / PM — defines scoped tasks with acceptance criteria
- **Codex:** Builder — executes tasks, maintains implementation checklist
- **Mark:** Human routing layer — relays between Claude and Codex, tests on local hardware

## Source References
- DeerFlow 2.0: `github.com/bytedance/deer-flow`
- OpenClaw OAuth internals: `@mariozechner/pi-ai` package (installed via OpenClaw)
- OpenCode Codex auth plugin: `github.com/numman-ali/opencode-openai-codex-auth`
- OpenClaw docs: `docs.openclaw.ai/concepts/model-providers`

## Task 1+2 Detailed Spec
Full reconnaissance findings with exact file paths and code references saved at:
`~/workspace/notes/projects/deer-flow/task1-task2-spec.md`
