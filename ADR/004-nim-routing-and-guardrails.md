# ADR 004 — NIM/Bedrock routing and layered guardrails

- **Status:** Accepted
- **Date:** 2026-11-01
- **Deciders:** Compliance Agent Platform team
- **Scope:** Week 4. Which backend serves each queue's `evaluate` call (self-hosted
  NVIDIA NIM vs Amazon Bedrock), what happens when the primary fails, and how the
  input/output guardrails behave across the two. Builds on the `evaluate` node from
  [ADR-001](001-why-langgraph.md) and the audit chain from
  [ADR-002](002-audit-chain-design.md).

## Context

`sanctions_screening` is latency-sensitive and high-volume, so it is routed to a
self-hosted NIM pod (`meta/llama-3.1-8b-instruct`, OpenAI-compatible API, reached on
EKS) with Bedrock Claude as the automatic fallback. `aml_analysis` is
reasoning-intensive and goes straight to Bedrock. Validating this against a real NIM
(Week 4, runbook stages A-E) surfaced behaviour the offline tests could not:

- NIM rejects `max_completion_tokens` and `response_format: json_schema`, both of
  which the default LangChain OpenAI client sends.
- The guardrail ran on the primary model *outside* the fallback `try`, so a NIM outage
  crashed the run instead of falling back.
- The 8B self-check rail is deterministic but unreliable on long prompts: it allowed a
  forced-disposition instruction and a raw SSN buried in the ~2k-token evaluate prompt
  (3/3 runs), yet blocked the same content as short standalone messages (3/3). Claude
  on Bedrock blocked all variants.

## Decision

1. **Backend-aware structured output.** NIM uses `json_mode` with the JSON schema
   appended to the system prompt; Bedrock keeps tool-use (`ChatBedrockConverse` has no
   `json_mode`). Both return the same `EvaluationResult`. The NIM client sends
   `max_tokens` (not `max_completion_tokens`).
2. **Fallback covers the whole primary path.** The input rail runs inside the same
   `try` as the primary call, so a NIM outage in either place falls back to Bedrock. A
   `GuardrailViolationError` is a verdict on the prompt and never triggers a fallback.
3. **The Disposition names the model that answered.** `evaluate` writes
   `evaluation_model` into state and `dispose` stamps it, so the Disposition and the
   audit row agree after a fallback. Audit `detail.queue` is the routed queue.
4. **Layered input guardrail.**
   - A deterministic SSN / card-number check (`guardrails/patterns.py`, Luhn and
     issuer-prefix validated) runs over the *whole* prompt before any model call, so it
     is independent of both backends and fires when NIM is down. Block reasons name the
     category only, never the value.
   - The LLM self-check rail judges only attacker-influenceable free text (names,
     aliases, adverse media, upstream ids), not the retrieved corpus or JSON scaffolding.
     Validated live: 7/7 adversarial cases blocked on the 8B model (3/3 each) once the
     input is short.
   - `Settings.guardrail_input_backend` (`serving` default, `bedrock` opt-in) moves the
     input rail to Bedrock for NIM-routed calls. Off by default: it adds a Bedrock call
     (~4s vs ~1.2s on NIM in testing) and sends the untrusted alert text to Bedrock on
     every NIM-routed run.

## Consequences

- A NIM outage degrades to Bedrock without losing guardrail coverage or audit fidelity.
- SSN/card detection no longer depends on how well the serving model reads a long
  prompt.
- The LLM rail is still only as good as the model judging it; the Bedrock option is the
  lever for stricter environments. `tests/test_guardrails_live.py` (opt-in via
  `CAP_LIVE_GUARDRAILS=1`) keeps the 8B-vs-Claude gap measurable.

## Pending enhancements (not in Week 4 scope)

- **Passport and bank-account numbers in the deterministic check.** The pre-filter
  covers only well-formed SSNs and card numbers. The input-rail prompt
  (`guardrails/config/prompts.yml`) also names passport and bank-account numbers, but
  those have no fixed format, so today they rely solely on the LLM rail, which the 8B
  model can miss. A future change should add *label-anchored* patterns (a label such as
  "passport no." or "account number" followed by digits, plus IBAN with mod-97
  validation), tested for false positives against transaction refs and entry ids before
  being enabled. Until then, a NIM-routed alert with such a number in a free-text field
  is protected only by the probabilistic rail.
- **Output-rail scoping.** The output rail still receives the whole rationale; apply
  the same measure-first approach if the 8B model proves weak there.
