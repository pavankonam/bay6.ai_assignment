"""Evaluation runner (M10, PRD §8.12 and §11.1).

Runs the 60 YAML conversations in `tests/eval/conversations/` end to end through
`SupportRuntime` (the same code path as the API), each in fresh temporary databases with
`APP_TODAY=2026-10-22`, and reports:

- intent accuracy (≥ 95%): `nlu.primary` per annotated turn,
- slot accuracy (≥ 95%), including date roles and years,
- end-to-end task success (≥ 90%): correct tools, write arguments and final outcome,
- unauthorized or unexpected writes (hard gate: 0),
- responses with IDs, dates or amounts not present in tool results or slots (hard gate: 0),
- responses with coverage/payment language (hard gate: 0),
- average turns per intent on happy paths (status ≤ 3, claim ≤ 7, cancellation ≤ 5),
- LLM calls and tokens.

Usage:
    make eval                                   # real Claude model (needs ANTHROPIC_API_KEY)
    python -m tests.eval.run_eval --provider fake   # dry run of the harness itself
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONVERSATIONS = Path(__file__).resolve().parent / "conversations"
APP_TODAY = "2026-10-22"
THRESHOLDS = {"intent_accuracy": 0.95, "slot_accuracy": 0.95, "task_success": 0.90}
MAX_AVG_TURNS = {"policy_status": 3, "report_claim": 7, "cancel_policy": 5}
WRITE_TOOLS = {"create_claim", "cancel_policy"}


@dataclass
class ConversationResult:
    id: str
    intent: str
    category: str
    expected_outcome: str | None
    outcome: str | None = None
    success: bool = False
    failures: list[str] = field(default_factory=list)
    intent_checks: int = 0
    intent_correct: int = 0
    slot_checks: int = 0
    slot_correct: int = 0
    unauthorized_writes: int = 0
    hallucinations: list[str] = field(default_factory=list)
    coverage_violations: list[str] = field(default_factory=list)
    user_turns: int = 0
    llm_calls: int = 0
    tokens_in: float = 0.0
    tokens_out: float = 0.0
    seconds: float = 0.0
    transcript: list[str] = field(default_factory=list)


def load_conversations(only: Sequence[str] = ()) -> list[dict[str, Any]]:
    conversations = []
    for path in sorted(CONVERSATIONS.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        if not only or data["id"] in only:
            conversations.append(data)
    return conversations


def _norm(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return round(float(value), 2)
    if isinstance(value, str):
        text = " ".join(value.split()).lower()
        try:
            return round(float(text), 2)
        except ValueError:
            return text
    return value


def _matches(expected: Any, actual: Any) -> bool:
    return bool(_norm(expected) == _norm(actual))


def runtime_config(thread_id: str) -> Any:
    return {"configurable": {"thread_id": thread_id}}


def _setup_environment(provider: str, workdir: Path) -> None:
    os.environ["LLM_PROVIDER"] = provider
    os.environ["APP_TODAY"] = APP_TODAY
    os.environ.setdefault("TOOL_RETRY_BASE_SECONDS", "0.1")
    if provider == "fake":
        os.environ["SUPPORT_NO_DOTENV"] = "1"
    os.environ["CHECKPOINT_DB_PATH"] = str(workdir / "unused-checkpoints.db")


def run_conversation(conv: Mapping[str, Any], workdir: Path) -> ConversationResult:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langgraph.checkpoint.memory import InMemorySaver

    from app.concurrency import ThreadLocks
    from app.config import get_settings
    from app.db import dispose_engines
    from app.graph.builder import build_graph
    from app.mock_api.faults import reset_fault_injector
    from app.mock_api.service import set_services
    from app.observability.metrics import metrics
    from app.responses.guard import coverage_language, facts_supported
    from app.runtime import SupportRuntime
    from app.seed import load_seed

    expect = conv.get("expect") or {}
    result = ConversationResult(conv["id"], conv["intent"], conv["category"], expect.get("outcome"))
    os.environ["APP_DB_PATH"] = str(workdir / conv["id"] / "app.db")
    get_settings.cache_clear()
    dispose_engines()
    reset_fault_injector()
    set_services(None)
    metrics.reset()
    load_seed()
    runtime = SupportRuntime(build_graph(InMemorySaver()), locks=ThreadLocks())
    thread_id = runtime.create_thread()
    started = time.perf_counter()
    observed_slots: dict[str, Any] = {}
    outcomes: list[str] = []
    approval_reasons: list[str] = []
    agent_texts: list[str] = []
    try:
        for item in conv["turns"]:
            if "user" in item:
                result.user_turns += 1
                text = str(item["user"])
                result.transcript.append(f"USER: {text}")
                reply = runtime.post_message(thread_id, text)
                agent_texts.extend(reply.messages)
                result.transcript.extend(f"AGENT: {m}" for m in reply.messages)
                state = runtime.snapshot(thread_id).values
                if "expect_intent" in item:
                    result.intent_checks += 1
                    human = next(
                        m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)
                    )
                    actual = (human.additional_kwargs.get("nlu") or {}).get("primary")
                    expected = item["expect_intent"]
                    active = state.get("active_intent")
                    ok = actual == expected or (expected == "provide_info" and actual == active)
                    result.intent_correct += int(ok)
                    if not ok:
                        result.failures.append(
                            f"intent '{text}': expected {expected}, got {actual}"
                        )
            else:
                review = item["review"]
                state = runtime.snapshot(thread_id).values
                approval_id = (state.get("pending_action") or {}).get("approval_id")
                if state.get("status") != "awaiting_approval" or not approval_id:
                    result.failures.append("review step but no pending approval")
                    continue
                decision = runtime.decide(
                    str(approval_id), review["decision"], "eval-reviewer", review.get("comment")
                )
                agent_texts.extend(decision.messages)
                result.transcript.append(f"REVIEWER: {review['decision']}")
                result.transcript.extend(f"AGENT: {m}" for m in decision.messages)
                state = runtime.snapshot(thread_id).values
            for key, value in (state.get("slots") or {}).items():
                if value is not None:
                    observed_slots[key] = value
            action = state.get("pending_action") or {}
            for key, value in (action.get("payload") or {}).items():
                observed_slots.setdefault(key, value)
            if action.get("approval_reasons") and state.get("status") == "awaiting_approval":
                approval_reasons = list(action["approval_reasons"])
            if state.get("last_outcome"):
                outcomes.append(str(state["last_outcome"]))
    except Exception as exc:  # a crash is a failed conversation, not a crashed eval
        result.failures.append(f"exception: {type(exc).__name__}: {exc}")
    result.seconds = round(time.perf_counter() - started, 2)
    values = runtime.snapshot(thread_id).values
    messages = list(values.get("messages") or [])
    result.llm_calls = int(values.get("llm_calls") or 0)
    result.tokens_in = sum(
        v
        for k, v in metrics._values.get("llm_tokens_total", {}).items()
        if ("direction", "input") in k
    )
    result.tokens_out = sum(
        v
        for k, v in metrics._values.get("llm_tokens_total", {}).items()
        if ("direction", "output") in k
    )

    # --- tools and writes ----------------------------------------------------------------------
    tool_names = [str(m.name) for m in messages if isinstance(m, ToolMessage)]
    call_args = {
        c["id"]: c["args"] for m in messages if isinstance(m, AIMessage) for c in m.tool_calls
    }
    writes = [
        (str(m.name), call_args.get(m.tool_call_id, {}))
        for m in messages
        if isinstance(m, ToolMessage)
        and m.name in WRITE_TOOLS
        and isinstance(m.artifact, Mapping)
        and m.artifact.get("ok")
    ]
    expected_tools = list(expect.get("tools") or [])
    remaining = iter(tool_names)
    if not all(any(t == name for t in remaining) for name in expected_tools):
        result.failures.append(f"tools: expected subsequence {expected_tools}, got {tool_names}")
    for forbidden in expect.get("forbidden_tools") or []:
        if forbidden in tool_names:
            result.failures.append(f"forbidden tool called: {forbidden}")
    expected_write = expect.get("write")
    if expected_write is None:
        if writes:
            result.unauthorized_writes = len(writes)
            result.failures.append(f"unexpected write(s): {[w[0] for w in writes]}")
    elif len(writes) != 1 or writes[0][0] != expected_write["tool"]:
        result.failures.append(f"write: expected one {expected_write['tool']}, got {writes}")
    else:
        args = writes[0][1]
        for key, value in (expected_write.get("args") or {}).items():
            if not _matches(value, args.get(key)):
                result.failures.append(
                    f"write arg {key}: expected {value!r}, got {args.get(key)!r}"
                )
        if expected_write.get("allow_duplicate") and not args.get("allow_duplicate"):
            result.failures.append("write: expected allow_duplicate")

    # --- slots (incl. date roles/years) -------------------------------------------------------
    expected_slots = dict(expect.get("slots") or {})
    if expected_write:
        expected_slots.update(expected_write.get("args") or {})
    write_args = writes[0][1] if writes else {}
    for key, value in expected_slots.items():
        actual = (
            write_args.get(key, observed_slots.get(key)) if write_args else observed_slots.get(key)
        )
        result.slot_checks += 1
        if _matches(value, actual):
            result.slot_correct += 1
        else:
            result.failures.append(f"slot {key}: expected {value!r}, got {actual!r}")

    # --- outcome, text, approval ---------------------------------------------------------------
    from app.graph.state import TERMINAL_OUTCOMES

    terminal = [o for o in outcomes if o in TERMINAL_OUTCOMES]
    result.outcome = terminal[-1] if terminal else (outcomes[-1] if outcomes else None)
    if result.outcome != expect.get("outcome"):
        result.failures.append(f"outcome: expected {expect.get('outcome')}, got {result.outcome}")
    joined = "\n".join(agent_texts)
    for needle in expect.get("text_contains") or []:
        if needle not in joined:
            result.failures.append(f"missing text: {needle!r}")
    for needle in expect.get("text_absent") or []:
        if needle in joined:
            result.failures.append(f"forbidden text present: {needle!r}")
    if expect.get("approval_reasons") and approval_reasons != expect["approval_reasons"]:
        result.failures.append(
            f"approval_reasons: expected {expect['approval_reasons']}, got {approval_reasons}"
        )
    if "max_turns" in expect and result.user_turns > int(expect["max_turns"]):
        result.failures.append(f"turns: {result.user_turns} > {expect['max_turns']}")

    # --- grounding and coverage language on every agent message --------------------------------
    today = date.fromisoformat(APP_TODAY)
    # Everything the agent legitimately held at any checkpoint: tool results, internal helper
    # results (claim_checks), slots, payloads, quotes, approvals (FR5a resets clear them later).
    history = [
        {
            k: snap.values.get(k)
            for k in (
                "slots",
                "claim_checks",
                "pending_action",
                "quote",
                "policy_snapshot",
                "approval",
                "response_facts",
            )
        }
        for snap in runtime.graph.get_state_history(runtime_config(thread_id))
    ]
    facts: dict[str, Any] = {
        "tools": [m.artifact for m in messages if isinstance(m, ToolMessage)],
        "calls": list(call_args.values()),
        "history": history,
        "dates": [today.isoformat(), (today + timedelta(days=60)).isoformat()],
        "template_examples": ["POL-123456"],  # the format hint in the identity question
    }
    for text in agent_texts:
        if not facts_supported(text, facts):
            result.hallucinations.append(text)
        if coverage_language(text):
            result.coverage_violations.append(text)
    result.success = not result.failures
    return result


def summarize(results: Sequence[ConversationResult]) -> dict[str, Any]:
    def ratio(num: float, den: float) -> float:
        return round(num / den, 4) if den else 1.0

    summary: dict[str, Any] = {
        "conversations": len(results),
        "intent_accuracy": ratio(
            sum(r.intent_correct for r in results), sum(r.intent_checks for r in results)
        ),
        "slot_accuracy": ratio(
            sum(r.slot_correct for r in results), sum(r.slot_checks for r in results)
        ),
        "task_success": ratio(sum(r.success for r in results), len(results)),
        "unauthorized_writes": sum(r.unauthorized_writes for r in results),
        "ungrounded_responses": sum(len(r.hallucinations) for r in results),
        "coverage_violations": sum(len(r.coverage_violations) for r in results),
        "llm_calls": sum(r.llm_calls for r in results),
        "llm_calls_per_turn": ratio(
            sum(r.llm_calls for r in results), sum(r.user_turns for r in results)
        ),
        "tokens_in": sum(r.tokens_in for r in results),
        "tokens_out": sum(r.tokens_out for r in results),
        "avg_turns": {},
    }
    for intent in MAX_AVG_TURNS:
        happy = [r for r in results if r.intent == intent and r.category == "happy"]
        summary["avg_turns"][intent] = (
            round(sum(r.user_turns for r in happy) / len(happy), 2) if happy else 0.0
        )
    gates = {
        "intent_accuracy": summary["intent_accuracy"] >= THRESHOLDS["intent_accuracy"],
        "slot_accuracy": summary["slot_accuracy"] >= THRESHOLDS["slot_accuracy"],
        "task_success": summary["task_success"] >= THRESHOLDS["task_success"],
        "unauthorized_writes": summary["unauthorized_writes"] == 0,
        "ungrounded_responses": summary["ungrounded_responses"] == 0,
        "coverage_violations": summary["coverage_violations"] == 0,
        **{f"avg_turns_{k}": summary["avg_turns"][k] <= v for k, v in MAX_AVG_TURNS.items()},
    }
    summary["gates"] = gates
    summary["passed"] = all(gates.values())
    return summary


def render_markdown(
    summary: Mapping[str, Any], results: Sequence[ConversationResult], provider: str
) -> str:
    lines = [
        "# Evaluation report",
        "",
        f"- Provider: `{provider}` · conversations: {summary['conversations']} · "
        f"APP_TODAY: {APP_TODAY}",
        f"- Overall: **{'PASS' if summary['passed'] else 'FAIL'}**",
    ]
    if provider == "fake":
        lines.append(
            "- ⚠️ This run used the **fake** rule-based model. It validates the harness and the "
            "hard gates; the accuracy targets in PRD §11.1 apply to the real LLM (`make eval`)."
        )
    lines += ["", "| Metric | Value | Target | Pass |", "|---|---|---|---|"]
    targets = {
        "intent_accuracy": "≥ 95%",
        "slot_accuracy": "≥ 95%",
        "task_success": "≥ 90%",
        "unauthorized_writes": "0",
        "ungrounded_responses": "0",
        "coverage_violations": "0",
    }
    for key, target in targets.items():
        value = summary[key]
        shown = f"{value:.1%}" if isinstance(value, float) else str(value)
        lines.append(f"| {key} | {shown} | {target} | {'✅' if summary['gates'][key] else '❌'} |")
    for intent, limit in MAX_AVG_TURNS.items():
        lines.append(
            f"| avg turns ({intent}, happy) | {summary['avg_turns'][intent]} | ≤ {limit} | "
            f"{'✅' if summary['gates'][f'avg_turns_{intent}'] else '❌'} |"
        )
    lines += [
        f"| LLM calls (total / per turn) | {summary['llm_calls']} / "
        f"{summary['llm_calls_per_turn']} | — | — |",
        f"| Tokens (in / out) | {int(summary['tokens_in'])} / "
        f"{int(summary['tokens_out'])} | — | — |",
        "",
        "## Conversations",
        "",
        "| ID | Category | Expected outcome | Outcome | Result | Notes |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        notes = "; ".join(r.failures)[:300].replace("|", "\\|")
        lines.append(
            f"| {r.id} | {r.category} | {r.expected_outcome} | {r.outcome} | "
            f"{'✅' if r.success else '❌'} | {notes} |"
        )
    failed = [r for r in results if not r.success]
    if failed:
        lines += ["", "## Failed conversation transcripts", ""]
        for r in failed:
            lines += [f"### {r.id}", "", "```text", *r.transcript, "```", ""]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--provider", choices=["anthropic", "fake"], default="anthropic")
    parser.add_argument("--out", default=str(ROOT / "reports" / "eval.md"))
    parser.add_argument("--only", action="append", default=[])
    args = parser.parse_args(argv)
    workdir = Path(tempfile.mkdtemp(prefix="support-eval-"))
    _setup_environment(args.provider, workdir)
    sys.path.insert(0, str(ROOT))
    from app.config import get_settings
    from app.observability.logging import configure_logging

    get_settings.cache_clear()
    configure_logging("WARNING")
    if args.provider == "anthropic" and get_settings().ANTHROPIC_API_KEY is None:
        print(
            "ANTHROPIC_API_KEY is not set. `make eval` measures the real LLM; set the key in "
            ".env (or run with --provider fake to dry-run the harness).",
            file=sys.stderr,
        )
        return 2
    results = [run_conversation(c, workdir) for c in load_conversations(args.only)]
    summary = summarize(results)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(summary, results, args.provider))
    out.with_suffix(".json").write_text(
        json.dumps(
            {
                "provider": args.provider,
                "summary": summary,
                "results": [asdict(r) for r in results],
            },
            indent=2,
            default=str,
        )
    )
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "conversations",
                    "intent_accuracy",
                    "slot_accuracy",
                    "task_success",
                    "unauthorized_writes",
                    "ungrounded_responses",
                    "coverage_violations",
                    "avg_turns",
                    "llm_calls",
                    "passed",
                )
            },
            indent=2,
        )
    )
    print(f"Report: {out}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
