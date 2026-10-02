# Software Design Review (SDR): Intent Router — Hybrid Edition (Jev System 1 + Gemma System 2)

| | |
|---|---|
| **Author** | GoGo (for Anuj Gupta) |
| **Date** | 2026-10-02 |
| **Status** | Draft — POC built, mock/mock verified; live backends untested |
| **Repo** | `intent-router-hybrid` (local; GitHub push pending) |
| **Predecessors** | `intent-router-poc` (Gemma-only), `intent-router-jev` (Jev-only) |

## 1. Problem statement

The Jev-only router is fast and cheap but has a ceiling: a single
decision model, however calibrated, cannot deliberate. Ambiguous queries,
novel phrasings, and adversarial inputs (prompt injection, social
engineering) are exactly where a fast classifier is weakest — and where a
wrong automatic action (refund, deletion, account change) is most
expensive.

The Gemma-only router can deliberate but pays the full LLM price on every
query: seconds of latency, dollars of compute, and brittle JSON parsing
even on the trivial cases.

The hybrid resolves the tension with dual-process routing: **Jev as
System 1** (fast, typed, calibrated — the gatekeeper) and **Gemma as
System 2** (slow, deliberative — invoked only when System 1 is uncertain
or flags risk). Plus a **Score-based guardrail** in System 1 that can
hard-block risky queries without spending a single LLM token.

## 2. Goals

- G1. Route each query to the correct A2A worker with the cheapest
  sufficient cognition: Jev alone for the clear cases, Gemma review for
  the unclear ones, human for the risky ones.
- G2. System 1 decides four things in one 70–500 ms call: intent
  (Choice), human-review probability (Noul), utterance type (Choice),
  and **guardrail risk (Score)** over the ordered rubric
  `[safe, low, medium, high, critical]` → expected score 0–4.
- G3. Guardrail block: risk ≥ `GUARDRAIL_BLOCK_SCORE` (default 3.0) →
  fallback worker immediately, System 2 not consulted.
- G4. System 2 trigger: confidence < 0.6 OR Noul > 0.5 OR risk ≥
  `GUARDRAIL_REVIEW_SCORE` (default 1.5) → Gemma reviews Jev's full read
  and confirms, overrides, or escalates.
- G5. Fail safe everywhere: Jev outage → max guardrail score (block
  path); Gemma outage → escalate to human. No silent auto-routing, ever.
- G6. New intent = new `data/intents.yaml` block. No prompt surgery, no
  fine-tuning.
- G7. Deploy on GCP (Cloud Run/GKE); secrets via Secret Manager / K8s
  Secret.

## 3. Non-goals

- NG1. The router does not replace worker agents or answer queries — it
  only decides *who* acts.
- NG2. No Jev self-hosting or fine-tuning (closed weights, hosted API).
- NG3. System 2 is not a general chatbot here; it is a reviewer with a
  closed output contract (intent + escalate flag + rationale).
- NG4. Workers remain stub agents in the POC.

## 4. Design

### 4.1 The four System 1 questions

One System One request, `state = {query}`, four questions:

| # | Type | Question | Purpose |
|---|---|---|---|
| 1 | Choice | intent | Closed intent classification; per-option probabilities |
| 2 | Noul | human_review | P(human should review) — the ambiguity axis |
| 3 | Choice | utterance_type | question / command / statement / other |
| 4 | **Score** | **guardrail_risk** | **Expected risk 0–4 over [safe, low, medium, high, critical]** |

The Score question is the guardrail capability: it asks "how risky would
it be to act on this query automatically?" considering irreversible
actions, sensitive data, and injection/social-engineering signals. The
answer is a number, thresholded **in code** — no text parsing, no LLM
judge in the hot path.

### 4.2 Routing policy

```
s1 = jev.decide(query)                      # 70–500 ms
if s1.guardrail_score >= BLOCK (3.0):
    → fallback worker                       # guardrail_block; System 2 skipped
elif (s1.confidence >= 0.6 and s1.needs_human <= 0.5
      and s1.guardrail_score < REVIEW (1.5)):
    → worker(s1.intent)                     # fast path; zero LLM cost
else:
    s2 = gemma.review(query, s1)            # seconds; sees Jev's full read
    if s2.escalate_to_human: → fallback worker
    else: → worker(s2.intent)               # confirm or override
```

The `/route` response reports `path` (`fast`/`system2`/`guardrail_block`
/`empty`), all four gate booleans, `guardrail_score` + `guardrail_band`,
`system2_used` / `system2_escalated` / `system2_rationale`, `worker` +
`worker_name`, and the A2A dispatch record — everything needed to tune
thresholds from production data.

### 4.3 System 2 contract

Gemma receives the query plus Jev's read presented as *evidence, not
orders* (top intent, top-3 probabilities, Noul, guardrail score/band,
utterance type) and must return strict JSON:

```json
{"intent": "<declared intent>", "confidence": 0.0-1.0,
 "escalate_to_human": true/false, "rationale": "<one sentence>"}
```

Parsing is defensive (ported from the Gemma POC): no JSON / invalid
JSON / unknown intent → escalate to human. `intent: fallback` forces
escalation. The prompt's standing rule: *when in doubt, escalate — a
wrong automatic action costs more than a human review.*

Backends: `mock` (deterministic rules mirroring the prompt — confirm
when System 1 was fairly confident and risk is low/medium-safe,
escalate on medium+ bands or very low confidence), `ollama`
(local Gemma, e.g. `gemma-3-4b-it`), `vertex` (Gemma on a Vertex AI
endpoint).

### 4.4 Why this beats either system alone

| | Jev-only | Gemma-only | Hybrid |
|---|---|---|---|
| Clear query | 70–500 ms, $0.042/M in | ~1.6 s+, $$$ | = Jev-only (fast path) |
| Ambiguous query | best-guess or human | deliberates | Gemma deliberates, informed by Jev's read |
| Adversarial/risky | Noul may catch it | may catch it, slowly | Score blocks in ms, before any LLM spend |
| Jev outage | degrade to fallback | n/a | max guardrail → block (fail safe) |
| Gemma outage | n/a | request fails | escalate to human |

The key structural win: the guardrail is a **number from the fast
model**, not an LLM judge. Blocking "delete my account" costs one Jev
call, not one Jev call plus a Gemma review.

## 5. Failure handling

- **Jev 401/422** → fail fast with clear messages (bad key / malformed
  questions — code bugs, not retryable).
- **Jev 408/429/5xx** → backoff (0.5s, 1s, 2s), then degrade to a
  synthetic System 1 decision with **guardrail_score = 4.0 (critical)**:
  the block path routes to fallback, and System 2 is skipped (nothing
  trustworthy to review). A dead System 1 can never silently auto-route.
- **Gemma failure** (any backend) → escalate to human with the error in
  the rationale; the request still completes.
- **A2A dispatch** unchanged from predecessors (SDK or stub).

## 6. Security

- `TYPESAFE_API_KEY` via Secret Manager (Cloud Run) / K8s Secret /
  External Secrets (GKE). Helm renders from
  `--set secrets.typesafeApiKey=...`, never recreated on upgrade.
- Query text goes to TypeSafe's US-hosted API and (on the slow path) to
  the Gemma backend: PII policy applies to both until terms are signed.
- The guardrail rubric and thresholds are code/config, auditable and
  versioned — unlike a "be careful" system prompt.

## 7. Testing & evaluation

- **Unit** (`eval/test_hybrid.py`, 25 tests): config validation (incl.
  block > review threshold ordering), four-question construction, Score
  parsing + clamping + band mapping, mock System 1 shape, mock guardrail
  bands, System 2 prompt content, judgment parsing (happy/bad-JSON/
  unknown-intent), mock System 2 confirm vs escalate, and all four
  routing paths (fast / system2-confirm / system2-escalate /
  guardrail_block) plus the Jev-outage fail-safe. No network.
- **Smoke** (mock/mock): all four paths verified end to end, e.g.
  "please delete my account now" → `guardrail_block` → fallback with
  System 2 untouched; "invoice login problem" → System 2 confirms
  `billing_inquiry`.
- **Before prod**: shadow mode with live Jev + live Gemma; measure
  per-path rates (what % takes the slow path = your LLM bill), System 2
  override/escalation rates, guardrail band distribution, and calibration
  of all four System 1 signals. Tune the four thresholds from those
  curves.

## 8. Deployment (GCP)

- **Cloud Run** (recommended): `Dockerfile` image,
  `SYSTEM1_BACKEND=jev`, `TYPESAFE_API_KEY` from Secret Manager.
  `SYSTEM2_BACKEND=mock` to start; `ollama` needs a sidecar/service,
  `vertex` needs `VERTEX_PROJECT`/`VERTEX_ENDPOINT_ID` (Gemma deployed
  as a Vertex endpoint).
- **GKE**: `helm/intent-router-hybrid/` — ConfigMap now includes both
  backends, Gemma settings, and both guardrail thresholds; Secret for
  the key; `/health` probes.
- **Cost shape**: fast path ≈ one Jev call ($0.042/M input, output
  free); slow path adds a Gemma inference. The slow-path rate is the
  number to watch — it *is* the LLM bill.

## 9. Rollout plan

1. mock/mock to validate wiring and policy (done in POC).
2. Live Jev + mock Gemma in shadow: validate System 1 signals and
   threshold choices without LLM spend.
3. Live Gemma on the slow path in shadow: measure override/escalation
   quality vs. a human-labeled set.
4. Cut over per-intent; fallback worker stays the safety net.
5. Monitor: path distribution, guardrail band histogram, System 2
   escalation rate, p50/p99 per path, 429/5xx from Jev.

## 10. Open questions

- OQ1. Score calibration: is Jev's risk Score as well-calibrated as its
  Choice confidence? Measure on adversarial + edge-case labeled sets.
- OQ2. Should the block path consult Gemma for an *explanation* (not a
  decision) to show the user? Cost/UX trade-off.
- OQ3. Multi-turn: carry guardrail state across turns (a "safe" query
  can turn risky in context).
- OQ4. Where should the System 2 reviewer run in prod — Vertex Gemma
  endpoint vs. a small fine-tuned reviewer?
- OQ5. GitHub PAT for the repo push is pending — repo currently local only.

## 11. Risks

- R1. **Guardrail false positives** block legitimate queries (e.g.
  "refund" language in a benign question). Mitigation: review band
  (1.5–3.0) sends those to Gemma instead of blocking; tune from shadow
  data.
- R2. **System 2 latency** on the slow path (seconds). Mitigation: it
  only runs on the uncertain slice; set SLOs per path, not globally.
- R3. **Two-vendor dependence** (TypeSafe + Google/OpenAI for Gemma).
  Mitigation: both systems are behind interfaces; either can be
  swapped (cheap-LLM System 1, different reviewer).
- R4. Jev Score is the newest question type — validate its behavior on
  your workload before trusting the block threshold.
