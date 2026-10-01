"""``build_retrieval_query`` over the existing example alert fixtures."""

from __future__ import annotations

from compliance_agent_platform.graph.nodes import build_retrieval_query
from compliance_agent_platform.schemas.alert import ScreeningAlert
from compliance_agent_platform.schemas.enrichment import EnrichmentBundle


def test_query_includes_hit_list_names(load_alert):
    alert = ScreeningAlert.model_validate(load_alert("alert_true_match"))
    query = build_retrieval_query(alert, EnrichmentBundle())

    assert all(hit.list_name in query for hit in alert.hits)


def test_query_includes_nationality_and_country():
    alert = ScreeningAlert(
        alert_id="A1", screened_name="X", nationality="RU", address_country="AE", hits=[]
    )
    query = build_retrieval_query(alert, EnrichmentBundle())

    assert "nationality RU" in query
    assert "country AE" in query


def test_query_includes_adverse_media_summaries():
    alert = ScreeningAlert(alert_id="A1", screened_name="X", hits=[])
    enrichment = EnrichmentBundle(adverse_media_summaries=["linked to a sanctioned network"])
    query = build_retrieval_query(alert, enrichment)

    assert "linked to a sanctioned network" in query


def test_query_falls_back_to_screened_name_when_nothing_else_available():
    alert = ScreeningAlert(alert_id="A1", screened_name="Jane Doe", hits=[])
    query = build_retrieval_query(alert, EnrichmentBundle())

    assert query == "Jane Doe"
