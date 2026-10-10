#!/usr/bin/env python
"""
Gmail ingestion: reads new email from Gmail and starts one LangGraph run for each.

Run it by hand for a one-off check, or let the cron job (setup_cron.py) run it
on a schedule.
"""

import uuid
import hashlib
import asyncio
import argparse
import httpx
from datetime import datetime
from googleapiclient.discovery import build
from langgraph_sdk import get_client
from dotenv import load_dotenv

from email_assistant.cleanup import removed_email_ids
from email_assistant.tools.gmail.gmail_tools import extract_message_part, get_credentials, prefer_ipv4

load_dotenv()
prefer_ipv4()

# The Gmail sign-in comes from gmail_tools.get_credentials, the same loader
# the agent's tools use: the GMAIL_TOKEN setting, else .secrets/token.json.
# This file had its own copy, with different logic.

def extract_email_data(message):
    """Extract key information from a Gmail message."""
    headers = message['payload']['headers']
    
    # Extract key headers
    subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
    from_email = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
    to_email = next((h['value'] for h in headers if h['name'] == 'To'), 'Unknown Recipient')
    date = next((h['value'] for h in headers if h['name'] == 'Date'), 'Unknown Date')
    
    # Extract message content
    content = extract_message_part(message['payload'])
    
    # Create email data object
    email_data = {
        "from_email": from_email,
        "to_email": to_email,
        "subject": subject,
        "page_content": content,
        "id": message['id'],
        "thread_id": message['threadId'],
        "send_time": date
    }
    
    return email_data


def sent_by_this_mailbox(message) -> bool:
    """True for an email this mailbox sent: a reply of the agent's, or one of yours.

    Gmail puts the SENT label on every email the account sends, the ones the
    agent sends through the API included. Such an email is never new mail to
    answer. The search used to ask for mail "from:" the mailbox too, so each
    run after a reply fed the agent its own reply: triage ignored most of them,
    and sent one that said "scheduled" to Telegram as a notify (28 Sep).
    What the mailbox sent still reaches the agent as an earlier email of the
    conversation: conversation_history reads the whole conversation.
    """
    return "SENT" in message.get("labelIds", [])


# How many earlier emails of the Gmail conversation the agent is given.
HISTORY_EMAILS = 10


def conversation_history(service, message, limit=HISTORY_EMAILS):
    """Up to `limit` emails before `message` in its Gmail conversation, oldest first.

    Each is {from, to, date, body}. Drafts are left out. The agent used to see
    only the newest email, so "Can we make it 15:30?" arrived with nothing to
    say which meeting "it" was. A failed read returns [] - the email itself
    still goes to the agent.
    """
    try:
        thread = service.users().threads().get(userId="me", id=message["threadId"]).execute()
    except Exception as e:
        print(f"Could not read the conversation {message['threadId']}: {str(e)}")
        return []
    sent_at = int(message.get("internalDate", 0))
    earlier = [
        m for m in thread.get("messages", [])
        if m["id"] != message["id"]
        and "DRAFT" not in m.get("labelIds", [])
        and int(m.get("internalDate", 0)) <= sent_at
    ]
    earlier.sort(key=lambda m: int(m.get("internalDate", 0)))
    history = []
    for m in earlier[-limit:]:
        data = extract_email_data(m)
        history.append({"from": data["from_email"], "to": data["to_email"],
                        "date": data["send_time"], "body": data["page_content"]})
    return history


def thread_id_for(message_id: str) -> str:
    """The LangGraph thread for one Gmail message: the same email, the same thread.

    Keyed on the message, not on Gmail's conversation (threadId). A reply in a
    conversation is a new email, so it gets its own thread. When the key was
    the conversation, a reply was added to a finished run's messages, and the
    model call failed.
    """
    return str(uuid.UUID(hex=hashlib.md5(message_id.encode("UTF-8")).hexdigest()))


async def ingest_email_to_langgraph(email_data, graph_name, url="http://127.0.0.1:2024", rerun=False):
    """Start one run for one email.

    Returns (thread_id, run), or (thread_id, None) when the email is skipped.
    An email whose thread already exists was ingested before - it is waiting
    in Telegram, answered or ignored - so it is skipped. With `rerun` it gets
    a new thread instead; nothing is deleted.
    """
    client = get_client(url=url)
    thread_id = thread_id_for(email_data["id"])

    try:
        await client.threads.get(thread_id)
        exists = True
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
        exists = False

    if exists and not rerun:
        print(f"Already processed (thread {thread_id}), skipped. Use --rerun to process it again.")
        return thread_id, None
    # A finished email leaves the server after a day (cleanup.py) and keeps a
    # line in a list. Without this check, a search wider than a day would
    # start it again. Read off the event loop: the dev server refuses a
    # blocking call on it.
    if not exists and not rerun and email_data["id"] in await asyncio.to_thread(removed_email_ids):
        print(f"Already processed, then removed from the server by the clean-up job (email {email_data['id']}), skipped. Use --rerun to process it again.")
        return thread_id, None
    if exists:
        thread_id = str(uuid.uuid4())
        print(f"--rerun: processing it again in a new thread {thread_id}")

    await client.threads.create(
        thread_id=thread_id,
        metadata={"email_id": email_data["id"], "gmail_thread_id": email_data["thread_id"]},
    )

    print(f"Creating run for thread {thread_id} with graph {graph_name}")
    run = await client.runs.create(
        thread_id,
        graph_name,
        input={"email_input": {
            "from": email_data["from_email"],
            "to": email_data["to_email"],
            "subject": email_data["subject"],
            "body": email_data["page_content"],
            "id": email_data["id"],
            # When it was sent, for the **Date** line of the model's copy.
            "date": email_data.get("send_time"),
            "history": email_data.get("history") or [],
        }},
    )

    print(f"Run created successfully with thread ID: {thread_id}")
    return thread_id, run

def read_new_emails(args):
    """The Gmail part of one ingest: the emails to answer, each with its earlier emails.

    Returns (emails, found, sent_count): the emails to ingest, how many the
    search found, and how many of those this mailbox sent. None when the Gmail
    sign-in cannot be loaded. Plain blocking code with no await in it, so
    fetch_and_process_emails can run all of it in a worker thread.
    """
    # Load Gmail credentials
    credentials = get_credentials()
    if not credentials:
        return None

    # Build Gmail service
    service = build("gmail", "v1", credentials=credentials)

    # Construct Gmail search query: mail sent TO the mailbox. What it sent
    # itself is never an email to answer (sent_by_this_mailbox).
    query = f"to:{args.email}"

    # Add time constraint if specified
    if args.minutes_since > 0:
        # Calculate timestamp for filtering
        from datetime import timedelta
        after = int((datetime.now() - timedelta(minutes=args.minutes_since)).timestamp())
        query += f" after:{after}"

    # Only include unread emails unless include_read is True
    if not args.include_read:
        query += " is:unread"

    print(f"Gmail search query: {query}")

    # Execute the search
    results = service.users().messages().list(userId="me", q=query).execute()
    messages = results.get("messages", [])
    if messages:
        print(f"Found {len(messages)} emails")

    emails = []
    sent_count = 0
    for i, message_info in enumerate(messages):
        # Stop early if requested
        if args.early and i > 0:
            print(f"Early stop after reading {i} emails")
            break

        # Get the full message
        message = service.users().messages().get(userId="me", id=message_info["id"]).execute()

        # An email to yourself also matches "to:", so the label decides.
        if sent_by_this_mailbox(message):
            print(f"Skipped email {i+1}/{len(messages)}: sent by this mailbox")
            sent_count += 1
            continue

        # Extract email data, and the emails before it in its conversation
        email_data = extract_email_data(message)
        email_data["history"] = conversation_history(service, message)
        emails.append(email_data)

    return emails, len(messages), sent_count


async def fetch_and_process_emails(args):
    """Fetch emails from Gmail and process them through LangGraph."""
    processed_count = 0
    skipped_count = 0

    try:
        # The Google client blocks while it waits for Google. In the cron
        # graph this function runs on the LangGraph server's event loop, where
        # a blocking call holds up every other run, and `langgraph dev`
        # refuses it: "Blocking call to socket.socket.connect" stopped every
        # cron run (6 Oct). So the Gmail part runs in a worker thread.
        read = await asyncio.to_thread(read_new_emails, args)
        if read is None:
            print("Failed to load Gmail credentials")
            return 1
        emails, found, sent_count = read

        if not found:
            print("No emails found matching the criteria")
            return 0

        for number, email_data in enumerate(emails, 1):
            print(f"\nProcessing email {number}/{len(emails)}:")
            print(f"From: {email_data['from_email']}")
            print(f"Subject: {email_data['subject']}")

            # Ingest to LangGraph. An email that was ingested before is
            # skipped, unless --rerun is given.
            thread_id, run = await ingest_email_to_langgraph(
                email_data,
                args.graph_name,
                url=args.url,
                rerun=args.rerun,
            )

            if run is None:
                skipped_count += 1
            else:
                processed_count += 1

        print(f"\nProcessed {processed_count} emails, skipped {skipped_count} already processed "
              f"and {sent_count} sent by this mailbox")
        return 0

    except Exception as e:
        print(f"Error processing emails: {str(e)}")
        return 1

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Simple Gmail ingestion for LangGraph with reliable tracing")
    
    parser.add_argument(
        "--email", 
        type=str, 
        required=True,
        help="Email address to fetch messages for"
    )
    parser.add_argument(
        "--minutes-since", 
        type=int, 
        default=120,
        help="Only retrieve emails newer than this many minutes"
    )
    parser.add_argument(
        "--graph-name", 
        type=str, 
        default="email_assistant_hitl_memory_gmail",
        help="Name of the LangGraph to use"
    )
    parser.add_argument(
        "--url", 
        type=str, 
        default="http://127.0.0.1:2024",
        help="URL of the LangGraph deployment"
    )
    parser.add_argument(
        "--early", 
        action="store_true",
        help="Early stop after processing one email"
    )
    parser.add_argument(
        "--include-read",
        action="store_true",
        help="Include emails that have already been read"
    )
    parser.add_argument(
        "--rerun", 
        action="store_true",
        help="Process the same emails again even if already processed"
    )
    parser.add_argument(
        "--skip-filters",
        action="store_true",
        help="Skip filtering of emails"
    )
    return parser.parse_args()

if __name__ == "__main__":
    # Get command line arguments
    args = parse_args()
    
    # Run the script
    exit(asyncio.run(fetch_and_process_emails(args)))