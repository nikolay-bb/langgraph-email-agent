"""Outcome reports and two open prompts, driving the real handlers with a fake Telegram.

How the list replaces posting each request lives in check_list.py.
"""
from harness import World, check, request, run_all, section, t

SENT_RESULT = [{"type": "tool", "content": "Email sent to sarah@x.com with subject 'Re: pilot'"}]


def finished(thread_id, answer, status="idle"):
    """A world where you answered `thread_id` and its run has now ended."""
    w = World(default_status=status, messages=SENT_RESULT)
    w.bot_data["watching"] = {thread_id: {"answer": answer, "messages": 0, "store": {}, "approved": set()}}
    return w


@section("1. The outcome must be reported")
def _():
    w = finished("tid-1", "accept")
    w.run(t.poll_outcomes(w.context))
    check("an outcome message was sent", len(w.sent) == 1)
    check("it names the real outcome", w.sent and "Email sent to sarah@x.com" in w.texts()[0])
    check("it does NOT say Finished", w.sent and "Finished" not in w.texts()[0])
    check("no mock footer", w.sent and "nothing actually left" not in w.texts()[0])
    check("stops watching once reported", w.bot_data["watching"] == {})


@section("2. An ignore that changed nothing sends nothing more")
def _():
    w = finished("tid-2", "ignore")
    w.run(t.poll_outcomes(w.context))
    check("no second Ignored message", w.sent == [])
    check("stops watching the ignore too", w.bot_data["watching"] == {})


@section("3. A failed run is reported")
def _():
    w = finished("tid-9", "accept", status="error")
    w.run(t.poll_outcomes(w.context))
    check("failure is reported", w.sent and "failed" in w.texts()[0].lower())


@section("4. Two requests open at once must not collide")
def _():
    w = World()
    w.bot_data["pending"] = {"tid-A": request(args={"content": "A draft"}),
                             "tid-B": request(args={"content": "B draft"})}
    prompt_a, prompt_b = w.message("prompt A"), w.message("prompt B")
    w.run(t.remember_prompt(w.context, prompt_a, "tid-A", "respond"))
    w.run(t.remember_prompt(w.context, prompt_b, "tid-B", "respond"))
    check("both prompts remembered", len(w.bot_data["awaiting"]) == 2)
    check("a warning was shown", any("More than one request" in s for s in w.texts()))

    # Reply directly to prompt A - must reach A, not the most recent (B).
    w.clear()
    w.type("feedback for A", reply_to=prompt_a)
    answers = w.answers()
    check("reply routes to the request you meant", answers and answers[0][0] == "tid-A",
          f"went to {answers[0][0] if answers else 'nowhere'}")
    check("the other is still open", len(w.bot_data["awaiting"]) == 1)

    w.clear()
    w.type("feedback for B")
    check("plain text goes to the one left", w.answers() and w.answers()[0][0] == "tid-B")
    check("nothing left waiting", w.bot_data["awaiting"] == {})


run_all()
