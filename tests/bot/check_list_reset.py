"""/list as the way out: it closes every prompt still waiting for your text.

Each section starts from a fresh world with two emails waiting.
"""
from harness import World, check, run_all, section, t, waiting

NO_EDIT = {"allow_accept": True, "allow_ignore": True, "allow_respond": True, "allow_edit": False}


def arrived():
    w = World(interrupts=[
        waiting("TA", "a1", "Question", "Tom Reilly <tom@x.com>", "Following up", allowed=NO_EDIT),
        waiting("TB", "b1", "schedule_meeting", "Sarah Chen <sarah@x.com>", "Automation pilot", allowed=NO_EDIT),
    ])
    w.poll()
    return w


def closed(w):
    return [s for s in w.texts() if s.startswith("Closed ")]


def hints(w):
    return [s for s in w.texts() if s.startswith("Nothing is waiting for your text")]


def lost_and_listed():
    """Respond on A, then get lost and send /list."""
    w = arrived()
    w.press("open|TA")
    w.press("respond|TA|a1")
    w.clear()
    w.command(t.cmd_list, "/list")
    return w


@section("1. /list with nothing open says nothing extra")
def _():
    w = arrived()
    w.clear()
    w.command(t.cmd_list, "/list")
    check("no 'Closed' message", len(closed(w)) == 0, str(closed(w)))
    check("a list was posted", len(w.lists()) == 1)


@section("2. Respond on A, then get lost and send /list")
def _():
    w = arrived()
    w.press("open|TA")
    w.press("respond|TA|a1")
    check("A's prompt is open", len(w.bot_data["awaiting"]) == 1)
    w.clear()
    w.command(t.cmd_list, "/list")
    check("says one request was closed",
          closed(w) == ["Closed 1 request that was waiting for your text. Nothing was sent."], str(closed(w)))
    check("no prompt left open", w.bot_data["awaiting"] == {} and w.bot_data["awaiting_last"] is None)
    check("focus released", w.bot_data["focus"] is None)
    texts = w.texts()
    check("list comes AFTER the note, so it is the last thing on screen",
          len(w.lists()) == 1 and bool(closed(w)) and texts.index(w.lists()[0]) > texts.index(closed(w)[0]))


@section("3. A stray message now answers nothing")
def _():
    w = lost_and_listed()
    w.clear()
    w.type("sounds good")
    check("nothing sent to the agent", w.commands == [], str(w.commands))
    check("you are told why", len(hints(w)) == 1, str(w.texts()))


@section("4. Two open prompts -> both closed, plural wording")
def _():
    w = arrived()
    w.press("open|TA")
    w.press("respond|TA|a1")
    w.press("open|TB")
    w.press("respond|TB|b1")
    check("two prompts open", len(w.bot_data["awaiting"]) == 2)
    w.clear()
    w.command(t.cmd_list, "/list")
    check("says two requests were closed",
          closed(w) == ["Closed 2 requests that were waiting for your text. Nothing was sent."], str(closed(w)))
    check("none left open", w.bot_data["awaiting"] == {})


@section("5. The normal path still works after the reset")
def _():
    w = arrived()
    w.press("open|TB")
    w.press("respond|TB|b1")
    w.command(t.cmd_list, "/list")
    w.bot_data["opened"].clear()  # re-open at once, without the double-tap pause
    w.press("open|TB")
    w.press("respond|TB|b1")
    w.clear()
    w.type("make it 30 minutes")
    check("feedback reached B, once", [thread_id for thread_id, _ in w.commands] == ["TB"], str(w.commands))
    check("feedback text passed through",
          w.answers() and w.answers()[0][1]["args"] == "make it 30 minutes", str(w.answers()))
    check("no hint this time", len(hints(w)) == 0)


@section("6. /prefs previews are not touched by /list")
def _():
    w = arrived()
    w.bot_data["prefs_draft"]["cal"] = {"based_on": "old", "text": "new"}
    w.command(t.cmd_list, "/list")
    check("preview draft kept", "cal" in w.bot_data["prefs_draft"])


run_all()
