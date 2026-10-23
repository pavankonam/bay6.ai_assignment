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
