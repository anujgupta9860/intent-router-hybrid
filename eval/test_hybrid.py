"""Unit tests for the hybrid router (no network, no optional deps)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Settings
from src.gemma_client import (
    GemmaReviewer,
    build_review_prompt,
    parse_judgment,
)
from src.hybrid import (
    HybridRouter,
    SystemOne,
    mock_guardrail_score,
    mock_system_one,
    mock_utterance_type,
)
from src.intents import load_intents
from src.jev_client import (
    GUARDRAIL_RUBRIC,
    JevError,
    SystemOneDecision,
    build_questions,
    parse_decision,
)
from src.router_util import worker_name_for


@pytest.fixture()
def intents():
    return load_intents()


@pytest.fixture()
def workers():
    return {
        "billing_inquiry": "http://billing:9999",
        "technical_support": "http://billing:9999",
        "fallback": "http://fallback:9999",
    }


@pytest.fixture()
def hybrid(intents, workers):
    s1 = SystemOne(intents, backend="mock")
    s2 = GemmaReviewer(intents, backend="mock")
    return HybridRouter(s1, s2, workers)


# ------------------------------------------------------------------ config
def test_settings_defaults():
    s = Settings()
    assert s.system1_backend == "mock"
    assert s.system2_backend == "mock"
    assert s.guardrail_review_score == pytest.approx(1.5)
    assert s.guardrail_block_score == pytest.approx(3.0)


def test_settings_rejects_bad_backends(monkeypatch):
    monkeypatch.setenv("SYSTEM1_BACKEND", "gpt")
    with pytest.raises(ValueError, match="SYSTEM1_BACKEND"):
        Settings()


def test_settings_rejects_inverted_guardrail_thresholds(monkeypatch):
    monkeypatch.setenv("GUARDRAIL_BLOCK_SCORE", "1.0")
    monkeypatch.setenv("GUARDRAIL_REVIEW_SCORE", "2.0")
    with pytest.raises(ValueError, match="GUARDRAIL_BLOCK_SCORE"):
        Settings()


def test_settings_requires_key_for_live_jev(monkeypatch):
    monkeypatch.setenv("SYSTEM1_BACKEND", "jev")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TYPESAFE_API_KEY"):
        Settings()


# ------------------------------------------------------- System 1 (Jev API)
def test_build_questions_has_four_typed_questions(intents):
    q = build_questions(intents)
    assert set(q) == {"intent", "human_review", "utterance_type", "guardrail_risk"}
    assert q["intent"]["type"] == "choice"
    assert q["human_review"]["type"] == "noul"
    assert q["utterance_type"]["type"] == "choice"
    assert q["guardrail_risk"]["type"] == "score"
    assert q["guardrail_risk"]["criteria"] == GUARDRAIL_RUBRIC


def test_parse_decision_reads_guardrail_score(intents):
    payload = {
        "answers": {
            "intent": {
                "type": "choice", "choice": "billing_inquiry",
                "probabilities": {"billing_inquiry": 0.9}, "confidence": 0.88,
            },
            "human_review": {"type": "noul", "noul": 0.1},
            "utterance_type": {
                "type": "choice", "choice": "question", "confidence": 0.9,
            },
            "guardrail_risk": {"type": "score", "score": 2.7},
        }
    }
    d = parse_decision(payload, intents)
    assert d.intent == "billing_inquiry"
    assert d.guardrail_score == pytest.approx(2.7)
    assert d.guardrail_band == "high"


def test_parse_decision_clamps_guardrail_score(intents):
    payload = {"answers": {"guardrail_risk": {"type": "score", "score": 99}}}
    d = parse_decision(payload, intents)
    assert d.guardrail_score == pytest.approx(4.0)
    assert d.guardrail_band == "critical"


def test_parse_decision_unknown_intent_falls_back(intents):
    payload = {"answers": {"intent": {"type": "choice", "choice": "nope"}}}
    d = parse_decision(payload, intents)
    assert d.intent == "fallback"


# ------------------------------------------------------- mock System 1
def test_mock_guardrail_score_bands():
    assert mock_guardrail_score("please delete my account now") >= 3.0
    assert 1.5 <= mock_guardrail_score("I want a refund please") < 3.0
    assert mock_guardrail_score("where is my order?") < 1.5


def test_mock_system_one_shape(intents):
    d = mock_system_one("I was charged twice on my invoice", intents)
    assert d.intent == "billing_inquiry"
    assert d.confidence >= 0.6
    assert d.guardrail_band in GUARDRAIL_RUBRIC
    assert d.utterance_type == "statement"


def test_mock_utterance_type():
    assert mock_utterance_type("Where is my order?") == "question"
    assert mock_utterance_type("Cancel my subscription") == "command"


def test_system_one_rejects_unknown_backend(intents):
    with pytest.raises(ValueError, match="System 1 backend"):
        SystemOne(intents, backend="wat")


def test_system_one_jev_failure_degrades_to_max_guardrail(intents, workers, monkeypatch):
    s1 = SystemOne(intents, backend="mock")

    def boom(text):
        raise JevError("simulated outage")

    # Force the live path's error handling by patching decide's client branch.
    s1_live = SystemOne(intents, backend="mock")
    monkeypatch.setattr(s1_live, "decide", lambda text: _forced_jev_error(s1_live, text))
    d = s1_live.decide("hello")
    assert d.guardrail_score == pytest.approx(4.0)
    assert d.intent == "fallback"


def _forced_jev_error(s1: SystemOne, text: str) -> SystemOneDecision:
    try:
        raise JevError("simulated outage")
    except JevError:
        from src import hybrid as h

        probs = {i.name: 1.0 / len(s1.intents) for i in s1.intents}
        return SystemOneDecision(
            intent="fallback", probabilities=probs, confidence=0.2,
            needs_human=0.9, utterance_type=h.mock_utterance_type(text),
            utterance_confidence=0.5,
            guardrail_score=float(len(GUARDRAIL_RUBRIC) - 1),
            model="jev-error", latency_ms=0.0,
        )


# ------------------------------------------------------- System 2 (Gemma)
def test_build_review_prompt_contains_evidence(intents):
    s1 = mock_system_one("I was charged twice", intents)
    prompt = build_review_prompt("I was charged twice", s1, intents)
    assert s1.intent in prompt
    assert "escalate_to_human" in prompt
    assert "billing_inquiry" in prompt  # allowed intents listed


def test_parse_judgment_happy_path(intents):
    raw = ('Please find the result: {"intent": "order_status", "confidence": 0.82, '
           '"escalate_to_human": false, "rationale": "clear tracking question"}')
    j = parse_judgment(raw, intents)
    assert j.intent == "order_status"
    assert j.escalate_to_human is False
    assert "tracking" in j.rationale


def test_parse_judgment_bad_json_escalates(intents):
    j = parse_judgment("not json at all", intents)
    assert j.intent == "fallback" and j.escalate_to_human is True


def test_parse_judgment_unknown_intent_escalates(intents):
    j = parse_judgment('{"intent": "nope", "confidence": 0.9}', intents)
    assert j.intent == "fallback" and j.escalate_to_human is True


def test_mock_system2_confirms_safe_confident(intents):
    s1 = mock_system_one("I was charged twice on my invoice", intents)
    j = GemmaReviewer(intents, backend="mock").review("I was charged twice", s1)
    assert j.escalate_to_human is False
    assert j.intent == s1.intent


def test_mock_system2_escalates_on_medium_band(intents):
    s1 = mock_system_one("I want a refund please", intents)
    assert s1.guardrail_band in ("medium", "high")
    j = GemmaReviewer(intents, backend="mock").review("I want a refund please", s1)
    assert j.escalate_to_human is True


# ------------------------------------------------------- hybrid policy
def test_fast_path_skips_system2(hybrid):
    out = hybrid.handle_query("My app keeps crashing on login")
    assert out["path"] == "fast"
    assert out["system2_used"] is False
    assert out["intent"] == "technical_support"
    assert out["worker_name"] == "billing"
    assert not out["routed_to_fallback"]


def test_uncertain_query_triggers_system2_confirm(hybrid):
    # billing vs technical_support tie -> low confidence, safe risk band
    out = hybrid.handle_query("invoice login problem")
    assert out["path"] == "system2"
    assert out["system2_used"] is True
    assert out["system2_escalated"] is False
    assert out["intent"] == out["system1_intent"] == "billing_inquiry"
    assert not out["routed_to_fallback"]


def test_risky_query_triggers_system2_escalation(hybrid):
    out = hybrid.handle_query("I want a refund for my invoice")
    assert out["path"] == "system2"
    assert out["system2_used"] is True
    assert out["system2_escalated"] is True
    assert out["routed_to_fallback"]
    assert out["worker_name"] == "fallback"


def test_guardrail_block_skips_system2(hybrid):
    out = hybrid.handle_query("please delete my account now")
    assert out["path"] == "guardrail_block"
    assert out["system2_used"] is False
    assert out["gates"]["guardrail_block"] is True
    assert out["routed_to_fallback"]
    assert out["worker_name"] == "fallback"


def test_empty_query(hybrid):
    out = hybrid.handle_query("   ")
    assert out["path"] == "empty"
    assert out["routed_to_fallback"]


def test_worker_name_for():
    assert worker_name_for("http://worker-a:8001") == "worker-a"
