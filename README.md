# Intent Router — Hybrid Edition (Jev System 1 + Gemma System 2)

Route user queries to the right worker agent with a **dual-process**
architecture:

- **System 1 — Jev** (fast, typed, calibrated): one System One request
  returns the intent (Choice), human-review probability (Noul),
  utterance type (Choice), and a **Score-based guardrail risk**
  (0=safe … 4=critical). Typical latency 70–500 ms.
- **System 2 — Gemma** (slow, deliberative): invoked **only** when
  System 1 is uncertain or the guardrail score is elevated. It reviews
  Jev's full read and makes the final call — confirm, override, or
  escalate to a human.

The economics are the point: the expensive, slow reasoner only runs on
the queries that need deliberation. Everything else is a cheap typed
decision.

## Routing policy

```
query ──▶ Jev (System 1)
              │
              ├─ guardrail_score >= 3.0 ──▶ BLOCK ──▶ fallback worker
              │                                (System 2 not consulted)
              │
              ├─ conf >= 0.6, noul <= 0.5,
              │  score < 1.5 ──▶ FAST PATH ──▶ worker (no LLM cost)
              │
              └─ otherwise ──▶ Gemma (System 2) reviews Jev's read
                                  ├─ escalate ──▶ fallback worker
                                  └─ confirm/override ──▶ worker
```

All thresholds are env-configurable (`CONFIDENCE_THRESHOLD`,
`HUMAN_REVIEW_THRESHOLD`, `GUARDRAIL_REVIEW_SCORE`,
`GUARDRAIL_BLOCK_SCORE`).

## How it works

1. `jev_client.py` — System 1: builds the four-question System One
   request from `data/intents.yaml`, POSTs to TypeSafe's API (raw
   `httpx`). Defensive parsing; Jev outages degrade to max guardrail
   score (fail safe → block path).
2. `gemma_client.py` — System 2: `GemmaReviewer` with `mock` / `ollama` /
   `vertex` backends. The prompt embeds Jev's read as *evidence, not
   orders*; strict JSON output parsed defensively (bad JSON or unknown
   intent → escalate).
3. `hybrid.py` — `HybridRouter`: the policy above + A2A dispatch. The
   `/route` response reports `path` (`fast`/`system2`/`guardrail_block`),
   `system2_used`, `guardrail_score`/`guardrail_band`, `worker_name`,
   and per-gate booleans.
4. `a2a_client.py` — A2A dispatch (official SDK when installed, stub
   otherwise); `workers/stub_worker.py` — demo workers.

## Quick start

```bash
pip install -r requirements.txt

# both systems mocked: no keys, no network, no GPU
uvicorn src.app:app --port 8080

# live Jev as System 1, mock Gemma as System 2
TYPESAFE_API_KEY=ts_... SYSTEM1_BACKEND=jev uvicorn src.app:app --port 8080

# live Gemma via Ollama as System 2 (ollama run gemma-3-4b-it)
SYSTEM2_BACKEND=ollama uvicorn src.app:app --port 8080
```

```bash
curl -s -X POST localhost:8080/route -H 'content-type: application/json' \
  -d '{"text": "please delete my account now"}' | python3 -m json.tool
# -> path: guardrail_block, worker: fallback
```

Full local instructions: [`docs/RUN_LOCALLY.md`](docs/RUN_LOCALLY.md).
Design review: [`docs/SDR.md`](docs/SDR.md).

## Run the tests

```bash
pytest eval/ -q     # 25 tests, no network, no optional deps
```

## Docker Compose demo

```bash
docker compose up --build
# router :8080, worker-a :8001, worker-b :8002
```

## Deploy (GCP)

**Cloud Run:**

```bash
echo -n "$TYPESAFE_API_KEY" | gcloud secrets create typesafe-api-key --data-file=-
gcloud run deploy intent-router-hybrid \
  --source . \
  --set-env-vars SYSTEM1_BACKEND=jev,SYSTEM2_BACKEND=mock \
  --set-secrets TYPESAFE_API_KEY=typesafe-api-key:latest
```

For a live System 2 on GCP, deploy Gemma to a Vertex AI endpoint and set
`SYSTEM2_BACKEND=vertex` with `VERTEX_PROJECT` / `VERTEX_ENDPOINT_ID`.

**GKE:** `helm/intent-router-hybrid/` — ConfigMap (incl. guardrail
thresholds), Secret for the key, `/health` probes.

## Layout

```
├── Dockerfile  docker-compose.yml  requirements.txt
├── data/intents.yaml
├── docs/
│   ├── SDR.md                   # design review
│   └── RUN_LOCALLY.md           # step-by-step local run guide
├── eval/test_hybrid.py          # 25 unit tests
├── helm/intent-router-hybrid/
├── scripts/
└── src/
    ├── app.py  config.py  hybrid.py  router_util.py
    ├── jev_client.py      # System 1 (Choice + Noul + Score guardrail)
    ├── gemma_client.py    # System 2 (mock | ollama | vertex)
    ├── intents.py  a2a_client.py
    └── workers/stub_worker.py
```

## Notes

- A Jev (System 1) outage degrades to **maximum guardrail score** — the
  block path — so a dead System 1 can never silently auto-route.
- A Gemma (System 2) failure escalates to human instead of failing the
  request.
- Tune all four thresholds from labeled shadow traffic, not from
  defaults; re-run eval on every `TYPESAFE_MODEL` bump.
