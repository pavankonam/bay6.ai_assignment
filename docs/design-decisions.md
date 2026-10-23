# Production design decisions

This document explains the design choices behind the agent (PRD §8.9) and the tradeoffs a reviewer should know about. Smaller implementation-level choices are logged in [`DECISIONS.md`](../DECISIONS.md).

## 1. LLM for language, code for control
The LLM does four things:
- classifies the intent and extracts slot candidates (`understand`, one structured call),
- proposes the next **read** tool (`plan_tool`, in `TOOL_SELECTION_MODE=llm`),
- classifies non-bare confirmation and yes/no replies,
- writes prose for non-decision replies (status summaries, smalltalk, out-of-scope).

Everything that decides what happens is deterministic, lives in plain Python, and is unit-tested: routing (`app/graph/routing.py`, pure functions of state), validation, date-role assignment and year resolution, verification gating, authorization, write execution, and the wording of every decision, coverage and escalation message.

## 2. Write tools are never directly executable by the LLM
- `plan_tool` binds only the read tools on the allowlist for the current intent and phase (`app/tools/allowlist.py`).
- `validate_tool_call` replaces any proposed call that isn't allowed, and always overwrites its arguments from validated slots and `verified_policy_id`, emitting a same-id `AIMessage` so `add_messages` replaces the proposal.
- `ToolNode` only knows the read tools.
- `create_claim` and `cancel_policy` run only inside `execute_write_action`, behind the FR26 guard: verified identity, payload policy = verified policy, valid slots, customer confirmation, and an approval whose id matches when required.

A prompt injection can therefore at worst change the wording of a reply. It can't cause a write or an unverified read.

## 3. Explicit consent at every write
- **Customer confirmation comes first.** Only a *bare* reply ("yes", "ok", "go ahead") is matched by regex. Anything longer goes to `ConfirmationResult`, and an edit always beats a yes: either the model reports slot changes, or the deterministic extractors find a date or policy number that differs from the summary.
- **Reviewer approval comes next, for risky actions:** every cancellation, and claims with injuries, an estimate of $10,000 or more, or no estimate (unknown severity counts as risky).
- **A failed approved write is retried only on the customer's explicit "retry".** "No", "stop", "wait" or two unrelated replies withdraw it.

## 4. Truthful write outcomes
A timeout is not a failure until the backend has been checked. After write retries are exhausted, `execute_write_action` calls `lookup_write_result(idempotency_key)`:
- **Found:** report the real confirmation number.
- **Absent:** offer a retry or withdrawal.
- **Lookup failed:** say we couldn't confirm and offer a retry, which is safe because the same key replays.

Before telling a customer their request was withdrawn, the lookup runs again. If it still can't tell, the agent says a specialist will confirm, escalates, and logs at ERROR with the key (`WRITE_OUTCOME_UNKNOWN`). The magic policy POL-999502, which commits and then times out, exercises this path.

## 5. Durable execution
`SqliteSaver` checkpoints every super-step, so a pending approval survives a restart. Three failure windows are covered:
- **Crash between `announce_approval` and `interrupt()`:** `ensure_interrupt()` sees `next == ("human_approval",)` with no pending interrupt and runs the thread forward to the interrupt without re-running the announcement, so there is no duplicate message.
- **Crash between the decision update and the resume:** every decision or expiry is written with `resume_status='pending'` in the same conditional `UPDATE … WHERE status='pending'`. The customer's next message (step 3b) or reconciliation finishes the resume.
- **A thread stuck at an interrupt:** the API never invokes the graph over a pending interrupt. Expiry happens lazily (on the next message or a late decision) and always resumes the thread with `expired`.

## 6. Idempotency at three layers
1. **Write keys.** `idempotency_key = sha256(thread_id + pending_action_id + action_type + canonical_json(payload))`, and the payload holds business fields only. A new summary means a new `pending_action_id`, so a resubmission can never replay an old approval or write. A replay with the same key and payload returns the original result, while a different payload returns `CONFLICT`.
2. **Approval records.** `INSERT … ON CONFLICT(idempotency_key) DO NOTHING`, then `SELECT`.
3. **Client message IDs:** a retried POST with the same `client_message_id` returns the stored response.

## 7. One retry layer
Automatic retries happen only inside the tool wrapper: tenacity, 2 retries, 0.5 s / 1 s backoff with jitter, and a per-attempt timeout. The graph has no retry edges, so step counts stay bounded.

After a read failure, the next customer message re-enters through `validate_slots`. Corrections ("actually make it Nov 5") are merged and the FR29a invalidation applied before the failed step re-runs naturally.

Internal errors (`CONFLICT`, `APPROVAL_REQUIRED`, `NOT_FOUND` after verification, the write guard, unknown codes) escalate instead of pretending to be outages.

## 8. Explicit conversational state
These are all typed state fields with deterministic handlers, not implicit LLM memory:
- confirmations, yes/no follow-ups (`pending_question`), retries and handoffs,
- task completion (`last_outcome`, with a terminal set that drives the FR5a task reset),
- suspension (`suspended_tasks`) and status transitions (one writer per status).

The state schema is enforced: a node that writes an unknown key raises (FR3). Each turn's interpretation is recorded on that turn's `HumanMessage.additional_kwargs`, so routing stays pure and every message carries an audit trail of how it was read.

## 9. Structured outputs with validation, repair and fallback
Every structured call is validated by Pydantic, retried once with a repair hint, and otherwise answered by rules (FR48):
- the policy-number regex,
- the field-aware date extractor (labels, the slot last asked, plausibility),
- keyword intents and bare yes/no.

Dates are assigned to fields deterministically wherever possible: "Nov 1" means next November for an effective date and last November for an incident date, and a yearless DOB is re-asked. When two dates compete, the agent asks rather than guesses.

## 10. Fact-grounded responses
- **Decision outcomes use templates only:** claim filed, ineligible, rejected, cancellation completed, expired, withdrawn, escalated, and so on.
- **LLM prose passes two post-checks** (`app/responses/guard.py`):
  - every reference ID, date and dollar amount must appear in the facts;
  - a coverage/payment-language filter blocks phrases like "will be covered", "guarantee" and "we'll pay".
  
  A failure falls back to the template and increments `response_guard_fallbacks_total{check}`.
- **"Will I be covered?"** is detected deterministically and always gets the templated "an adjuster will determine eligibility" answer.

## 11. Least-information errors and brute-force limits
- A verification failure never says which field was wrong, and an unknown policy looks the same as a wrong DOB.
- No policy- or claim-derived data (status, eligibility, duplicate claim IDs) is computed before verification *and* an intent: `route_slots` steps 4–5 gate steps 6–7.
- Lockouts apply per thread (3 failures → `locked`) and, as hardening, per policy across threads (see the section below).
- Customer endpoints are rate-limited per IP.

## 12. Data minimization toward the LLM vendor
See [what is sent to the LLM vendor](#what-is-sent-to-the-llm-vendor) below.

## 13. Swappable backends
The mock services implement `PolicyService`, `ClaimService`, `CancellationService` and `WriteLookupService` Protocols (`app/mock_api/service.py`). `set_services()` swaps in real adapters (or test doubles) without touching the graph.

## 14. Deterministic testability
- **Fake LLM:** `FakeScriptedChatModel` is a real `BaseChatModel`, so it takes the same `with_structured_output` / `bind_tools` path as Claude. It can be scripted, and otherwise answers with rule-based heuristics.
- **Fixed business date:** `APP_TODAY`.
- **Seeded faults:** `MOCK_FAULTS`, magic policy IDs, `MOCK_SEED`.
- **Gates:** the whole suite runs with no network (enforced by a socket guard), in both tool-selection modes, with ≥ 85% branch coverage over `app/`.

## 15. Single-process MVP with one mutation path
- Handlers are synchronous (threadpool), with one shared SQLite connection (`check_same_thread=False`, WAL) and an in-process `threading.Lock` per thread.
- The CLI is only an HTTP client of the API, so nothing (reviewer CLI, admin tasks, reconciliation) can mutate a thread outside its lock.

Phase 2 moves to Postgres (`langgraph-checkpoint-postgres`), async execution and distributed locks.

## 16. Core vs hardening scope
- **Core build (M0–M8):** everything the brief asks for.
- **Hardening (M9):** the multi-intent queue, the per-policy lockout with operator unlock, reconciliation, client message idempotency and the retention purge. It is built afterwards so the "small agent" can be demonstrated first.

---

## What is sent to the LLM vendor
When `LLM_PROVIDER=anthropic`, these go to Anthropic:
- **The system prompt** (`app/prompts/*.md`) plus a code-generated JSON context block: active intent, phase, the *names* of filled slots, the slot last asked for, ISO values of deterministic date mentions, and for `respond` the facts for that reply.
- **The last 6 customer-visible messages** (customer and agent text, at most `LLM_HISTORY_MAX_CHARS`). No tool messages and no tool-call messages are included.
- **Policy numbers, unmasked.** They're needed for context and are fictional in the mock.
- **Free-text incident details**, such as the location and description.

These are not sent:
- **The date of birth.** Before every call, any span the deterministic extractor recognizes as a DOB, in the current or earlier messages, is replaced by `[DOB]`. So is any rendering of the DOB already on file (ISO, US numeric, month-name forms). DOB-candidate dates in the context block appear only as `"[DOB]"`.
- **The holder's name and DOB from the policy lookup.** `get_policy_status` doesn't return them (NFR8b), so they never enter `policy_snapshot`, `response_facts` or prompts. Reviewers see the name and a masked DOB via the approvals service, which reads the repository directly.

**Known limitation:** if a customer writes their DOB in a phrasing the extractor doesn't recognize (for example "the twelfth of April, nineteen eighty-five"), that current message goes to the vendor unmasked. A test asserts that the raw DOB never appears in fake-LLM payloads for all supported formats.

Token usage is logged per call and counted in `llm_tokens_total{node,direction}`. `max_tokens`, the history limits and `MAX_LLM_CALLS_PER_THREAD` cap the cost (NFR3a).

## Per-policy lockout: the DoS tradeoff (FR19a)
A per-policy lockout (5 failed verifications within 24 h → chat verification refused in every thread) stops an attacker from guessing a DOB by opening new threads. The price is that the attacker can **deny chat service to the real policyholder** by deliberately failing 5 times.

Mitigations:
- **Non-extending window.** Attempts made while the policy is locked are not recorded, so the lock can't be kept alive indefinitely.
- **Phone recovery.** The locked customer is told to call support, so only the chat channel is blocked.
- **Operator unlock.** `support policy unlock <policy_id>` (an API client of `POST /admin/policies/{id}/unlock`) clears the failures after phone verification, logged at WARNING.
- **Observability.** `verification_lockouts_total` makes abuse visible.

**Phase 3 alternative:** replace the hard lock with adaptive throttling (increasing delays) plus OTP verification over a channel the real customer controls, which removes the DoS lever.

## Approval expiry
Approvals expire after `APPROVAL_TTL_HOURS` (48). Expiry is lazy rather than run by a timer:
- **Read paths** (`GET /approvals`) only *display* past-TTL approvals as expired.
- **The only code that persists expiry** is `approvals_service.expire()`. It is called by the messages endpoint (the customer's next message), by the decision endpoint (a late reviewer decision gets 409 *and* resumes the thread), and by reconciliation.

The customer is then offered a restart, which rebuilds a fresh confirmation with a new `pending_action_id` and a new approval.

## Things deliberately left for later phases
- **Phase 2:**
  - side-run handling of other requests while an approval is pending (today: a status reply plus "start a new conversation");
  - customer withdrawal of a pending request;
  - Postgres, multi-worker deployment and async;
  - streaming responses;
  - a reviewer web UI.
- **Phase 3:**
  - real identity verification (OTP);
  - real insurer adapters behind the same Protocols;
  - more intents;
  - coverage-FAQ RAG.
