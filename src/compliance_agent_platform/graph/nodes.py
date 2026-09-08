"""Node implementations for the screening graph.

Each node is built by a factory that closes over a :class:`NodeContext` (providers,
LLM, audit connection, settings) so the node functions themselves stay pure functions
of ``state``. State channels carry JSON-serializable dicts; nodes parse the models they
need at entry and dump them back on exit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from langgraph.types import interrupt

from compliance_agent_platform.audit.log import AuditRecord, hash_payload, write_audit
from compliance_agent_platform.schemas.alert import ScreeningAlert
from compliance_agent_platform.schemas.disposition import (
    DecidedBy,
    Disposition,
    DispositionType,
    EvidenceItem,
)
from compliance_agent_platform.schemas.enrichment import EnrichmentBundle, WatchlistCandidate
from compliance_agent_platform.schemas.evaluation import (
    AnalystDecision,
    AnalystResolution,
    EvaluationResult,
)
from compliance_agent_platform.schemas.state import ScreeningState

if TYPE_CHECKING:
    from compliance_agent_platform.graph.builder import NodeContext

Node = Callable[[ScreeningState], dict]

_EVAL_SYSTEM = (
    "You are a sanctions-screening analyst assistant. Given an alert and enrichment "
    "data, decide whether the screened party is the same person/entity as a watchlist "
    "candidate. Return one of: clear (not a match), true_match (confirmed match), "
    "escalate_to_analyst (plausible match needing human judgement), insufficient_data "
    "(cannot decide with what is provided). Base confidence on name/DOB/nationality "
    "agreement and adverse media. Cite evidence."
)


def _audit(ctx: "NodeContext", record: AuditRecord) -> None:
    write_audit(ctx.audit_conn, record)


# --- intake ------------------------------------------------------------------


def make_intake(ctx: "NodeContext") -> Node:
    def intake(state: ScreeningState) -> dict:
        alert = ScreeningAlert.model_validate(state["alert"])
        _audit(
            ctx,
            AuditRecord(
                alert_id=alert.alert_id,
                thread_id=alert.alert_id,
                step="intake",
                actor="system",
                event="node_completed",
                payload_hash=hash_payload(state["alert"]),
                detail={"hits": len(alert.hits)},
            ),
        )
        return {"alert": alert.model_dump(mode="json")}

    return intake


# --- enrich ----------------------------------------------------------------


def make_enrich(ctx: "NodeContext") -> Node:
    def enrich(state: ScreeningState) -> dict:
        alert = ScreeningAlert.model_validate(state["alert"])

        candidates: list[WatchlistCandidate] = []
        for hit in alert.hits:
            entry = ctx.deps.watchlist.fetch_entry(hit.list_name, hit.entry_id)
            if entry is None:
                continue
            entry.name_similarity = _name_similarity(
                alert.screened_name, entry.primary_name, entry.aliases
            )
            entry.dob_match = _opt_eq(alert.date_of_birth, entry.date_of_birth)
            entry.nationality_match = _opt_eq(alert.nationality, entry.nationality)
            candidates.append(entry)

        customer = (
            ctx.deps.customer.fetch_profile(alert.subject_id) if alert.subject_id else None
        )
        media = ctx.deps.adverse_media.search(alert)

        missing: list[str] = []
        if alert.hits and not candidates:
            missing.append("watchlist_entries_unresolved")
        if alert.date_of_birth is None:
            missing.append("subject_date_of_birth")
        insufficient = bool(alert.hits) and (
            not candidates or "subject_date_of_birth" in missing
        )

        bundle = EnrichmentBundle(
            candidates=candidates,
            customer=customer,
            adverse_media_summaries=media,
            insufficient_data=insufficient,
            missing=missing,
        )
        _audit(
            ctx,
            AuditRecord(
                alert_id=alert.alert_id,
                thread_id=alert.alert_id,
                step="enrich",
                actor="system",
                event="node_completed",
                payload_hash=hash_payload(state["alert"]),
                detail={
                    "candidates": len(candidates),
                    "has_customer": customer is not None,
                    "adverse_media": len(media),
                    "insufficient_data": insufficient,
                    "missing": missing,
                },
            ),
        )
        return {"enrichment": bundle.model_dump(mode="json")}

    return enrich


# --- evaluate --------------------------------------------------------------


def make_evaluate(ctx: "NodeContext") -> Node:
    structured = ctx.model.with_structured_output(EvaluationResult)

    def evaluate(state: ScreeningState) -> dict:
        alert = ScreeningAlert.model_validate(state["alert"])
        enrichment = EnrichmentBundle.model_validate(state["enrichment"])

        # Rule overlay: never ask the model to guess when enrichment is thin.
        if enrichment.insufficient_data:
            result = EvaluationResult(
                recommended=DispositionType.INSUFFICIENT_DATA,
                confidence=1.0,
                rationale=f"Enrichment incomplete: missing {', '.join(enrichment.missing)}.",
            )
            _audit(
                ctx,
                AuditRecord(
                    alert_id=alert.alert_id,
                    thread_id=alert.alert_id,
                    step="evaluate",
                    actor="agent",
                    event="node_completed",
                    payload_hash=hash_payload(
                        {"alert": state["alert"], "enrichment": state["enrichment"]}
                    ),
                    model_id=ctx.settings.bedrock_model_id,
                    prompt_version=ctx.settings.prompt_version,
                    detail={"rule_overlay": "insufficient_data"},
                ),
            )
            return {"evaluation": result.model_dump(mode="json")}

        prompt = _build_eval_prompt(alert, enrichment)
        messages = [("system", _EVAL_SYSTEM), ("human", prompt)]
        result: EvaluationResult = structured.invoke(messages)

        _audit(
            ctx,
            AuditRecord(
                alert_id=alert.alert_id,
                thread_id=alert.alert_id,
                step="evaluate",
                actor="agent",
                event="node_completed",
                payload_hash=hash_payload(
                    {"alert": state["alert"], "enrichment": state["enrichment"]}
                ),
                llm_request={"system": _EVAL_SYSTEM, "human": prompt},
                llm_response=result.model_dump(mode="json"),
                model_id=ctx.settings.bedrock_model_id,
                prompt_version=ctx.settings.prompt_version,
            ),
        )
        return {"evaluation": result.model_dump(mode="json")}

    return evaluate


# --- escalate (HITL) ------------------------------------------------------


def make_escalate(ctx: "NodeContext") -> Node:
    def escalate(state: ScreeningState) -> dict:
        alert = ScreeningAlert.model_validate(state["alert"])
        evaluation = EvaluationResult.model_validate(state["evaluation"])

        _audit(
            ctx,
            AuditRecord(
                alert_id=alert.alert_id,
                thread_id=alert.alert_id,
                step="escalate",
                actor="agent",
                event="interrupt_raised",
                detail={"agent_recommendation": state["evaluation"]},
            ),
        )

        raw = interrupt(
            {
                "alert_id": alert.alert_id,
                "screened_name": alert.screened_name,
                "agent_recommendation": evaluation.recommended.value,
                "agent_confidence": evaluation.confidence,
                "agent_rationale": evaluation.rationale,
                "candidates": state["enrichment"].get("candidates", []),
            }
        )

        decision = AnalystDecision.model_validate(raw)
        _audit(
            ctx,
            AuditRecord(
                alert_id=alert.alert_id,
                thread_id=alert.alert_id,
                step="escalate",
                actor="analyst",
                event="resumed",
                detail=decision.model_dump(mode="json"),
            ),
        )
        return {"analyst_decision": decision.model_dump(mode="json")}

    return escalate


# --- dispose --------------------------------------------------------------


def make_dispose(ctx: "NodeContext") -> Node:
    def dispose(state: ScreeningState) -> dict:
        alert = ScreeningAlert.model_validate(state["alert"])
        evaluation = EvaluationResult.model_validate(state["evaluation"])
        analyst = (
            AnalystDecision.model_validate(state["analyst_decision"])
            if state.get("analyst_decision")
            else None
        )

        if analyst is not None:
            disposition = Disposition(
                alert_id=alert.alert_id,
                queue=ctx.settings.queue,
                disposition=_RESOLUTION_MAP[analyst.resolution],
                rationale=analyst.rationale,
                confidence=None,
                matched_entities=analyst.matched_entities or evaluation.matched_entities,
                evidence=evaluation.evidence,
                decided_by=DecidedBy.ANALYST,
                analyst_id=analyst.analyst_id,
                prompt_version=ctx.settings.prompt_version,
            )
        else:
            disposition = Disposition(
                alert_id=alert.alert_id,
                queue=ctx.settings.queue,
                disposition=evaluation.recommended,
                rationale=evaluation.rationale,
                confidence=evaluation.confidence,
                matched_entities=evaluation.matched_entities,
                evidence=evaluation.evidence or _fallback_evidence(state),
                decided_by=DecidedBy.AGENT,
                model_id=ctx.settings.bedrock_model_id,
                prompt_version=ctx.settings.prompt_version,
            )

        _audit(
            ctx,
            AuditRecord(
                alert_id=alert.alert_id,
                thread_id=alert.alert_id,
                step="dispose",
                actor=disposition.decided_by.value,
                event="disposition",
                detail=disposition.model_dump(mode="json"),
            ),
        )
        return {"disposition": disposition.model_dump(mode="json")}

    return dispose


_RESOLUTION_MAP = {
    AnalystResolution.CLEAR: DispositionType.CLEAR,
    AnalystResolution.TRUE_MATCH: DispositionType.TRUE_MATCH,
    AnalystResolution.INSUFFICIENT_DATA: DispositionType.INSUFFICIENT_DATA,
}


# --- helpers -------------------------------------------------------------


def _opt_eq(a, b) -> bool | None:
    if a is None or b is None:
        return None
    return str(a).strip().lower() == str(b).strip().lower()


def _name_similarity(a: str, primary: str, aliases: list[str]) -> float:
    from difflib import SequenceMatcher

    names = [primary, *aliases]
    return max(SequenceMatcher(None, a.lower(), n.lower()).ratio() for n in names)


def _build_eval_prompt(alert: ScreeningAlert, enrichment: EnrichmentBundle) -> str:
    return (
        f"ALERT\n{alert.model_dump_json(indent=2)}\n\n"
        f"ENRICHMENT\n{enrichment.model_dump_json(indent=2)}\n\n"
        "Decide the disposition."
    )


def _fallback_evidence(state: ScreeningState) -> list[EvidenceItem]:
    enrichment = EnrichmentBundle.model_validate(state["enrichment"])
    items: list[EvidenceItem] = []
    for c in enrichment.candidates:
        items.append(
            EvidenceItem(
                source=c.list_name,
                detail=(
                    f"name_similarity={c.name_similarity:.2f}, dob_match={c.dob_match}, "
                    f"nationality_match={c.nationality_match}"
                ),
                reference=c.source_url or c.entry_id,
            )
        )
    return items
