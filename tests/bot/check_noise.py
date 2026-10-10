"""Replay the exact message sequence from a screenshot of a real chat."""
from harness import World, check, run_all, section, t


def ai(*calls):
    return {"type": "ai", "tool_calls": [{"id": i, "name": n} for i, n in calls]}


def tool(i, c):
    return {"type": "tool", "tool_call_id": i, "content": c}


MESSAGES = [
    ai(("c1", "check_calendar_availability"), ("c2", "check_calendar_availability")),
    tool("c1", "Available times on 2026-09-08: 9:00 AM, 2:00 PM, 4:00 PM"),
    tool("c2", "Available times on 2026-09-09: 9:00 AM, 2:00 PM, 4:00 PM"),
    ai(("s1", "schedule_meeting")),
    tool("s1", "User gave feedback, which can we incorporate into the meeting request. Feedback: I want to have 30 mins meeting"),
    ai(("s2", "schedule_meeting")),
    tool("s2", "Meeting 'Automation Pilot Scope Walkthrough' scheduled on Tuesday, September 08, 2026 at 1400 for 30 minutes with 1 attendees"),
    ai(("w1", "write_email")),
    tool("w1", "Email sent to sarah.chen@brightpath.example with subject 'Re: Automation pilot - can we speak this week?' and content: Hi Sarah,\n\nGreat"),
]


@section("1. Only the approved tools are reported (BEFORE: 5 lines - 2 calendar lookups, a feedback echo, 2 real outcomes)")
def _():
    w = World(messages=MESSAGES)
    mark = {"answer": "accept", "messages": 0, "store": {}, "approved": {"schedule_meeting", "write_email"}}
    out = w.run(t.final_summary(w.client, "tid", mark))
    print("  AFTER:")
    for line in out.split("\n"):
        print("   ", line)
    check("calendar lookups gone", "Available times" not in out)
    check("feedback echo gone", "User gave feedback" not in out)
    check("meeting reported", "scheduled on Tuesday" in out)
    check("email reported", "Email sent to sarah.chen@brightpath.example" in out)
    check("exactly two outcome lines", out.count("✅") == 2)


@section("2. After a restart (nothing remembered) it must still show something")
def _():
    w = World(messages=MESSAGES)
    out = w.run(t.final_summary(w.client, "tid", {"messages": 0, "store": {}, "approved": set()}))
    check("falls back to showing all", out.count("✅") >= 2)


run_all()
