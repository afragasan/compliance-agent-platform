"""Node-level behaviour with mock adapters and a fake model (no DB, no network)."""

from __future__ import annotations

from datetime import date

import pytest
from fakes import FakeAuditConn

from compliance_agent_platform.adapters.mock import default_deps
from compliance_agent_platform.config import Settings
from compliance_agent_platform.graph.builder import NodeContext
from compliance_agent_platform.graph.nodes import make_dispose, make_enrich, make_evaluate
from compliance_agent_platform.schemas.alert import ScreeningAlert, WatchlistHit
from compliance_agent_platform.schemas.disposition import DecidedBy, DispositionType, MatchedEntity
from compliance_agent_platform.schemas.enrichment import EnrichmentBundle
from compliance_agent_platform.schemas.evaluation import EvaluationResult

_SETTINGS = Settings(
    clear_confidence_threshold=0.85,
    bedrock_model_id="test-model-id",
    prompt_version="test-v1",
    queue="sanctions_screening",
)


class _FakeModel:
    def __init__(self, result: EvaluationResult | None = None) -> None:
        self.result = result
        self.invoked = False

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        self.invoked = True
        return self.result


def _ctx(model=None):
    return NodeContext(
        deps=default_deps(),
        audit_conn=FakeAuditConn(),
        settings=_SETTINGS,
        model=model or _FakeModel(),
    )


def _alert(**overrides) -> dict:
    base = ScreeningAlert(
        alert_id="A1",
        subject_id="CUST-1001",
        screened_name="Viktor Petrov",
        date_of_birth=date(1969, 4, 12),
        nationality="RU",
        hits=[
            WatchlistHit(
                list_name="OFAC SDN",
                entry_id="SDN-12345",
                matched_name="Viktor Petrov",
                score=0.95,
            )
        ],
    )
    return base.model_copy(update=overrides).model_dump(mode="json")


# --- enrich ----------------------------------------------------------------


def test_enrich_resolves_candidate_and_computes_features():
    out = make_enrich(_ctx())({"alert": _alert()})
    bundle = EnrichmentBundle.model_validate(out["enrichment"])

    assert bundle.insufficient_data is False
    assert len(bundle.candidates) == 1
    cand = bundle.candidates[0]
    assert cand.entry_id == "SDN-12345"
    assert cand.dob_match is True
    assert cand.nationality_match is True
    assert cand.name_similarity == pytest.approx(1.0)
    assert bundle.customer is not None and bundle.customer.subject_id == "CUST-1001"
    assert bundle.adverse_media_summaries  # "petrov" triggers the mock


def test_enrich_flags_insufficient_when_dob_missing():
    bundle = EnrichmentBundle.model_validate(
        make_enrich(_ctx())({"alert": _alert(date_of_birth=None)})["enrichment"]
    )
    assert bundle.insufficient_data is True
    assert "subject_date_of_birth" in bundle.missing


def test_enrich_flags_insufficient_when_hit_unresolved():
    alert = _alert(
        hits=[
            WatchlistHit(list_name="OFAC SDN", entry_id="SDN-UNKNOWN", matched_name="x", score=0.9)
        ]
    )
    bundle = EnrichmentBundle.model_validate(make_enrich(_ctx())({"alert": alert})["enrichment"])
    assert bundle.candidates == []
    assert bundle.insufficient_data is True
    assert "watchlist_entries_unresolved" in bundle.missing


# --- evaluate --------------------------------------------------------------


def test_evaluate_rule_overlay_skips_model_when_insufficient():
    model = _FakeModel(result=None)  # would blow up if invoked
    enrichment = EnrichmentBundle(insufficient_data=True, missing=["subject_date_of_birth"])
    state = {"alert": _alert(), "enrichment": enrichment.model_dump(mode="json")}

    out = make_evaluate(_ctx(model))(state)

    assert model.invoked is False
    result = EvaluationResult.model_validate(out["evaluation"])
    assert result.recommended is DispositionType.INSUFFICIENT_DATA
    assert result.confidence == 1.0


def test_evaluate_calls_model_when_enrichment_sufficient():
    canned = EvaluationResult(
        recommended=DispositionType.CLEAR, confidence=0.9, rationale="not a match"
    )
    model = _FakeModel(result=canned)
    enrichment = EnrichmentBundle(insufficient_data=False)
    state = {"alert": _alert(), "enrichment": enrichment.model_dump(mode="json")}

    out = make_evaluate(_ctx(model))(state)

    assert model.invoked is True
    assert EvaluationResult.model_validate(out["evaluation"]).recommended is DispositionType.CLEAR


# --- dispose --------------------------------------------------------------


def _eval_state(**ev) -> dict:
    evaluation = EvaluationResult(
        recommended=DispositionType.CLEAR,
        confidence=0.92,
        rationale="agent rationale",
        **ev,
    )
    return {
        "alert": _alert(),
        "enrichment": EnrichmentBundle().model_dump(mode="json"),
        "evaluation": evaluation.model_dump(mode="json"),
    }


def test_dispose_agent_path():
    d = make_dispose(_ctx())(_eval_state())["disposition"]
    assert d["decided_by"] == DecidedBy.AGENT.value
    assert d["disposition"] == DispositionType.CLEAR.value
    assert d["model_id"] == "test-model-id"
    assert d["prompt_version"] == "test-v1"
    assert d["analyst_id"] is None


def test_dispose_analyst_path_maps_resolution_and_falls_back_to_eval_matches():
    match = MatchedEntity(list_name="OFAC SDN", entry_id="SDN-12345", primary_name="Viktor Petrov")
    state = _eval_state(matched_entities=[match])
    state["analyst_decision"] = {
        "resolution": "true_match",
        "analyst_id": "AN-9",
        "rationale": "confirmed identity",
        "matched_entities": [],  # analyst gave none -> node falls back to evaluation's
    }

    d = make_dispose(_ctx())(state)["disposition"]

    assert d["decided_by"] == DecidedBy.ANALYST.value
    assert d["disposition"] == DispositionType.TRUE_MATCH.value
    assert d["analyst_id"] == "AN-9"
    assert d["confidence"] is None
    assert len(d["matched_entities"]) == 1
