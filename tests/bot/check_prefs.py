"""/prefs: see a preference profile and change it in plain words.

Two halves. First the `preferences` graph in-process, with an InMemoryStore
and the model call faked. Then the bot's handlers against a fake Telegram and
a fake `runs.wait`. No server, no model. Each section starts from a fresh
store or world.
"""
from contextlib import contextmanager

from langgraph.store.memory import InMemoryStore

import email_assistant.preferences_graph as pg
from email_assistant.email_assistant_hitl_memory_gmail import get_memory
from harness import World, callbacks, check, run_all, section, r, t

NS = ("email_assistant", "cal_preferences")


# ================================================================ the graph

def prefs_graph():
    """A fresh store, and the preferences graph compiled on it."""
    store = InMemoryStore()
    return store, pg.builder.compile(store=store)


@contextmanager
def fake_model(seen=None):
    """The model's rewrite, faked for this section only: it adds one line."""
    def rewrite(namespace, current, instruction):
        if seen is not None:
            seen.update(namespace=namespace, current=current, instruction=instruction)
        return current.rstrip() + "\nScoping calls are 30 minutes.\n"
    original, pg.rewrite = pg.rewrite, rewrite
    try:
        yield
    finally:
        pg.rewrite = original


def propose(graph):
    return graph.invoke({"namespace": "cal_preferences", "mode": "propose",
                         "instruction": "scoping calls are 30 minutes"})


def apply(graph, proposal):
    return graph.invoke({"namespace": "cal_preferences", "mode": "apply",
                         "text": proposal["proposed"], "based_on": proposal["current"]})


@section("1. Graph: show falls back to the default when nothing is stored")
def _():
    store, graph = prefs_graph()
    out = graph.invoke({"namespace": "cal_preferences", "mode": "show"})
    check("returns the default text", out.get("current") == pg.default_cal_preferences)
    check("stores nothing", store.get(NS, "user_preferences") is None)


@section("2. Graph: propose calls the model and writes nothing")
def _():
    store, graph = prefs_graph()
    seen = {}
    with fake_model(seen):
        out = propose(graph)
    check("the model saw the current text and the instruction",
          seen.get("current") == pg.default_cal_preferences and seen.get("instruction") == "scoping calls are 30 minutes")
    check("proposed text returned", "Scoping calls are 30 minutes." in (out.get("proposed") or ""))
    check("still stores nothing", store.get(NS, "user_preferences") is None)


@section("3. Graph: apply writes a plain STRING, which the email graph reads back")
def _():
    store, graph = prefs_graph()
    with fake_model():
        out = apply(graph, propose(graph))
    check("saved", out.get("saved") is True, str(out.get("error")))
    item = store.get(NS, "user_preferences")
    check("the store holds a str, like the email graph writes", isinstance(item.value, str), type(item.value).__name__)
    check("the email graph's get_memory reads the new text",
          "Scoping calls are 30 minutes." in get_memory(store, NS, pg.default_cal_preferences))


@section("4. Graph: apply refuses when the profile changed meanwhile")
def _():
    store, graph = prefs_graph()
    with fake_model():
        apply(graph, propose(graph))
    before = store.get(NS, "user_preferences").value
    out = graph.invoke({"namespace": "cal_preferences", "mode": "apply", "text": "OVERWRITE", "based_on": "something older"})
    check("not saved", out.get("saved") is False and out.get("error") == "changed meanwhile", str(out))
    check("store unchanged", store.get(NS, "user_preferences").value == before)


@section("5. Graph: bad input is an error, not a crash")
def _():
    _store, graph = prefs_graph()
    check("unknown namespace", "unknown namespace" in graph.invoke({"namespace": "secrets", "mode": "show"}).get("error", ""))
    check("empty instruction", "empty" in graph.invoke({"namespace": "cal_preferences", "mode": "propose",
                                                        "instruction": "  "}).get("error", ""))
    check("unknown mode", "unknown mode" in graph.invoke({"namespace": "cal_preferences", "mode": "delete"}).get("error", ""))


# ================================================================= the bot

ORIGINAL = "30 minute meetings.\nAvoid Fridays."


class PrefsRuns:
    """Stands in for the preferences graph behind runs.wait."""
    def __init__(self):
        self.calls, self.fail = [], False
        self.profiles = {"cal_preferences": ORIGINAL}

    async def wait(self, thread_id, assistant, input=None, **kw):
        self.calls.append((thread_id, assistant, input))
        if self.fail:
            raise RuntimeError("server down")
        ns, mode = input["namespace"], input["mode"]
        current = self.profiles[ns]
        if mode == "show":
            return {"current": current}
        if mode == "propose":
            return {"current": current, "proposed": current + "\nScoping calls are 30 minutes."}
        if input["based_on"] != current:
            return {"saved": False, "error": "changed meanwhile", "current": current}
        self.profiles[ns] = input["text"]
        return {"saved": True}


def prefs_world():
    w = World()
    w.client.runs = PrefsRuns()
    return w


def proposed():
    """Change -> your instruction -> the proposal is shown, waiting for Apply."""
    w = prefs_world()
    w.press("prefs|cal|change")
    w.clear()
    w.type("scoping calls are 30 minutes")
    return w


@section("6. /prefs shows a menu of the three profiles")
def _():
    w = prefs_world()
    w.command(t.cmd_prefs, "/prefs")
    data = callbacks(w.sent[-1][1])
    check("three buttons", data == ["prefs|cal|show", "prefs|reply|show", "prefs|triage|show"], str(data))
    check("every button fits Telegram's 64 bytes", all(len(d.encode()) <= 64 for d in data))


@section("7. Show: the current text comes from the graph, with Change and Back")
def _():
    w = prefs_world()
    w.press("prefs|cal|show")
    call = w.client.runs.calls[-1]
    check("asked the graph, with no thread", call[0] is None and call[1] == t.PREFS_ASSISTANT_ID
          and call[2] == {"namespace": "cal_preferences", "mode": "show"}, str(call))
    check("shows the text", "Avoid Fridays." in w.texts()[-1])
    check("Change and Back buttons", callbacks(w.sent[-1][1]) == ["prefs|cal|change", "prefs|-|menu"])


@section("8. Change: asks for an instruction and waits for the text")
def _():
    w = prefs_world()
    w.press("prefs|cal|change")
    check("asks in plain words", "Tell me what to change" in w.texts()[-1])
    job = next(iter(w.bot_data["awaiting"].values()), None)
    check("waiting for text, keyed to the profile", job == {"thread_id": "cal", "mode": "prefs", "field": None}, str(job))


@section("9. The text goes to propose; the diff is shown; NOTHING is saved yet")
def _():
    w = proposed()
    runs = w.client.runs
    call = runs.calls[-1]
    check("propose called with the instruction", len(runs.calls) == 1 and call[2]["mode"] == "propose"
          and call[2]["instruction"] == "scoping calls are 30 minutes", str(call))
    shown = w.texts()[-1]
    check("ONE message: the whole text with the change marked in place",
          "30 minute meetings." in shown and "<b>+ Scoping calls are 30 minutes.</b>" in shown, shown[:200])
    check("unchanged lines are plain, not marked", "− 30 minute meetings." not in shown and "<b>+ 30 minute" not in shown)
    check("legend explains the marks", "Struck through = removed" in shown)
    check("Apply and Cancel on that message", callbacks(w.sent[-1][1]) == ["prefs|cal|apply", "prefs|cal|cancel"])
    check("no separate diff message", not any("Proposed change to" in s for s in w.texts()))
    preview = "\n".join(r.preference_preview("Keep me.\nDrop me.", "Keep me.\nAdd me."))
    check("a removed line is struck through", "<s>− Drop me.</s>" in preview, preview)
    check("an added line is bold with a plus", "<b>+ Add me.</b>" in preview)
    check("a kept line is plain", preview.startswith("Keep me."))
    check("nothing saved", runs.profiles["cal_preferences"] == ORIGINAL)
    check("draft remembers what it was based on", w.bot_data["prefs_draft"]["cal"]["based_on"] == ORIGINAL)
    check("prompt consumed", w.bot_data["awaiting"] == {})


@section("10. Apply writes it and says so")
def _():
    w = proposed()
    w.clear()
    w.press("prefs|cal|apply")
    runs = w.client.runs
    call = runs.calls[-1][2]
    check("apply sent the text and what it was based on",
          call["mode"] == "apply" and call["based_on"] == ORIGINAL and "Scoping calls" in call["text"])
    check("the profile changed", "Scoping calls are 30 minutes." in runs.profiles["cal_preferences"])
    check("says Saved", any("Saved" in s for s in w.texts()))
    check("buttons removed", w.buttons_removed >= 1)
    check("draft cleared", "cal" not in w.bot_data["prefs_draft"])


@section("11. Apply after the profile changed meanwhile is refused")
def _():
    w = prefs_world()
    w.bot_data["prefs_draft"]["cal"] = {"based_on": "something older", "text": "OVERWRITE"}
    w.press("prefs|cal|apply")
    check("refused with a reason", any("changed while you were editing" in s for s in w.texts()))
    check("profile untouched", w.client.runs.profiles["cal_preferences"] == ORIGINAL)
    check("draft cleared", "cal" not in w.bot_data["prefs_draft"])


@section("12. Cancel drops the draft")
def _():
    w = prefs_world()
    w.bot_data["prefs_draft"]["cal"] = {"based_on": "x", "text": "y"}
    w.press("prefs|cal|cancel")
    check("says cancelled", any("Cancelled" in s for s in w.texts()))
    check("draft gone", "cal" not in w.bot_data["prefs_draft"])


@section("13. A stale Apply (no draft) is refused politely")
def _():
    w = prefs_world()
    w.press("prefs|cal|apply")
    check("says expired", any("expired" in s for s in w.texts()))


@section("14. Server down: propose re-arms the prompt, apply keeps the draft")
def _():
    w = prefs_world()
    w.press("prefs|cal|change")
    w.client.runs.fail = True
    w.clear()
    w.type("anything")
    check("told it could not reach the agent", any("Could not reach" in s for s in w.texts()))
    awaiting = w.bot_data["awaiting"]
    check("prompt re-armed", len(awaiting) == 1 and next(iter(awaiting.values()))["mode"] == "prefs")
    awaiting.clear()
    w.bot_data["prefs_draft"]["cal"] = {"based_on": "b", "text": "t"}
    w.clear()
    w.press("prefs|cal|apply")
    check("apply says try again", any("press Apply again" in s for s in w.texts()))
    check("draft kept", "cal" in w.bot_data["prefs_draft"])


@section("15. Back goes to the menu; a stranger gets nothing")
def _():
    w = prefs_world()
    w.press("prefs|-|menu")
    check("menu again", callbacks(w.sent[-1][1])[0] == "prefs|cal|show")
    w.clear()
    w.command(t.cmd_prefs, "/prefs", chat=1)
    w.press("prefs|cal|show", chat=1)
    check("stranger ignored", w.sent == [])


run_all()
