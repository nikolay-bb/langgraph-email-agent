"""Long drafts, edit fields and button data, without Telegram or a running server.

Fixtures follow the graph's real shape (utils.format_gmail_markdown): the
email ends with a `---` line, and the tool section comes after it.
"""
from harness import check, run_all, section, r

WRITE_EMAIL_ARGS = {"to": "sarah@brightpath.example", "subject": "Re: pilot", "content": "Hi Sarah..."}


@section("1. A long draft must not pass Telegram's 4096 limit")
def _():
    long_body = ("This is a very long paragraph about the pilot scope. " * 60)
    long_draft = ("Dear Sarah,\n\n" + "I am writing at considerable length. " * 300 + "\n\nBest,\nAlex")
    desc = (
        f"**From**: Sarah Chen <sarah@brightpath.example>\n"
        f"**Subject**: Automation pilot\n"
        f"\n{long_body}\n\n---\n"
        f"# Email Draft\n\n**To**: sarah@brightpath.example\n**Subject**: Re: pilot\n\n{long_draft}"
    )
    msg = r.render_message({"action": "write_email", "args": {}, "allowed": {}, "description": desc})
    check("rendered message fits", len(msg) < r.TELEGRAM_LIMIT, f"len={len(msg)} limit={r.TELEGRAM_LIMIT}")
    check("draft is still visible", "Dear Sarah" in msg)
    check("trim is signposted", "trimmed" in msg)


@section("2. With no description, it falls back to the raw arguments, and they fit too")
def _():
    req = {"action": "write_email", "args": {f"field_{i}": "x" * 500 for i in range(20)},
           "allowed": {}, "description": ""}
    msg = r.render_message(req)
    check("args-only message fits", len(msg) < r.TELEGRAM_LIMIT, f"len={len(msg)}")
    check("says fields were dropped", "more fields not shown" in msg)


@section("3. Edit must reach every field, body first")
def _():
    order = r.field_order(WRITE_EMAIL_ARGS)
    check("body is the first button", order[0] == "content", f"order={order}")
    check("recipient is reachable", "to" in order)
    check("subject is reachable", "subject" in order)

    meeting_args = {"attendees": ["a@x.com", "b@x.com"], "subject": "Pilot", "duration_minutes": 45}
    check("meeting fields all reachable", set(r.field_order(meeting_args)) == set(meeting_args))
    check("list edit stays a list", r.coerce(["a@x.com"], "c@x.com, d@x.com")[0] == ["c@x.com", "d@x.com"])
    check("int edit stays an int", r.coerce(45, "30")[0] == 30)
    check("bad int is REJECTED, not silently kept", r.coerce(45, "half an hour")[1] is not None)
    check("14:30 into an hour field is rejected", r.coerce(14, "14:30")[1] is not None)


@section("4. The wholesale-replacement rule still holds")
def _():
    edited = dict(WRITE_EMAIL_ARGS)
    edited["content"] = r.coerce(edited["content"], "New body.")[0]
    check("every original field echoed back", set(edited) == set(WRITE_EMAIL_ARGS))
    check("only the chosen field changed",
          edited["to"] == WRITE_EMAIL_ARGS["to"] and edited["content"] == "New body.")


@section("5. Button callback data must fit Telegram's 64 bytes")
def _():
    tid = "11111111-2222-4333-8444-555555555555"
    for i, key in enumerate(r.field_order(WRITE_EMAIL_ARGS)):
        data = f"field|{tid}|{i}"
        check(f"callback_data for '{key}' fits", len(data.encode()) <= 64, f"{len(data.encode())} bytes")


@section("6. Sanity - a normal short email is untouched")
def _():
    small = "**From**: Tom <tom@x.com>\n**Subject**: Hi\n\nShort note.\n\n---\n# Email Draft\n\nHi Tom,\n\nYes.\n\nAlex"
    out = r.render_message({"action": "write_email", "args": {}, "allowed": {}, "description": small})
    check("no spurious trimming", "trimmed" not in out)
    check("bold survived as HTML", "<b>" in out and "**" not in out)
    e, pr, inv = out.find("Email"), out.find("Proposed:"), out.find("Email Draft")
    check("email comes FIRST, proposal below it", -1 < e < pr < inv, f"email@{e} proposed@{pr} draft@{inv}")


run_all()
