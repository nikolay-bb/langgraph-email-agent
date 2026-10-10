"""Run the Telegram bot's check suites under pytest.

Each file in tests/bot/ is a standalone script that drives the REAL handlers in
scripts/telegram_inbox.py against a stubbed Telegram and a stubbed LangGraph
server. They print one line per check and exit non-zero if any fails, so pytest
runs each as a subprocess and surfaces its output on failure.

Why subprocesses rather than plain test functions: the bot is a script, not a
package, and a suite may replace a module-level name of the bot for one of
its sections. Running them in one process would let that leak into the next
suite. The fakes and the report are shared, in tests/bot/harness.py.

    uv run --group bot --group dev pytest tests -q
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SUITES = sorted((Path(__file__).parent / "bot").glob("check_*.py"))
REPO_ROOT = Path(__file__).resolve().parent.parent
# Every suite by name. Add a new one here too.
EXPECTED = {"check_drafts_and_fields", "check_edges", "check_focus", "check_layout", "check_list",
            "check_list_reset", "check_multi_edit", "check_noise", "check_outcomes", "check_prefs",
            "check_reports_and_prompts", "check_responsive"}


@pytest.mark.parametrize("suite", SUITES, ids=lambda p: p.stem)
def test_suite(suite: Path):
    result = subprocess.run(
        [sys.executable, str(suite)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = result.stdout + result.stderr
    failures = [line.strip() for line in output.splitlines() if "FAIL" in line]
    assert result.returncode == 0, "\n".join(failures) or output[-2000:]


def test_every_suite_is_collected():
    """A suite that stops being picked up would pass by simply vanishing.

    It used to ask for at least 7 suites while 12 existed, so five could go
    missing unnoticed. Now each is named, and a missing one is named too.
    """
    missing = EXPECTED - {s.stem for s in SUITES}
    assert not missing, f"missing suites: {sorted(missing)}"
