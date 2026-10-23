# Checkpoints

Gates for every milestone: the PRD §12 exit criteria, plus `make check`:
- ruff lint + format check, and `mypy --strict app`
- the full fake-LLM, no-network test suite in **both** `TOOL_SELECTION_MODE=llm` and `deterministic`
- ≥ 85% branch coverage over `app/` in each mode

Each milestone commit is tagged `checkpoint_mN`. A commit cannot contain its own hash, so the hash column is filled in by the follow-up "record checkpoint" commit. The hash listed is the tagged commit.

| Milestone | Status | Tests passed (llm / deterministic) | Coverage (llm / det) | Commit | Tag |
|---|---|---|---|---|---|
| M0: Scaffold | ✅ Passed | 15 / 15 | 98.0% / 98.0% | `7d9ce56` | `checkpoint_m0` |
| M1: Mock backend + tools | ⏳ Not started | | | | |
| M2: State + graph skeleton | ⏳ Not started | | | | |
| M3: Understanding + slots + verification | ⏳ Not started | | | | |
| M4: Claim + cancellation + confirmation | ⏳ Not started | | | | |
| M5: Human-in-the-loop | ⏳ Not started | | | | |
| M6: Error handling + responses | ⏳ Not started | | | | |
| M7: API + CLI | ⏳ Not started | | | | |
| M8: Docs (core sign-off) | ⏳ Not started | | | | |
| M9: Hardening | ⏳ Not started | | | | |
| M10: Evaluation | ⏳ Not started | | | | |

## M0: Scaffold
- **Exit criteria (PRD §12):** `make lint test` passes on an empty test suite; seed loads.
- **Result:**
  - `make check` passes.
  - The suite has 15 scaffold tests rather than zero: config defaults, `.env.example` parsing, `APP_TODAY`, secret masking in repr, PII masking, masked JSON log output, seed completeness against PRD §8.4.3, and idempotent seed.
  - `make seed` loads 10 policies and 1 claim into `data/app.db`, and running it a second time is a no-op.
