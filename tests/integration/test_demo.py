"""M8: the README's `make demo` keeps working (all three requests + approve + reject)."""

from __future__ import annotations

import os
import subprocess
import sys

from app.config import PROJECT_ROOT


def test_demo_script_runs_end_to_end() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("APP_DB", "CHECKPOINT_DB"))}
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "demo.py")],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    out = result.stdout
    assert "Your policy POL-100001 for the 2022 Toyota Camry is ACTIVE." in out
    assert "Your claim CLM-2026-000002 has been filed." in out
    assert "Estimated refund: $175.55" in out
    assert "is still pending review" in out
    assert "Confirmation: CNL-2026-000001." in out
    assert "Your claim was reviewed and not approved" in out
    assert "Traceback" not in out + result.stderr
