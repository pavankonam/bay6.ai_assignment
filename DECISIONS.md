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
