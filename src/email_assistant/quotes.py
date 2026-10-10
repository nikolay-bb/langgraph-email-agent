"""The earlier emails quoted under a reply: found in one place.

Two readers use this. The model's copy of an email
(utils.without_repeated_quote) drops a quote the history already holds, and
the Telegram card (scripts/inbox_render.py) folds it. Each had its own copy of
the rules, the copies differed, and the card hid a sentence that the model's
copy kept (5 Oct). Standard library only, so the bot can import it without the
agent's other code.

A quote is a run of lines that start with `>`: that is how Gmail, and
html2text for an HTML email, mark the earlier emails. The line above it,
"On Mon, 28 Sep 2026 at 16:17, Sarah <s@x.com> wrote:", names the email that
is quoted. Only this English form is known: from a mail program in another
language, that line stays in the email's own text.
"""
from __future__ import annotations

import re

# The marks in front of a quoted line: "> ", or "> > > " three emails down.
QUOTE_MARKS = re.compile(r"^(?:>[ \t]?)+")


def ends_attribution(line: str) -> bool:
    """True for a line that ends the "... wrote:" line above a quoted email.

    The line must END with it: "As I wrote: the meeting is at 3" is a sentence.
    """
    return line.rstrip().endswith("wrote:")


def wrapped_start(line: str, above: str) -> bool:
    """True when `above` is the first half of the "... wrote:" line that `line` ends.

    Gmail wraps a long one: "On Mon, 28 Sep 2026 15:31:28 +0100, Sarah
    <s@x.com>", then "wrote:". The digit and the missing full stop keep a
    sentence of the sender's own, such as "On 5 Oct I am away.", out of it.
    """
    above = above.rstrip()
    return bool(not line.startswith("On ") and re.match(r"On\b.*\d", above) and above[-1] not in ".!?")


def quote_spans(lines: list[str]) -> list[tuple[int, int, int]]:
    """Where each quote is in `lines`, as (first, start, end).

    lines[start:end] is the quote: its `>` lines and the blank lines between
    them. lines[first:start] is the "... wrote:" line above it, with the blank
    lines under that line; first == start when there is none. Only a line that
    ends with "wrote:" goes with the quote: anything else above it is the
    sender's own text and stays outside.
    """
    spans: list[tuple[int, int, int]] = []
    at = 0
    while at < len(lines):
        if not lines[at].startswith(">"):
            at += 1
            continue
        start = end = at
        while at < len(lines) and (lines[at].startswith(">") or not lines[at].strip()):
            at += 1
            if lines[at - 1].startswith(">"):
                end = at
        # Look above the quote for its "... wrote:" line, never into the quote before it.
        floor = spans[-1][2] if spans else 0
        first, above = start, start - 1
        while above >= floor and not lines[above].strip():
            above -= 1
        if above >= floor and ends_attribution(lines[above]):
            first = above
            if above > floor and wrapped_start(lines[above], lines[above - 1]):
                first = above - 1
        spans.append((first, start, end))
        at = end
    return spans


def quoted_emails(attribution: str, quote: list[str]) -> list[tuple[str, str]]:
    """The emails in one quote, newest first, as (its "... wrote:" line, its text).

    `quote` is the quote's lines with their marks, and `attribution` the line
    above the quote ("" when there is none). An email quoted inside the quote
    starts at its own "... wrote:" line. The texts have no marks.
    """
    emails: list[tuple[str, list[str]]] = [(attribution, [])]
    for line in quote:
        line = QUOTE_MARKS.sub("", line.rstrip())
        text = emails[-1][1]
        if ends_attribution(line):
            # Gmail wraps a long line again each time it is quoted. Six emails
            # down (live, 5 Oct) the line was "... <r@x.com", then "> >", then
            # "> > > wrote:": the closing ">" of the address alone on a line,
            # where it reads as a quote mark and leaves a blank line.
            above = len(text) - 1
            lost_bracket = above > 0 and not text[above].strip() and text[above - 1].count("<") > text[above - 1].count(">")
            if lost_bracket:
                above -= 1
            if above >= 0 and wrapped_start(line, text[above]):
                line = f"{text[above].strip()}{'>' if lost_bracket else ''} {line}"
                del text[above:]
            emails.append((line.strip(), []))
        else:
            text.append(line)
    found = [(header, "\n".join(text).strip()) for header, text in emails]
    return [(header, text) for header, text in found if header or text]
