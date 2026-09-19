"""Validator behaviour for the typed disposition schema."""

from __future__ import annotations

import pytest

from compliance_agent_platform.schemas.disposition import (
    DecidedBy,
    Disposition,
    DispositionType,
    EvidenceItem,
    MatchedEntity,
)

_MATCH = MatchedEntity(
    list_name="OFAC SDN", entry_id="SDN-12345", primary_name="Viktor Petrov", programs=["RU"]
)


def _agent(**overrides):
    base = dict(
        alert_id="A1",
        disposition=DispositionType.CLEAR,
        rationale="no meaningful match",
        confidence=0.95,
        decided_by=DecidedBy.AGENT,
        model_id="anthropic.claude-sonnet-4",
        prompt_version="v1",
    )
    base.update(overrides)
    return base


def _analyst(**overrides):
    base = dict(
        alert_id="A1",
        disposition=DispositionType.CLEAR,
        rationale="reviewed, not the same person",
        decided_by=DecidedBy.ANALYST,
        analyst_id="AN-7",
    )
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "disposition",
    [
        DispositionType.CLEAR,
        DispositionType.ESCALATE_TO_ANALYST,
        DispositionType.INSUFFICIENT_DATA,
    ],
)
def test_agent_dispositions_valid(disposition):
    d = Disposition(**_agent(disposition=disposition))
    assert d.disposition is disposition
    assert d.decided_by is DecidedBy.AGENT


def test_agent_true_match_valid_with_match_and_confidence():
    d = Disposition(**_agent(disposition=DispositionType.TRUE_MATCH, matched_entities=[_MATCH]))
    assert d.matched_entities == [_MATCH]


@pytest.mark.parametrize(
    "resolution",
    [DispositionType.CLEAR, DispositionType.TRUE_MATCH, DispositionType.INSUFFICIENT_DATA],
)
def test_analyst_dispositions_valid(resolution):
    kwargs = _analyst(disposition=resolution)
    if resolution is DispositionType.TRUE_MATCH:
        kwargs["matched_entities"] = [_MATCH]
    d = Disposition(**kwargs)
    assert d.decided_by is DecidedBy.ANALYST
    assert d.confidence is None  # analyst decisions carry no model confidence


def test_analyst_requires_analyst_id():
    with pytest.raises(ValueError, match="analyst_id is required"):
        Disposition(**_analyst(analyst_id=None))


def test_agent_requires_model_id():
    with pytest.raises(ValueError, match="model_id is required"):
        Disposition(**_agent(model_id=None))


def test_true_match_requires_matched_entities():
    with pytest.raises(ValueError, match="true_match disposition requires"):
        Disposition(**_agent(disposition=DispositionType.TRUE_MATCH, matched_entities=[]))


def test_agent_clear_requires_confidence():
    with pytest.raises(ValueError, match="requires a confidence score"):
        Disposition(**_agent(confidence=None))


def test_confidence_out_of_range_rejected():
    with pytest.raises(ValueError):
        Disposition(**_agent(confidence=1.5))


def test_empty_rationale_rejected():
    with pytest.raises(ValueError):
        Disposition(**_agent(rationale=""))


def test_json_round_trip():
    original = Disposition(
        **_agent(
            disposition=DispositionType.TRUE_MATCH,
            matched_entities=[_MATCH],
            evidence=[
                EvidenceItem(source="OFAC SDN", detail="name+dob match", reference="SDN-12345")
            ],
        )
    )
    dumped = original.model_dump(mode="json")
    assert dumped["disposition"] == "true_match"
    assert dumped["decided_by"] == "agent"
    assert Disposition.model_validate(dumped) == original
