"""M10: the evaluation set and runner (the real-LLM metrics are measured by `make eval`)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

from app.config import PROJECT_ROOT
from tests.eval.run_eval import ConversationResult, load_conversations, summarize


def test_conversation_set_shape() -> None:
    conversations = load_conversations()
    assert len(conversations) == 60
    assert len({c["id"] for c in conversations}) == 60
    assert Counter(c["intent"] for c in conversations) == {
        "policy_status": 20,
        "report_claim": 20,
        "cancel_policy": 20,
    }
    assert sum(c["category"] in {"edge", "adversarial"} for c in conversations) == 15
    for conv in conversations:
        assert conv["turns"]
        assert "outcome" in conv["expect"]
        for turn in conv["turns"]:
            assert set(turn) <= {"user", "expect_intent", "review"}


def test_summary_gates() -> None:
    ok = ConversationResult(
        "x",
        "policy_status",
        "happy",
        "STATUS_PROVIDED",
        success=True,
        intent_checks=2,
        intent_correct=2,
        slot_checks=2,
        slot_correct=2,
        user_turns=2,
    )
    assert summarize([ok])["passed"] is True
    bad = ConversationResult(
        "y", "report_claim", "happy", None, unauthorized_writes=1, user_turns=9
    )
    summary = summarize([ok, bad])
    assert summary["passed"] is False
    assert summary["gates"]["unauthorized_writes"] is False
    assert summary["gates"]["avg_turns_report_claim"] is False


def test_runner_without_key_refuses(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env.update(SUPPORT_NO_DOTENV="1")
    result = subprocess.run(
        [sys.executable, "-m", "tests.eval.run_eval", "--out", str(tmp_path / "e.md")],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 2
    assert "ANTHROPIC_API_KEY is not set" in result.stderr


def test_fake_dry_run_holds_hard_gates(tmp_path: Path) -> None:
    out = tmp_path / "eval.md"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("APP_DB", "CHECKPOINT_DB"))}
    result = subprocess.run(
        [sys.executable, "-m", "tests.eval.run_eval", "--provider", "fake", "--out", str(out)],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr[-2000:]
    report = json.loads(out.with_suffix(".json").read_text())
    summary = report["summary"]
    assert summary["conversations"] == 60
    # Hard gates hold even for the rule-based fake model.
    assert summary["unauthorized_writes"] == 0
    assert summary["ungrounded_responses"] == 0
    assert summary["coverage_violations"] == 0
    assert all(
        not r["failures"] or not r["failures"][0].startswith("exception") for r in report["results"]
    )
    assert "# Evaluation report" in out.read_text()
