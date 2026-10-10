"""
Gmail tools implementation module. 
This module formats the Gmail API functions into LangChain tools.
"""

import os
import sys
import base64
import email.utils
import json
import logging
import socket
from datetime import date, datetime, time as clock_time, timedelta
from typing import List, Optional, Dict, Any
from pathlib import Path
from zoneinfo import ZoneInfo
from pydantic import Field, BaseModel
from langchain_core.tools import tool

from email_assistant.utils import MEETING_TIMEZONE, html_to_text

# The working day that free slots are counted in, in MEETING_TIMEZONE. Fixed
# for now; the calendar preferences say the same thing in words.
WORK_START = clock_time(9, 0)
WORK_END = clock_time(17, 0)

# Setup basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


GOOGLE_HOSTS = (".googleapis.com", ".google.com")


def prefer_ipv4():
    """Make connections to Google in this process try IPv4 addresses before IPv6 ones.

    Measured on one network, 24 Sep 2026: IPv6 to gmail.googleapis.com timed out
    every time, while IPv4 answered in 0.09s. The system lists the IPv6
    addresses first, and httplib2 (under the Google client) gives up on the
    first timeout instead of trying the next address, so every Gmail and
    Calendar call failed. Other hosts (OpenAI, LangSmith, the local server)
    keep the system's order. The IPv6 addresses stay at the end of the list,
    so a Google host with no IPv4 address still works.

    Set GOOGLE_ALLOW_IPV6=1 to go back to the default behaviour. It is read
    on every lookup, so it works whether .env is loaded before or after this
    module is imported.
    """
    if getattr(socket.getaddrinfo, "prefers_ipv4", False):
        return
    system_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        found = system_getaddrinfo(host, *args, **kwargs)
        if os.getenv("GOOGLE_ALLOW_IPV6") == "1" or not (isinstance(host, str) and host.endswith(GOOGLE_HOSTS)):
            return found
        return sorted(found, key=lambda info: info[0] != socket.AF_INET)

    getaddrinfo.prefers_ipv4 = True
    socket.getaddrinfo = getaddrinfo


prefer_ipv4()

# Define paths for credentials and tokens
_ROOT = Path(__file__).parent.absolute()
_SECRETS_DIR = _ROOT / ".secrets"

# The Google API libraries. Without them every tool returns a clear error.
# `logging` and `timedelta` are imported once, at the top of this file.
try:
    from googleapiclient.discovery import build
    from email.mime.text import MIMEText
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request

    # Function to get credentials from token.json or environment variables.
    # The only credential loader: run_ingest.py uses it too.
    def get_credentials(gmail_token=None, gmail_secret=None):
        """
        Get Gmail API credentials from token.json or environment variables.
        
        This function attempts to load credentials from multiple sources in this order:
        1. Directly passed gmail_token and gmail_secret parameters
        2. Environment variables GMAIL_TOKEN and GMAIL_SECRET
        3. Local files at token_path (.secrets/token.json) and secrets_path (.secrets/secrets.json)
        
        Args:
            gmail_token: Optional JSON string containing token data
            gmail_secret: Optional JSON string containing credentials
            
        Returns:
            Google OAuth2 Credentials object or None if credentials can't be loaded
        """
        token_path = _SECRETS_DIR / "token.json"
        token_data = None
        
        # Try to get token data from various sources
        if gmail_token:
            # 1. Use directly passed token parameter if available
            try:
                token_data = json.loads(gmail_token) if isinstance(gmail_token, str) else gmail_token
                logger.info("Using directly provided gmail_token parameter")
            except Exception as e:
                logger.warning(f"Could not parse provided gmail_token: {str(e)}")
                
        if token_data is None:
            # 2. Try environment variable
            env_token = os.getenv("GMAIL_TOKEN")
            if env_token:
                try:
                    token_data = json.loads(env_token)
                    logger.info("Using GMAIL_TOKEN environment variable")
                except Exception as e:
                    logger.warning(f"Could not parse GMAIL_TOKEN environment variable: {str(e)}")
        
        if token_data is None:
            # 3. Try local file
            if os.path.exists(token_path):
                try:
                    with open(token_path, "r") as f:
                        token_data = json.load(f)
                    logger.info(f"Using token from {token_path}")
                except Exception as e:
                    logger.warning(f"Could not load token from {token_path}: {str(e)}")
        
        # If we couldn't get token data from any source, return None
        if token_data is None:
            logger.error("Could not find valid token data in any location")
            return None
        
        try:
            # Create credentials object with specific format. These are
            # google.auth credentials, so the Google client never calls an
            # `authorize` method on them (googleapiclient/_auth.py); the fake
            # one that used to be added here did nothing.
            return Credentials(
                token=token_data.get("token"),
                refresh_token=token_data.get("refresh_token"),
                token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
                client_id=token_data.get("client_id"),
                client_secret=token_data.get("client_secret"),
                scopes=token_data.get("scopes", ["https://www.googleapis.com/auth/gmail.modify"])
            )
        except Exception as e:
            logger.error(f"Error creating credentials object: {str(e)}")
            return None

    # Type alias for better readability
    EmailData = Dict[str, Any]

    GMAIL_API_AVAILABLE = True

except ImportError:
    GMAIL_API_AVAILABLE = False


def google_service(api: str, version: str):
    """A Google API client for the signed-in mailbox: ("gmail", "v1") or ("calendar", "v3").

    Every tool here builds its client through this one function.
    """
    if not GMAIL_API_AVAILABLE:
        raise RuntimeError("the Google API libraries are not installed")
    creds = get_credentials(
        gmail_token=os.getenv("GMAIL_TOKEN"),
        gmail_secret=os.getenv("GMAIL_SECRET")
    )
    return build(api, version, credentials=creds)


def gmail_service():
    """A Gmail client for the signed-in mailbox."""
    return google_service("gmail", "v1")


def calendar_service():
    """A Google Calendar client for the signed-in mailbox."""
    return google_service("calendar", "v3")


def extract_message_part(payload):
    """The text of a Gmail message payload: text/plain first, else text/html."""
    # If this is multipart, process with preference for text/plain
    if payload.get("parts"):
        # First try to find text/plain part
        for part in payload["parts"]:
            mime_type = part.get("mimeType", "")
            if mime_type == "text/plain" and part.get("body", {}).get("data"):
                data = part["body"]["data"]
                return base64.urlsafe_b64decode(data).decode("utf-8")

        # If no text/plain found, try text/html
        for part in payload["parts"]:
            mime_type = part.get("mimeType", "")
            if mime_type == "text/html" and part.get("body", {}).get("data"):
                data = part["body"]["data"]
                return base64.urlsafe_b64decode(data).decode("utf-8")

        # If we still haven't found content, recursively check for nested parts
        for part in payload["parts"]:
            content = extract_message_part(part)
            if content:
                return content

    # Not multipart, try to get content directly
    if payload.get("body", {}).get("data"):
        data = payload["body"]["data"]
        return base64.urlsafe_b64decode(data).decode("utf-8")

    return ""


def quote_original(headers: dict, body: str) -> str:
    """The email being answered, quoted the way mail programs do it.

    "On <date>, <sender> wrote:", then each line with "> " in front. `headers`
    has lower-case names. The agent's replies used to quote nothing, so the
    other person's answer quoted only the agent's last email, and the agent
    could not see what had been said before.
    """
    text = html_to_text(body or "").strip()
    if not text:
        return ""
    quoted = "\n".join(f"> {line}" if line else ">" for line in text.splitlines())
    when = headers.get("date")
    intro = f"On {when}, {headers.get('from', 'the sender')} wrote:" if when else f"{headers.get('from', 'The sender')} wrote:"
    return f"{intro}\n{quoted}"


class SendEmailInput(BaseModel):
    """
    Input schema for the send_email_tool.
    """
    email_id: str = Field(
        description="Gmail message ID of the email you are replying to - the ID shown with the email, for example 199a3f5e2b7c4d10."
    )
    response_text: str = Field(
        description="Content of the reply, for example 'Hi Sarah, Thursday at 14:00 works for me.'"
    )
    email_address: str = Field(
        description="Your own address - the one the email was sent to, for example you@example.com."
    )
    additional_recipients: Optional[List[str]] = Field(
        default=None,
        description="Optional extra addresses to copy in, for example ['colleague@example.com']."
    )

def send_email(
    email_id: str,
    response_text: str,
    email_address: str,
    addn_receipients: Optional[List[str]] = None
) -> str:
    """
    Reply to an email in its own Gmail thread. The reply goes to its sender.

    Args:
        email_id: Gmail message ID of the email being answered
        response_text: Content of the reply
        email_address: Current user's email address (the sender)
        addn_receipients: Optional addresses to copy in

    Returns:
        The Gmail ID of the sent reply.

    Raises:
        Any error from Google, or RuntimeError when the Google libraries are
        missing. Nothing is sent then. The old version sent the reply to
        recipient@example.com when it could not read the original email, and
        reported success when the libraries were missing.
    """
    service = gmail_service()

    # The reply is built from the original: its sender, subject, thread and
    # text. "full" gives the text too, so the reply can quote it.
    original = service.users().messages().get(userId="me", id=email_id, format="full").execute()
    headers = {h["name"].lower(): h["value"] for h in original["payload"]["headers"]}

    subject = headers.get("subject", "")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    quote = quote_original(headers, extract_message_part(original["payload"]))
    msg = MIMEText(f"{response_text}\n\n{quote}" if quote else response_text)
    # Header names are written exactly as given here. They were lower case
    # ("to:"), and on 22 Sep Gmail put "Be careful with this message" on the
    # mailbox's own sent copy; that is the most likely cause, not proven.
    msg["To"] = headers["from"]
    msg["From"] = email_address
    msg["Subject"] = subject
    if addn_receipients:
        msg["Cc"] = ", ".join(addn_receipients)
    # threadId (below) files the reply in this mailbox's conversation. These
    # two headers do the same in the other person's mail program.
    if headers.get("message-id"):
        msg["In-Reply-To"] = headers["message-id"]
        msg["References"] = " ".join(filter(None, [headers.get("references"), headers["message-id"]]))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    sent = (
        service.users()
        .messages()
        .send(userId="me", body={"raw": raw, "threadId": original["threadId"]})
        .execute()
    )
    logger.info(f"Email sent: Message ID {sent['id']}")
    return sent["id"]

@tool(args_schema=SendEmailInput)
def send_email_tool(
    email_id: str,
    response_text: str,
    email_address: str,
    additional_recipients: Optional[List[str]] = None
) -> str:
    """
    Reply to an email in its Gmail thread. The reply goes to the email's sender.

    Args:
        email_id: Gmail message ID of the email you are replying to
        response_text: Content of the reply
        email_address: Current user's email address (the sender)
        additional_recipients: Optional addresses to copy in

    Returns:
        Confirmation message, or the reason nothing was sent
    """
    try:
        send_email(
            email_id,
            response_text,
            email_address,
            addn_receipients=additional_recipients
        )
    except Exception as e:
        logger.error(f"Error sending email: {str(e)}")
        return f"Failed to send email: {str(e)}"
    return f"Email reply sent successfully to message ID: {email_id}"

class CheckCalendarInput(BaseModel):
    """
    Input schema for the check_calendar_tool.
    """
    dates: List[str] = Field(
        description="Dates to check, each as DD-MM-YYYY, for example ['16-09-2026', '17-09-2026']."
    )

def parse_calendar_date(text: str) -> date:
    """A day to check: DD-MM-YYYY, as the tool asks, or YYYY-MM-DD."""
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{text!r} is not a DD-MM-YYYY date")


def free_slots(busy, window_start: datetime, window_end: datetime):
    """The free (start, end) gaps inside a window, given busy (start, end) pairs.

    All times must carry a time zone. Busy time is cut to the window first,
    so an event at 18:00 does not open a free slot after the working day.
    """
    slots, current = [], window_start
    for start, end in sorted(busy):
        start = min(max(start, window_start), window_end)
        end = min(max(end, window_start), window_end)
        if end <= start:
            continue
        if start > current:
            slots.append((current, start))
        current = max(current, end)
    if current < window_end:
        slots.append((current, window_end))
    return slots


def _event_time(value: str, zone: ZoneInfo) -> datetime:
    """A Calendar dateTime such as 2026-09-24T14:00:00+01:00 or ...Z, in `zone`."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(zone)


def _event_details(event: dict) -> str:
    """ " (id: abc123; with r@x.com)" for one line of the calendar check.

    The ID is what update_meeting_tool and cancel_meeting_tool take. Asked to
    reschedule on 22 Sep, the agent could only create a second event, because
    nothing told it which event to change.
    """
    others = [a["email"] for a in event.get("attendees", []) if a.get("email") and not a.get("self")]
    with_whom = f"; with {', '.join(others)}" if others else ""
    return f" (id: {event.get('id', 'unknown')}{with_whom})"


def _next_half_hour(moment: datetime) -> datetime:
    """The first :00 or :30 at or after `moment`."""
    start = moment.replace(minute=0, second=0, microsecond=0)
    while start < moment:
        start += timedelta(minutes=30)
    return start


def describe_calendar_days(service, dates: List[str], now: Optional[datetime] = None) -> str:
    """Each day's events and free working time, in MEETING_TIMEZONE.

    `service` is a Calendar API client. A day runs from midnight to midnight
    UK time. The old limits were midnight UTC, so a summer day started at
    01:00. Every event time is converted to the UK zone before it is compared
    with the working day. The old code compared a zone-aware event time with a
    naive 09:00, which raised TypeError, and the error path then returned
    made-up events.

    `now` is the clock by default; tests pass a fixed time. Each day is named
    ("Events for Sunday 27-09-2026"), so the model can see which weekday a
    date is. A day before today is not looked up. On today, free time starts
    at the next half hour: on 25 Sep the whole of 09:00 - 17:00 was offered,
    and the agent booked 10:00 for an email that came in at 12:12.
    """
    zone = ZoneInfo(MEETING_TIMEZONE)
    now = datetime.now(zone) if now is None else now.astimezone(zone)
    result = f"Calendar events (times in {MEETING_TIMEZONE}):\n\n"
    for date_str in dates:
        day = parse_calendar_date(date_str)
        result += f"Events for {day:%A} {date_str}:\n"
        if day < now.date():
            result += "  This day has passed. Do not schedule on it.\n\n"
            continue
        events = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=datetime.combine(day, clock_time(0), zone).isoformat(),
                timeMax=datetime.combine(day + timedelta(days=1), clock_time(0), zone).isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
            .get("items", [])
        )

        if not events:
            result += "  No events found for this day\n"
        busy, busy_all_day = [], False
        for event in events:
            title = event.get("summary", "(no title)")
            # "transparent" is Google's "show me as free": the event is
            # listed, but it does not block time.
            blocks = event.get("transparency") != "transparent"
            if "dateTime" in event["start"]:
                start = _event_time(event["start"]["dateTime"], zone)
                end = _event_time(event["end"]["dateTime"], zone)
                result += f"  - {start:%H:%M} - {end:%H:%M}: {title}{_event_details(event)}\n"
                if blocks:
                    busy.append((start, end))
            else:
                result += f"  - All day: {title}{_event_details(event)}\n"
                busy_all_day = busy_all_day or blocks

        work_start = datetime.combine(day, WORK_START, zone)
        work_end = datetime.combine(day, WORK_END, zone)
        if day == now.date():
            result += f"  It is {now:%H:%M} now; earlier times today have passed.\n"
            work_start = max(work_start, _next_half_hour(now))
        slots = [] if busy_all_day or work_start >= work_end else free_slots(busy, work_start, work_end)
        if slots:
            result += "  Available: " + ", ".join(f"{s:%H:%M} - {e:%H:%M}" for s, e in slots) + "\n\n"
        elif work_start >= work_end:
            result += "  Available: none, the working day is over\n\n"
        else:
            result += f"  Available: none between {WORK_START:%H:%M} and {WORK_END:%H:%M}\n\n"
    return result


def get_calendar_events(dates: List[str]) -> str:
    """
    Check Google Calendar for events on specified dates.

    Args:
        dates: List of dates to check in DD-MM-YYYY format

    Returns:
        Each day's events and its free time between 09:00 and 17:00 UK time,
        or a text that starts "Calendar check failed". It never returns
        made-up events: the old fallback looked exactly like a real calendar,
        so the agent could book a slot that was not free.
    """
    try:
        return describe_calendar_days(calendar_service(), dates)
    except Exception as e:
        logger.error(f"Error checking calendar: {str(e)}")
        return (f"Calendar check failed: {str(e)}. No free time is known; "
                "do not assume any, ask the user instead.")

@tool(args_schema=CheckCalendarInput)
def check_calendar_tool(dates: List[str]) -> str:
    """
    Check Google Calendar for events on specified dates.
    
    Args:
        dates: List of dates to check in DD-MM-YYYY format
        
    Returns:
        Formatted calendar events for the specified dates
    """
    try:
        events = get_calendar_events(dates)
        return events
    except Exception as e:
        return f"Failed to check calendar: {str(e)}"

class ScheduleMeetingInput(BaseModel):
    """
    Input schema for the schedule_meeting_tool.

    Every field says what shape of value is expected and gives an example -
    these descriptions are what the model reads. Google Calendar reads
    start_time and end_time in `timezone`, so a Z or an offset on them would
    silently move the meeting.
    """
    attendees: List[str] = Field(
        description="Email addresses of everyone invited, for example ['sarah.chen@brightpath.example']."
    )
    title: str = Field(
        description="Title of the meeting, for example 'Automation pilot scope walkthrough'."
    )
    start_time: str = Field(
        description="Local start as YYYY-MM-DDTHH:MM:SS, for example 2026-09-16T14:00:00. No Z and no offset - it is read in timezone."
    )
    end_time: str = Field(
        description="Local end in the same format, for example 2026-09-16T14:45:00. No Z and no offset."
    )
    organizer_email: str = Field(
        description="The organiser's own address - the one the incoming email was sent to, for example you@example.com."
    )
    timezone: str = Field(
        default=MEETING_TIMEZONE,
        description=f"IANA time zone that start_time and end_time are in, for example {MEETING_TIMEZONE}."
    )

def send_calendar_invite(
    attendees: List[str],
    title: str,
    start_time: str,
    end_time: str,
    organizer_email: str,
    timezone: str = MEETING_TIMEZONE
) -> str:
    """
    Create the meeting in Google Calendar and email the invites.

    Args:
        attendees: Email addresses of meeting attendees
        title: Meeting title/subject
        start_time: Meeting start time in ISO format (YYYY-MM-DDTHH:MM:SS)
        end_time: Meeting end time in ISO format (YYYY-MM-DDTHH:MM:SS)
        organizer_email: Email address of the meeting organizer
        timezone: Timezone for the meeting

    Returns:
        The event's link in Google Calendar.

    Raises:
        Any error from Google, or RuntimeError when the Google libraries are
        missing. No event is made then.
    """
    service = calendar_service()

    event = {
        "summary": title,
        "start": {
            "dateTime": start_time,
            "timeZone": timezone,
        },
        "end": {
            "dateTime": end_time,
            "timeZone": timezone,
        },
        "attendees": [{"email": email} for email in attendees],
        "organizer": {
            "email": organizer_email,
            "self": True,
        },
        "reminders": {
            "useDefault": True,
        },
    }

    # sendUpdates is a parameter of insert(), not a field of the event. It used
    # to sit inside the event, where Google ignores it, so no invite was
    # emailed (Google's default is to send none).
    created = service.events().insert(calendarId="primary", body=event, sendUpdates="all").execute()
    logger.info(f"Meeting created: {created.get('htmlLink')}")
    return created.get("htmlLink", "")

@tool(args_schema=ScheduleMeetingInput)
def schedule_meeting_tool(
    attendees: List[str],
    title: str,
    start_time: str,
    end_time: str,
    organizer_email: str,
    timezone: str = MEETING_TIMEZONE
) -> str:
    """
    Schedule a meeting with Google Calendar and send invites.

    Args:
        attendees: Email addresses of meeting attendees
        title: Meeting title/subject
        start_time: Local start in ISO format (YYYY-MM-DDTHH:MM:SS), read in timezone
        end_time: Local end in ISO format (YYYY-MM-DDTHH:MM:SS), read in timezone
        organizer_email: Email address of the meeting organizer
        timezone: IANA time zone of start_time and end_time (default: Europe/London)

    Returns:
        Success message, or the reason no meeting was made
    """
    try:
        send_calendar_invite(
            attendees,
            title,
            start_time,
            end_time,
            organizer_email,
            timezone
        )
    except Exception as e:
        logger.error(f"Error scheduling meeting: {str(e)}")
        return f"Failed to schedule meeting: {str(e)}"
    return (f"Meeting '{title}' scheduled successfully from {start_time} to {end_time} "
            f"({timezone}) with {len(attendees)} attendees; invites sent")


def describe_event(event: dict) -> str:
    """One event as markdown lines for a Move or Cancel card, in MEETING_TIMEZONE."""
    zone = ZoneInfo(MEETING_TIMEZONE)
    lines = [f"**Meeting**: {event.get('summary', '(no title)')}"]
    start, end = event.get("start", {}), event.get("end", {})
    if "dateTime" in start:
        begin, finish = _event_time(start["dateTime"], zone), _event_time(end["dateTime"], zone)
        lines.append(f"**Now**: {begin:%Y-%m-%d (%A)}, {begin:%H:%M} to {finish:%H:%M} ({MEETING_TIMEZONE})")
    else:
        lines.append(f"**Now**: {start.get('date')}, all day")
    others = [a["email"] for a in event.get("attendees", []) if a.get("email") and not a.get("self")]
    if others:
        lines.append(f"**Attendees**: {', '.join(others)}")
    if event.get("status") == "cancelled":
        lines.append("⚠️ This event is already cancelled.")
    return "\n".join(lines)


def event_card_lines(event_id: str) -> str:
    """The event as it is now, read from Google, for a Move or Cancel card.

    Never raises: a card must still be shown, so a failed read is shown on it.
    The graph calls this in its own node (read_event_cards), before the stop,
    so the resume after your answer does not read Google again.
    """
    try:
        event = calendar_service().events().get(calendarId="primary", eventId=event_id).execute()
        return describe_event(event)
    except Exception as e:
        logger.error(f"Error reading event {event_id}: {str(e)}")
        return f"**Event ID**: {event_id} ⚠️ could not be read: {str(e)}"


class UpdateMeetingInput(BaseModel):
    """
    Input schema for the update_meeting_tool. Times as in ScheduleMeetingInput.
    """
    event_id: str = Field(
        description="ID of the existing event, as the calendar check lists it after 'id:', for example 5q8m2b1c3d4e6f7g8h9i0j1k2l."
    )
    start_time: str = Field(
        description="New local start as YYYY-MM-DDTHH:MM:SS, for example 2026-09-28T11:00:00. No Z and no offset - it is read in timezone."
    )
    end_time: str = Field(
        description="New local end in the same format, for example 2026-09-28T11:30:00. No Z and no offset."
    )
    timezone: str = Field(
        default=MEETING_TIMEZONE,
        description=f"IANA time zone that start_time and end_time are in, for example {MEETING_TIMEZONE}."
    )


def move_calendar_event(event_id: str, start_time: str, end_time: str, timezone: str = MEETING_TIMEZONE) -> dict:
    """Move an event to a new time and email the attendees the change.

    patch() changes only the start and the end; title, attendees and the
    rest stay. Returns the updated event. Raises any error from Google - for
    example when the event does not exist, or when someone else organises it.
    """
    body = {
        "start": {"dateTime": start_time, "timeZone": timezone},
        "end": {"dateTime": end_time, "timeZone": timezone},
    }
    return (calendar_service().events()
            .patch(calendarId="primary", eventId=event_id, body=body, sendUpdates="all")
            .execute())


@tool(args_schema=UpdateMeetingInput)
def update_meeting_tool(
    event_id: str,
    start_time: str,
    end_time: str,
    timezone: str = MEETING_TIMEZONE
) -> str:
    """
    Move an existing meeting in Google Calendar to a new time and email the attendees the change.

    Args:
        event_id: ID of the event, from the calendar check
        start_time: New local start (YYYY-MM-DDTHH:MM:SS), read in timezone
        end_time: New local end (YYYY-MM-DDTHH:MM:SS), read in timezone
        timezone: IANA time zone of start_time and end_time (default: Europe/London)

    Returns:
        Success message, or the reason the meeting was not moved
    """
    try:
        moved = move_calendar_event(event_id, start_time, end_time, timezone)
    except Exception as e:
        logger.error(f"Error moving meeting: {str(e)}")
        return f"Failed to move meeting: {str(e)}"
    return (f"Meeting '{moved.get('summary', event_id)}' moved successfully to {start_time} - {end_time} "
            f"({timezone}); attendees notified")


class CancelMeetingInput(BaseModel):
    """
    Input schema for the cancel_meeting_tool.
    """
    event_id: str = Field(
        description="ID of the existing event, as the calendar check lists it after 'id:', for example 5q8m2b1c3d4e6f7g8h9i0j1k2l."
    )


def cancel_calendar_event(event_id: str) -> None:
    """Delete an event and email the attendees that it is cancelled.

    Raises any error from Google; nothing is deleted then.
    """
    (calendar_service().events()
     .delete(calendarId="primary", eventId=event_id, sendUpdates="all")
     .execute())


@tool(args_schema=CancelMeetingInput)
def cancel_meeting_tool(event_id: str) -> str:
    """
    Cancel an existing meeting in Google Calendar and email the attendees.

    Args:
        event_id: ID of the event, from the calendar check

    Returns:
        Success message, or the reason the meeting was not cancelled
    """
    try:
        cancel_calendar_event(event_id)
    except Exception as e:
        logger.error(f"Error cancelling meeting: {str(e)}")
        return f"Failed to cancel meeting: {str(e)}"
    return f"Meeting {event_id} cancelled successfully; attendees notified"


def mark_as_read(message_id) -> bool:
    """Remove the UNREAD label from one email. True when it worked.

    Never raises. It runs after Done, when the reply and the meeting are
    already sent, so a failure here (an expired sign-in, no network) is
    logged and the run still ends normally. It used to raise, and the
    Telegram bot then said "The run failed" about an email that was answered.
    """
    try:
        gmail_service().users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
    except Exception as e:
        logger.error(f"Error marking email {message_id} as read: {str(e)}")
        return False
    return True