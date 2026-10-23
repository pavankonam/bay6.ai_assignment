# Checkpoints

Gates for every milestone: the PRD §12 exit criteria, plus `make check`:
- ruff lint + format check, and `mypy --strict app`
- the full fake-LLM, no-network test suite in **both** `TOOL_SELECTION_MODE=llm` and `deterministic`
- ≥ 85% branch coverage over `app/` in each mode

Each milestone commit is tagged `checkpoint_mN`. A commit cannot contain its own hash, so the hash column is filled in by the follow-up "record checkpoint" commit. The hash listed is the tagged commit.

| Milestone | Status | Tests passed (llm / deterministic) | Coverage (llm / det) | Commit | Tag |
|---|---|---|---|---|---|
| M0: Scaffold | ✅ Passed | 15 / 15 | 98.0% / 98.0% | `7d9ce56` | `checkpoint_m0` |
| M1: Mock backend + tools | ✅ Passed | 80 / 80 | 97.7% / 97.7% | `c9cbbb1` | `checkpoint_m1` |
| M2: State + graph skeleton | ✅ Passed | 276 / 276 | 96.2% / 96.2% | `c723f01` | `checkpoint_m2` |
| M3: Understanding + slots + verification | ✅ Passed | 491 / 491 | 92.1% / 92.1% | `e90bd80` | `checkpoint_m3` |
| M4: Claim + cancellation + confirmation | ✅ Passed | 549 / 549 | 94.0% / 94.0% | `ef6ce0e` | `checkpoint_m4` |
| M5: Human-in-the-loop | ✅ Passed | 574 / 574 | 94.7% / 94.7% | `4dfd866` | `checkpoint_m5` |
| M6: Error handling + responses | ✅ Passed | 630 / 630 | 96.3% / 96.3% | `85bfbfb` | `checkpoint_m6` |
| M7: API + CLI | ✅ Passed | 656 / 656 | 96.3% / 96.3% | `c9d8bd7` | `checkpoint_m7` |
| M8: Docs (core sign-off) | ✅ Passed | 657 / 657 | 96.3% / 96.3% | `2ddc749` | `checkpoint_m8` |
| M9: Hardening | ⏳ Not started | | | | |
| M10: Evaluation | ⏳ Not started | | | | |

## M0: Scaffold
- **Exit criteria (PRD §12):** `make lint test` passes on an empty test suite; seed loads.
- **Result:**
  - `make check` passes.
  - The suite has 15 scaffold tests rather than zero: config defaults, `.env.example` parsing, `APP_TODAY`, secret masking in repr, PII masking, masked JSON log output, seed completeness against PRD §8.4.3, and idempotent seed.
  - `make seed` loads 10 policies and 1 claim into `data/app.db`, and running it a second time is a no-op.

## M1: Mock backend + tools
- **Exit criteria (PRD §12):** unit tests for all tools including faults, refund calc, idempotency, DUPLICATE_CLAIM, and no DOB in the lookup.
- **Result:** All covered in `tests/unit/test_mock_api.py` and `tests/unit/test_tools.py` (65 new tests):
  - Refunds: Flow C refund = $175.55, LAPSED refund = $0.00, ROUND_HALF_UP, and the window boundaries.
  - Idempotency: replay returns the original result (including when `approval_id` differs), same key with a different payload → `CONFLICT`.
  - Duplicates: `DUPLICATE_CLAIM` carries the existing ID; `allow_duplicate` overrides it.
  - Faults: every magic ID (999500/999408/999501/999502), `MOCK_FAULT_CLEAR_AFTER`, `MOCK_FAULT_LOOKUP`, seeded random faults scoped by `MOCK_FAULT_TOOLS`, and latency.
  - Retry wrapper: at most 3 attempts, a 0.5 s / 1 s backoff, a real timeout, and no retries for business errors.
  - Other: the lookup returns no DOB or name; `ToolNode` runs the read tools and rejects write tools.

## M2: State + graph skeleton
- **Exit criteria (PRD §12):** routing unit tests pass; the graph compiles; `support graph draw` outputs Mermaid.
- **Result:**
  - **Routing tests:** `tests/unit/test_routing.py` covers every branch of `route_entry`, `route_intent` (including read-retry precedence), each `route_slots` step 1–10 with its precedence conflicts, every `route_after_tool` row, `route_confirm`, `route_approval`, `route_write`, `route_pending_question`, `route_error` and `route_next_task`.
  - **Graph tests:** the graph compiles and runs on `SqliteSaver`, and its state survives a reopened connection. FR3 (unknown key → error) and the schema-equals-PRD check are tested.
  - **Tool-call tests:**
    - NFR4: no write tool is bindable for any intent/phase.
    - Deterministic `AIMessage` synthesis.
    - `validate_tool_call` keeps the same id, replaces rejected calls, and overwrites arguments (including an injection attempt).
    - `plan_tool` in both modes, including LLM-failure fallback.
  - **Other tests:** the LLM gateway (repair, fallback, cap, tokens, redaction).
  - **Draw:** `support graph draw` writes `docs/graph.md`.
  - Nodes beyond `ingest`, `plan_tool` and `validate_tool_call` are still stubs at this checkpoint, as the milestone specifies.

## M3: Understanding + slot filling + verification
- **Exit criteria (PRD §12):** Flows A, A2, E and H pass with the fake LLM; the consecutive-task, multi-date, year-boundary and re-verification tests pass.
- **Result:**
  - **Integration tests** (`tests/integration/test_understanding_flows.py`):
    - Flow A, with NFR2 call counts in both modes; Flow A2 makes zero tool calls on its first turn.
    - Flow E: resume yes/no/unrelated.
    - Flow H: status → cancel reuses the identity (the full Flow B → cancel version is in M4).
    - Two consecutive tasks; no slot leaks after a terminal outcome; smalltalk and out-of-scope messages mid-claim keep the slots.
    - A multi-date message; ambiguity clarification with carry-over; a yearless DOB; Dec/Jan boundaries for incident and effective dates.
    - Verification: wrong → correct DOB (attempts == 1); lockout at 3; FR21/FR11 switch after lookup with a policy change, then resume (re-verify + fresh lookup).
    - Escalation: low-confidence → escalate; max-clarification escalation; handoff anywhere and at a pending question.
    - FR51 coverage template; turn limit.
    - LLM: FR48 timeout fallback, repair-then-fallback, the per-thread call cap.
    - Data: no raw DOB in any LLM payload; no DOB or name in the lookup.
  - **Unit tests:** the extractor (every relative phrase, formats, roles, ambiguity, two-digit and missing years), validators, the FR29a matrix, the FR5a lifecycle, the rules and bare-reply lists, and redaction.

## M4: Claim + cancellation + confirmation
- **Exit criteria (PRD §12):** Flows B and I pass; the eligibility, duplicate (incl. unverified and unrelated reply), edit-invalidation, mixed-confirmation and injection tests pass.
- **Result:**
  - **Integration tests** (`tests/integration/test_claims_and_cancellations.py`):
    - Flows: Flow B end to end, with the exact summary and payload, the claim row and its idempotency key, and NFR2 counts. Full Flow H (claim → cancel, no slot leak). Two consecutive claims. Flow I: handoff at confirmation → escalated, no write, no `pending_action`.
    - Approval rules: a skipped estimate needs approval; the injuries / ≥ $10,000 / $9,999 thresholds.
    - Confirmation replies: bare no; other ×2 → decline; the three mixed replies ("yes, but change the location…", "ok, actually the date was the 20th", "confirm but date is 10/19") are edits with no write and a fresh `pending_action_id`; edit-over-LLM-yes; FR48 non-bare is never yes.
    - Edits: an `effective_date` edit re-quotes; an `injuries` edit now needs approval; a `policy_id` edit re-verifies.
    - Cancellations: the Flow C summary ($175.55); LAPSED $0.00 with the balance note; the window checks; already-cancelled creates no approval.
    - Eligibility: not in force, > 365 days, future date, PENDING_CANCELLATION allowed, the liability-only disclaimer.
    - Duplicates: an unverified duplicate never calls `find_open_claims` and reveals no claim ID; yes still needs confirmation and writes with `allow_duplicate`; no / unrelated leave no claim task.
    - Injection: three cases (text, LLM output, a write-tool call in `plan_tool`) — no write.
  - **Unit tests** (`tests/unit/test_write_guard.py`): every FR26 guard condition, the blocked write being an internal error with no DB write, prepare_action's fresh IDs and keys, the FR32 reasons, and deterministic edit detection.

## M5: Human-in-the-loop
- **Exit criteria (PRD §12):** Flows C, D and G pass; the restart and crash-before-interrupt tests pass; a double decision → 409; a late decision unsticks the thread; the concurrency test passes.
- **Result:**
  - **Integration tests** (`tests/integration/test_approvals.py`):
    - Flow C approve: the exact pending-review and final messages, interrupt payload fields (masked DOB, summary ≤ 3 sentences), FR38 no repeat, FR44 delivery via history.
    - Flow D reject: no write, the comment and the phone number shown.
    - Flow G: expiry on the customer's message → notice + restart offer → "yes" → fresh summary with a new `pending_action_id`; also expiry followed by a different intent.
    - A late decision → `ConflictError` (409 at the API in M7), approval expired, thread no longer interrupted, then "yes" restarts. Read paths show "expired" without persisting it.
    - A message while pending → side reply; graph not invoked (same checkpoint id, interrupt intact); history merged with side messages.
    - A claim with approval.
    - Recovery: a restart mid-approval (new graph, same SQLite file); a crash between announce and the interrupt → `ensure_interrupt` on the next message, with a single pending-review message.
    - Resume: a double resume writes once (NFR12); a crashed resume is completed by the customer's next message (step 3b).
    - Concurrency: decision and message at once ×3.
  - **Unit tests:** the approvals service (insert-or-get uniqueness with `customer`, conditional decide/expire setting `resume_status='pending'`, TTL).

## M6: Error handling + responses
- **Exit criteria (PRD §12):** Flows F and F2 pass; the correction-after-failure, write-retry consent matrix, commit-then-timeout and internal-error tests pass; the fault-injection suite passes; the hallucination and coverage-guard tests pass.
- **Result:**
  - **Integration tests** (`tests/integration/test_errors.py`):
    - **Read failures:**
      - Flow F: a quote timeout with 3 attempts, then "actually make it Nov 5" → the quote is called with 2026-11-05.
      - A lookup failure followed by a `policy_id` correction re-verifies.
      - Smalltalk keeps the failure state; a new intent suspends the failed task.
    - **Flow F2** on POL-999501:
      - Consent matrix, each with a write-count assertion: retry/yes → exactly 1 write with the original key; no / stop / cancel that / wait / withdraw → 0 writes; unrelated twice → re-ask then withdraw; a new intent → re-ask; handoff → truthful status then escalation.
      - The claim-without-approval wording.
    - **FR47a:**
      - POL-999502 reports the real confirmation number.
      - With `MOCK_FAULT_LOOKUP`, an unknown outcome → "no" → `WRITE_OUTCOME_UNKNOWN` escalation, never "withdrawn".
      - If the lookup recovers, "no" reports the real success.
    - **FR47b:** CONFLICT, an unknown code, NOT_FOUND after verification, APPROVAL_REQUIRED and UnauthorizedWriteError all escalate (not `failed_retryable`).
    - **Guards:** hallucinated ID, date and amount → template; coverage language → template; coverage questions mid-claim.
    - **Wrappers:** `RECURSION_LIMIT=3` → escalation (NFR13); an unhandled exception → system-unavailable with the status unchanged and the next message processed normally (NFR14).
    - **Soak:** a seeded random-fault soak (×4) with no crashes, no leaked internals and at most one write.
  - **Unit tests:** the guard functions (`tests/unit/test_guard.py`).

## M7: API + CLI
- **Exit criteria (PRD §12):** the API integration tests pass; the CLI-vs-API concurrency test passes; the interactive CLI demo works.
- **Result:**
  - **API tests** (`tests/integration/test_api.py`):
    - Flow A over HTTP with no tool messages returned; the 2,000/2,001-character boundary (FR56); a new `thread_id` starting fresh (FR2) and a 404 for unknown history.
    - History: customer-visible only, DOBs masked, side messages merged in `created_at` order.
    - Auth: `/state` needs the admin token and `/approvals` the reviewer token (401 without).
    - Approvals: the full flow with 200, a second decision → 409 and FR44 delivery; decision validation (422) and 404; a late decision → 409 that unsticks the thread.
    - Operations: rate limit → 429; `/healthz` ok/degraded; `/metrics` text; database failure → 503; no internal identifiers in responses; the missing-key startup warning; tracing enabled only with a key; no secrets logged.
  - **CLI tests** (`tests/integration/test_cli.py`, against a live Uvicorn server):
    - Unreachable API → exit 1 with a message; `chat`, `chat --thread-id` showing the reviewer outcome, and `/refresh` picking up a decision made mid-session.
    - `approvals list`/`decide` (409 on repeat); exactly one of `--approve`/`--reject` required; a wrong token → 401; truncation at 2,000 characters.
    - The **CLI decision + API customer message running concurrently ×3**: one write, one outcome message, one pending-review message.
  - **Manual check:** a real `uvicorn app.main:app` process plus the `support chat`, `support approvals list` and `support approvals decide` processes (fake LLM) ran Flow C end to end; `/metrics` showed the expected counters.

## M8: Docs — **Core MVP sign-off**
- **Exit criteria (PRD §12):** the README walkthrough is reproducible from a clean clone.
- **Result:** Verified from a fresh `git clone` into a scratch directory on Python 3.11.9:
  - `make install` (venv + `requirements.lock`) works.
  - `make demo` shows the three requests plus approve and reject; `tests/integration/test_demo.py` also runs it.
  - `make check` passes: lint + mypy, 657 tests in each mode, 96.3% coverage.
  - The interactive walkthrough (`make run-offline` + `.venv/bin/support chat` + `support approvals list`) works.
  - `git status` is clean afterwards: the databases are ignored.
- **Deliverables:** `README.md`, `docs/design-decisions.md` (the 16 §8.9 decisions, what is sent to the LLM vendor, the lockout DoS tradeoff, expiry), `scripts/demo.py`, and a regenerated `docs/graph.md`.
