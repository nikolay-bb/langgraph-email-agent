"""Edit two fields, then send. Nothing must reach the graph until Send.

Each section starts from a fresh world and replays the steps before it.
"""
from harness import World, check, labels, request, run_all, section, r

ORIGINAL = {"attendees": ["sarah.chen@brightpath.example"], "subject": "Automation Pilot Scope Walkthrough",
            "duration_minutes": 45, "preferred_day": "2026-09-08", "start_time": 14}


def edit_opened():
    """Press Edit on a meeting request. Returns the world and the message the buttons are on."""
    w = World()
    w.bot_data["pending"]["T1"] = request(action="schedule_meeting", args=ORIGINAL)
    panel = w.message("the announcement")
    w.press("edit|T1", panel)
    return w, panel


def set_field(w, panel, name, value):
    """Press the field's button on the panel, then type its new value."""
    i = r.field_order(w.bot_data["draft"]["T1"]).index(name)
    w.press(f"field|T1|{i}", panel)
    w.type(value)


def panel_buttons(w):
    return labels(w.sent[-1][1])


@section("1. Press Edit")
def _():
    w, _panel = edit_opened()
    check("a draft was opened", "T1" in w.bot_data["draft"])
    check("panel lists the fields", w.sent and "duration_minutes" in w.texts()[-1])
    check("a Send button exists", any("Send" in x for x in panel_buttons(w)), str(panel_buttons(w)))
    check("nothing sent to the graph yet", not w.commands)


@section("2. Change preferred_day")
def _():
    w, panel = edit_opened()
    set_field(w, panel, "preferred_day", "2026-10-11")
    check("draft updated", w.bot_data["draft"]["T1"]["preferred_day"] == "2026-10-11")
    check("STILL nothing sent", not w.commands, "<- this was the bug")
    check("panel offered again", w.sent and "Send" in str(panel_buttons(w)))


@section("3. Change duration_minutes too")
def _():
    w, panel = edit_opened()
    set_field(w, panel, "preferred_day", "2026-10-11")
    set_field(w, panel, "duration_minutes", "30")
    check("second field also updated", w.bot_data["draft"]["T1"]["duration_minutes"] == 30)
    check("kept its integer type", isinstance(w.bot_data["draft"]["T1"]["duration_minutes"], int))
    check("still nothing sent", not w.commands)


@section("4. Press Send")
def _():
    w, panel = edit_opened()
    set_field(w, panel, "preferred_day", "2026-10-11")
    set_field(w, panel, "duration_minutes", "30")
    w.press("send|T1", panel)
    answers = w.answers()
    check("exactly one resume", len(answers) == 1, f"{len(answers)}")
    args = answers[0][1]["args"]["args"] if answers else {}
    check("both edits present", args.get("preferred_day") == "2026-10-11" and args.get("duration_minutes") == 30)
    check("untouched fields intact",
          args.get("attendees") == ORIGINAL["attendees"] and args.get("subject") == ORIGINAL["subject"])
    check("every original field echoed", set(args) == set(ORIGINAL), str(set(ORIGINAL) - set(args)))
    check("draft cleared", "T1" not in w.bot_data["draft"])


run_all()
