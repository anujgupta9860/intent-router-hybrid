# Run Locally — Step by Step

Two ways to run the router on your machine: **plain Python** (fastest)
or **Docker Compose** (router + both worker agents, closest to prod).

Both systems start mocked: no API keys, no network, no GPU.

## Option A — Plain Python (recommended for development)

### 1. Prerequisites

- Python 3.11+

### 2. Create a virtual environment and install dependencies

```bash
cd intent-router-hybrid
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Start the router (both systems mocked)

```bash
uvicorn src.app:app --port 8080
```

The router is now live at `http://localhost:8080`.

### 4. Try all four routing paths

Fast path (System 1 confident, risk low — Gemma not consulted):

```bash
curl -s -X POST http://localhost:8080/route \
  -H 'content-type: application/json' \
  -d '{"text": "My app keeps crashing on login"}' | python3 -m json.tool
# -> path: fast, system2_used: false, worker_name: worker-a
```

System 2 path (System 1 uncertain — Gemma reviews and confirms):

```bash
curl -s -X POST http://localhost:8080/route \
  -H 'content-type: application/json' \
  -d '{"text": "invoice login problem"}' | python3 -m json.tool
# -> path: system2, system2_used: true, system2_escalated: false
```

System 2 escalation (risk in the review band — Gemma escalates):

```bash
curl -s -X POST http://localhost:8080/route \
  -H 'content-type: application/json' \
  -d '{"text": "I want a refund for my invoice"}' | python3 -m json.tool
# -> path: system2, system2_escalated: true, worker_name: worker-b (fallback)
```

Guardrail block (risk critical — Gemma not even consulted):

```bash
curl -s -X POST http://localhost:8080/route \
  -H 'content-type: application/json' \
  -d '{"text": "please delete my account now"}' | python3 -m json.tool
# -> path: guardrail_block, system2_used: false, guardrail_band: critical
```

Classify only (System 1 read, plus System 2 when the policy triggers it):

```bash
curl -s -X POST http://localhost:8080/classify \
  -H 'content-type: application/json' \
  -d '{"text": "Where is my order?"}' | python3 -m json.tool
```

### 5. Run the test suite

```bash
pytest eval/ -q
```

25 tests, no network, no optional dependencies.

## Option B — Docker Compose (router + real stub workers)

### 1. Prerequisites

- Docker Desktop (or Docker Engine) installed and running

### 2. Start everything

```bash
docker compose up --build
```

This starts:
- **router** on `http://localhost:8080`
- **worker-a** on `http://localhost:8001` (billing, support, sales)
- **worker-b** on `http://localhost:8002` (account, orders, fallback)

### 3. Try it

Run the same four curl commands from Option A — now `dispatch` shows a
real (stub) worker replying.

### 4. Stop

```bash
docker compose down
```

## Option C — Live backends

### Live System 1 (Jev)

```bash
export TYPESAFE_API_KEY="ts_..."   # from console.typesafe.ai
export SYSTEM1_BACKEND=jev
uvicorn src.app:app --port 8080
```

### Live System 2 (Gemma via Ollama)

```bash
ollama run gemma-3-4b-it   # once, to pull the model (in another terminal)
export SYSTEM2_BACKEND=ollama
# export OLLAMA_URL="http://localhost:11434"   # default
# export GEMMA_MODEL="gemma-3-4b-it"           # default
uvicorn src.app:app --port 8080
```

Both live at once:

```bash
TYPESAFE_API_KEY=ts_... SYSTEM1_BACKEND=jev SYSTEM2_BACKEND=ollama \
  uvicorn src.app:app --port 8080
```

### Live System 2 (Gemma on Vertex AI)

```bash
pip install google-cloud-aiplatform   # optional dep
export SYSTEM2_BACKEND=vertex
export VERTEX_PROJECT="my-gcp-project"
export VERTEX_ENDPOINT_ID="<gemma endpoint id>"
# export VERTEX_REGION="us-central1"  # default
uvicorn src.app:app --port 8080
```

## Configuration cheat sheet

| Env var | Default | What it does |
|---|---|---|
| `SYSTEM1_BACKEND` | `mock` | `mock` (offline) or `jev` (live Jev) |
| `TYPESAFE_API_KEY` | — | Required for live System 1 |
| `TYPESAFE_MODEL` | `jev-1.13.0` | Pin it; re-run eval on bump |
| `SYSTEM2_BACKEND` | `mock` | `mock`, `ollama`, or `vertex` |
| `GEMMA_MODEL` | `gemma-3-4b-it` | Ollama model name |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server |
| `VERTEX_PROJECT` / `VERTEX_ENDPOINT_ID` | — | Required for vertex backend |
| `CONFIDENCE_THRESHOLD` | `0.6` | Below → System 2 |
| `HUMAN_REVIEW_THRESHOLD` | `0.5` | Noul above → System 2 |
| `GUARDRAIL_REVIEW_SCORE` | `1.5` | Risk ≥ → System 2 (0=safe..4=critical) |
| `GUARDRAIL_BLOCK_SCORE` | `3.0` | Risk ≥ → hard block to fallback |
| `WORKER_AGENTS_JSON` | stub defaults | Maps intent → worker A2A URL |

## Troubleshooting

- **`ModuleNotFoundError: src`** — run `uvicorn` from the repo root, not
  from inside `src/`.
- **Port 8080 in use** — change `--port` (and the curl URLs).
- **`TYPESAFE_API_KEY must be set`** — you set `SYSTEM1_BACKEND=jev`
  without the key.
- **`401` from Jev** — bad/expired key. **`422`** — malformed questions;
  check `data/intents.yaml` edits.
- **Ollama connection refused** — start Ollama first and pull the model:
  `ollama run gemma-3-4b-it`.
- **Slow first request in Docker** — that's the image build; later runs
  are cached.
