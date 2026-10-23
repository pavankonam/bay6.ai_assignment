"""M0: configuration, masking, logging and seed loading."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from sqlmodel import select

from app.config import PROJECT_ROOT, Settings, get_settings, today
from app.db import session_scope
from app.mock_api.repository import Claim, Policy
from app.observability import logging as app_logging
from app.observability.masking import (
    mask_dob,
    mask_name,
    mask_policy_id,
    mask_structure,
)
from app.seed import load_seed


def test_settings_defaults_match_env_example() -> None:
    settings = Settings(_env_file=None)
    assert settings.LLM_MODEL == "claude-sonnet-5-5"
    assert settings.MAX_TURNS == 30
    assert settings.CLAIM_APPROVAL_THRESHOLD_USD == 10000
    assert settings.APPROVAL_TTL_HOURS == 48
    assert settings.RECURSION_LIMIT == 50
    assert settings.SUPPORT_PHONE == "1-800-555-0199"
    assert "verify_policyholder" not in settings.mock_fault_tools
    assert settings.mock_fault_tools == {
        "get_policy_status",
        "get_cancellation_quote",
        "create_claim",
        "cancel_policy",
    }


def test_env_example_parses_with_inline_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("LLM_PROVIDER", "TOOL_SELECTION_MODE", "APP_TODAY", "TOOL_RETRY_BASE_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text((PROJECT_ROOT / ".env.example").read_text())
    settings = Settings(_env_file=env_file)
    assert settings.LLM_PROVIDER == "anthropic"
    assert settings.TOOL_SELECTION_MODE == "llm"
    assert settings.APP_TODAY is None
    assert settings.MOCK_FAULT_CLEAR_AFTER is None
    assert settings.ANTHROPIC_API_KEY is None
    assert settings.REVIEWER_TOKEN.get_secret_value() == "change-me"


def test_today_uses_app_today(set_env: Callable[..., None]) -> None:
    assert today() == date(2026, 10, 22)
    set_env(APP_TODAY="2027-01-02")
    assert today() == date(2027, 1, 2)


def test_today_defaults_to_utc_date(set_env: Callable[..., None]) -> None:
    set_env(APP_TODAY=None)
    assert get_settings().APP_TODAY is None
    assert isinstance(today(), date)


def test_secrets_are_not_rendered() -> None:
    settings = Settings(_env_file=None, ADMIN_TOKEN="super-secret")  # type: ignore[arg-type]
    assert "super-secret" not in repr(settings)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1985-04-12", "****-**-12"), (date(1990, 9, 30), "****-**-30"), ("bad", "****-**-**")],
)
def test_mask_dob(value: str | date, expected: str) -> None:
    assert mask_dob(value) == expected


def test_mask_dob_none() -> None:
    assert mask_dob(None) == ""


def test_mask_name_and_policy() -> None:
    assert mask_name("Jane Doe") == "J*** D**"
    assert mask_name("") == ""
    assert mask_policy_id("POL-100001") == "POL-***001"
    assert mask_policy_id("garbage") == "POL-******"
    assert mask_policy_id(None) == ""


def test_mask_structure_masks_nested_pii() -> None:
    masked = mask_structure(
        {
            "policy_id": "POL-100001",
            "dob": "1985-04-12",
            "holder_name": "Jane Doe",
            "admin_token": "abc",
            "nested": [{"note": "customer POL-100002 called"}, 3],
        }
    )
    assert masked == {
        "policy_id": "POL-***001",
        "dob": "****-**-12",
        "holder_name": "J*** D**",
        "admin_token": "***",
        "nested": [{"note": "customer POL-***002 called"}, 3],
    }


def test_log_output_is_json_and_masked() -> None:
    stream = io.StringIO()
    app_logging.configure_logging("INFO", stream=stream, force=True)
    app_logging.configure_logging("INFO")  # second call is a no-op
    try:
        app_logging.get_logger("test").info(
            "verify", policy_id="POL-100001", dob="1985-04-12", thread_id="t-1"
        )
    finally:
        app_logging.configure_logging("INFO", force=True)
    line = stream.getvalue().strip().splitlines()[-1]
    record = json.loads(line)
    assert record["event"] == "verify"
    assert record["level"] == "info"
    assert record["policy_id"] == "POL-***001"
    assert record["dob"] == "****-**-12"
    assert "1985-04-12" not in line


def test_seed_loads_all_rows_idempotently(seeded: None) -> None:
    report = load_seed()
    assert report.policies == 10
    assert report.claims == 1
    with session_scope() as session:
        policies = session.exec(select(Policy)).all()
        claims = session.exec(select(Claim)).all()
    assert len(policies) == 10
    assert len(claims) == 1
    jane = next(p for p in policies if p.policy_id == "POL-100001")
    assert jane.dob == date(1985, 4, 12)
    assert jane.annual_premium_usd == 1200
    assert {c["type"] for c in jane.coverages} == {"LIABILITY", "COLLISION", "COMPREHENSIVE"}
    assert claims[0].claim_id == "CLM-2026-000001"
    assert claims[0].incident_date == date(2026, 10, 15)


def test_seed_rows_include_every_policy_column() -> None:
    rows = json.loads((PROJECT_ROOT / "data" / "seed" / "policies.json").read_text())
    columns = set(Policy.model_fields)
    for row in rows:
        assert set(row) == columns, row["policy_id"]


def test_seed_matches_prd_table() -> None:
    rows = {
        r["policy_id"]: r
        for r in json.loads((PROJECT_ROOT / "data" / "seed" / "policies.json").read_text())
    }
    assert rows["POL-100002"]["status"] == "LAPSED"
    assert rows["POL-100002"]["paid_through"] == "2026-08-31"
    assert rows["POL-100003"]["cancellation_effective_date"] == "2026-06-30"
    assert rows["POL-100004"]["coverages"] == [{"type": "LIABILITY", "deductible": None}]
    assert rows["POL-100005"]["status"] == "PENDING_CANCELLATION"
    assert rows["POL-100005"]["cancellation_effective_date"] == "2026-11-15"
    assert {"POL-999500", "POL-999408", "POL-999501", "POL-999502"} <= set(rows)
