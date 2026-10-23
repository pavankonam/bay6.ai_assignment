"""Idempotent loader for `data/seed/*.json` into `app.db` (`make seed`)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db import session_scope
from app.mock_api.repository import Claim, Policy

_POLICY_DATE_FIELDS = (
    "dob",
    "term_start",
    "term_end",
    "paid_through",
    "cancellation_effective_date",
)


@dataclass(frozen=True)
class SeedReport:
    policies: int
    claims: int


def _parse_dates(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    parsed = dict(row)
    for name in fields:
        value = parsed.get(name)
        if isinstance(value, str):
            parsed[name] = date.fromisoformat(value)
    return parsed


def load_seed(seed_dir: Path | None = None, db_path: Path | None = None) -> SeedReport:
    """Upsert seed policies (reset to seed values) and seed claims. Running twice is a no-op."""
    directory = Path(seed_dir or get_settings().SEED_DIR)
    policies = json.loads((directory / "policies.json").read_text())
    claims = json.loads((directory / "claims.json").read_text())
    with session_scope(db_path) as session:
        for row in policies:
            session.merge(Policy(**_parse_dates(row, _POLICY_DATE_FIELDS)))
        for row in claims:
            session.merge(Claim(**_parse_dates(row, ("incident_date",))))
    return SeedReport(policies=len(policies), claims=len(claims))


def main() -> None:  # pragma: no cover - thin CLI entry point
    report = load_seed()
    print(
        f"Seeded {report.policies} policies and {report.claims} claims "
        f"into {get_settings().APP_DB_PATH}"
    )


if __name__ == "__main__":  # pragma: no cover
    main()
