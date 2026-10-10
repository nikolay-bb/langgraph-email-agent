from contextlib import asynccontextmanager
from typing import Optional
from dataclasses import dataclass
import langsmith as ls
from langgraph.graph import StateGraph
from email_assistant.dev_server import keep_saving, server_saver
from email_assistant.tools.gmail.run_ingest import fetch_and_process_emails

@dataclass(kw_only=True)
class JobKickoff:
    """State for the email ingestion cron job"""
    email: str
    minutes_since: int = 60
    graph_name: str = "email_assistant_hitl_memory_gmail"
    url: str = "http://127.0.0.1:2024"
    include_read: bool = False
    rerun: bool = False
    early: bool = False
    skip_filters: bool = False
    # What `main` returns. LangGraph keeps only keys the state has, so without
    # these fields the result of every cron run was silently dropped.
    status: str = ""
    exit_code: Optional[int] = None
    error: str = ""

async def main(state: JobKickoff):
    """Run the email ingestion process"""
    print(f"Kicking off job to fetch emails from the past {state.minutes_since} minutes")
    print(f"Email: {state.email}")
    print(f"URL: {state.url}")
    print(f"Graph name: {state.graph_name}")

    # The dev server stops writing its checkpoints to disk after the first
    # read of a thread's state (dev_server.py). This run is the one thing
    # that happens each minute inside the server, so the repair is here. It
    # must never stop the Gmail check.
    try:
        saver = server_saver()
        if saver is not None and (repaired := keep_saving(saver)):
            print(f"Put {repaired} of the saver's dictionaries back in the server's flush list")
    except Exception as e:
        print(f"Could not check the saver's flush list: {e}")

    try:
        # The state goes to the ingester as it is: its fields have the names
        # of the script's own options (run_ingest.parse_args). It used to be
        # copied into a second object here, field by field.
        print("Starting fetch_and_process_emails...")
        result = await fetch_and_process_emails(state)
        print(f"fetch_and_process_emails returned: {result}")

        # Return the result status
        return {"status": "success" if result == 0 else "error", "exit_code": result}
    except Exception as e:
        import traceback
        print(f"Error in cron job: {str(e)}")
        print(traceback.format_exc())
        return {"status": "error", "error": str(e)}

# Build the graph
graph = StateGraph(JobKickoff)
graph.add_node("ingest_emails", main)
graph.set_entry_point("ingest_emails")
graph = graph.compile()


@asynccontextmanager
async def make_graph():
    """What the server loads as the `cron` graph (langgraph.json): the graph, with LangSmith tracing off.

    Every cron run is one trace. One run a minute is about 43,200 traces a
    month, and the free plan stops at 5,000. The server holds this context
    open for the whole run, so the run sends no trace. The email graph is not
    loaded through here: its runs keep their traces.
    """
    with ls.tracing_context(enabled=False):
        yield graph
