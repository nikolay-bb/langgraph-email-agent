#!/usr/bin/env python
"""
The clean-up job: finished emails leave the server, one line stays for each.

The dev server (`langgraph dev`) holds every thread in memory, and nothing
removes one: its `Threads.sweep_ttl` is "Not implemented for inmem server".
So its memory grew with every email. A finished thread has a reader for its
first hour only: the ingester asks Gmail for the last 60 minutes, and skips
an email whose thread exists. This job removes the finished threads that had
no change for a day and writes one line per email to LIST_FILE. The ingester
reads that list (`removed_email_ids`), so a removed email stays "already
processed", also for a wide search by hand. The server's delete leaves most
of a thread's data in memory; `free_leftovers` takes that out too.

The server runs it as the `cleanup` graph on a cron job (langgraph.json).
This script sets that job, removes it, or starts one run:

    uv run python src/email_assistant/cleanup.py --schedule "30 3 * * *"
    uv run python src/email_assistant/cleanup.py --delete
    uv run python src/email_assistant/cleanup.py --now
"""

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from langgraph.graph import StateGraph
from langgraph_sdk import get_sync_client

from email_assistant.dev_server import keep_saving, saver_report, server_saver

# The graph that runs the clean-up (langgraph.json).
GRAPH = "cleanup"
DEFAULT_URL = "http://127.0.0.1:2024"

# One JSON line per removed email: which email it was and how it ended, never
# its text. In logs/, which git ignores, so no sender or subject reaches GitHub.
LIST_FILE = Path(__file__).resolve().parents[2] / "logs" / "processed_emails.jsonl"

# How many threads one search asks the server for.
PAGE = 100


def listed() -> list:
    """The lines of LIST_FILE, oldest first; [] when there is no file yet."""
    if not LIST_FILE.exists():
        return []
    return [json.loads(line) for line in LIST_FILE.read_text().splitlines() if line.strip()]


def removed_email_ids() -> set:
    """The Gmail message ids of the emails this job removed from the server."""
    return {entry["email_id"] for entry in listed() if entry.get("email_id")}


def list_line(thread: dict, now: datetime) -> dict:
    """What stays of a removed thread."""
    values = thread.get("values") or {}
    email = values.get("email_input") or {}
    return {
        "email_id": (thread.get("metadata") or {}).get("email_id") or email.get("id"),
        "thread_id": thread["thread_id"],
        "from": email.get("from"),
        "subject": email.get("subject"),
        "date": email.get("date"),
        "result": values.get("classification_decision"),
        "removed": now.isoformat(timespec="seconds"),
    }


def finished_threads(client, older_than: datetime) -> list:
    """Every thread that is finished ("idle") and had no change since `older_than`.

    A thread that waits for an answer is "interrupted" and one in work is
    "busy". The status is asked for in the search and checked again here, so
    neither is ever removed, whatever its age.
    """
    found, offset = [], 0
    while True:
        page = client.threads.search(status="idle", limit=PAGE, offset=offset)
        found += [
            thread for thread in page
            if thread.get("status") == "idle" and datetime.fromisoformat(thread["updated_at"]) < older_than
        ]
        if len(page) < PAGE:
            return found
        offset += PAGE


def clean(client, days: float = 1, now: Optional[datetime] = None) -> dict:
    """Remove the finished threads older than `days` from the server.

    The list lines are written first. A write that fails raises here, before
    any delete: no thread goes without its line.
    """
    now = now or datetime.now(timezone.utc)
    old = finished_threads(client, now - timedelta(days=days))
    # A thread whose delete failed in an earlier run has its line already.
    known = {entry["thread_id"] for entry in listed()}
    new_lines = [list_line(thread, now) for thread in old if thread["thread_id"] not in known]
    if new_lines:
        LIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LIST_FILE.open("a") as f:
            f.writelines(json.dumps(line, ensure_ascii=False) + "\n" for line in new_lines)
        LIST_FILE.chmod(0o600)  # senders and subjects: for the file's owner only
    for thread in old:
        client.threads.delete(thread["thread_id"])
    return {"removed": len(old)}


def free_leftovers(saver, removed_thread_ids) -> int:
    """Take what the server's own delete leaves of the removed threads out of its memory.

    The dev server's thread delete removes a thread's checkpoints
    (`saver.storage`) and leaves its channel values (`saver.blobs`), the
    largest part, where they are: `_delete_checkpoints_for_thread` in
    langgraph_runtime_inmem/ops.py has no line for `blobs` (0.34.1, read
    9 Oct). In one run, 28 removed threads kept 2.7 MB of their 3 MB.

    Only threads in the clean-up list are touched, and of those only the ones
    with no checkpoint left: `--rerun` gives a removed email a new thread
    with the same id, and that one is alive. Keys are (thread_id, ...). Call
    it on the server's event loop, with no `await` inside: no run can write a
    checkpoint between the check and the delete.
    """
    gone = {thread_id for thread_id in removed_thread_ids if thread_id not in saver.storage}
    freed = 0
    for store in (saver.writes, saver.blobs):
        for key in [key for key in store if key[0] in gone]:
            del store[key]
            freed += 1
    return freed


@dataclass(kw_only=True)
class CleanupJob:
    """State for the clean-up cron job"""
    url: str = DEFAULT_URL
    days: float = 1
    # What `main` returns. LangGraph keeps only keys the state has.
    removed: Optional[int] = None
    freed: Optional[int] = None


async def main(state: CleanupJob):
    """Run the clean-up against the server this graph runs on.

    The file and the calls to the server run in a worker thread: the dev
    server refuses a blocking call on its event loop. `free_leftovers` runs
    on the loop, see there. An error is not caught: the run fails, and the
    server's log and LangSmith show it.
    """
    result = await asyncio.to_thread(clean, get_sync_client(url=state.url), state.days)
    listed_ids = await asyncio.to_thread(lambda: {entry["thread_id"] for entry in listed()})
    saver = server_saver()
    if saver is not None:
        result["freed"] = free_leftovers(saver, listed_ids)
        keep_saving(saver)  # the deletes above took `saver.writes` out of the flush list
        print(f"Clean-up, the server's saver now: {saver_report(saver)}")
    print(f"Clean-up: removed {result['removed']} finished emails with no change for {state.days} day(s); "
          f"freed {result.get('freed')} leftover entries")
    return result


# Build the graph
graph = StateGraph(CleanupJob)
graph.add_node("clean", main)
graph.set_entry_point("clean")
graph = graph.compile()


def set_job(client, schedule: Optional[str], days: float = 1, url: str = DEFAULT_URL):
    """Replace the clean-up cron job; with no schedule, only remove it.

    One job at a time, as in setup_cron.py. The search takes the graph's
    assistant ID: the server refuses a graph name there.
    """
    [assistant] = client.assistants.search(graph_id=GRAPH, limit=1)
    for old in client.crons.search(assistant_id=assistant["assistant_id"], limit=100):
        client.crons.delete(old["cron_id"])
        print(f"Removed the clean-up cron job with schedule: {old['schedule']}")
    if schedule is None:
        return None
    cron = client.crons.create(GRAPH, schedule=schedule, input={"url": url, "days": days})
    print(f"Clean-up cron job created with schedule: {schedule} (UTC)")
    print(f"It removes finished emails with no change for {days} day(s)")
    return cron


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="The clean-up job of the email agent; the server must run")
    parser.add_argument("--url", type=str, default=DEFAULT_URL, help="URL to the LangGraph server")
    parser.add_argument("--days", type=float, default=1, help="Remove a finished email after this many days with no change")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--schedule", type=str, help='Set or replace the cron job, for example "30 3 * * *" (03:30 UTC each day)')
    action.add_argument("--delete", action="store_true", help="Remove the cron job and create none")
    action.add_argument("--now", action="store_true", help="Start one clean-up run on the server and print its result")
    args = parser.parse_args()

    server = get_sync_client(url=args.url)
    if args.now:
        print(server.runs.wait(None, GRAPH, input={"url": args.url, "days": args.days}))
    else:
        set_job(server, None if args.delete else args.schedule, days=args.days, url=args.url)
