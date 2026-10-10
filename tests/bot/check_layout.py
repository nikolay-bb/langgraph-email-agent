"""Email first, proposal below - for EVERY kind of request.

Descriptions are built with the graph's OWN formatters (email_assistant.utils),
not hand-written strings, so a change to how the graph formats a request is
caught here.
"""
import re

from harness import check, run_all, section, r
from email_assistant.utils import format_gmail_markdown, format_for_display, without_repeated_quote


def plain(html):
    return re.sub(r"</?(b|i|code|pre)>", "", html)


EMAIL = format_gmail_markdown(
    "Automation pilot", "Sarah Chen <s@x.com>", "Alex <n@x.com>",
    "Hi Alex,\n\nCould we find 45 minutes?\n\nBest,\nSarah")

CASES = {
    "Question": {"name": "Question", "args": {"content": "Which day suits you?"}},
    "send_email_tool": {"name": "send_email_tool",
                        "args": {"email_id": "abc", "response_text": "Hi", "email_address": "n@x.com"}},
    "schedule_meeting_tool": {"name": "schedule_meeting_tool",
                              "args": {"attendees": ["s@x.com"], "title": "Scope",
                                       "start_time": "2026-09-15T14:00:00",
                                       "end_time": "2026-09-15T14:30:00",
                                       "organizer_email": "n@x.com"}},
}


def newsletter():
    html = ("<html><body><h1>5 AI tools that shipped</h1><p>Your weekly roundup.</p>"
            "<hr><h2>Tool one</h2><p>Details here.</p></body></html>")
    return format_gmail_markdown("Roundup", "Weekly Roundup <t@n.com>", "n@x.com", html)


@section("1. Every tool request: email, then Proposed, then the tool's details")
def _():
    for label, call in CASES.items():
        tool_section = format_for_display(call)
        heading = tool_section.strip().splitlines()[0].lstrip("#").strip()
        out = plain(r.render_message({"action": call["name"], "args": call["args"],
                                      "allowed": {}, "description": EMAIL + tool_section}))
        e, body = out.find("\U0001f4e7 Email"), out.find("Could we find 45 minutes")
        pr, h = out.find("Proposed:"), out.find(heading)
        check(label, -1 < e < body < pr < h, f"email@{e} body@{body} proposed@{pr} '{heading}'@{h}")


@section("2. Triage stop: email first, Proposed last, nothing invented")
def _():
    out = plain(r.render_message({"action": "Email Assistant: notify", "args": {}, "allowed": {},
                                  "description": EMAIL}))
    check("email before Proposed", -1 < out.find("\U0001f4e7 Email") < out.find("Proposed:"))
    check("Proposed is the last thing", out.rstrip().endswith("Proposed: Email Assistant: notify"))


@section("3. HTML newsletter with its OWN headings (real Gmail case)")
def _():
    news = newsletter()
    check("html2text really put a # heading inside the email", re.search(r"^#+ ", news, re.M) is not None)

    out = plain(r.render_message({"action": "Email Assistant: notify", "args": {}, "allowed": {},
                                  "description": news}))
    check("triage: newsletter heading stays in the EMAIL section",
          -1 < out.find("5 AI tools") < out.find("Proposed:"),
          f"heading@{out.find('5 AI tools')} proposed@{out.find('Proposed:')}")

    out = plain(r.render_message({"action": "send_email_tool", "args": {}, "allowed": {},
                                  "description": news + format_for_display(CASES["send_email_tool"])}))
    check("with a draft: heading in email, draft below Proposed",
          -1 < out.find("5 AI tools") < out.find("Proposed:") < out.find("Email Reply"))


@section("4. With a proposal, a normal-size email is shown in full")
def _():
    body = "Hi Alex,\n\n" + "We would like to start with a small pilot. " * 20 + "\n\nEND OF EMAIL\n\nSarah"
    mail = format_gmail_markdown("Automation pilot", "Sarah Chen <s@x.com>", "Alex <n@x.com>", body)
    invite = format_for_display(CASES["schedule_meeting_tool"])
    heading = invite.strip().splitlines()[0].lstrip("#").strip()
    out = plain(r.render_message({"action": "schedule_meeting_tool", "args": {}, "allowed": {},
                                  "description": mail + invite}))
    check("the end of a ~900-character email is shown", "END OF EMAIL" in out)
    check("the proposal still follows it", -1 < out.find("END OF EMAIL") < out.find("Proposed:") < out.find(heading))


@section("5. A long email with a long draft still fits one message")
def _():
    long_mail = format_gmail_markdown("Pilot", "Sarah Chen <s@x.com>", "n@x.com", "Word " * 1500)
    draft = {"name": "send_email_tool", "args": {"email_id": "abc", "email_address": "n@x.com",
                                                 "response_text": "Draft line. " * 200}}
    msg = r.render_message({"action": "send_email_tool", "args": draft["args"], "allowed": {},
                            "description": long_mail + format_for_display(draft)})
    check("a long email with a long draft still fits", len(msg) < r.TELEGRAM_LIMIT, f"len={len(msg)}")


@section("6. What the model is sent: every argument described, with an example")
def _():
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from email_assistant.tools.gmail.gmail_tools import (schedule_meeting_tool, check_calendar_tool,
                                                         update_meeting_tool, cancel_meeting_tool)
    for tool_ in (schedule_meeting_tool, check_calendar_tool, update_meeting_tool, cancel_meeting_tool):
        props = convert_to_openai_tool(tool_)["function"]["parameters"]["properties"]
        bare = [k for k, v in props.items() if "example" not in v.get("description", "")]
        check(f"{tool_.name}: every field has a description with an example", not bare, f"missing: {bare}")
    gmail = convert_to_openai_tool(schedule_meeting_tool)["function"]["parameters"]["properties"]
    check("gmail: default time zone is Europe/London, not Los Angeles",
          gmail["timezone"].get("default") == "Europe/London", str(gmail["timezone"]))
    check("gmail: start_time forbids a Z suffix", "No Z" in gmail["start_time"]["description"])


# A Gmail reply, three emails down. The last "On ... wrote:" line is wrapped,
# and Gmail put its second half at another depth (real case, 5 Oct).
def reply_mail(body):
    return format_gmail_markdown("Re: Sat catch up", "Robin <r@x.com>", "n@x.com", body)


REPLY_BODY = (
    "Thanks, see you then. Should I bring anything?\n\nRobin\n\n"
    "On Mon, Sep 28, 2026 at 4:17 PM <n@x.com> wrote:\n\n"
    "> Hi Robin,\n>\n> I have updated our catch up.\n>\n> Alex\n>\n"
    "> On Mon, 28 Sep 2026 16:13:08 +0100, test test <r@x.com> wrote:\n"
    "> > Hey Alex\n> > Can we make it 2 hours?\n> >\n"
    "> > On Mon, 28 Sep 2026 at 15:41, <n@x.com> wrote:\n> >\n"
    "> > > Saturday at 12pm works.\n> > >\n"
    "> > > On Mon, 28 Sep 2026 15:31:28 +0100, test test <r@x.com>\n> wrote:\n"
    "> > > > Can we meet this Saturday at 12pm ?\n")
REPLY = reply_mail(REPLY_BODY)
# The same four earlier emails, as the ingester gives them to the model.
REPLY_HISTORY = [{"body": text} for text in (
    "Can we meet this Saturday at 12pm ?", "Saturday at 12pm works.", "Hey Alex\nCan we make it 2 hours?",
    "Hi Robin,\n\nI have updated our catch up.\n\nAlex")]


def card(mail, call=None):
    return r.render_message({"action": (call or {}).get("name", "Email Assistant: notify"), "args": {},
                             "allowed": {}, "description": mail + (format_for_display(call) if call else "")})


@section("7. A reply with the earlier emails quoted under it")
def _():
    out = card(REPLY, CASES["send_email_tool"])
    fold = out.find("<blockquote expandable>")
    check("no line starts with a > mark", not [line for line in out.splitlines() if line.startswith("&gt;")])
    check("the new email comes first, outside the quote",
          -1 < out.find("Should I bring anything?") < out.find("Robin\n") < fold)
    check("one folded quote, closed before the proposal",
          out.count("<blockquote expandable>") == 1 == out.count("</blockquote>")
          and out.find("</blockquote>") < out.find("Proposed:"))
    check("the line above the quote is its first line, in bold",
          "<blockquote expandable><b>On Mon, Sep 28, 2026 at 4:17 PM &lt;n@x.com&gt; wrote:</b>\nHi Robin," in out)
    check("each earlier email starts with a bold line", out.count("wrote:</b>") == 4, str(out.count("wrote:</b>")))
    check("a wrapped line is joined",
          "<b>On Mon, 28 Sep 2026 15:31:28 +0100, test test &lt;r@x.com&gt; wrote:</b>" in out)
    check("the oldest email is still there", "Can we meet this Saturday at 12pm ?" in out)

    full = r.markdown_to_html(REPLY)
    check("Full text: the quote is open, not folded", "<blockquote>" in full and "expandable" not in full)
    check("Full text: no line starts with a > mark",
          not [line for line in full.splitlines() if line.startswith("&gt;")])

    check("an email with no quote has no quote block", "blockquote" not in card(EMAIL))
    out = card(reply_mail("On another note, thanks.\nOn Mon, 28 Sep 2026 at 15:41, <n@x.com> wrote:\n> Hi"))
    check("the sender's own line above the quote stays in view",
          -1 < out.find("On another note, thanks.") < out.find("<blockquote"))
    out = card(reply_mail("See my answers below.\n> Can we meet?\nYes.\n> At 12?\nFine."))
    check("answers between the quotes stay in view, in order",
          -1 < out.find("See my answers") < out.find("Can we meet?") < out.find("Yes.")
          < out.find("At 12?") < out.find("Fine.") and out.count("<blockquote") == 2 == out.count("</blockquote>"))

    # Review, 5 Oct: the card put this sentence in the folded quote, in bold, as a header.
    out = card(reply_mail("On 5 Oct I am away.\nTest <r@x.com> wrote:\n> Can we make it 2 hours?"))
    check("a sentence that starts with On and a date stays in view",
          -1 < out.find("On 5 Oct I am away.") < out.find("<blockquote")
          and "<blockquote expandable><b>Test &lt;r@x.com&gt; wrote:</b>" in out)
    out = card(reply_mail("Ok.\n> As I wrote: the meeting is at 3"))
    check("a quoted sentence with 'wrote:' in it is not a header",
          "As I wrote: the meeting is at 3" in out and "<b>As I wrote" not in out)

    # Live, 5 Oct: Gmail broke this line after the address; the card showed a lone bold "wrote:".
    out = card(reply_mail("See you.\n\nOn Mon, Oct 5, 2026 at 1:56 PM <n@x.com> wrote:\n\n> Yes.\n>\n"
                          "> On Mon, 28 Sep 2026 15:31:28 +0100, test test <r@x.com\n> >\n> > wrote:\n> > Can we?\n"))
    check("a 'wrote:' line that Gmail broke after the address is one bold line",
          "<b>On Mon, 28 Sep 2026 15:31:28 +0100, test test &lt;r@x.com&gt; wrote:</b>\nCan we?" in out
          and "<b>wrote:</b>" not in out)

    own_on_card = " ".join(part for quoted, part in r.quote_blocks(REPLY_BODY) if not quoted)
    own_for_model = without_repeated_quote(REPLY_BODY, {"history": REPLY_HISTORY})
    check("the card and the model keep the same text of the email",
          own_on_card.split() == own_for_model.split() == "Thanks, see you then. Should I bring anything? Robin".split())


@section("8. Full text in more than one message: a quote stays a quote")
def _():
    quoted = "".join(f"> Paragraph {n} of the earlier email, long enough to fill a line of text.\n>\n" for n in range(1, 13))
    mail = reply_mail("My new answer.\n\nOn Mon, 28 Sep 2026 at 15:41, <n@x.com> wrote:\n\n" + quoted)
    pieces = r.full_text_pieces(mail, 300)
    check("the text needs more than one message", len(pieces) > 2, str(len(pieces)))
    check("each message has complete quote blocks",
          all(piece.count("<blockquote>") == piece.count("</blockquote>") for piece in pieces))
    check("no line starts with a > mark", not [line for piece in pieces for line in piece.splitlines() if line.startswith("&gt;")])
    outside = [n for n in range(1, 13) for piece in pieces
               if f"Paragraph {n} of" in re.sub(r"<blockquote>.*?</blockquote>", "", piece, flags=re.DOTALL)]
    check("every quoted paragraph is in a quote block, in each message", not outside, str(outside))
    check("all of the quote is there", all(any(f"Paragraph {n} of" in piece for piece in pieces) for n in range(1, 13)))
    first = next(piece for piece in pieces if "<blockquote>" in piece)
    check("the quote starts with its 'wrote:' line", "<blockquote><b>On Mon, 28 Sep 2026 at 15:41, &lt;n@x.com&gt; wrote:</b>" in first)
    check("a short text is one message", len(r.full_text_pieces(EMAIL)) == 1)


run_all()
