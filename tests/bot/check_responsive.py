"""The bot must react when a run settles, not on its next timer tick."""
import asyncio

from harness import FakeRuns, World, check, memory, request, run_all, section, t


def answered_world(**kw):
    """A world with one request waiting, whose runs.create gives run R1."""
    w = World(run_result={"run_id": "R1"}, **kw)
    w.bot_data["pending"]["T1"] = request()
    return w


@section("1. The timer interval is the fallback, not the main path")
def _():
    check("poll interval is short", t.POLL_SECONDS <= 5, f"{t.POLL_SECONDS}s")


@section("2. Answering a request follows its run")
def _():
    w = answered_world(default_status="idle")
    w.press("accept|T1")
    w.run(asyncio.sleep(0.05))
    check("runs.join was called", w.joined == [("T1", "R1")], str(w.joined))


@section("3. Two polls never overlap, and the second is not dropped")
def _():
    w = answered_world(default_status="idle")
    rounds, active = [], [0, 0]  # rounds: the guard at each start; active: now, most at once

    async def slow(context):
        active[0] += 1
        active[1] = max(active[1], active[0])
        rounds.append(context.bot_data["polling"])
        await asyncio.sleep(0.05)
        active[0] -= 1

    original, t.poll_interrupts = t.poll_interrupts, slow
    try:
        async def both():
            await asyncio.gather(t.poll(w.context), t.poll(w.context))
        w.run(both())
    finally:
        t.poll_interrupts = original
    check("never two polls at once", active[1] == 1, f"{active[1]} at once")
    # It used to be dropped, so an answer's result sometimes waited a whole tick.
    check("the second one ran straight after the first", len(rounds) == 2, f"{len(rounds)} ran")
    check("guard was released", w.bot_data["polling"] is False)
    check("no poll left owed", w.bot_data["poll_again"] is False)


class BrokenJoin(FakeRuns):
    async def join(self, thread_id, run_id):
        raise RuntimeError("server went away")


@section("4. A broken join must not stop the fallback poll")
def _():
    w = answered_world(default_status="idle")
    w.client.runs = BrokenJoin(w)
    w.run(t.follow_run(w.context, "T1", "R1"))
    check("survived a failed join", True)
    check("guard not left stuck", w.bot_data["polling"] is False)


@section("5. Memory learned at the FIRST answer must survive to the outcome")
def _():
    # busy: the run is not finished, so nothing is reported yet
    w = answered_world(store_items=[memory("triage_preferences", "v1")])
    w.run(t.resume(w.context, "T1", {"type": "response", "args": "be shorter"}))
    first = dict(w.bot_data["cycle_store"]["T1"])
    check("first answer took a memory reading", "email_assistant/triage_preferences/user_preferences" in first)
    w.store_items[0]["value"] = "v2 LEARNED"  # the Respond taught it
    w.bot_data["pending"]["T1"] = request()
    w.run(t.resume(w.context, "T1", {"type": "accept", "args": None}))
    check("second answer kept the FIRST reading", w.bot_data["cycle_store"]["T1"] == first)
    check("so the outcome will still see the change", w.bot_data["watching"]["T1"]["store"] == first
          and first["email_assistant/triage_preferences/user_preferences"] == "v1")


run_all()
