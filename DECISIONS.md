# Decisions Log

Choices the PRD (`docs/PRD.md`) does not dictate: library versions, ambiguities, workarounds and deviations. Each entry: **Context → Decision → Why.**

---

## M0: Scaffold

### D-001 Project location and PRD copy
- **Context:** The PRD's project tree is rooted at `insurance-support-agent/`. The PRD lives on the Desktop, outside any repo.
- **Decision:** The project lives in `~/Desktop/insurance-support-agent/` (its own git repo). A verbatim copy of the PRD is committed as `docs/PRD.md`.
- **Why:** Reviewers can read the spec next to the code; the repo is self-contained.

### D-002 Packaging: `venv` + `pip` with exact pins and a lock file (not `uv`)
- **Context:** §8.1 suggests `uv` / `pyproject.toml`. `uv` is not installed on the build machine and a reviewer may not have it either.
- **Decision:** `pyproject.toml` declares every dependency with an exact `==` pin. `requirements.lock` (from `pip freeze`) pins the full transitive set. `make install` creates `.venv` with the stdlib `venv` module and installs from the lock file. No `uv` needed.
- **Why:** Works from a clean clone with only Python ≥ 3.11. Reproducible thanks to the lock file. `uv pip install -r requirements.lock` still works for people who prefer `uv`.

### D-003 Library versions (latest stable at build time, 2026-10-22)
- **Context:** §8.1 says "`langgraph` (latest stable 1.x, pinned)".
- **Decision:** `langgraph==1.2.16`, `langgraph-checkpoint==4.3.0`, `langgraph-checkpoint-sqlite==3.1.2`, `langgraph-prebuilt==1.1.1`, `langchain-core==1.7.0`, `langchain-anthropic==1.7.6`, `pydantic==2.14.1`, `pydantic-settings==2.16.1`, `python-dateutil==3.0.0`, `tenacity==9.2.1`, `structlog==26.1.0`, `sqlmodel==0.0.48`, `SQLAlchemy==2.1.6`, `fastapi==0.161.1`, `uvicorn==0.54.0`, `typer==0.27.3`, `rich==15.0.0`, `httpx==0.28.1`, `PyYAML==6.0.3`. Dev: `pytest==9.1.1`, `pytest-asyncio==1.4.0`, `pytest-cov==7.1.0`, `ruff==0.17.2`, `mypy==2.4.0`. Python 3.13.6 in development, `requires-python >= 3.11`.
- **Why:** Latest stable at build time, so we're not pinning known-old bugs. Pinned per R9.

### D-004 LangGraph APIs verified against the installed packages
- **Context:** Rule: check LangGraph APIs against the installed versions, not memory.
- **Decision:** A smoke script was run against `langgraph==1.2.16` before any design work. It confirmed:
  - `interrupt(value)` / `Command(resume=...)` resume semantics, and that `invoke` returns `__interrupt__`.
  - `StateSnapshot` exposes `.next`, `.interrupts` and `.tasks[*].interrupts`.
  - A graph compiled with `interrupt_before=["human_approval"]` (used to simulate a crash) yields `next == ("human_approval",)` with `interrupts == ()`, and `invoke(None, config)` then runs to the dynamic interrupt without re-running the previous node. This is the PRD's `ensure_interrupt()` premise.
  - `update_state(config, values, as_node=...)` works on a finished thread.
  - `SqliteSaver(conn)` + `.setup()` works, and `SqliteSaver.delete_thread(thread_id)` exists.
  - `ToolNode(tools, handle_tool_errors=..., wrap_tool_call=...)` and `Graph.draw_mermaid()` both work.
- **Why:** The HITL design (§8.8) depends on these exact behaviors.

### D-005 Quality-gate scope
- **Context:** NFR18 requires ruff + `mypy --strict` on `app/`. NFR19 requires ≥ 85% coverage on `app/graph`, `app/tools` and `app/validation`. The build rules require lint, mypy strict, and the full suite in both tool-selection modes with ≥ 85% coverage.
- **Decision:**
  - `make check` = `ruff check` + `ruff format --check` on `app/` and `tests/`, plus `mypy --strict` on `app/` (configured in `pyproject.toml`).
  - It then runs the full pytest suite twice (`TOOL_SELECTION_MODE=llm` and `deterministic`). **Each run** must reach ≥ 85% branch coverage measured over **all of `app/`**, which is stricter than NFR19's three packages.
  - Tests are type-annotated and ruff-clean but are not checked with `mypy --strict`.
- **Why:** This is a superset of both the PRD and the build rules. Strict mypy on test code adds cost without catching product bugs.

### D-006 Settings not listed in `.env.example`
- **Context:** Tests and operators need to relocate databases, and tests must not sleep through real backoff delays.
- **Decision:** Added these settings (defaults in parentheses):
  - `APP_DB_PATH` (`data/app.db`), `CHECKPOINT_DB_PATH` (`data/checkpoints.db`), `SEED_DIR` (`data/seed`)
  - `LOG_LEVEL` (`INFO`), `RETENTION_DAYS` (30, the NFR10 default)
  - `TOOL_RETRY_BASE_SECONDS` (0.5, the FR45 base backoff; tests set it to 0)
  - `SUPPORT_NO_DOTENV=1` (tests only) skips reading `.env`.
- **Why:** Per-test database isolation; fast deterministic retries; a developer's `.env` (for example, one with a real API key) can never change test behavior.

### D-007 Tests have no network access
- **Context:** The suite must run with the fake LLM and no network.
- **Decision:** An autouse fixture in `tests/conftest.py` replaces `socket.socket.connect` and raises on any non-loopback address. The conftest also forces `LLM_PROVIDER=fake` and removes `ANTHROPIC_API_KEY` from the test environment.
- **Why:** "No network" is enforced rather than assumed.

### D-008 Storage representation details
- **Context:** §8.4.2 lists column types (`NUMERIC` premium, timestamp columns) without specifying the SQLite representation.
- **Decision:** Money is stored as SQLite `REAL` (SQLAlchemy `Float`), and refund math uses `Decimal` on the string form with `ROUND_HALF_UP`. Timestamps (`created_at`, `decided_at`, `failed_at`) are ISO-8601 UTC strings. Calendar dates use the SQLAlchemy `Date` type.
- **Why:** SQLite has no native decimal type (SQLAlchemy warns on `Numeric`), and seed premiums are whole dollars. ISO strings sort correctly and match the `created_at` format on messages (FR53b).

### D-009 Seed semantics and unstated seed values
- **Context:** §8.4.3 requires `make seed` to be idempotent and gives the data table, but not plates, the coverage JSON shape for liability, or the open claim's details.
- **Decision:**
  - `load_seed` **upserts** seed policies (resetting them to seed values) and merges seed claims. Records created during demos (new claims, cancellations, approvals) are kept. `make reset` deletes the DBs and re-seeds.
  - Liability coverage is `{"type": "LIABILITY", "deductible": null}`. Plates are `ACM-100N` / `TST-xxx`.
  - The LAPSED history entry carries `"reason": "non-payment"`.
  - The seeded open claim CLM-2026-000001 (POL-100006, incident 2026-10-15) has location "Oak Rd & 3rd St, Springfield", an estimate of $1,800, and `created_at` 2026-10-16.
- **Why:** Re-running `make seed` always gives the same policy state, and nothing seeded contradicts the PRD.

### D-010 Recording commit hashes in CHECKPOINTS.md
- **Context:** The rules ask for each milestone's commit hash in `CHECKPOINTS.md`, but a commit cannot contain its own hash.
- **Decision:** Commit the milestone and tag it `checkpoint_mN`, then make a one-line follow-up commit that writes the tagged commit's hash into `CHECKPOINTS.md`.
- **Why:** The tag points at exactly the verified code, and the recorded hash stays accurate.

---

## M1: Mock backend + tools

### D-011 Timeouts: real per-attempt timeout plus simulated magic timeouts
- **Context:** FR45 requires a per-call timeout (`TOOL_TIMEOUT_SECONDS`, default 5). POL-999408 must "time out". Sleeping 5 seconds × 3 attempts in tests is unacceptable.
- **Decision:** `run_tool` runs each attempt on a worker thread and waits with `future.result(timeout)`. A late result raises `ToolTimeoutError`, which is a real timeout. The magic IDs (POL-999408, and the post-commit half of POL-999502) raise `ToolTimeoutError` immediately, as if the deadline had passed.
- **Why:** Real timeouts are enforced (and tested with a 20 ms deadline) without slow tests. A timed-out thread may still finish its work, which is exactly the "unknown write outcome" that FR47a handles.

### D-012 Error envelope extensions
- **Context:** §8.6 fixes the envelope as `{ok, data, error:{code, message, retryable}}`. The duplicate template needs the existing claim ID, and `ErrorInfo.attempts` needs an attempt count.
- **Decision:** `error` also carries `attempts`. `DUPLICATE_CLAIM` failures set `data = {"existing_claim_id": ...}`. An unexpected exception inside a tool becomes code `INTERNAL` (non-retryable) with a generic message.
- **Why:** These are additive fields; nothing in the PRD reads the envelope in a way they break. `INTERNAL` follows FR47b's "any unknown error code → escalate", and the message never leaks exception text (FR49).

### D-013 Check order in mock write APIs
- **Context:** The order of the idempotency, approval and eligibility checks isn't specified.
- **Decision:** The order is:
  1. Fault injection.
  2. `APPROVAL_REQUIRED` (`cancel_policy` only).
  3. Idempotency-key lookup: same hash → replay, different hash → `CONFLICT`.
  4. Eligibility (`NOT_ELIGIBLE`).
  5. Duplicate check (`DUPLICATE_CLAIM`).
  6. Insert.
  
  Writes are serialized by a process-wide lock in the mock service.
- **Why:** A replay must return the original result even after the first write changed the policy (for example, it is now `PENDING_CANCELLATION`). Otherwise the POL-999502 replays would wrongly report `NOT_ELIGIBLE`.

### D-014 The mock API re-checks business rules (defense in depth)
- **Context:** FR31 and FR34 rules are enforced by the graph. §8.6 lists `NOT_ELIGIBLE` for the write tools and the quote.
- **Decision:**
  - `create_claim` returns `NOT_ELIGIBLE` if the policy was not in force on the incident date, if the date is in the future, or if it is more than 365 days ago.
  - `get_cancellation_quote` and `cancel_policy` return `NOT_ELIGIBLE` outside `today..today+60` or when the status isn't ACTIVE/LAPSED.
  - An unknown policy on a write returns `NOT_ELIGIBLE`, since `NOT_FOUND` isn't in the write tools' code list.
- **Why:** The backend should not trust the agent. This is also the PRD's "policy status changed between approval and execution → `WRITE_NOT_ELIGIBLE`" case.

### D-015 `lookup_write_result` takes an optional `policy_id` hint
- **Context:** `MOCK_FAULT_LOOKUP` must make the lookup fail "for magic IDs". The lookup is keyed only by `idempotency_key` (a hash), and for POL-999501 no record exists to infer the policy from.
- **Decision:** `lookup_write_result(idempotency_key, policy_id=None)`. The hint is used only by fault injection.
- **Why:** This is the minimal change that makes the fault testable. Real lookups ignore the hint.

### D-016 Status history after a cancellation
- **Context:** §8.6 says `cancel_policy` sets `PENDING_CANCELLATION` (or `CANCELLED` if effective today) and appends to `status_history`. It doesn't give the dates.
- **Decision:** The open history entry is closed on `today − 1`, and the new status starts `today`, following the seeded POL-100005 pattern. `cancellation_effective_date` is set. LAPSED policies follow the same rule.
- **Why:** This is consistent with the seed data and with FR31's "PENDING_CANCELLATION before the effective date is in force".

### D-017 Payload canonicalization
- **Context:** `payload_hash` must be identical whether a value arrives as `2500` or `2500.0`, or as a `date` or its ISO string.
- **Decision:** `canonical_json` converts every non-bool number to `float` and dates to ISO strings, then serializes with `sort_keys=True` and compact separators.
- **Why:** This avoids spurious `CONFLICT`s between the hash computed in `prepare_action` and the one computed by the mock API.

### D-018 Modules not in the PRD's project tree
- **Decision:**
  - `app/rules.py`: in-force-on-date, refund formula, collision check. Shared by the graph and the mock backend.
  - `app/tools/idempotency.py`: canonical payload, hash, key.
  - `app/db.py`: engine and sessions.
  - `app/seed.py`: the `make seed` loader.
- **Why:** These rules are needed by both the graph and the mock backend. Putting them in `mock_api` would make the graph depend on the mock.

### D-019 `MOCK_FAULT_CLEAR_AFTER` semantics
- **Decision:** The counter covers magic-ID faults only (random faults don't count). It is process-wide and resets on restart (or `reset_fault_injector()` in tests). With `MOCK_FAULT_CLEAR_AFTER=3`, the three attempts of the first write fail, and the explicit customer "retry" then succeeds.
- **Why:** This matches the stated purpose ("lets the write succeed on an explicit retry").

### D-020 Own Prometheus text renderer instead of `prometheus_client`
- **Decision:** `app/observability/metrics.py` is a ~60-line, thread-safe counter registry with a text-exposition `render()`.
- **Why:** NFR17 only needs counters. This avoids a dependency and its global registry, which complicates resetting between tests.

---

## M2: State + graph skeleton

### D-021 Per-turn interpretation lives on the turn's `HumanMessage`, not in new state keys
- **Context:** FR3 says the state must follow `SupportState` **exactly**. But several node → router hand-offs need this turn's interpretation:
  - `understand` → `validate_slots` / `route_intent` (candidates, dates, confidence),
  - `handle_confirmation` → `route_confirm` (decision, slot changes),
  - `handle_pending_question` → `route_pending_question` (answer),
  - "the slot the agent last asked for" (FR12a step 2).
  
  None of these has a key in the schema.
- **Decision:**
  - The node replaces the latest `HumanMessage` (same `id`, so `add_messages` updates it in place) with extra `additional_kwargs`: `nlu`, `confirmation` or `pending_answer`.
  - `ask_for_info` stores `asked_slots` on its own `AIMessage`.
  - Routers read these through `app/graph/messages.annotation()`.
- **Why:** The schema stays exactly as specified, and routing remains a pure function of state. The annotations are checkpointed with the message, which gives an audit trail of how each message was interpreted. They are never returned to customers (only `content` is) and never sent to the LLM.

### D-022 The fake LLM is a real `BaseChatModel` inside `app/`
- **Context:** §8.10 says `get_chat_model()` returns `FakeScriptedChatModel` when `LLM_PROVIDER=fake`. The no-API-key README demo needs the fake at runtime, not only in tests.
- **Decision:**
  - `app/llm_fake.py` implements `FakeScriptedChatModel(BaseChatModel)` with `bind_tools` support, so `with_structured_output` and `bind_tools` follow the same code path as `ChatAnthropic`.
  - Each prompt file starts with `<!-- prompt: name -->`, and the fake uses that header to identify the call.
  - Tests script responses with `FAKE_SCRIPT.push(kind, ...)`. A response can be a model, a dict, text, an `AIMessage` or an exception to raise. Unscripted calls go to rule-based heuristics (`app/nlu/fake_heuristics.py`).
  - Every request is recorded, which is used for the NFR2 call counts and the NFR8a payload checks.
  - `tests/fixtures/fake_llm.py` re-exports the helpers.
- **Why:** One code path for the real and fake models, a demo that works without a key, and assertions on exactly what would be sent to a vendor.

### D-023 `route_slots` step 5 for `policy_status`
- **Context:** Step 5 is "policy_snapshot not loaded → lookup". For a status request, a snapshot cached by another task would let the flow fall through to step 10 (`prepare_action`), but there's no action to prepare for a status check.
- **Decision:** For `active_intent == "policy_status"`, step 5 always routes to `plan_tool` (lookup). `route_after_tool` then sends the fresh result to `respond`.
- **Why:** A status answer should reflect current data, and this makes the status path impossible to misroute.

### D-024 Extra routing branches not drawn in the §8.3 diagram
- **Decision:**
  1. `route_intent` → `escalate` when the confidence is low (or a date is ambiguous) and the intent-clarification counter (`clarification_attempts["_intent"]`) has reached `MAX_CLARIFICATION_ATTEMPTS`. This implements §9.1 "Gibberish → … after 3 tries → escalate".
  2. `route_pending_question` → `escalate` when a `retry_write` withdrawal leaves `last_outcome = WRITE_OUTCOME_UNKNOWN` (FR47a). The diagram sends this through `handle_error`; going directly is equivalent and avoids building a fake error.
  3. `route_error` escalates any **non-retryable** error, not only those flagged `internal`.
- **Why:** These implement written requirements that the diagram doesn't show explicitly. Every branch has a unit test.

### D-025 The tool phase is computed, not trusted from `state.phase`
- **Decision:** `app/graph/phases.tool_phase(state)` (verify → lookup → quote) is a pure function of the slots and verification state. `plan_tool`, `validate_tool_call` and `validate_slots` all use it, and `validate_slots` still writes `phase` for observability.
- **Why:** The allowlist must be applied to the real phase. A stale `phase` value can never unlock a tool.

### D-026 LLM call wrapper details
- **Decision:**
  - The repair retry (§9.3, "1 repair") re-invokes with a short, code-written hint appended to the system prompt, and counts as a second call.
  - The context block in the system prompt contains only code-derived values (intent, phase, slot names, ISO dates), never raw customer text (NFR5).
  - History is the last 6 customer-visible messages, trimmed from the oldest to fit `LLM_HISTORY_MAX_CHARS`, and always starts with a user message.
  - An exception, an invalid output after the repair, an empty text reply, a missing tool call, or the per-thread cap all return `value=None`. The node then applies the FR48 rule-based fallback and `llm_fallbacks_total` is incremented.
- **Why:** This bounds calls per turn (NFR2), keeps prompt injection out of the system role, and gives one consistent fallback signal.

### D-027 `ToolNode` requires the graph runtime in `langgraph-prebuilt==1.1.1`
- **Context:** Calling `ToolNode.invoke` outside a graph raises `Missing required config key 'N/A'` in this version.
- **Decision:** Tests exercise `ToolNode` inside a minimal compiled graph.
- **Why:** This was verified against the installed package (rule 4).

### D-028 Node function typing
- **Decision:** Node functions take `Mapping[str, Any]` and return `dict[str, Any]`. `graph_node(name)` raises `StateSchemaError` on unknown keys (FR3) and logs the node. The builder casts the node callables at the `add_node` boundary.
- **Why:** LangGraph's `add_node` overloads expect the TypedDict input type. A cast at the one wiring point keeps mypy strict everywhere else.

### D-029 `app/nlu/` package
- **Decision:** `app/nlu/` holds `schemas.py` (`UnderstandResult`, `ConfirmationResult`, `YesNoResult`), `redaction.py` (NFR8a) and the rule-based NLU used both by the FR48 fallback and by the fake model's heuristics.
- **Why:** Language-understanding code is shared by several nodes and the fake model, so it has its own package.

### D-030 `ingest` initializes the defaults
- **Decision:** `ingest` fills in any missing state key with its default (empty slots, `verified=False`, counters at 0, …). Existing values are never overwritten.
- **Why:** Every downstream node can rely on a fully populated state without defensive defaults.

---

## M3: Understanding, slot filling, verification

### D-031 Date parsing details (FR12a)
- **Context:** FR12a says "absolute dates via python-dateutil (US MM/DD/YYYY for ambiguous numerics), always with an explicit default anchored to APP_TODAY". Two-digit years and missing years need role-specific rules that dateutil can't apply, because dateutil resolves two-digit years against the host clock.
- **Decision:**
  - Month-name forms ("April 12", "12 April 1985", "Nov. 1") are parsed with `dateutil.parser.parse(..., default=datetime(APP_TODAY...), dayfirst=False)`. The year is extracted separately.
  - ISO and US numeric forms (`MM/DD[/YY[YY]]`, `-` separators) are parsed with an explicit regex in US order. A `.` separator is not accepted, so "2.5k" is never read as a date.
  - A day-only ordinal ("the 20th") is also supported.
  - Each role's missing-year and two-digit-year rule is applied in `resolve()`.
- **Why:** The behavior is deterministic and never depends on the host clock (A7). It is exactly the FR12a contract, including the December/January boundaries (tested).

### D-032 Plausibility uses only the roles relevant to the conversation
- **Context:** FR12a step 3 assigns a role "if only one role fits". A yearless date like "Nov 1" fits both `incident_date` (2025-11-01) and `effective_date` (2026-11-01), so a cancellation reply "Nov 1" would always be ambiguous.
- **Decision:** Plausibility considers only `dob` plus the date slot of the intent being discussed (the primary intent of this message, else `active_intent`). With no known intent, all three roles are considered.
- **Why:** A claim has no effective date, and a cancellation has no incident date. Genuinely ambiguous cases still produce the clarifying question.

### D-033 Incident dates more than 365 days ago pass the validator; FR31 blocks them
- **Context:** §8.5 lists the `incident_date` validator as "≤ today, ≥ today − 365d". FR31 and §9.1 say a date more than 365 days ago should be explained ("can't be filed via chat") with the escalation contact, not re-asked.
- **Decision:** The validator rejects only future dates ("The incident date can't be in the future."). An older date is kept, and after verification and lookup FR31 marks the claim `TOO_OLD` → `CLAIM_INELIGIBLE` (a template with the phone number).
- **Why:** This gives the customer the §9.1 behavior, and the check stays verification-gated (R18).

### D-034 Unambiguous values survive a date-disambiguation turn
- **Context:** `route_intent` sends ambiguous dates to `clarify`, which skips `validate_slots`. Every other value in that message (policy number, DOB, intent) would be lost and have to be repeated.
- **Decision:**
  - `understand` still sets `active_intent`, and leaves the ambiguous role out of the candidates.
  - On the next turn, `validate_slots` also merges the previous message's still-unmerged extraction, but only if that message triggered a date clarification.
- **Why:** The agent never guesses dates (FR12a), and it doesn't make the customer repeat unambiguous information (US2).

### D-035 Intent switching
- **Decision:**
  - A switch (FR11) happens only when the new intent is classified with confidence ≥ the threshold.
  - A `policy_status` task is not pushed to `suspended_tasks`. It has no task slots and finishes in one lookup, so offering to "continue checking your status" would be noise.
  - When a suspended task is restored on a different policy than the one now verified, the customer's DOB is cleared, because it belongs to the other policy. The agent asks: "To continue with policy POL-…, please confirm the date of birth on that policy." Verification and lookup then re-run.
- **Why:** FR11 requires re-verification and a fresh lookup on resume. The DOB on file can't verify a different policy.

### D-036 Merge rules
- **Decision:**
  - A newly given `policy_id` that differs from the current one is treated as a correction (FR21) even without explicit correction wording. Other slots are overwritten only when `is_correction` is set (FR12).
  - Every held value is re-validated on each `validate_slots` run, so restored tasks are re-checked like newly extracted values (FR10/FR11) and a stale effective date that is now in the past is caught.
  - Candidates are merged once per turn: the annotation is marked `merged`.
- **Why:** This follows FR12 and FR21. Merging once per turn prevents a correction from re-invalidating a verification that has just succeeded.

### D-037 Optional slots are "asked once"
- **Decision:** After an optional slot (damage estimate, police report) has been asked once, a reply that doesn't provide it, or a skip word, records it as an explicit `null` in `slots_skipped` (FR16). A skipped damage estimate later triggers the `damage_estimate_unknown` approval (FR32).
- **Why:** FR16 says optional slots are asked once. Re-asking until the customer says "skip" would contradict it.

### D-038 Verification counters and messages
- **Decision:**
  - `verification_attempts` counts **failed** verifications. The FR20a test requires `== 1` after one failure followed by a success.
  - At `MAX_VERIFICATION_ATTEMPTS`, `escalate` sets `status = locked`, and every later message in that thread gets the locked message.
  - The FR20 message is shown once, followed by the identity question.
- **Why:** This matches FR19, FR20 and FR20a exactly.

### D-039 Conversational wording
- **Decision:**
  - The thread's first agent reply starts with "Hi, I'm the Acme Mutual support assistant." (FR5).
  - The first question of a task gets an intent-specific lead-in ("I'm sorry to hear that. I'll help you report a claim.").
  - After a successful verification, the next question starts with "Thanks, you're verified." (Flow B).
  - Smalltalk and out-of-scope replies mid-task repeat the pending question and carry its `asked_slots`, so the customer's next answer is still attributed correctly.
- **Why:** This follows the PRD flows, and the pending task survives off-topic turns (FR5a).

### D-040 Fallback and clarification behavior (FR7, FR48)
- **Decision:**
  - If the LLM fails, `understand` uses `rule_understand(strict=True)`: keyword intents, the policy-number regex and the date extractor.
  - If even the rules recognize nothing, `clarify` sends the system-unavailable message (FR48 "cannot proceed").
  - A non-bare reply to a yes/no question without the LLM is treated as `unrelated`.
  - Low-confidence clarifications are counted in `clarification_attempts["_intent"]`, and after 3 the thread escalates (§9.1).
  - Coverage/payment questions ("will I be covered?") are detected deterministically and always get the FR51 template, whatever the LLM says.
- **Why:** These are deterministic fail-safes on the LLM's output.

### D-041 Redaction also covers the code-built context block
- **Context:** A test in this milestone found the raw DOB in the system prompt: the deterministic `date_mentions` list included the DOB's ISO value.
- **Decision:** Mentions that are, or could be, the DOB appear in the context as `"[DOB]"`. The test asserts that no supported DOB format (ISO, US numeric, month name) appears anywhere in any fake-LLM request.
- **Why:** NFR8a applies to everything sent to the vendor, not just to customer messages.

### D-042 Invalid policy-number feedback for bare numbers
- **Decision:** When the agent asked for a policy number and the reply contains a bare 3–10 digit number (like "12345"), that number is validated as a `policy_id` candidate. It fails, and the customer gets "Policy numbers look like POL-123456. Could you check yours?" (§9.1).
- **Why:** The §9.1 example is a bare "12345".

### D-043 Flow H is tested in two parts
- **Context:** Flow H continues from a completed Flow B claim. Claim submission arrives in M4.
- **Decision:** M3 tests the same mechanics with status → cancellation: identity is carried over and only the date and reason are asked. M4 adds the full Flow B → cancellation test.
- **Why:** The milestone order puts writes after understanding.

---

## M4: Claims, cancellations, confirmation

### D-044 Nodes receive `RunnableConfig` (for the FR27 idempotency key)
- **Context:** FR27 builds `idempotency_key` from `thread_id`, which lives in the run config, not in state. LangGraph 1.2.16 injects `config` only when the node callable's own signature declares a parameter annotated `RunnableConfig` (verified in `langgraph/_internal/_runnable.py`). `functools.wraps` would hide that parameter.
- **Decision:** The `graph_node` wrapper declares `config: RunnableConfig` and passes it to node functions that take two parameters. It does not use `functools.wraps`; it copies the name and doc itself and exposes `node_fn`.
- **Why:** This is the documented injection mechanism of the installed version, so node code never needs to reach for globals to find the thread.

### D-045 Code-invoked writes are recorded as an audit tool-call pair
- **Decision:** `execute_write_action` appends an `AIMessage` with the write's `tool_calls` plus a `ToolMessage` holding the result envelope as `artifact`.
- **Why:** The thread's history then shows exactly what was written and with which idempotency key. These messages are never customer-visible (FR53) and never reach the LLM (§8.10 history filter).

### D-046 Confirmation summary content (FR28, FR31, FR35)
- **Decision:**
  - Dates are restated as `2026-10-21 (October 21, 2026)`.
  - Any approval requirement is explained ("Because injuries were reported, a support specialist will review this claim before it's filed.").
  - A liability-only policy gets the FR31 collision disclaimer, and an `allow_duplicate` claim names the open claim.
  - A LAPSED cancellation adds "Any outstanding balance will be reviewed by a specialist." Every cancellation summary says it will be reviewed (FR36).
  - Any invalid edit feedback is shown above the summary.
- **Why:** FR28 requires the full payload "including restated dates, the quote and any approval requirement".

### D-047 Confirmation classification details (FR29)
- **Decision:**
  - A first `other` re-asks only the question ("Sorry, I didn't catch that. Shall I submit this claim? …"). It does not repeat the whole summary, and `confirmation_reask_count` stays at 1 until a new summary resets it.
  - The `ConfirmationResult` call's context lists the summary **field names** only, never their values. The values are customer text, and NFR5 keeps customer text out of the system role; the model sees the summary in the history.
  - Deterministic edit detection covers policy numbers and the action's date field. Free-text edits such as the location need the LLM. In fallback mode they are treated as `other` (re-ask), never as `yes`.
  - An edit is applied through `invalidate_on_edit`, which always discards the pending action, even if every proposed value fails validation; the next summary shows the feedback.
- **Why:** This follows FR29, FR29a and FR48.

### D-048 `DUPLICATE_CLAIM` at write time
- **Context:** The validation-time FR33 check makes this case a race; for example, another thread filed a claim after the summary was shown. `route_write` sends it to `respond`.
- **Decision:** It becomes `last_outcome = CLAIM_NOT_FILED_DUPLICATE`, with a template naming the existing claim and saying how to file anyway.
- **Why:** This is defense in depth (§8.6). Re-asking at that point would need a second confirmation loop the PRD doesn't define.

### D-049 Status writers follow §8.3 exactly
- **Decision:** `handle_confirmation` and `invalidate_on_edit` do not write `status`. The next node sets it (`ask_confirmation`, `validate_slots`, `respond`, `escalate`, `announce_approval` or `execute_write_action`).
- **Why:** These two nodes aren't listed as writers in the §8.3 status-transition table.

### D-050 Cancellation `approval_reasons`
- **Decision:** A cancellation's `pending_action.approval_reasons` is `["cancellation_requires_review"]`.
- **Why:** FR36 always requires approval, but the PRD names reason codes only for claims (FR32). The reviewer payload needs a non-empty risk flag.

---

## M5: Human-in-the-loop

### D-051 The §8.8 procedures live in a framework-free runtime
- **Decision:** `app/runtime.py` (`SupportRuntime`) implements:
  - the customer message-handling algorithm (steps 1–4, including 2a, 3a, 3b and 3c),
  - the decision procedure (steps 1–6),
  - `ensure_interrupt()` and `resume_thread()`,
  - the NFR13/NFR14 invocation wrapper.
  
  It raises `NotFoundError`, `ConflictError` and `MessageTooLongError`. The FastAPI layer (M7) maps those to 404, 409 and 422 and adds nothing else.
- **Why:** The milestone order puts HITL (M5) before the API (M7), and the exit criteria (409 on a double decision, concurrency) must be testable here. It also keeps the endpoints thin, with one implementation of every locking rule.

### D-052 What the reviewer sees
- **Decision:**
  - The interrupt payload's `customer` comes from the approval row, built by `approvals_service` from the repository: full policy ID, `"Jane Doe"`, `"****-**-12"`.
  - The `summary` is generated by code in at most 3 sentences: the request, the refund (or the injuries and estimate), and the risk flags.
  - `risk_flags` = `pending_action.approval_reasons`.
- **Why:** FR37 and NFR9. Code-generated text avoids an extra LLM call, and the raw DOB is never included.

### D-053 Approval timing uses the wall clock, not `APP_TODAY`
- **Decision:** `created_at`, `decided_at` and the 48-hour TTL use real UTC time (`config.utcnow()`). Tests make an approval stale by backdating `created_at` in the DB.
- **Why:** `APP_TODAY` is a date-only business clock (A7) for slot logic. A TTL measured in hours needs timestamps.

### D-054 Customer history masks DOBs only
- **Decision:** `GET /threads/{id}` (`SupportRuntime.history`) runs every customer message through the DOB redactor, so DOBs show as `[DOB]`. Policy numbers and reference IDs are left unchanged.
- **Why:** FR53 asks for "PII masked". The customer needs their own policy and reference numbers, and NFR8's policy-ID masking is defined for logs and traces.

### D-055 State updates in the invocation wrapper
- **Decision:**
  - On `GraphRecursionError`, the escalation message, `status=escalated` and `last_outcome=ESCALATED` are written via `update_state(..., as_node="escalate")`.
  - On any other exception, the system-unavailable message and `last_outcome=SYSTEM_UNAVAILABLE` are written via `update_state(..., as_node="clarify")`, leaving `status` untouched.
  - Both `escalate` and `clarify` have a single edge to `END`, so the next customer message starts a normal run from `START`.
- **Why:** This follows NFR13 and NFR14 ("the next message is processed normally", status unchanged). `as_node` has to name a node whose edges lead to `END`.

### D-056 `human_approval` sets the outcome for a rejection or expiry
- **Decision:** On resume, `human_approval` writes `approval` and `status=in_progress`, and for a rejection or expiry also `last_outcome` (`CLAIM_REJECTED`, `CANCEL_REJECTED` or `APPROVAL_EXPIRED`). `respond` then renders the template, and `close_task` stashes an expired task as `restart_expired`.
- **Why:** `route_approval` sends both rejection and expiry straight to `respond`, which picks its template from `last_outcome`.

### D-057 Simulating a crash before the interrupt
- **Decision:** `build_graph(..., interrupt_before=[...])` exists for tests only. Compiling with `interrupt_before=["human_approval"]` reproduces the persisted state of a crash after `announce_approval` was checkpointed: `next == ("human_approval",)` and no interrupts. The test then reopens the same SQLite file with a normal graph.
- **Why:** This reproduces exactly the persisted state the PRD's step 2a must repair, without killing a real process mid-step.

---

## M6: Error handling + responses

### D-058 Write-failure wording depends on whether approval was involved
- **Context:** Flow F2's message ("Your request was approved, but we couldn't complete it right now…") assumes an approved request. A low-severity claim needs no approval but can still hit a write failure.
- **Decision:** It says "Your request was **confirmed**, but…" when `requires_approval` is false, and "approved" otherwise. The retry and withdraw instructions are identical.
- **Why:** The agent must never claim an approval that didn't happen.

### D-059 Unknown outcome (FR47a) is carried as an error code
- **Decision:** When write retries are exhausted and `lookup_write_result` itself fails, `execute_write_action` stores the error with code `WRITE_OUTCOME_UNKNOWN` (retryable) and `resume_step=execute_write`.
  - `handle_error` then sets `retry_write` with `context.outcome_unknown=true` and sends the "We couldn't confirm whether your approved request went through…" offer.
  - On "no", a second unrelated reply, or a handoff, `withdrawal_check()` calls the lookup again. Found → the real success template. Absent → `WRITE_WITHDRAWN`. Still unknown → `last_outcome=WRITE_OUTCOME_UNKNOWN`, an ERROR log with the idempotency key, and escalation.
- **Why:** The customer is never told a request was withdrawn when it may have committed (R19).

### D-060 Handoff during `retry_write`
- **Decision:** A handoff during `retry_write` first runs the withdrawal check and appends the truthful status message (success or withdrawn), then escalates with the handoff message. If the status can't be determined, only the "a specialist will confirm the status" escalation is sent.
- **Why:** FR29b: "run the FR47a withdrawal path first (truthful status), then escalate".

### D-061 FR47b escalation details
- **Decision:**
  - `handle_error` handles any non-retryable error code: `CONFLICT`, `APPROVAL_REQUIRED`, `UNAUTHORIZED_WRITE`, `NOT_FOUND`, `INTERNAL`, or any unknown code. It marks the error `internal`, sets `last_outcome=ESCALATED`, and logs at ERROR (CRITICAL for `UNAUTHORIZED_WRITE`).
  - `escalate` sends the generic internal-error message, applies the FR5a reset (so no stale `pending_action` survives) and increments `escalations_total{reason="internal_error"}`. The metric is incremented once, in `escalate`, not in both nodes.
- **Why:** FR47b. Counting in one place avoids double-counting the metric.

### D-062 What the fact check (FR50) recognizes
- **Decision:** It extracts reference IDs (POL/CLM/CNL/APR), ISO dates, long-form dates ("December 31, 2026" → ISO) and `$` amounts from the LLM reply. Every one must appear in the flattened `response_facts` (plus the pending question text). Numbers are compared as floats rounded to cents.
- **Why:** These are the three value classes FR50 names. Anything else (for example "2 business days") is prose.

### D-063 NFR14 restores the turn's starting status
- **Context:** `ingest` checkpoints its normalization (`awaiting_info → in_progress`) before a later node can fail. "The thread stays at its last checkpoint with its status unchanged" otherwise loses the turn's starting status.
- **Decision:** The invocation wrapper captures `status` before invoking. On an unhandled exception it writes that status back, together with the system-unavailable message and `last_outcome=SYSTEM_UNAVAILABLE`.
- **Why:** This is the intent of NFR14: a crash leaves the conversation exactly where the customer last saw it, and the next message is processed normally (tested).

---

## M7: API + CLI

### D-064 API structure
- **Decision:**
  - `app/main.py` holds `create_app()` plus a module-level `app` for `uvicorn app.main:app`.
  - Handlers are synchronous `def`s, so they run in the threadpool (A8), and call `SupportRuntime`, which holds the per-thread locks.
  - `MessageIn.content` has `max_length=2000`, so FastAPI returns 422 (FR56).
  - Runtime errors map to HTTP: `NotFoundError` → 404, `ConflictError` → 409.
  - A SQLite/SQLAlchemy `OperationalError` or `DatabaseError` → 503 with a generic body (§9.3). `/healthz` returns `degraded` when either database can't be queried.
- **Why:** The endpoints stay thin, and all the §8.8 logic lives in one place.

### D-065 Auth and secrets
- **Decision:** The reviewer and admin tokens are read once when the app is created and compared with `secrets.compare_digest`. A missing or wrong header → 401 (FR55). Tokens are never logged; the masking processor also blanks keys named like tokens.
- **Why:** Settings are loaded once at startup, as in production. Reading them per request would also make the server and an in-process CLI share a token in tests, which hid a misconfiguration.

### D-066 Rate limiting
- **Decision:** An in-memory token bucket per `request.client.host` with capacity `RATE_LIMIT_PER_MINUTE`, refilling continuously over 60 seconds. It applies only to `POST /threads` and `POST /threads/{id}/messages` (FR19b), and an empty bucket → 429.
- **Why:** This is the spec, and in-memory is fine for the single-process MVP (A8).

### D-067 Startup conveniences
- **Decision:**
  - If the `policies` table is empty at startup, the seed data is loaded, so `make run` works from a clean clone without `make seed`. `make seed` stays the idempotent loader.
  - With `LLM_PROVIDER=anthropic` and no `ANTHROPIC_API_KEY`, startup logs `llm_key_missing` with a hint to use `LLM_PROVIDER=fake`. Calls would otherwise silently degrade to the FR48 rules.
  - When `LANGSMITH_API_KEY` is set, `LANGSMITH_TRACING=true` is enabled (NFR16). Nothing is traced otherwise.
- **Why:** A reviewer without a key gets a working demo and a clear signal about which LLM is in use.

### D-068 CLI behavior
- **Decision:**
  - Every command except `graph draw` uses `httpx` against `SUPPORT_API_URL`. A transport error → exit code 1 with "API not reachable at {SUPPORT_API_URL}" (FR54). HTTP errors print `Error <code>: <detail>` and exit 1.
  - `support chat` offers `/refresh`, which fetches `GET /threads/{id}` to show a reviewer's outcome (FR44), and `/quit`. Resuming with `--thread-id` prints the history so far. Messages over 2,000 characters are truncated with a warning (§9.1).
  - `support approvals list` prints one block per approval (customer with masked DOB, payload, risk flags, summary) rather than a table, which wrapped unreadably at 80 columns.
- **Why:** These follow FR54 and §9.1, and the output is usable in a normal terminal.

### D-069 How the CLI-vs-API concurrency test runs
- **Decision:** `tests/integration/test_cli.py` starts a real Uvicorn server on a free localhost port in a background thread; the network guard allows loopback. It then fires a CLI decision (`CliRunner` → `httpx` → the server) and an API customer message on the same thread behind a barrier, three times.
- **Why:** This tests the actual FR54/NFR14a path (CLI → HTTP → per-thread lock), not a shortcut.

---

## M8: Docs

### D-070 Demo design
- **Decision:**
  - `make demo` (`scripts/demo.py`) starts its own API on a free localhost port with temporary databases, the fake LLM and `APP_TODAY=2026-10-22`.
  - It plays the customer over HTTP and the reviewer through the real CLI (subprocesses), covering: status, a low-severity claim, a cancellation with a "still pending" reply then approval, and an injury claim with no estimate that is rejected.
  - `tests/integration/test_demo.py` runs the script, so the README demo can't silently break.
  - `make run-offline` starts the API with the fake LLM and the PRD's `APP_TODAY` for interactive use.
- **Why:**
  - **No key or network:** the demo has to work from a clean clone without either.
  - **A pinned date:** the seed policies have 2026 terms, so pinning the business date keeps every example (Flow C's $175.55, "yesterday" = 2026-10-21) reproducible whatever the real date is.
  - **Real code paths:** both the HTTP and CLI paths are exercised.

### D-071 Python version verified from a clean clone
- **Decision:** The M8 walkthrough was run from a fresh `git clone` on **Python 3.11.9**: `make install`, `make demo`, `make check` (657 tests ×2), and `make run-offline` + `support chat`. The development environment is 3.13.6.
- **Why:** This confirms that `requires-python >= 3.11` and the lock file (generated on 3.13) work on the minimum supported version.

---

## M9: Hardening

### D-072 Multi-intent queue (FR10)
- **Decision:**
  - When the primary intent is a confident supported intent, `understand` appends every other supported intent in the message to `intent_queue` as `{intent, slots}`. The slots are the candidates attributed to that intent: the LLM's `task_slots[intent]` plus deterministic date mentions whose role belongs to that intent.
  - When the active task reaches a terminal outcome, `offer_next_task` offers queued tasks first, then suspended ones.
  - "Yes" restores the slots, which `validate_slots` re-validates like new values.
  - The Core MVP note ("I can help with your other request once this one is done") has been replaced by the queue.
- **Why:** FR10. Queued tasks come from the same message, so they go before older suspended work.

### D-073 Per-policy lockout (FR19a)
- **Decision:**
  - `verify_policyholder` returns `{"verified": false, "locked": true}` without checking the DOB when the submitted policy ID has at least `POLICY_VERIFY_LOCK_FAILURES` failures within `POLICY_VERIFY_LOCK_HOURS`.
  - `process_tool_result` records each failure under the **submitted** policy ID (with the thread ID), except while the policy is locked, so the window can't be extended.
  - The customer sees the FR20 message plus "For your security, please call 1-800-555-0199 to continue."
  - `verification_lockouts_total` is incremented when a failure locks a policy.
  - `POST /admin/policies/{id}/unlock` (CLI `support policy unlock`) deletes that policy's failure rows and logs at WARNING.
- **Why:** FR19a. Unknown policy IDs collect failures the same way, so the `locked` flag can't be used to enumerate policies.

### D-074 Reconciliation (FR43a)
- **Decision:** `reconcile(runtime)` runs in the FastAPI lifespan before requests are served, and via `POST /admin/approvals/reconcile` (CLI `support approvals reconcile`). It holds each thread's lock while it:
  1. runs `ensure_interrupt` for any pending approval whose thread sits at `human_approval` with no interrupt;
  2. expires pending approvals past their TTL;
  3. resumes every approval with `resume_status='pending'`.
  
  Threads stuck before the interrupt are found through their approval rows, which `announce_approval` always inserts before the interrupt can run.
- **Why:** This covers all three FR43a cases without scanning every checkpoint, and FR27 idempotency makes a repeated resume safe (tested).

### D-075 Message idempotency (FR53a)
- **Decision:** With a `client_message_id`, `SupportRuntime.post_message` returns the stored response for `(thread_id, client_message_id)`. It checks once before taking the lock and again under it, so concurrent retries are safe. Otherwise it processes the message and stores the response in `processed_messages` (UNIQUE pair).
- **Why:** FR53a. Checking under the lock closes the race between two identical retries.

### D-076 Retention purge (NFR10)
- **Decision:**
  - A thread's age is its latest checkpoint timestamp (from `SqliteSaver.list(None)`).
  - For each thread older than N days, holding its lock and skipping any thread with an interrupt or a pending approval, the purge runs `checkpointer.delete_thread(thread_id)` and deletes the thread's `approvals`, `thread_side_messages` and `processed_messages`. It also removes `verification_failures` older than N days. Claims and cancellations are kept.
  - Both databases use `PRAGMA secure_delete=ON`, and the checkpoint WAL is truncated after a purge.
- **Why:** NFR10 requires that "no raw DOB for a purged thread remains in either DB". Without `secure_delete`, SQLite leaves deleted rows in free pages; the test scans the raw checkpoint file bytes.

### D-077 Shared FastAPI dependency module
- **Context:** With `from __future__ import annotations`, FastAPI evaluates dependency annotations against module globals. A dependency function defined inside a router factory was silently treated as a query parameter (this surfaced as a 422 in a test).
- **Decision:** `get_runtime` and the `Runtime` alias live in `app/deps.py` and are used by `main.py` and `admin.py`.
- **Why:** This is how FastAPI resolves postponed annotations in the installed version.

---

## M10: Evaluation

### D-078 How the evaluation is run and scored
- **Decision:**
  - `tests/eval/run_eval.py` drives each YAML conversation through `SupportRuntime`, the same path as the API, including reviewer decisions. Every conversation gets fresh temporary databases, the seed data and `APP_TODAY=2026-10-22`.
  - **Intent accuracy** compares `nlu.primary` on annotated turns. An expected `provide_info` also counts as correct when the model repeats the already-active intent, since answering a question while restating the request is harmless.
  - **Slot accuracy** compares the expected slots and write arguments, normalized for whitespace, case and float rounding. Values come from the actual write payload if a write happened, otherwise from the slots observed across turns.
  - **Task success** requires all of: the expected terminal outcome, the expected tools as a subsequence, no forbidden tools, a matching write (or none), the required text, the approval reasons, and the turn limit.
  - **Grounding:** every agent message is checked with the FR50 fact check against everything the agent legitimately held at any checkpoint (`get_state_history`: tool results, `claim_checks`, slots, payloads, quotes, approvals), plus the fixed format example "POL-123456".
  - **Coverage language:** every agent message is also checked with the FR51 filter.
- **Why:** These are the PRD §11.1 definitions, measured on the real code path. Using the full state history avoids false "hallucination" reports caused by the FR5a reset clearing state after a task completes.

### D-079 Negated dates are not candidates (a product change found by the eval)
- **Context:** "actually the accident was on the 20th, not yesterday" produced two incident-date mentions, so FR12a's "two dates claim the same role → ask" rule fired, even though the customer explicitly rejected one date.
- **Decision:** A date mention preceded within 20 characters by "not", "never", "instead of", "rather than", "wasn't" or "isn't" is dropped from role assignment. Unit tests cover three phrasings.
- **Why:** The customer has said which date is right. Asking would be the "guess vs ask" rule misfiring, not real ambiguity.

### D-080 Grounding accepts amounts quoted verbatim from facts
- **Decision:** In the FR50 fact check, a `$` amount is grounded if it is a numeric fact **or** appears verbatim in the fact text (for example, inside a customer-provided location).
- **Why:** Echoing the customer's own wording isn't a hallucination. Amounts that appear nowhere in the facts are still rejected.

### D-081 Fake-model heuristics improved, but not tuned to the eval
- **Decision:** These are general fixes to the fake's slot heuristics, which are also used by the demo: a place name after "on/at/in" in a claim's opening message becomes the location, and a yes/no answer about injuries or another car, a money amount, or a bare "yes" is never taken as a location. I did not tune the fake further to pass the eval set.
- **Why:** The fake makes the no-key demo usable. Tuning it to the eval phrases would only test the fake, while §11.1 targets the real LLM.

### D-082 M10 exit criterion is blocked on an API key
- **Context:** M10's exit criterion is "Success metrics in 11.1 met", which needs the real LLM. No `ANTHROPIC_API_KEY` is available in the build environment, and the test suite is network-free by design.
- **Decision:**
  - M10 is recorded as **blocked, not passed**, and is not tagged `checkpoint_m10`.
  - The deliverables are complete: 60 conversations, the runner, `make eval`, and a Markdown/JSON report that exits non-zero below target.
  - The harness is validated with the fake model (`make eval-fake`, also run by `tests/integration/test_eval_harness.py`): all three hard gates are at 0, along with the turn targets.
  - To close M10: put the key in `.env` and run `make eval`.
- **Why:** Rule 1 says a milestone is done only when its exit criteria pass. Reporting fake-model numbers as the §11.1 result would be misleading.
