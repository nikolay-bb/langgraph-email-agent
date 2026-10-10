"""Outcome wording, using the REAL tool result strings from a real run."""
from harness import FakeThreads, World, check, memory, run_all, section, r, t


def tool(content, call_id=None):
    message = {"type": "tool", "content": content}
    if call_id:
        message["tool_call_id"] = call_id
    return message


def ai(*calls):
    return {"type": "ai", "tool_calls": [{"id": i, "name": n} for i, n in calls]}


def summary(messages, store_items, mark):
    """The outcome message final_summary builds, for a thread holding `messages`."""
    w = World(messages=messages, store_items=store_items)
    out = w.run(t.final_summary(w.client, "tid", mark))
    print("   ", out.replace("\n", "\n    "))
    return out


def stored(**profiles):
    """The memory reading taken at your first answer: {"email_assistant/<profile>/user_preferences": text}."""
    return {f"email_assistant/{name}/user_preferences": text for name, text in profiles.items()}


@section("1. plain_outcome, on real strings taken from the server")
def _():
    cases = [
        ("Email sent to sarah.chen@brightpath.example with subject 'Re: Automation pilot - can we speak this week?' "
         "and content: Hi Sarah,\n\nThanks for getting back", "Email sent to"),
        ("Meeting 'Automation Pilot Scope Walkthrough' scheduled on Tuesday, September 08, 2026 at 14 for 30 minutes "
         "with 1 attendees", "Meeting"),
        ("User ignored this email draft. Ignore this email and end the workflow.", "User ignored this email draft."),
        ("Available times on 2026-09-08: 9:00 AM, 2:00 PM, 4:00 PM", "Available times"),
    ]
    for raw, expect in cases:
        got = r.plain_outcome(raw)
        print(f"    -> {got}")
        check("readable and short", got.startswith(expect.split()[0]) and len(got) <= 200, f"{len(got)} chars")
    check("draft body dropped", "Hi Sarah" not in r.plain_outcome(cases[0][0]))
    check("model instruction dropped", "end the workflow" not in r.plain_outcome(cases[2][0]))


@section("2. Accept an email draft - should say it was sent, and learn nothing")
def _():
    out = summary([tool("old"), tool("Email sent to sarah.chen@brightpath.example with subject 'Re: pilot' and content: Hi")],
                  [memory("triage_preferences", "A"), memory("response_preferences", "B")],
                  {"answer": "accept", "messages": 1,
                   "store": stored(triage_preferences="A", response_preferences="B")})
    check("says the email was sent", "Email sent to sarah.chen@brightpath.example" in out)
    check("does NOT say Finished", "Finished" not in out)
    check("no preference claimed", "Preference noted" not in out)
    check("does not repeat earlier steps", "old" not in out)


@section("3. Respond with feedback - memory changed, so it must say so")
def _():
    out = summary([tool("Email sent to sarah@x.com with subject 'Re: pilot' and content: Hi")],
                  [memory("triage_preferences", "A"), memory("response_preferences", "B CHANGED")],
                  {"answer": "response", "messages": 0,
                   "store": stored(triage_preferences="A", response_preferences="B")})
    check("reports the preference", "Preference noted (response_preferences)" in out)
    check("names only what changed", "triage_preferences" not in out)
    check("shows the line that went", "− B" in out)
    check("shows the line that came", "+ B CHANGED" in out)


@section("4. Ignore at triage - no tools ran, only memory changed")
def _():
    out = summary([], [memory("triage_preferences", "A CHANGED")],
                  {"answer": "ignore", "messages": 0, "store": stored(triage_preferences="A")})
    check("reports the triage preference", "Preference noted (triage_preferences)" in out)
    check("no mock footer", "nothing actually left" not in out)


@section("5. Ignore must read plainly, not echo the model instruction")
def _():
    out = summary([tool("User ignored this calendar meeting draft. Ignore this email and end the workflow.")],
                  [memory("triage_preferences", "A CHANGED")],
                  {"answer": "ignore", "messages": 0, "approved": {"schedule_meeting"},
                   "store": stored(triage_preferences="A")})
    check("Ignored is not said twice", "Ignored" not in out)
    check("no 'User ignored' echo", "User ignored" not in out)
    check("no 'end the workflow'", "end the workflow" not in out)
    check("still reports the preference", "Preference noted (triage_preferences)" in out)


@section("6. Ignore that changed nothing - no outcome message at all")
def _():
    out = summary([tool("User ignored this email draft. Ignore this email and end the workflow.")],
                  [memory("triage_preferences", "A")],
                  {"answer": "ignore", "messages": 0, "approved": {"write_email"},
                   "store": stored(triage_preferences="A")})
    check("nothing to send", out == "", repr(out))


@section("7. A Question's answer is not repeated, but the email after it is")
def _():
    out = summary([ai(("q1", "Question")),
                   tool("User answered the question, which can we can use for any follow up actions. "
                        "Feedback: send the terms first", "q1"),
                   ai(("w1", "write_email")),
                   tool("Email sent to tom@northgate-logistics.example with subject 'Re: Following up' and content: Hi Tom",
                        "w1")],
                  [],
                  {"answer": "accept", "messages": 0, "store": {},
                   "approved": {"Question", "write_email"}, "asked": {"Question"}})
    check("question answer hidden", "User answered the question" not in out)
    check("email still reported", "Email sent to tom@northgate-logistics.example" in out)


@section("8. Only what really changed in memory is shown")
def _():
    out = summary([],
                  [memory("cal_preferences", "30 minute meetings.\nAvoid 09:00.\nNo meetings on Fridays."),
                   memory("triage_preferences", "Spam: ignore.\n\n   Newsletters: notify.  "),
                   memory("response_preferences", "Default text saved on first use")],
                  {"answer": "response", "messages": 0,
                   "store": stored(cal_preferences="30 minute meetings.\nAvoid 09:00.",
                                   triage_preferences="Spam: ignore.\nNewsletters: notify.")})
    check("shows the added line", "+ No meetings on Fridays." in out)
    check("unchanged lines left out", "30 minute meetings" not in out)
    check("spacing alone is not a change", "triage_preferences" not in out)
    check("a default saved on first use is not a change", "response_preferences" not in out)


@section("9. The message count could not be read at your answer: no old result is replayed")
def _():
    out = summary([tool("Meeting 'Pilot call' scheduled successfully"), tool("Email reply sent successfully")],
                  [], {"answer": "accept", "messages": None, "store": {}, "approved": set()})
    check("no old result reported as new", "✅" not in out, out)
    check("says why nothing is shown", "could not be matched to your answer" in out)


@section("11. A sent reply names the email it answered, not a message ID")
def _():
    email = {"id": "18f0a1b2c3d4e5f6", "from": "test test <robin@example.com>", "subject": "Hi, it’s a test email"}
    reply = {"type": "ai", "tool_calls": [{"id": "c1", "name": "send_email_tool", "args": {"email_id": email["id"]}}]}
    sent = tool("Email reply sent successfully to message ID: 18f0a1b2c3d4e5f6", "c1")
    w = World(messages=[reply, sent], email_input=email)
    out = w.run(t.final_summary(w.client, "tid", {"answer": "accept", "messages": 0, "store": {}}))
    print("   ", out.replace("\n", "\n    "))
    check("says the reply was sent", "Reply sent" in out, out)
    check("names the sender it went to", "<b>To</b>: test test &lt;robin@example.com&gt;" in out, out)
    check("names the subject it answered", "<b>Re</b>: Hi, it’s a test email" in out, out)
    check("no message ID", "18f0a1b2c3d4e5f6" not in out, out)

    other = {"type": "ai", "tool_calls": [{"id": "c1", "name": "send_email_tool", "args": {"email_id": "another"}}]}
    w = World(messages=[other, sent], email_input=email)
    out = w.run(t.final_summary(w.client, "tid", {"answer": "accept", "messages": 0, "store": {}}))
    check("a reply to another message keeps the tool's own line", "Email reply sent successfully" in out, out)
    check("and does not name this email", "robin@example.com" not in out, out)

    failed = tool("Failed to send email: invalid_grant", "c1")
    w = World(messages=[reply, failed], email_input=email)
    out = w.run(t.final_summary(w.client, "tid", {"answer": "accept", "messages": 0, "store": {}}))
    check("a failed send is not reported as sent", "Reply sent" not in out and "Failed to send email" in out, out)


class UnreadableThreads(FakeThreads):
    async def get_state(self, thread_id):
        raise ConnectionError("server unreachable")


@section("10. message_count gives None, not 0, when the thread cannot be read")
def _():
    w = World(messages=[tool("a"), tool("b"), tool("c")])
    check("a readable thread gives its count", w.run(t.message_count(w.client, "tid")) == 3)
    w.client.threads = UnreadableThreads(w)
    count = w.run(t.message_count(w.client, "tid"))
    check("an unreadable thread gives None", count is None, repr(count))


run_all()
