"""What the Telegram bot shows: the graph's requests as Telegram text and buttons.

Pure functions and fixed settings. Nothing here calls Telegram or the
LangGraph server, or reads the bot's memory (bot_data), so each function can
be used on its own. telegram_inbox.py, the bot itself, imports what it
needs from here. It used to hold all of this too, in one 1,800-line file.
"""

from __future__ import annotations

import difflib
import html
import os
import re
from typing import Any

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# The one thing the bot takes from the agent's package: the rules that find the
# quoted emails under a reply. The model's copy of an email uses the same
# rules, so the card and the model cut an email at the same place.
from email_assistant.quotes import quote_spans, quoted_emails

# ENABLE_EDIT below is read from .env. The bot imports this file before it
# loads .env itself, so it is loaded here first.
load_dotenv()


# Edit is off by default. Respond does the same job in plain English and the
# agent redrafts, whereas edit replaces the tool's arguments WHOLESALE - which
# is where most of this bot's bugs came from. The graph still supports edit;
# the button is simply not offered. Set TELEGRAM_ENABLE_EDIT=1 to bring it back.
#
# The trade-off: edit is exact, respond is not. If a value must be precisely
# right - a recipient on the real Gmail agent - check the redraft before you
# accept it.
ENABLE_EDIT = os.getenv("TELEGRAM_ENABLE_EDIT", "0") == "1"

# Fields that hold the main body of text. These go FIRST in the edit menu,
# because the body is what you nearly always want to change. The Gmail
# reply tool calls it `response_text`.
TEXT_FIELDS = ("content", "response_text", "body", "text")

# Hints shown when you edit a field whose format is easy to get wrong. Only the
# hint is added - the value that reaches the graph is always exactly what you
# typed. start_time is a 24-hour local time:
# "2026-09-16T14:30:00".
FIELD_NOTES = {"start_time": "Keep the format of the current value, on a 24-hour clock: 14:30, not 2:30pm."}

# Telegram refuses any message over 4096 characters. The poller deliberately
# does not mark a failed announcement as seen, so one over-long draft used to
# retry every 15 seconds forever. Everything below is trimmed to stay well
# inside the limit, and `send_html` catches anything that still slips through.
TELEGRAM_LIMIT = 4096
SAFE_LIMIT = 3400
PROPOSAL_LIMIT = 2200
# The inbound email's body is shown in full up to this many characters.
EMAIL_LIMIT = 2800
# How much of each email's text the list shows, at most.
LIST_PREVIEW = 90
# Full text, and a profile too long for one message, go in pieces this long.
CHUNK_LIMIT = 3000
# The most of one value that is shown: in the raw argument list, as the
# current value of a field you edit, and in the edit panel.
ARG_VALUE_LIMIT = 600
FIELD_VALUE_LIMIT = 1500
DRAFT_VALUE_LIMIT = 300


# --------------------------------------------------------------------------
# Reading interrupts out of the graph
# --------------------------------------------------------------------------


def first_interrupt(thread: dict) -> tuple[str, dict] | None:
    """Pull the one interrupt out of a Thread from threads.search().

    Thread.interrupts is dict[task_id, list[Interrupt]]. The agent always calls
    interrupt([request]) with a single request, so we want the first Interrupt
    of the first task, and then its .value - which is itself a one-item list.

    Returns (interrupt_id, value). The id matters: it is stable while one
    interrupt is waiting, and different for the next one. That is what lets
    the poller tell "the request I just answered" from "a new request".
    """
    buckets = thread.get("interrupts") or {}
    for items in buckets.values():
        if not items:
            continue
        raw = items[0]
        if isinstance(raw, dict):
            value, ident = raw.get("value"), raw.get("id")
        else:
            value, ident = getattr(raw, "value", None), getattr(raw, "id", None)
        if isinstance(value, list) and value:
            value = value[0]
        if isinstance(value, dict):
            return str(ident or ""), value
    return None


def header(description: str, name: str) -> str:
    """Pull one **Name**: value line out of the graph's description.

    Only a line that starts with the name counts. The subject is the sender's
    own text and comes first: without the anchor, a subject that held
    "**From**: someone else" put that name on the card as the sender.
    """
    match = re.search(rf"^\*\*{name}\*\*:\s*(.+)", description, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def short_sender(sender: str) -> str:
    """'Sarah Chen <sarah@x.co.uk>' -> 'Sarah Chen'. Fits on a button."""
    name = sender.split("<")[0].strip().strip('"')
    return name or sender


def parse_request(value: dict) -> dict:
    """Normalise the Agent Inbox request shape into the bits we need."""
    action_request = value.get("action_request") or {}
    description = value.get("description") or ""
    return {
        "action": action_request.get("action") or "(unknown action)",
        "args": dict(action_request.get("args") or {}),
        "allowed": value.get("config") or {},
        "description": description,
        "sender": header(description, "From"),
        "subject": header(description, "Subject"),
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def esc(text: str) -> str:
    """Escape text for Telegram's HTML mode.

    Only < > & actually need escaping. Python's html.escape ALSO turns quotes
    into &quot; and apostrophes into &#x27;, which are only meaningful inside
    an HTML attribute - and Telegram is not guaranteed to turn them back. An
    apostrophe in a subject line would then reach the phone as &#x27;.
    """
    return html.escape(text, quote=False)


def short_id(interrupt_id: str) -> str:
    """First 8 characters of an interrupt id.

    Enough to tell two questions on the same thread apart, and short enough to
    fit Telegram's 64-byte callback limit alongside a 36-character thread id.
    """
    return (interrupt_id or "")[:8]


def build_keyboard(thread_id: str, allowed: dict, interrupt_id: str = "") -> InlineKeyboardMarkup:
    """Buttons come from the graph's own flags, never from assumptions.

    A triage stop and a Question both disable edit and accept, so those buttons
    simply will not appear. No 'Mark as resolved' - that one resumes with a
    bare string and crashes the node.
    """
    row: list[InlineKeyboardButton] = []
    if allowed.get("allow_accept"):
        row.append(InlineKeyboardButton("Accept", callback_data=f"accept|{thread_id}|{short_id(interrupt_id)}"))
    if allowed.get("allow_edit") and ENABLE_EDIT:
        row.append(InlineKeyboardButton("Edit", callback_data=f"edit|{thread_id}|{short_id(interrupt_id)}"))
    if allowed.get("allow_respond"):
        row.append(InlineKeyboardButton("Respond", callback_data=f"respond|{thread_id}|{short_id(interrupt_id)}"))
    if allowed.get("allow_ignore"):
        row.append(InlineKeyboardButton("Ignore", callback_data=f"ignore|{thread_id}|{short_id(interrupt_id)}"))
    # Two per line keeps them readable on a phone.
    rows = [row[i : i + 2] for i in range(0, len(row), 2)]
    rows.append([
        InlineKeyboardButton("\U0001f4c4 Full text", callback_data=f"full|{thread_id}"),
        InlineKeyboardButton("\u2190 Back to list", callback_data="list|-"),
    ])
    return InlineKeyboardMarkup(rows)


def render_list(pending: dict) -> tuple[str, InlineKeyboardMarkup]:
    """The inbox: one line per waiting request, one button to open each.

    The bot used to post every request in full as it arrived, which buried the
    chat. Now only this short list is posted, and the detail is shown when you
    ask for it.

    Each entry names the email, not the tool: its subject, its sender and the
    start of its text. The tool name ("schedule_meeting_tool") told the owner
    nothing about which email was waiting (25 Sep); it is used only when an
    email has no subject. The previews share the room SAFE_LIMIT leaves, so a
    full inbox still fits one message.
    """
    entries = []
    for number, (thread_id, req) in enumerate(pending.items(), start=1):
        subject = req.get("subject") or req["action"]
        if len(subject) > 70:
            subject = subject[:70] + " ..."
        who = short_sender(req.get("sender") or "")
        if len(who) > 40:
            who = who[:40] + " ..."
        entries.append((number, thread_id, subject, who, req.get("description") or ""))

    def entry_lines(number, subject, who, preview):
        lines = ["", f"<b>{number}. {esc(subject)}</b>" + (f" - {esc(who)}" if who else "")]
        if preview:
            lines.append(f"<i>{esc(preview)}</i>")
        return lines

    head = f"<b>{len(pending)} waiting</b>"
    # +1: the line break that joins each entry to the one before it.
    bare = sum(len("\n".join(entry_lines(n, s, w, ""))) + 1 for n, _, s, w, _ in entries)
    # The most each preview may take once escaped, on its own line in <i></i>.
    room = (SAFE_LIMIT - len(head) - bare) // len(entries) - len("\n<i></i>") if entries else 0
    lines, buttons = [head], []
    for number, thread_id, subject, who, description in entries:
        # Under 20 characters a preview says nothing, so leave it out.
        preview = email_preview(description, min(LIST_PREVIEW, room)) if room >= 20 else ""
        lines += entry_lines(number, subject, who, preview)
        buttons.append([InlineKeyboardButton(f"{number}. {subject}"[:60], callback_data=f"open|{thread_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def format_value(val: Any) -> str:
    """One place that turns a value into text.

    Both the display and the editable block MUST use this. They used to differ:
    the block printed a list with Python's repr, so `['a@b.com']` came back as a
    one-item list containing the literal string "['a@b.com']": a malformed
    recipient.
    """
    if isinstance(val, list):
        return ", ".join(str(v) for v in val)
    if val is None:
        return ""
    return str(val)


def render_args(args: dict) -> str:
    """Show every argument, whatever they happen to be called."""
    lines = []
    used = 0
    for key, val in args.items():
        text = format_value(val)
        if len(text) > ARG_VALUE_LIMIT:
            text = text[:ARG_VALUE_LIMIT] + " ..."
        line = f"<b>{esc(key)}</b>: {esc(text)}"
        # Drop a whole line rather than cut one in half. A cut could land
        # inside an HTML tag, and Telegram then refuses the entire message.
        if used + len(line) > SAFE_LIMIT:
            lines.append("<i>[... more fields not shown]</i>")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def field_order(args: dict) -> list[str]:
    """Body first, then every other field in the order the tool gave them.

    The body is the usual edit, so it should be the first button. The rest
    still have to be reachable - a wrong recipient on the Gmail agent is
    exactly the thing you want to catch before anything is sent.
    """
    keys = list(args)
    return sorted(keys, key=lambda k: (k not in TEXT_FIELDS, keys.index(k)))


def chunk_text(text: str, size: int) -> list[str]:
    """Split on blank lines so a chunk never lands inside a word or a tag."""
    chunks, current = [], ""
    for para in text.split("\n\n"):
        if current and len(current) + len(para) + 2 > size:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
        while len(current) > size:          # one enormous paragraph
            chunks.append(current[:size])
            current = current[size:]
    if current:
        chunks.append(current)
    return chunks or [""]


def render_draft(args: dict, changed: str | None = None) -> str:
    """The edit panel: every field as it currently stands."""
    if changed:
        head = f"<b>{esc(changed)} updated.</b> Change another field, or press Send."
    else:
        head = "<b>Editing.</b> Tap a field to change it, then press Send."
    lines = [head, ""]
    for key, val in args.items():
        text = format_value(val)
        if len(text) > DRAFT_VALUE_LIMIT:
            text = text[:DRAFT_VALUE_LIMIT].rstrip() + " ..."
        lines.append(f"<b>{esc(key)}</b>: {esc(text)}")
    return "\n".join(lines)


def edit_keyboard(thread_id: str, keys: list[str]) -> InlineKeyboardMarkup:
    """A button per field, and Send to commit the whole lot.

    Nothing reaches the graph until Send is pressed. Editing one field used to
    resume the run immediately, so a second correction was impossible.
    """
    buttons = [
        InlineKeyboardButton(key, callback_data=f"field|{thread_id}|{i}")
        for i, key in enumerate(keys)
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("\u2705 Send", callback_data=f"send|{thread_id}")])
    return InlineKeyboardMarkup(rows)


def quote_blocks(text: str) -> list[tuple[bool, str]]:
    """Text cut into its own parts and its quoted parts, in order: (quoted, text).

    email_assistant.quotes finds the quotes. A quoted part is returned without
    its `>` marks. Each email in it starts with its "On ... wrote:" line in
    `**`, so the emails can be told apart; the line above the quote is the
    first of them.
    """
    lines = text.splitlines()
    parts, at = [], 0
    for first, start, end in quote_spans(lines):
        parts.append((False, "\n".join(lines[at:first])))
        attribution = " ".join(" ".join(lines[first:start]).split())
        emails = quoted_emails(attribution, lines[start:end])
        parts.append((True, "\n\n".join(f"**{header}**\n{body}" if header else body for header, body in emails)))
        at = end
    parts.append((False, "\n".join(lines[at:])))
    return [(quoted, part.strip()) for quoted, part in parts if part.strip()]


def html_part(quoted: bool, part: str, fold_quotes: bool = False) -> str:
    """One part from quote_blocks as Telegram HTML; a quoted part as a quote block."""
    out = esc(part)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.DOTALL)
    # Telegram has no heading tag, so "# Calendar Invite" arrived with the hash
    # showing. Bold is the closest thing it has.
    out = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", out, flags=re.MULTILINE)
    # The markdown horizontal rule is noise on a phone.
    out = re.sub(r"^\s*-{3,}\s*$", "", out, flags=re.MULTILINE)
    # Collapse the blank lines that leaves behind.
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    if quoted and out:
        out = f"<blockquote{' expandable' if fold_quotes else ''}>{out}</blockquote>"
    return out


def markdown_to_html(text: str, fold_quotes: bool = False) -> str:
    """The graph builds its description in markdown, but Telegram is told to
    parse HTML - so `**Subject**` arrived on screen with the asterisks showing.
    Escape first (so a stray < in an email cannot break the message), then turn
    the bold markers into real tags.

    A quoted part (quote_blocks) becomes a Telegram quote block: a reply three
    emails down arrived as a wall of "> > >" (5 Oct). With `fold_quotes`,
    Telegram shows the first lines of the block and the rest on a tap."""
    parts = (html_part(quoted, part, fold_quotes) for quoted, part in quote_blocks(text))
    return "\n\n".join(part for part in parts if part)


def full_text_pieces(text: str, size: int = CHUNK_LIMIT) -> list[str]:
    """The whole request as Telegram HTML, in pieces of about `size` characters.

    The text is cut part by part (quote_blocks), and a part that is too long
    at its blank lines (chunk_text), so a quote that goes over the end of one
    message is a quote in the next one too. When the plain text was cut first,
    the "On ... wrote:" line could stay in one message while its quote, with
    no header, went to the next.
    """
    pieces, current = [], ""
    for quoted, part in quote_blocks(text):
        for chunk in chunk_text(part, size):
            piece = html_part(quoted, chunk)
            if current and len(current) + len(piece) + 2 > size:
                pieces.append(current)
                current = piece
            else:
                current = f"{current}\n\n{piece}" if current else piece
    if current:
        pieces.append(current)
    return pieces or [""]


def inbound_and_proposal(text: str) -> tuple[str, str]:
    """The graph's description cut in two: the inbound email, and the proposal
    with its leading `#` removed ("" when there is none)."""
    # The graph appends its proposal AFTER the email's closing `---` line:
    # utils.format_gmail_markdown always ends with it, and format_for_display
    # always starts with a `# ` heading. So split at the LAST such line.
    # Splitting at the first `#` broke on HTML emails - html2text turns an
    # <h1> into `# Heading` INSIDE the email, which was then shown as the
    # proposal. With no tool section (a triage stop) nothing follows the last
    # `---`, so a heading inside the email can never be mistaken for one.
    sections = re.split(r"^\s*-{3,}\s*$", text, flags=re.MULTILINE)
    tail = sections[-1].strip()
    if len(sections) > 1 and tail.startswith("#"):
        return "\n".join(sections[:-1]), tail.lstrip("#").strip()
    return text, ""


def email_body(inbound: str) -> str:
    """The inbound email without its From/Subject/To/ID lines and `---` rules."""
    body = re.sub(r"^\*\*(Subject|From|To|ID)\*\*:.*$", "", inbound, flags=re.MULTILINE)
    return re.sub(r"^\s*-{3,}\s*$", "", body, flags=re.MULTILINE).strip()


def email_preview(description: str, limit: int = LIST_PREVIEW) -> str:
    """The start of the email's text on one line, for the list.

    At most `limit` characters once escaped for Telegram (& becomes &amp;),
    ending " ..." when cut. Markdown marks are dropped (html2text writes them
    into Gmail's HTML emails), and a link keeps only its text. The earlier
    emails quoted under a reply are left out; an email that is only a quote
    keeps it.
    """
    inbound, _ = inbound_and_proposal(description)
    body = email_body(inbound)
    own = "\n".join(part for quoted, part in quote_blocks(body) if not quoted)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", own or body)
    text = re.sub(r"^\s*(#{1,6}|>+)\s*", "", text, flags=re.MULTILINE)
    text = " ".join(text.replace("**", "").split())
    if len(esc(text)) <= limit:
        return text
    cut = max(limit - len(" ..."), 0)
    while cut and len(esc(text[:cut].rstrip())) > limit - len(" ..."):
        cut -= 1
    return text[:cut].rstrip() + " ..."


def split_description(text: str) -> tuple[str, str]:
    """Separate the inbound email from what the agent proposes to do.

    The graph builds its description as: the whole inbound email, then a
    `# Email Draft` or `# Calendar Invite` section holding the proposal. On a
    phone that is a lot of text, and the inbound email repeats on every
    interrupt of the same thread. So: keep the proposal in full, and show the
    email as sender, subject and a body of up to EMAIL_LIMIT characters.

    Returned separately so the email can be shown FIRST and the proposal
    under it - read what arrived, then decide what to do about it.
    """
    inbound, proposal = inbound_and_proposal(text)

    head = [f"**{label}**: {v}" for label, v in (("From", header(inbound, "From")), ("Re", header(inbound, "Subject"))) if v]

    body = email_body(inbound)

    # The same limit with or without a proposal. Under a proposal the body used
    # to be cut to 200 characters, which hid the end of even a short email and
    # meant pressing Full text every time. If a long draft needs the room, the
    # SAFE_LIMIT check below still trims the email to fit.
    if len(body) > EMAIL_LIMIT:
        body = body[:EMAIL_LIMIT].rstrip() + " ..."

    # The proposal was once kept in full, and a long draft pushed the whole
    # message past Telegram's 4096 characters so it was refused.
    if len(proposal) > PROPOSAL_LIMIT:
        proposal = proposal[:PROPOSAL_LIMIT].rstrip() + "\n\n[... draft trimmed]"

    email = "\n".join(head)
    if body:
        email += f"\n\n{body}"
    proposal_md = f"# {proposal}" if proposal else ""

    # Trim here, on the plain text, and never after the HTML is built - a cut
    # inside a tag makes Telegram reject the message.
    if len(email) + len(proposal_md) > SAFE_LIMIT:
        room = max(SAFE_LIMIT - len(proposal_md), 200)
        email = email[:room].rstrip() + "\n\n[... trimmed]"
    return email, proposal_md


def compact_description(text: str) -> str:
    """Email and proposal as one block, email first."""
    email, proposal = split_description(text)
    return "\n\n".join(p for p in (email, proposal) if p)


DIVIDER = "\u2500" * 14


def render_message(req: dict) -> str:
    """Email first, then what the agent proposes to do about it.

    The proposal header used to sit at the very top, above the email it was a
    response to, which read backwards.
    """
    action = f"<b>\U0001f449 Proposed: {esc(req['action'])}</b>"
    description = req["description"].strip()
    if not description:
        # No description to work from, so fall back to the raw arguments.
        return f"{action}\n\n{render_args(req['args'])}"
    email, proposal = split_description(description)
    parts = []
    if email:
        # The earlier emails under a reply are folded: the new email is what
        # the card is about, and Full text shows them open.
        parts.append(f"<b>\U0001f4e7 Email</b>\n{markdown_to_html(email, fold_quotes=True)}")
        parts.append(DIVIDER)
    parts.append(action)
    if proposal:
        parts.append(markdown_to_html(proposal))
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# Outcome reports and preference changes
# --------------------------------------------------------------------------


def plain_outcome(text: str) -> str:
    """Turn one tool result into one readable line.

    Presentation only. The tools write their own result strings, so this trims
    the obvious padding and falls back to the raw text when nothing matches -
    a reworded tool makes the line longer, never wrong.
    """
    text = text.strip().splitlines()[0] if text.strip() else ""
    # The email tools append the whole draft, which you have already read.
    for marker in (" and content:", " with content:"):
        if marker in text:
            text = text.split(marker)[0]
            break
    # A short first sentence is usually the whole story ("User ignored this
    # email draft."); what follows is instruction aimed at the model.
    first, dot, _ = text.partition(". ")
    if dot and len(first) < 120:
        text = first + "."
    return text if len(text) <= 200 else text[:200].rstrip() + " ..."


def reply_outcome(email: dict | None, email_id: str | None) -> str | None:
    """A sent reply as the email it answered: its sender and subject.

    The reply tool's own result names only a Gmail message ID, which tells you
    nothing. The reply always goes to the sender of the email it answers
    (gmail_tools.send_email), and that email is the thread's email_input.
    None when the reply answered some other message, so the caller falls back
    to the tool's own line.
    """
    if not email or not email_id or email.get("id") != email_id:
        return None
    lines = ["✅ Reply sent"]
    if email.get("from"):
        lines.append(f"<b>To</b>: {esc(email['from'])}")
    if email.get("subject"):
        lines.append(f"<b>Re</b>: {esc(email['subject'])}")
    return "\n".join(lines)


# How much of one preference's change to show. A whole rewritten preference
# can run to thousands of characters, and the outcome must stay inside
# Telegram's 4096 alongside the other lines.
CHANGE_LINE_LIMIT = 400
CHANGE_LIMIT = 1000


def line_changes(before: str, after: str) -> list[tuple[str, str]]:
    """Two preference texts compared line by line, as (kind, line) pairs.

    kind is "same", "removed" or "added", in the order of the new text; a
    reworded line is one "removed" and one "added". Blank lines and
    indentation are ignored, so re-spacing alone is no change. The outcome
    report (preference_change) and the /prefs preview (preference_preview)
    both use this one comparison.
    """
    old = [line.strip() for line in str(before).splitlines() if line.strip()]
    new = [line.strip() for line in str(after).splitlines() if line.strip()]
    pairs = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if tag == "equal":
            pairs += [("same", line) for line in new[j1:j2]]
            continue
        pairs += [("removed", line) for line in old[i1:i2]]
        pairs += [("added", line) for line in new[j1:j2]]
    return pairs


def preference_change(before: str, after: str) -> str:
    """The lines of one preference that changed, as Telegram HTML.

    The graph's memory step has the model rewrite the WHOLE preference text,
    so "Preference noted" alone does not say what it now believes. "+" is a
    line that is new, "-" a line that is gone (line_changes).

    Returns "" when nothing but spacing changed.
    """
    changes = [("− " if kind == "removed" else "+ ") + line
               for kind, line in line_changes(before, after) if kind != "same"]

    shown, used = [], 0
    for line in changes:
        if len(line) > CHANGE_LINE_LIMIT:
            line = line[:CHANGE_LINE_LIMIT].rstrip() + " ..."
        # Whole lines only, escaped one at a time, so a cut never lands inside
        # a tag - Telegram refuses the entire message if it does.
        if used + len(line) > CHANGE_LIMIT:
            shown.append("[... more changes]")
            break
        shown.append(esc(line))
        used += len(line) + 1
    return "\n".join(shown)


def preference_preview(before: str, after: str) -> list[str]:
    """The whole profile after the change, one Telegram HTML line per rule,
    with the changes marked IN PLACE: a removed line struck through, a new
    line bold with a plus, everything else plain.

    It used to be a diff followed by the full new text, which read as two
    different proposals. One text, marked up, is what a person can judge.
    Each line is self-contained HTML, so a message can be split between
    lines without breaking a tag.
    """
    marks = {"same": "{}", "removed": "<s>− {}</s>", "added": "<b>+ {}</b>"}
    return [marks[kind].format(esc(line)) for kind, line in line_changes(before, after)]


# --------------------------------------------------------------------------
# Edited values
# --------------------------------------------------------------------------


def coerce(original: Any, text: str) -> tuple[Any, str | None]:
    """Keep the original type. Returns (value, problem).

    `problem` is None when the value was accepted. It used to return the
    ORIGINAL value on a failed conversion and say nothing, so typing "14:30"
    into a whole-number field silently kept the old value and the edit looked
    like it had been applied.
    """
    text = text.strip()
    if isinstance(original, bool):
        if text.lower() in ("true", "yes", "1"):
            return True, None
        if text.lower() in ("false", "no", "0"):
            return False, None
        return original, "expects true or false"
    if isinstance(original, int) and not isinstance(original, bool):
        try:
            return int(text), None
        except ValueError:
            return original, f"'{text}' is not a whole number"
    if isinstance(original, list):
        parts = [p.strip() for p in text.split(",") if p.strip()]
        return (parts, None) if parts else (original, "that was empty")
    return text, None
