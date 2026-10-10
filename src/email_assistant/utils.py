from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import List, Any
from zoneinfo import ZoneInfo
import json
import re
import html2text

from email_assistant.quotes import quote_spans, quoted_emails

# The time zone a meeting is in when the model does not name one. The
# calendar preferences are written in UK time.
MEETING_TIMEZONE = "Europe/London"

# A plain date: 2026-09-16. The tool takes a date, so a full timestamp such
# as 2026-09-16T00:00:00Z means the model ignored the format.
PLAIN_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A local date and time with no Z and no offset: 2026-09-24T14:00:00. The
# Gmail tool reads start_time and end_time in the meeting's time zone, so a Z
# or an offset on them would move the meeting.
LOCAL_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")


def today_for_prompt(now: datetime | None = None) -> str:
    """Today and the time in UK time as "Monday 2026-09-21, 14:05", for the agent's prompt.

    Called for every email, not once at import: the old prompt read the clock
    when the server started, so a server left running past midnight kept
    yesterday's date. The day is taken in MEETING_TIMEZONE so a server clock
    on UTC does not change the date an hour late. The time is there because,
    without it, the agent booked 10:00 for an email that came in at 12:12
    (25 Sep).
    """
    zone = ZoneInfo(MEETING_TIMEZONE)
    now = datetime.now(zone) if now is None else now.astimezone(zone)
    return now.strftime("%A %Y-%m-%d, %H:%M")


def next_days_for_prompt(now: datetime | None = None, days: int = 14) -> str:
    """The days after today, each with its weekday, for the agent's prompt.

    "Saturday 2026-09-26, Sunday 2026-09-27, ...". The model counts days
    wrongly: asked for "this Saturday" on Friday 25 Sep 2026, it chose Sunday
    27 Sep. With this list it looks the date up instead of working it out.
    """
    zone = ZoneInfo(MEETING_TIMEZONE)
    now = datetime.now(zone) if now is None else now.astimezone(zone)
    today = now.date()
    return ", ".join(f"{today + timedelta(days=n):%A %Y-%m-%d}" for n in range(1, days + 1))


def dates_for_prompt(now: datetime | None = None) -> dict:
    """Both date gaps in the agent's prompt, {today} and {next_days}.

    One reading of the clock for both, so they cannot fall either side of
    midnight.
    """
    now = datetime.now(ZoneInfo(MEETING_TIMEZONE)) if now is None else now
    return {"today": today_for_prompt(now), "next_days": next_days_for_prompt(now)}


def format_meeting_day(preferred_day):
    """"2026-09-16", with a warning when it is not a plain YYYY-MM-DD date."""
    text = "not set" if preferred_day is None else str(preferred_day).strip()
    if not PLAIN_DATE.match(text):
        text += " ⚠️ not a YYYY-MM-DD date"
    return text


def format_meeting_span(start_time, end_time, timezone):
    """Day, time and duration of a Gmail meeting, each flagged when wrong.

    Returns ("2026-09-24 (Thursday)", "14:00 to 14:30 (Europe/London)",
    "30 minutes"). A value that is not a local
    YYYY-MM-DDTHH:MM:SS is shown exactly as written and flagged, for the same
    reason: Google would move or refuse the meeting at Accept.
    """
    zone = timezone or MEETING_TIMEZONE
    start = "not set" if start_time is None else str(start_time).strip()
    end = "not set" if end_time is None else str(end_time).strip()
    if not (LOCAL_TIMESTAMP.match(start) and LOCAL_TIMESTAMP.match(end)):
        return (format_meeting_day(start[:10]),
                f"{start} to {end} ({zone}) ⚠️ not a local YYYY-MM-DDTHH:MM:SS time",
                "unknown")
    begin, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
    until = f"{finish:%H:%M}" if finish.date() == begin.date() else f"{finish:%Y-%m-%d %H:%M}"
    minutes = int((finish - begin).total_seconds() // 60)
    duration = f"{minutes} minutes"
    if minutes <= 0:
        duration += " ⚠️ does not end after it starts"
    return f"{begin:%Y-%m-%d (%A)}", f"{begin:%H:%M} to {until} ({zone})", duration

def html_to_text(body):
    """An HTML email body as markdown text; any other body unchanged."""
    if body and (body.strip().startswith("<!DOCTYPE") or
                 body.strip().startswith("<html") or
                 "<body" in body):
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.body_width = 0  # Don't wrap text
        return h.handle(body)
    return body


# How much of each earlier email the model is given. The newest email is
# always given in full; these are background.
HISTORY_BODY_LIMIT = 1500


def without_quotes(body: str) -> str:
    """An email's own text: each quoted email in it cut out, with its "... wrote:" line.

    Each earlier email is in the history on its own, so its quotes would
    repeat the emails before it. The quotes are found by the shared rules
    (quotes.quote_spans). This had its own older rule, which cut everything
    under the first "On ... wrote:" line, an answer under the quote included.
    """
    lines = body.splitlines()
    own, at = [], 0
    for first, _, end in quote_spans(lines):
        own += lines[at:first]
        at = end
    return "\n".join(own + lines[at:]).strip()


def email_date(date: str | None) -> str | None:
    """An email's date in UK time, as "Monday 2026-09-28, 15:41 (UK time)".

    The ingester keeps each email's Date header as its mail program wrote it:
    "15:31:28 +0100" from the sender, and "10:41:37 -0400" on the mailbox's own
    replies, where Google's server writes the date. The model had to convert
    them to compare two emails (5 Oct). Now each one is in MEETING_TIMEZONE, in
    the form of the prompt's {today}. A date that cannot be used comes back as
    it is.
    """
    try:
        sent = parsedate_to_datetime(date)
    except (TypeError, ValueError, OverflowError):
        return date
    # "-0000" is UTC with no local zone given; Python returns it with no zone.
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    # A year far from now is a broken header. "31 Dec 9999" in a spam email
    # can be read but not converted to UK time: it would stop the triage node.
    if not 2000 <= sent.year <= 2100:
        return date
    return f"{today_for_prompt(sent)} (UK time)"


def history_body(mail: dict) -> str:
    """One earlier email as the model is given it: its own text, cut to HISTORY_BODY_LIMIT."""
    body = without_quotes(html_to_text(mail.get("body") or ""))
    if len(body) > HISTORY_BODY_LIMIT:
        body = body[:HISTORY_BODY_LIMIT].rstrip() + " [...]"
    return body


def format_history(email_input: dict) -> str:
    """The earlier emails of the conversation, oldest first, for the model.

    "" when there are none. The ingester puts up to 10 of them in
    email_input["history"] (run_ingest.conversation_history). Only the model
    reads this text: the Telegram card shows the newest email alone.
    """
    history = email_input.get("history") or []
    if not history:
        return ""
    parts = ["\n**Earlier emails in this conversation**, oldest first, for context only:"]
    for number, mail in enumerate(history, 1):
        parts.append(f"\n{number}. **From**: {mail.get('from')}\n**Date**: {email_date(mail.get('date'))}\n\n{history_body(mail)}\n")
    return "\n".join(parts)


def without_repeated_quote(body: str, email_input: dict) -> str:
    """The newest email without the quote at its end, when the history repeats it.

    A reply ends with the earlier emails, quoted by the sender's mail program,
    and format_history gives the model the same emails: it read them twice
    (5 Oct). The quote is cut only when each email in it is, word for word,
    one of the earlier emails as the model gets them. A match of single lines
    was not enough: "> ok" is in many emails. In every other case the body
    comes back unchanged, so no text is lost: no history, a quoted email that
    is not in the conversation or is older or longer than the history holds,
    answers written between or under the quoted lines, an HTML body, an email
    that is only the quote. Only the model's copy is cut; the Telegram card
    shows the whole email.
    """
    kept = (body or "").splitlines(keepends=True)
    lines = [line.rstrip("\r\n") for line in kept]
    spans = quote_spans(lines)
    # Only a quote at the end is a repeat: text under it is an answer to it.
    if not spans or any(line.strip() for line in lines[spans[-1][2]:]):
        return body
    first, start, end = spans[-1]
    held = {" ".join(history_body(mail).split()) for mail in email_input.get("history") or []}
    quoted = [" ".join(text.split()) for _, text in quoted_emails("", lines[start:end])]
    if not held or not all(text in held for text in quoted if text):
        return body
    # The model must have an email to answer, so one that is only the quote stays whole.
    return "".join(kept[:first]).rstrip() or body


def format_gmail_markdown(subject, author, to, email_thread, email_id=None, date=None):
    """Format Gmail email details into a nicely formatted markdown string for display,
    with HTML to text conversion for HTML content
    
    Args:
        subject: Email subject
        author: Email sender
        to: Email recipient
        email_thread: Email content (possibly HTML)
        email_id: Optional email ID (for Gmail API)
        date: Optional Date header of the email. Given for the model's copy
            only, where it becomes a **Date** line in UK time (email_date);
            the Telegram card has no such line.
    """
    id_section = f"\n**ID**: {email_id}" if email_id else ""
    date_section = f"\n**Date**: {email_date(date)}" if date else ""
    email_thread = html_to_text(email_thread)
    # One line each. The card reads these values back line by line
    # (inbox_render.header), and they are the sender's own text: a line break
    # in a subject must not start a line that looks like another header.
    subject, author, to = (re.sub(r"[\r\n]+", " ", str(value)) for value in (subject, author, to))

    return f"""

**Subject**: {subject}
**From**: {author}
**To**: {to}{date_section}{id_section}

{email_thread}

---
"""

def format_for_display(tool_call, current_event=None):
    """Format content for display in Agent Inbox

    Args:
        tool_call: The tool call to format
        current_event: For a move or a cancel, the event as it is now, as
            markdown lines (gmail_tools.event_card_lines). The tool call
            holds only the event's ID, which tells you nothing.
    """
    # Initialize empty display
    display = ""
    
    # Add tool call information
    if tool_call["name"] == "schedule_meeting_tool":
        args = tool_call["args"]
        day, when, duration = format_meeting_span(
            args.get("start_time"), args.get("end_time"), args.get("timezone"))
        display += f"""# Calendar Invite

**Meeting**: {args.get("title")}
**Attendees**: {', '.join(args.get("attendees", []))}
**Day**: {day}
**Time**: {when}
**Duration**: {duration}
"""
    elif tool_call["name"] == "update_meeting_tool":
        args = tool_call["args"]
        day, when, duration = format_meeting_span(
            args.get("start_time"), args.get("end_time"), args.get("timezone"))
        display += f"""# Move Meeting

{current_event or f"**Event ID**: {args.get('event_id')}"}
**New day**: {day}
**New time**: {when}
**Duration**: {duration}
"""
    elif tool_call["name"] == "cancel_meeting_tool":
        display += f"""# Cancel Meeting

{current_event or f"**Event ID**: {tool_call['args'].get('event_id')}"}
Google emails the attendees that it is cancelled.
"""
    elif tool_call["name"] == "send_email_tool":
        # The tool replies to the sender of the email being answered, so the
        # card names that person through the email shown above it.
        args = tool_call["args"]
        cc = args.get("additional_recipients")
        cc_line = f"**Cc**: {', '.join(cc)}\n" if cc else ""
        display += f"""# Email Reply

**To**: the sender of the email above
**From**: {args.get("email_address")}
{cc_line}
{args.get("response_text")}
"""
    elif tool_call["name"] == "Question":
        # Special formatting for questions to make them clear
        display += f"""# Question for User

{tool_call["args"].get("content")}
"""
    else:
        # Generic format for other tools
        display += f"""# Tool Call: {tool_call["name"]}

Arguments:"""
        
        # Check if args is a dictionary or string
        if isinstance(tool_call["args"], dict):
            display += f"\n{json.dumps(tool_call['args'], indent=2)}\n"
        else:
            display += f"\n{tool_call['args']}\n"
    return display

def parse_gmail(email_input: dict) -> tuple[str, str, str, str, str]:
    """Parse an email input dictionary for Gmail, including the email ID.
    
    It also returns the email ID,
    which is used specifically in the Gmail integration.

    Args:
        email_input (dict): Dictionary containing email fields in any of these formats:
            Gmail schema:
                - From: Sender's email
                - To: Recipient's email
                - Subject: Email subject line
                - Body: Full email content
                - Id: Gmail message ID
            
    Returns:
        tuple[str, str, str, str, str]: Tuple containing:
            - author: Sender's name and email
            - to: Recipient's name and email
            - subject: Email subject line
            - email_thread: Full email content
            - email_id: Email ID (or None if not available)

    It prints nothing. It used to print the whole input on every call, the
    history of up to 10 emails included, into the server log.
    """
    # Gmail schema
    return (
        email_input["from"],
        email_input["to"],
        email_input["subject"],
        email_input["body"],
        email_input["id"],
    )


def extract_message_content(message) -> str:
    """Extract content from different message types as clean string.
    
    Args:
        message: A message object (HumanMessage, AIMessage, ToolMessage)
        
    Returns:
        str: Extracted content as clean string
    """
    content = message.content
    
    # Check for recursion marker in string
    if isinstance(content, str) and '<Recursion on AIMessage with id=' in content:
        return "[Recursive content]"
    
    # Handle string content
    if isinstance(content, str):
        return content
        
    # Handle list content (AIMessage format)
    elif isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and 'text' in item:
                text_parts.append(item['text'])
        return "\n".join(text_parts)
    
    # Don't try to handle recursion to avoid infinite loops
    # Just return string representation instead
    return str(content)

def format_few_shot_examples(examples):
    """Format examples into a readable string representation.

    Args:
        examples (List[Item]): List of example items from the vector store, where each item
            contains a value string with the format:
            'Email: {...} Original routing: {...} Correct routing: {...}'

    Returns:
        str: A formatted string containing all examples, with each example formatted as:
            Example:
            Email: {email_details}
            Original Classification: {original_routing}
            Correct Classification: {correct_routing}
            ---
    """
    formatted = []
    for example in examples:
        # Parse the example value string into components
        email_part = example.value.split('Original routing:')[0].strip()
        original_routing = example.value.split('Original routing:')[1].split('Correct routing:')[0].strip()
        correct_routing = example.value.split('Correct routing:')[1].strip()
        
        # Format into clean string
        formatted_example = f"""Example:
Email: {email_part}
Original Classification: {original_routing}
Correct Classification: {correct_routing}
---"""
        formatted.append(formatted_example)
    
    return "\n".join(formatted)

def extract_tool_calls(messages: List[Any]) -> List[str]:
    """Extract tool call names from messages, safely handling messages without tool_calls."""
    tool_call_names = []
    for message in messages:
        # Check if message is a dict and has tool_calls
        if isinstance(message, dict) and message.get("tool_calls"):
            tool_call_names.extend([call["name"].lower() for call in message["tool_calls"]])
        # Check if message is an object with tool_calls attribute
        elif hasattr(message, "tool_calls") and message.tool_calls:
            tool_call_names.extend([call["name"].lower() for call in message.tool_calls])
    
    return tool_call_names

def format_messages_string(messages: List[Any]) -> str:
    """Format messages into a single string for analysis."""
    return '\n'.join(message.pretty_repr() for message in messages)

def show_graph(graph, xray=False):
    """Display a LangGraph mermaid diagram with fallback rendering.
    
    Handles timeout errors from mermaid.ink by falling back to pyppeteer.
    
    Args:
        graph: The LangGraph object that has a get_graph() method
    """
    from IPython.display import Image
    try:
        # Try the default renderer first
        return Image(graph.get_graph(xray=xray).draw_mermaid_png())
    except Exception as e:
        # Fall back to pyppeteer if the default renderer fails
        import nest_asyncio
        nest_asyncio.apply()
        from langchain_core.runnables.graph import MermaidDrawMethod
        return Image(graph.get_graph().draw_mermaid_png(draw_method=MermaidDrawMethod.PYPPETEER))