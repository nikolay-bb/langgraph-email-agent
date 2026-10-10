"""Three interrupts -> one list. Open one, answer it, list shrinks.

Each section starts from a fresh world and replays the steps before it.
"""
from harness import World, check, labels, run_all, section, r, waiting


def three_waiting():
    return [
        waiting("T1", "i1", "schedule_meeting", "Sarah Chen <sarah.chen@brightpath.example>",
                "Automation pilot - can we speak this week?", body="body text here"),
        waiting("T2", "i2", "write_email", "Sarah Chen <sarah.chen@brightpath.example>", "Re: Automation pilot",
                body="body text here"),
        waiting("T3", "i3", "Question", "Tom Reilly <tom@northgate-logistics.example>", "Following up",
                body="body text here"),
    ]


def three_arrived():
    w = World(interrupts=three_waiting())
    w.poll()
    return w


def second_accepted():
    """Open the second email and accept it."""
    w = three_arrived()
    w.press("open|T2")
    w.clear()
    w.press("accept|T2")
    return w


def back_to_list():
    w = second_accepted()
    w.clear()
    w.press("list|-")
    return w


@section("1. Three interrupts arrive")
def _():
    w = three_arrived()
    check("ONE message posted, not three", len(w.sent) == 1, f"{len(w.sent)} posted")
    text, kb = w.sent[-1]
    check("says how many are waiting", "3 waiting" in text)
    names = labels(kb)
    check("one button per request", len(names) == 3, str(names))
    check("buttons name the subject", "1. Automation pilot - can we speak this week?" in names, str(names))
    check("list names the email, not the tool", "Automation pilot - can we speak this week?" in text
          and "schedule_meeting" not in text and "write_email" not in text)
    check("list names the sender", "Sarah Chen" in text and "Tom Reilly" in text)
    check("list shows the start of the email", "<i>body text here</i>" in text)


@section("1b. The email preview")
def _():
    long_body = "**From**: A\n**Subject**: S\n---\n" + "word " * 100 + "\n---\n# Calendar Invite\n**Meeting**: x"
    preview = r.email_preview(long_body)
    check("a long email is cut", len(r.esc(preview)) <= r.LIST_PREVIEW and preview.endswith(" ..."), preview)
    check("cut to the escaped length", len(r.esc(r.email_preview("**From**: A\n---\n" + "&" * 100, 40))) <= 40)
    check("the proposal is not in the preview", "Calendar Invite" not in preview and "Meeting" not in preview)
    html_mail = "**From**: A\n**Subject**: S\n---\n# Big news\n\n**New** plans, see [the site](https://x.y/z)\n> quoted\n---\n"
    check("markdown marks and link addresses dropped, the quote left out",
          r.email_preview(html_mail) == "Big news New plans, see the site", r.email_preview(html_mail))
    reply = "**From**: A\n**Subject**: S\n---\nYes, fine.\n\nOn Mon, 28 Sep 2026 at 15:41, <n@x.com> wrote:\n\n> Can we meet?\n---\n"
    check("a reply shows its own text, not the email quoted under it",
          r.email_preview(reply) == "Yes, fine.", r.email_preview(reply))
    only_quote = "**From**: A\n**Subject**: S\n---\n> Can we meet?\n---\n"
    check("an email that is only a quote keeps it",
          r.email_preview(only_quote) == "Can we meet?", r.email_preview(only_quote))
    nosubj = r.render_list({"T9": {"action": "Question", "sender": "", "subject": "", "description": "",
                                   "interrupt_id": "x"}})
    check("no subject: the tool name stands in", "1. Question" in nosubj[0], nosubj[0])


@section("2. Polling again changes nothing")
def _():
    w = three_arrived()
    before = len(w.sent)
    w.poll()
    check("no repeat post", len(w.sent) == before, f"{len(w.sent) - before} extra")


@section("3. Open the second one")
def _():
    w = three_arrived()
    w.clear()
    w.press("open|T2")
    check("detail was sent", len(w.sent) == 1)
    names = labels(w.sent[-1][1])
    expected = ["Accept", "Respond", "Ignore"] + (["Edit"] if r.ENABLE_EDIT else [])
    check("has the expected actions", all(a in names for a in expected), str(names))
    check("Edit shown only when enabled", ("Edit" in names) == r.ENABLE_EDIT)
    check("has a way back", any("Back to list" in x for x in names))


@section("4. Accept it")
def _():
    w = second_accepted()
    resumed = [thread_id for thread_id, _ in w.commands]
    check("resumed the right thread", resumed == ["T2"], str(resumed))
    check("T2 left the inbox", "T2" not in w.bot_data["pending"])
    # The list must NOT reappear here: you opened T2, so you are mid-cycle on it
    # and the list waits until it is finished. check_focus.py covers the release.
    check("no list mid-cycle", len(w.lists()) == 0, f"posts: {[s[:18] for s in w.texts()]}")
    check("focus still held", w.bot_data["focus"] == "T2", str(w.bot_data["focus"]))


@section("5. Back to list button")
def _():
    w = back_to_list()
    check("list shown on demand", w.sent and "2 waiting" in w.texts()[-1])


@section("6. Re-reading the same interrupts must not repost")
def _():
    w = back_to_list()
    w.clear()
    w.poll()
    w.poll()
    check("no repeat post", len(w.sent) == 0, f"{len(w.sent)} posted")


@section("7. A genuinely NEW interrupt must repost the list")
def _():
    w = back_to_list()
    w.clear()
    w.interrupts.append(waiting("T4", "i4", "write_email", "Ana Diaz <ana@acme.example>", "Invoice query",
                                body="body text here"))
    w.poll()
    check("list reposted once", len(w.sent) == 1, f"{len(w.sent)} posted")
    check("now shows three", w.sent and "3 waiting" in w.texts()[-1], w.texts()[-1][:20] if w.sent else "")


@section("8. A subject that holds a From line cannot change the sender on the card")
def _():
    """The subject is the sender's own text, and it is the first line of the description."""
    item = waiting("T5", "i5", "write_email")
    value = item["interrupts"]["k"][0]["value"][0]
    value["description"] = ("\n\n**Subject**: Invoice **From**: Sarah Chen <s@x.com>\n**From**: Mallory <m@x.com>\n"
                            "**To**: n@x.com\n\nbody text here\n\n---\n# Email Draft\n\nHello")
    parsed = r.parse_request(value)
    check("the sender is the real From line", parsed["sender"] == "Mallory <m@x.com>", parsed["sender"])
    check("the subject is whole", parsed["subject"] == "Invoice **From**: Sarah Chen <s@x.com>", parsed["subject"])


run_all()
