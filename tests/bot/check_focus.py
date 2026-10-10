"""Open one email, work it to the end, and only THEN see the list.

The steps of one story. Each section starts from a fresh world and replays
the steps before it (the functions below), so a fault in one section cannot
leave a wrong state for the next.
"""
from harness import World, check, run_all, section, waiting


def email_a(interrupt_id, action):
    return waiting("TA", interrupt_id, action, "Tom Reilly <tom@x.com>", "Following up")


EMAIL_B = waiting("TB", "b1", "Email Assistant: notify", "Priya Raman <priya@x.com>", "Detailed requirements")


def two_arrived():
    w = World(interrupts=[email_a("a1", "Question"), EMAIL_B])
    w.poll()
    return w


def a_opened():
    w = two_arrived()
    w.clear()
    w.press("open|TA")
    return w


def a_answered():
    """You answer A's question; the graph then asks A's next question."""
    w = a_opened()
    w.clear()
    w.press("respond|TA")
    w.type("Yes, I'm good with that")
    w.interrupts = [email_a("a2", "write_email"), EMAIL_B]  # A moved on, B still waiting
    w.poll()
    return w


@section("1. Two emails arrive -> one list")
def _():
    w = two_arrived()
    check("a list was posted", len(w.lists()) == 1, f"{len(w.lists())}")


@section("2. Open A")
def _():
    w = a_opened()
    check("focus is on A", w.bot_data["focus"] == "TA")
    check("detail shown", len(w.details()) == 1)


@section("3. Respond to A - the graph then asks A's NEXT question")
def _():
    w = a_answered()
    check("NO list mid-cycle", len(w.lists()) == 0, f"{len(w.lists())} posted  <- this was the bug")
    check("A's next question shown straight away", len(w.details()) == 1)


@section("4. Accept it - A finishes")
def _():
    w = a_answered()
    w.clear()
    w.press("accept|TA")
    w.messages += [
        {"type": "ai", "tool_calls": [{"id": "x", "name": "write_email"}]},
        {"type": "tool", "tool_call_id": "x",
         "content": "Email sent to tom@northgate-logistics.example with subject 'Re: Following up'"}]
    w.interrupts = [EMAIL_B]
    w.status["TA"] = "idle"
    w.poll()
    check("outcome reported", any("Email sent to tom@" in s for s in w.texts()))
    check("focus released", w.bot_data["focus"] is None)
    check("list returns, showing B", len(w.lists()) == 1 and "1 waiting" in w.lists()[0])


@section("5. Back to list mid-cycle still works")
def _():
    w = two_arrived()
    w.press("open|TB")
    check("focused on B", w.bot_data["focus"] == "TB")
    w.press("list|-")
    check("focus released by Back", w.bot_data["focus"] is None)


run_all()
