#!/usr/bin/env python
"""
Setup cron job for email ingestion in LangGraph.

This script creates a scheduled cron job in LangGraph that periodically
runs the email ingestion graph to process new emails.

There is one cron job at a time: a second run of this script replaces the
job (a new schedule, a new mailbox), and --delete removes it. The server
keeps the job in its saved state, so it is still there after a restart.
"""

import argparse
import asyncio
from typing import Optional
from langgraph_sdk import get_client
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# The graph that runs the ingester (langgraph.json).
CRON_GRAPH = "cron"
DEFAULT_URL = "http://127.0.0.1:2024"

async def main(
    email: Optional[str] = None,
    url: Optional[str] = None,
    minutes_since: int = 60,
    schedule: str = "*/10 * * * *",
    graph_name: str = "email_assistant_hitl_memory_gmail",
    include_read: bool = False,
    delete: bool = False,
):
    """Set up a cron job for email ingestion, or with `delete` only remove it"""
    # Connect to LangGraph server
    url = url or DEFAULT_URL
    client = get_client(url=url)

    # Remove the job that is there. Two jobs would read Gmail twice on every
    # tick, and nothing else can stop a job: it is in the server's saved state.
    # The search takes the graph's assistant ID only: the server answers a
    # graph name with "Invalid assistant ID: must be a UUID" (langgraph-api 0.14.3).
    [assistant] = await client.assistants.search(graph_id=CRON_GRAPH, limit=1)
    for old in await client.crons.search(assistant_id=assistant["assistant_id"], limit=100):
        await client.crons.delete(old["cron_id"])
        print(f"Removed the cron job with schedule: {old['schedule']}")
    if delete:
        return None

    # Create cron job configuration
    cron_input = {
        "email": email,
        "minutes_since": minutes_since,
        "graph_name": graph_name,
        "url": url,
        "include_read": include_read,
        "rerun": False,
        "early": False,
        "skip_filters": False
    }

    # Register the cron job
    cron = await client.crons.create(
        CRON_GRAPH,          # The graph name for the cron
        schedule=schedule,   # Cron schedule expression
        input=cron_input     # Input parameters for the cron graph
    )

    print(f"Cron job created successfully with schedule: {schedule}")
    print(f"Email ingestion will run for: {email}")
    print(f"Processing emails from the past {minutes_since} minutes")
    print(f"Using graph: {graph_name}")

    return cron

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Set up a cron job for email ingestion in LangGraph")

    parser.add_argument(
        "--email",
        type=str,
        help="Email address to fetch messages for (not needed with --delete)",
    )
    parser.add_argument(
        "--url",
        type=str,
        default=DEFAULT_URL,
        help="URL to the LangGraph server",
    )
    parser.add_argument(
        "--minutes-since",
        type=int,
        default=60,
        help="Only process emails that are less than this many minutes old",
    )
    parser.add_argument(
        "--schedule",
        type=str,
        default="*/10 * * * *",
        help="Cron schedule expression (default: every 10 minutes)",
    )
    parser.add_argument(
        "--graph-name",
        type=str,
        default="email_assistant_hitl_memory_gmail",
        help="Name of the graph to use for processing emails",
    )
    parser.add_argument(
        "--include-read",
        action="store_true",
        help="Include emails that have already been read",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Remove the cron job and create none",
    )

    args = parser.parse_args()
    if not args.delete and not args.email:
        parser.error("--email is required (or use --delete)")

    asyncio.run(
        main(
            email=args.email,
            url=args.url,
            minutes_since=args.minutes_since,
            schedule=args.schedule,
            graph_name=args.graph_name,
            include_read=args.include_read,
            delete=args.delete,
        )
    )
