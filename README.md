# DeerFlow OAuth Bridge

**Use your ChatGPT Plus/Pro subscription to power DeerFlow 2.0 — no API key required.**

DeerFlow OAuth Bridge is a lightweight local proxy that connects [DeerFlow 2.0](https://github.com/bytedance/deer-flow) to OpenAI's models using your existing ChatGPT subscription via OAuth. It translates between DeerFlow's Chat Completions format and OpenAI's Codex Responses API, handling authentication, token refresh, and protocol translation transparently.

Built for the community. Open source. Zero API credits needed.

---

## How It Works

```
DeerFlow (LangChain)
    ↓ Chat Completions format (localhost)
OAuth Bridge (this project)
    ↓ Translates → Responses format
    ↓ Injects OAuth token + headers
ChatGPT Backend
    ↓ GPT-5 response (SSE stream)
OAuth Bridge translates back
    ↓ Chat Completions format
DeerFlow displays the result
```

The bridge runs locally on your machine. DeerFlow talks to it like a normal OpenAI endpoint. The bridge handles everything else — OAuth login, token refresh, request/response translation, and streaming.

---

## Quick Start

### Prerequisites

- Python 3.10+
- A ChatGPT Plus or Pro subscription
- DeerFlow 2.0 installed and running ([setup guide](https://github.com/bytedance/deer-flow))

### Install and Authenticate

```bash
git clone https://github.com/trentster/deerflow-oauth-bridge.git
cd deerflow-oauth-bridge
./setup.sh
```

The setup script creates a virtual environment, installs dependencies, and opens your browser for a one-time ChatGPT login. Your token is cached locally and refreshes automatically.

### Start the Bridge

```bash
cd deerflow-oauth-bridge
source venv/bin/activate
python server.py
```

The bridge starts on `http://127.0.0.1:8462`. You'll see:

```
🦌 DeerFlow OAuth Bridge
  Listening: http://127.0.0.1:8462
  Auth: authenticated
  Expires: 2026-03-18T02:29:04Z
```

### Configure DeerFlow

Add this to your DeerFlow `config.yaml` under the `models:` section:

```yaml
models:
  - name: gpt-5.3-codex
    display_name: GPT-5.3 Codex (OAuth Bridge)
    use: langchain_openai:ChatOpenAI
    model: gpt-5.3-codex
    api_key: "not-needed"
    base_url: http://host.docker.internal:8462/v1
    max_tokens: 4096
    temperature: 0.7
    supports_thinking: false
    supports_vision: true
```

> **Docker note:** If DeerFlow runs in Docker (recommended), use `http://host.docker.internal:8462/v1` as the `base_url`. If running DeerFlow locally without Docker, use `http://127.0.0.1:8462/v1`.

Restart DeerFlow and you're ready to go.

---

## Confirmed Working Models

| Model | Status |
|-------|--------|
| `gpt-5` | ✅ Working |
| `gpt-5.1` | ✅ Working |
| `gpt-5.1-codex` | ✅ Working |
| `gpt-5.2` | ✅ Working |
| `gpt-5.2-codex` | ✅ Working |
| `gpt-5.3-codex` | ✅ Working |
| `gpt-5.4` | ✅ Working (tested end-to-end: chat + web-search tool calls) — **recommended default**; incorporates GPT-5.3-codex coding capabilities |
| `gpt-5.4-codex` | ❌ Not supported with Codex OAuth (re-checked 2026-03-08) |
| `gpt-5.3` | ❌ Not supported with Codex OAuth (re-checked 2026-03-08) |
| `gpt-5-mini` | ❌ Not supported with Codex OAuth |
| `gpt-5-nano` | ❌ Not supported with Codex OAuth |
| `codex` / `codex-mini` / `codex-max` | ❌ Not supported with Codex OAuth |

---

## Tested and Verified

| Feature | Status |
|---------|--------|
| Simple chat (single turn) | ✅ Working |
| Multi-turn conversation (10+ turns) | ✅ Working — context retained |
| Web search with tool calls | ✅ Working (requires Tavily API key in DeerFlow) |
| Multi-step research with sub-agents | ✅ Working |
| Streaming responses | ✅ Working |
| Non-streaming responses | ✅ Working (auto-converted) |
| Token auto-refresh | ✅ Working — transparent, no re-login needed |
| Model switching | ✅ Working — swap model in config and restart |
| Vision / image upload | ⚠️ Requires DeerFlow sandbox configuration (not a bridge limitation) |

---

## Daily Usage

Once set up, your daily workflow is:

```bash
# Terminal 1: Start the bridge
cd deerflow-oauth-bridge
source venv/bin/activate
python server.py

# Terminal 2: Start DeerFlow (if not already running)
cd deer-flow
make docker-start
```

Open `http://localhost:2026` and start chatting. The bridge handles token refresh automatically — you shouldn't need to re-authenticate unless you've been offline for an extended period.

### Debug Mode

For troubleshooting, start the bridge with detailed logging:

```bash
python server.py --debug
```

This writes detailed request/response logs to `bridge.log` in the project directory.

---

## Pre-flight Test

The bridge includes a test script to verify everything is working:

```bash
# With the bridge running:
python test_bridge.py
```

Expected output:

```
Test 1 (Health): PASS
Test 2 (Models): PASS
Test 3 (Non-stream): PASS
Test 4 (Tools mapping): PASS
Test 5 (Streaming): PASS
Test 6 (Tool-call streaming): PASS
```

---

## Architecture

The bridge consists of three modules:

- **`oauth.py`** — Handles the OpenAI Codex OAuth PKCE flow, token storage, and auto-refresh
- **`translator.py`** — Bidirectional translation between Chat Completions and Responses API formats
- **`server.py`** — FastAPI proxy server that ties everything together

Tokens are stored locally at `~/.deerflow-bridge/auth.json` and never leave your machine.

---

## Known Issues

- **DeerFlow "more steps" indicator** — After some responses, DeerFlow's UI may show "X more steps" even though the response is fully rendered. This is a cosmetic DeerFlow frontend issue, not a bridge problem.
- **Vision/image upload** — Requires DeerFlow's sandbox container to be properly configured for file uploads. The bridge itself supports image content passthrough.
- **Thinking mode warning** — DeerFlow may log "thinking mode enabled but model does not support it." Add `supports_thinking: false` to your model config to suppress this.

---

## How It Was Built

This project was built in a single day by a human-AI team:

- **Claude (Anthropic)** — Architecture and project management
- **Codex (OpenAI)** — Implementation and testing
- **Mark** — Project lead, testing, and the human routing layer

The OAuth implementation details were derived from [OpenClaw's](https://github.com/openclaw/openclaw) open-source Codex OAuth provider, which OpenAI explicitly supports for external tool integration.

---

## Contributing

Found a bug? Want to add a feature? PRs are welcome.

Priority areas:
- Additional model testing as OpenAI releases new models
- DeerFlow sandbox integration for vision support
- Usage tracking dashboard
- Support for other LangChain-based tools beyond DeerFlow

---

## License

MIT

---

## Acknowledgements

- [DeerFlow](https://github.com/bytedance/deer-flow) by ByteDance — the agent framework this bridge serves
- [OpenClaw](https://github.com/openclaw/openclaw) — whose open-source OAuth implementation made this possible
- [OpenAI](https://openai.com) — for explicitly supporting OAuth usage in external tools
