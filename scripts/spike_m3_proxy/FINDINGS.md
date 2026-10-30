# Spike: Codex 0.164.0 ↔ model-API base URL (M3 PR1)

Date: 2026-10-30. Image: `opr-review:local` (`@openai/codex@0.164.0` from `Dockerfile.review`).
Harness: `scripts/spike_m3_proxy/fake_proxy.py` (no real model key).

## Questions answered

| Question | Result |
| --- | --- |
| Does `OPENAI_BASE_URL` env redirect traffic? | **No** on 0.164.0. Codex still called `api.openai.com` / `chatgpt.com`. Env string exists in the binary but is not honored for routing (matches upstream deprecation/removal in favor of config). |
| Does `openai_base_url` in `config.toml` work? | **Yes.** Built-in provider `openai` sends traffic to the override host. |
| Does `-c 'openai_base_url="http://…/v1"'` work? | **Yes** (same behavior as config file). |
| Does custom `[model_providers.*]` + `model_provider=` work? | **Yes.** Preferred for M3: no WebSocket attempts observed; HTTP SSE only. |
| Primary API path | **`POST /v1/responses`** (Responses API). `wire_api = "responses"` is the only supported value. |
| `chat/completions`? | **Not observed** in these runs. |
| `GET /models` on our base? | **Not observed** against the fake proxy. Built-in openai without override still hits `https://chatgpt.com/backend-api/codex/models` (catalog; 401 with fake token). |
| Streaming? | **Yes.** After WebSocket failure (or immediately for custom provider): `POST /v1/responses` with `Accept: text/event-stream` and JSON body `"stream": true` (SSE). |
| WebSockets? | Built-in openai (with `openai_base_url`): first tries **`GET /v1/responses` Upgrade: websocket** (`openai-beta: responses_websockets=2026-02-06`), retries, then falls back to HTTPS SSE. Custom provider: **HTTP SSE only** in this spike. |
| Auth header | **`Authorization: Bearer <token>`** where token is `CODEX_API_KEY` / `OPENAI_API_KEY` (same value in our runs). No separate `api-key` header observed. |
| User-Agent | `codex_exec/0.164.0 (Debian …)`. Extra headers: `originator`, `x-codex-turn-metadata`, `session-id`, `thread-id`, `x-codex-beta-features`, etc. |

## Recommended M3 wiring

1. **Do not rely on `OPENAI_BASE_URL` env** (docs still list it for in-process sandbox allowlist; container M3 must set config).
2. Prefer a **custom provider** in staged `CODEX_HOME/config.toml`:

```toml
model_provider = "opr_proxy"
# model stays the configured review model

[model_providers.opr_proxy]
name = "OPR model proxy"
base_url = "http://model-proxy:8081/v1"   # compose DNS
env_key = "CODEX_API_KEY"                 # job token in env
wire_api = "responses"
supports_websockets = false               # explicit; avoid WS upgrade noise
```

3. Inject **job token** into `CODEX_API_KEY` (+ `OPENAI_API_KEY` for compatibility).
4. Proxy MVP allowlist:
   - `POST /v1/responses` (SSE + non-stream)
   - Optionally reject or no-op WebSocket `GET /v1/responses` Upgrade if someone uses `openai_base_url` on the built-in provider instead of a custom provider
5. Proxy must speak **Responses** SSE (`text/event-stream`), not only `chat/completions`.
6. Expect large request bodies (system/developer prompt ~40 KiB in empty-workspace spike).

## Residual notes

- Fake proxy responses were deliberately incomplete; Codex exited 0 with empty agent message after SSE deltas — enough to prove routing/auth/stream shape, not full protocol compatibility. Next PR needs a Responses-shaped stream that Codex accepts end-to-end.
- Built-in provider may still attempt ChatGPT catalog refresh unless overridden; custom provider avoided that in this run.
- Re-run: start `fake_proxy.py` on Docker network, run `opr-review` with configs under `workspace/home_*` (see comments in `fake_proxy.py`).

## Experiments

- **A** `OPENAI_BASE_URL` only → 0 hits on fake proxy; real `api.openai.com` / `wss://api.openai.com/v1/responses`.
- **B** `openai_base_url` in config → WS GET then POST SSE to fake proxy.
- **C** custom `model_providers.opr_proxy` → POST SSE only to fake proxy.
- **D** `-c openai_base_url=…` → same as B (WS then SSE).