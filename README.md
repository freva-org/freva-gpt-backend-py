# FrevaGPT Backend (Python)

Python backend for Freva-GPT assistant.

## Highlights
- FastAPI app with strict auth parity to the production Rust service (`/api/chatbot/*`)
- Streaming responses via LiteLLM/OpenAI-compatible SSE (`application/x-ndjson`) with code + image variants
- Persistent conversation threads in MongoDB and JSONL files (`threads/`), plus per-user scratch space (`cache/`)
- MCP manager that wires the backend to dedicated tool servers (`rag`, `code`, `web-search`)
- Docker compose stack that includes LiteLLM, Ollama, the backend, and all MCP servers (rag, code, web-search)
- Comprehensive pytest suite covering auth, prompting, storage, litellm client helpers, and route matrices
- Web-search MCP server for ICON model + DKRZ/HPC docs with cancellable OpenAI Web Search calls

## Quick Start (deployment)

### Requirements
- `podman` or `docker`
- MongoDB reachable via vault URL
- Credentials & headers for the Freva auth/vault services 

### Configure environment
Create `.env` (used by FastAPI, Docker, and MCP servers). See `.env.example` for guidance.

### Full stack via Docker Compose
```bash
./prod.sh up -d --build
```
Services that start:
- `freva-gpt-backend`: FastAPI app (debugpy toggle via `DEBUG=true` for remote debugging session)
- `rag`: MCP server exposing `get_context_from_resources`
- `code`: MCP server running the sandboxed Jupyter kernel and exposing `code_interpreter`
- `web-search`: MCP server that uses OpenAI Web Search to crawl ICON model + DKRZ/HPC documentation
- `litellm`: LiteLLM proxy that reads `litellm_config.yaml`
- `ollama`: Optional local model runner for LiteLLM backends

Bind mounts expose `/work`, logs, threads, and shared `cache` to other Freva services. Provide GPU access to Ollama via Docker device reservations when needed.

## Quick Start (local dev)

### Requirements
- `podman` or `docker`

### Configure environment
Create `.env` (used by FastAPI, Docker, and MCP servers). See `.env.example` for guidance.

### Start docker containers in DEV mode
```bash
./dev.sh up -d --build
```

## Repository Layout
| Path | Purpose |
| --- | --- |
| `src/app.py` | FastAPI entrypoint, CORS policy, router registration, app lifespan hooks |
| `src/api/chatbot/*` | HTTP handlers for chat operations (`availablechatbots`, `streamresponse`, `getthread`, etc.) |
| `src/services/streaming/` | LiteLLM client, orchestrator, stream variant definitions, heartbeat helpers |
| `src/services/storage/` | MongoDB + disk-backed persistence (`threads/` JSONL, `cache/` scratch space) |
| `src/services/mcp/` | MCP manager and MCP client |
| `src/services/authentication/` | Authentication: DEV mode auth surpassing OIDC requirements |
| `src/core/` | Settings, prompt assembly, logging, startup checks, available-model parsing |
| `src/tools/` | MCP servers (code interpreter + RAG), auth helpers, header gate middleware |
| `src/tools/web_search/` | Web-search MCP server that hits ICON model and DKRZ/HPC docs via OpenAI Web Search |
| `prompt_library/` | Baseline system prompts, summary prompts, and few-shot examples (JSONL) |
| `resources/` | Documentation corpora used by the RAG tool (`stableclimgen` seed content) |
| `docker/` | Dockerfiles for backend, LiteLLM/Ollama helpers, rag/code MCP servers |
| `docker/web-search-server/` | Docker context for the web-search MCP image |
| `scripts/` | Dev utilities (`dev_chat.py`, `dev_script.py`, `check_kernel_env.py`) |
| `tests/` | Pytest suite covering auth, prompting, streaming, storage, and endpoints |
| `litellm_config.yaml` | Source of truth for model catalog (consumed by `available_chatbots()`) |

Generated artifacts that persist across runs:
- `threads/` (JSONL transcript per thread id)
- `cache/{user_id}/{thread_id}` (LLM-created files, plots, etc.)
- `logs/` (when mounted in Docker)

## Architecture at a Glance
1. **FastAPI layer** enforces auth via `AuthRequired` (Bearer tokens validated against `x-freva-rest-url`), injects usernames, and validates per-request headers (`x-freva-vault-url`, `freva-config`, etc.).
2. **LiteLLM proxy** (`FREVAGPT_LITE_LLM_ADDRESS`) provides OpenAI-compatible chat + embeddings endpoints; completions stream into `StreamVariant` classes that normalize assistant text, code blocks, tool hints, images, and server hints.
3. **Persistence** uses both MongoDB (main storage) and optional disk mirrors. The `x-freva-vault-url` header resolves the Mongo URI at runtime so each tenant can point at its own database.
4. **MCP Manager** (`src/services/mcp/mcp_manager.py`) connects to tool servers listed in `FREVAGPT_AVAILABLE_MCP_SERVERS` (e.g., `["rag", "code", "web-search"]`), discovers tools, exposes OpenAI function schemas to LiteLLM, and routes tool invocations with per-thread session ids.
5. **RAG + Code + Web-search MCP servers** run as separate ASGI apps (dockerized). Requests flow through `header_gate` so required headers (`mongodb-uri`, `freva-config-path`, `working-dir`) become ContextVars before code executes.
6. **Prompting** loads baseline templates + few-shot examples per model and replays thread history (minus prompts, meta) to LiteLLM, matching the Rust semantics.

## API Surface

| Method | Path | Description | Notes |
| --- | --- | --- | --- |
| `GET` | `/api/chatbot/ping` | Static ping stub | Placeholder |
| `GET` | `/api/chatbot/docs` | Docs payload stub | Placeholder |
| `GET` | `/api/chatbot/help` | Help payload stub | Placeholder |
| `GET` | `/api/chatbot/availablechatbots` | Returns model names from `litellm_config.yaml` | Requires auth |
| `GET` | `/api/chatbot/newthread` | Generates a fresh `thread_id` | Requires auth |
| `GET` | `/api/chatbot/getthread?thread_id=...` | Fetches thread contents omitting prompts + redundant StreamEnd variants | Needs `x-freva-vault-url` |
| `GET` | `/api/chatbot/getuserthreads` | Returns latest 10 threads for authenticated user | Falls back to query `user_id` only if `ALLOW_FALLBACK_OLD_AUTH` |
| `GET` | `/api/chatbot/streamresponse` | Starts an SSE stream of `StreamVariant` JSON payloads | Query params: `thread_id`, `input` (required), `chatbot` |
| `GET/POST` | `/api/chatbot/stop` | Initiates stopping of an active conversation | Requires auth |

### Streaming contract
- Response type: `application/x-ndjson`
- Each `data:` line is a JSON object with `variant` discriminators (`Assistant`, `Code`, `CodeOutput`, `CodeError`, `Image`, `ServerHint`, `StreamEnd`, etc.).
- Code tool calls stream incremental chunks while LiteLLM emits `tool_calls`. When the MCP tool resolves, results are converted back into JSON events and appended to Mongo/disk storage.
- The first chunk is a `ServerHint` carrying the `thread_id`; conversation variants are stored in-memory during streaming and flushed to MongoDB at the end, ensuring replay safety.
- Clients can call `/api/chatbot/stop?thread_id=...` to move a conversation into `STOPPING`; the streaming loop exits and cancels in-flight MCP requests (code, rag, web-search) via the shared `ActiveRequest` registry.

## Persistence, Prompts, and Assets
- **MongoDB (`mongodb_storage.py`)**: canonical record for threads. Each document stores `user_id`, `thread_id`, ISO timestamp, topic (summarized via LiteLLM), and serialized `StreamVariant` list.
- **`cache/` scratch**: `create_dir_at_cache()` ensures each user/thread has a writable directory for generated files (plots, CSVs). Entries are sanitized if user IDs contain unsupported characters.
- **Prompt library**: `prompt_library/baseline` contains `starting_prompt.txt`, `summary_prompt.txt`, and `examples.jsonl`. GPT-5 models currently fall back to baseline prompts (warning logged). Customize by adding new prompt sets and updating `_resolve_baseline_dir()` / `_resolve_gpt5_dir_or_placeholder()`.
- **Resources**: `resources/stableclimgen` seeds the RAG MCP server. Drop additional corpora per library folder and list them in `FREVAGPT_AVAILABLE_LIBRARIES` inside `src/tools/rag/server.py`.

## MCP Tooling
- **RAG server** (`src/tools/rag/server.py`): indexes documentation with custom loaders + splitters, stores embeddings in MongoDB (`embeddings`), and surfaces a single tool `get_context_from_resources`. LiteLLM requests embed queries through the same proxy (`FREVAGPT_LITE_LLM_ADDRESS`).
- **Code interpreter** (`src/tools/code_interpreter/server.py`): spins up per-session Jupyter kernels, sanitizes input, enforces configurable timeouts, and injects Freva config via environment variables. Outputs include stdout/stderr, display data, and structured errors.
- **Web-search** (`src/tools/web_search/server.py`): calls OpenAI Web Search (`gpt-4.1`) constrained to ICON model + DKRZ/HPC docs. Honors request cancellation and is enabled when `web-search` appears in `FREVAGPT_AVAILABLE_MCP_SERVERS` and `FREVAGPT_WEB_SEARCH_SERVER_URL` is set.
- **Header gate** (`src/tools/header_gate.py`): wraps each MCP ASGI app so critical headers become ContextVars and requests fail fast when missing/invalid (e.g., missing Mongo URI yields SSE-friendly JSON-RPC errors).
- **Manager** (`src/services/mcp/mcp_manager.py`): caches clients, discovers tool schemas, exports OpenAI function definitions, and pins MCP session ids to thread ids for deterministic tool contexts.

## Development Workflow
- **Spin up dev stack**: `./dev.sh up -d --build` (FastAPI, rag, code, web-search, litellm, ollama). Use `./dev.sh up --build` to tail the app.
- **Unit/functional tests**: `uv run pytest` or focus, e.g. `uv run pytest tests/test_auth.py -k bearer`.
- **Integration: code interpreter**: `FREVAGPT_CODE_SERVER_URL=http://localhost:8051 uv run pytest tests/full_integration_tests/test_code_interpreter.py -m integration`.
- **Integration: web-search**: `FREVAGPT_WEB_SEARCH_SERVER_URL=http://localhost:8052 uv run pytest tests/full_integration_tests/test_web_search.py -m integration`.
<!-- - **Manual REPL**: `uv run python scripts/dev_chat.py` exercises the full orchestrator; handy for verifying prompts, tool wiring, and thread persistence. -->

## Troubleshooting
- **Auth failures**: verify headers include both `Authorization` and `x-freva-rest-url`. Inspect FastAPI logs for the exact HTTP status.
- **Missing models**: ensure `litellm_config.yaml` is readable and contains `model_name` keys. `available_chatbots()` aborts the process if it cannot find any entries.
- **MCP issues**: backend logs warn but continue when tool discovery fails; LiteLLM will simply not emit tool calls. Use `settings.AVAILABLE_MCP_SERVERS` to enable/disable targets explicitly.
- **File access**: Make sure `/work` is mounted read-only where expected.
- **Mongo connectivity**: `_get_database()` retries without URI query params. Persistent failures return HTTP 503; check vault responses and network policies.

