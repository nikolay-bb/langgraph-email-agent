"""The agent's "today" is read for every email, in UK time, with the weekday and the time.

It used to be read once, when the server imported prompts.py, so a server left
running past midnight scheduled meetings from yesterday's date. On 25 Sep the
date alone was not enough: the agent booked 10:00 for an email that came in at
12:12, and took "this Saturday" to be Sunday 27 Sep.
"""
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from email_assistant.prompts import agent_system_prompt_hitl_memory
from email_assistant.utils import dates_for_prompt, next_days_for_prompt, today_for_prompt

GRAPH = Path("src/email_assistant/email_assistant_hitl_memory_gmail.py")


def test_weekday_date_and_time():
    # 12:00 UTC is 13:00 in London (BST).
    assert today_for_prompt(datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)) == "Monday 2026-09-21, 13:00"


def test_uk_date_not_server_date():
    # 23:30 UTC is 00:30 the next day in London (BST).
    assert today_for_prompt(datetime(2026, 9, 21, 23, 30, tzinfo=timezone.utc)) == "Tuesday 2026-09-22, 00:30"


def test_the_next_days_are_named():
    days = next_days_for_prompt(datetime(2026, 9, 25, 12, 12, tzinfo=ZoneInfo("Europe/London"))).split(", ")
    assert days[:2] == ["Saturday 2026-09-26", "Sunday 2026-09-27"]
    assert len(days) == 14 and days[-1] == "Friday 2026-10-09"


def test_both_gaps_come_from_one_clock_reading():
    # 23:59 UTC is 00:59 on the 22nd in London, for both gaps.
    dates = dates_for_prompt(datetime(2026, 9, 21, 23, 59, tzinfo=timezone.utc))
    assert dates["today"] == "Tuesday 2026-09-22, 00:59"
    assert dates["next_days"].startswith("Wednesday 2026-09-23, ")


def test_prompt_has_gaps_not_fixed_dates():
    assert "Today is {today} (UK time)" in agent_system_prompt_hitl_memory
    assert "The next 14 days are: {next_days}." in agent_system_prompt_hitl_memory
    filled = agent_system_prompt_hitl_memory.format(
        tools_prompt="", background="", response_preferences="", cal_preferences="",
        today="Friday 2026-09-25, 12:12", next_days="Saturday 2026-09-26",
    )
    assert "Today is Friday 2026-09-25, 12:12 (UK time)" in filled
    assert "The next 14 days are: Saturday 2026-09-26." in filled


def test_the_graph_fills_the_gaps():
    assert "**dates_for_prompt()" in GRAPH.read_text()
