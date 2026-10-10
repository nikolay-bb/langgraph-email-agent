"""The clean-up job: finished emails leave the server, one line stays for each.

Offline: a fake of the server's client, and a list file in a temporary folder.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from email_assistant import cleanup

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
SECRET_TEXT = "The text of the email, which must never be in the list"


def thread(thread_id, status="idle", hours_old=48, decision="respond"):
    """A thread as the server's search returns it."""
    return {
        "thread_id": thread_id,
        "status": status,
        "updated_at": (NOW - timedelta(hours=hours_old)).isoformat(),
        "metadata": {"email_id": f"mail-{thread_id}"},
        "values": {
            "email_input": {"id": f"mail-{thread_id}", "from": "Robin <r@x.com>", "subject": f"Subject {thread_id}",
                            "date": "Thu, 8 Oct 2026 10:00:00 +0100", "body": SECRET_TEXT},
            "classification_decision": decision,
            "messages": [{"content": SECRET_TEXT}],
        },
    }


class FakeThreads:
    """client.threads: a search by status, in pages, and a delete."""
    def __init__(self, threads, broken_delete=False):
        self.all, self.deleted, self.broken_delete = list(threads), [], broken_delete

    def search(self, status=None, limit=10, offset=0, **kw):
        hits = [t for t in self.all if status is None or t["status"] == status]
        return hits[offset:offset + limit]

    def delete(self, thread_id):
        if self.broken_delete:
            raise RuntimeError("the server did not answer")
        self.deleted.append(thread_id)
        self.all = [t for t in self.all if t["thread_id"] != thread_id]


def server(*threads, **kw):
    return SimpleNamespace(threads=FakeThreads(threads, **kw))


@pytest.fixture(autouse=True)
def list_file(tmp_path, monkeypatch):
    """Each test has its own list file; the real one is logs/processed_emails.jsonl."""
    path = tmp_path / "logs" / "processed_emails.jsonl"
    monkeypatch.setattr(cleanup, "LIST_FILE", path)
    return path


def test_a_finished_email_with_no_change_for_a_day_is_removed_and_listed(list_file):
    client = server(thread("t1", hours_old=25, decision="ignore"))
    assert cleanup.clean(client, days=1, now=NOW) == {"removed": 1}
    assert client.threads.deleted == ["t1"]
    [line] = cleanup.listed()
    assert line == {"email_id": "mail-t1", "thread_id": "t1", "from": "Robin <r@x.com>", "subject": "Subject t1",
                    "date": "Thu, 8 Oct 2026 10:00:00 +0100", "result": "ignore", "removed": "2026-10-09T12:00:00+00:00"}
    assert cleanup.removed_email_ids() == {"mail-t1"}


def test_an_email_that_waits_for_your_answer_is_never_removed():
    """A card can wait for days. Its thread is "interrupted", not "idle"."""
    client = server(thread("waits", status="interrupted", hours_old=24 * 30),
                    thread("in-work", status="busy", hours_old=24 * 30),
                    thread("failed", status="error", hours_old=24 * 30),
                    thread("done", hours_old=24 * 30))
    cleanup.clean(client, days=1, now=NOW)
    assert client.threads.deleted == ["done"]
    assert {t["thread_id"] for t in client.threads.all} == {"waits", "in-work", "failed"}


def test_a_search_that_ignores_the_status_filter_still_removes_no_waiting_email():
    client = server(thread("waits", status="interrupted"), thread("done"))
    client.threads.search = lambda status=None, limit=10, offset=0, **kw: client.threads.all[offset:offset + limit]
    cleanup.clean(client, days=1, now=NOW)
    assert client.threads.deleted == ["done"]


def test_an_email_younger_than_a_day_is_kept():
    """The ingester skips an unread email of the last hour only because its thread exists."""
    client = server(thread("young", hours_old=23), thread("old", hours_old=25))
    cleanup.clean(client, days=1, now=NOW)
    assert client.threads.deleted == ["old"]


def test_no_line_no_removal(list_file):
    list_file.parent.write_text("a file where the folder must be")  # so the list cannot be written
    client = server(thread("t1"))
    with pytest.raises(OSError):
        cleanup.clean(client, days=1, now=NOW)
    assert client.threads.deleted == []


def test_the_list_holds_no_email_text(list_file):
    cleanup.clean(server(thread("t1")), days=1, now=NOW)
    assert SECRET_TEXT not in list_file.read_text()
    assert oct(list_file.stat().st_mode)[-3:] == "600"  # senders and subjects: the owner only


def test_a_failed_delete_writes_no_second_line_in_the_next_run():
    with pytest.raises(RuntimeError):
        cleanup.clean(server(thread("t1"), broken_delete=True), days=1, now=NOW)
    client = server(thread("t1"))
    cleanup.clean(client, days=1, now=NOW)
    assert client.threads.deleted == ["t1"] and len(cleanup.listed()) == 1


def test_more_emails_than_one_search_page_are_all_removed():
    client = server(*(thread(f"t{i}") for i in range(cleanup.PAGE * 2 + 30)))
    assert cleanup.clean(client, days=1, now=NOW) == {"removed": cleanup.PAGE * 2 + 30}
    assert client.threads.all == [] and len(cleanup.listed()) == cleanup.PAGE * 2 + 30


def test_the_server_loads_the_cleanup_graph_and_a_run_returns_the_count(monkeypatch):
    spec = json.loads((Path(__file__).parents[1] / "langgraph.json").read_text())
    assert spec["graphs"]["cleanup"].endswith("cleanup.py:graph")
    client = server(thread("t1"), thread("waits", status="interrupted"))
    saver = FakeSaver(live=["waits"], leftover=["t1"])
    asked, kept = [], []
    monkeypatch.setattr(cleanup, "get_sync_client", lambda url: asked.append(url) or client)
    monkeypatch.setattr(cleanup, "server_saver", lambda: saver)
    monkeypatch.setattr(cleanup, "keep_saving", lambda saver: kept.append(saver) or 0)
    out = asyncio.run(cleanup.graph.ainvoke({"days": 1}))
    assert out["removed"] == 1 and out["freed"] == 2 and asked == ["http://127.0.0.1:2024"]
    assert {key[0] for key in (*saver.writes, *saver.blobs)} == {"waits"}
    # The server's delete takes `writes` out of the flush list: the run puts it back (dev_server.py).
    assert kept == [saver]


def test_a_run_outside_the_dev_server_frees_nothing_and_does_not_fail(monkeypatch):
    client = server(thread("t1"))
    monkeypatch.setattr(cleanup, "get_sync_client", lambda url: client)
    monkeypatch.setattr(cleanup, "server_saver", lambda: None)
    out = asyncio.run(cleanup.graph.ainvoke({}))
    assert out["removed"] == 1 and out.get("freed") is None


class FakeSaver:
    """The three dictionaries of the server's saver, with the real key shapes."""
    def __init__(self, live=(), leftover=()):
        self.storage = {thread_id: {"": {}} for thread_id in live}
        self.writes = {(thread_id, "", "checkpoint-1"): {} for thread_id in (*live, *leftover)}
        self.blobs = {(thread_id, "", "messages", 1): ("msgpack", b"...") for thread_id in (*live, *leftover)}


def test_the_leftovers_of_a_removed_email_leave_the_servers_memory():
    saver = FakeSaver(live=["alive"], leftover=["gone", "not-in-the-list"])
    assert cleanup.free_leftovers(saver, {"gone"}) == 2
    # A thread that is not in the clean-up list is never touched, with or without a checkpoint.
    assert {key[0] for key in (*saver.writes, *saver.blobs)} == {"alive", "not-in-the-list"}


def test_a_rerun_of_a_removed_email_keeps_its_data():
    """--rerun gives a removed email a new thread with the same id: listed, and alive."""
    saver = FakeSaver(live=["m1-thread"])
    assert cleanup.free_leftovers(saver, {"m1-thread"}) == 0
    assert len(saver.writes) == 1 and len(saver.blobs) == 1


def test_free_leftovers_reads_the_key_shapes_of_the_installed_saver():
    """A real graph on LangGraph's in-memory saver, the class the dev server's saver extends."""
    from typing import TypedDict
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph_runtime_inmem.checkpoint import InMemorySaver as ServerSaver
    assert issubclass(ServerSaver, InMemorySaver)

    class Text(TypedDict):
        text: str
    builder = StateGraph(Text)
    builder.add_node("shout", lambda state: {"text": state["text"] + "!"})
    builder.add_edge(START, "shout")
    builder.add_edge("shout", END)
    saver = InMemorySaver()
    app = builder.compile(checkpointer=saver)
    for thread_id in ("gone", "alive"):
        app.invoke({"text": thread_id}, {"configurable": {"thread_id": thread_id}})
    assert any(key[0] == "gone" for key in saver.blobs)

    del saver.storage["gone"]  # all that the dev server's thread delete does for the values
    assert cleanup.free_leftovers(saver, {"gone", "alive"}) > 0
    assert not [key for key in (*saver.writes, *saver.blobs) if key[0] == "gone"]
    assert app.get_state({"configurable": {"thread_id": "alive"}}).values == {"text": "alive!"}


def test_outside_the_dev_server_there_is_no_saver_and_none_is_made():
    from langgraph_runtime_inmem import checkpoint
    assert cleanup.server_saver() is None and checkpoint.MEMORY is None


def test_the_report_says_when_a_part_is_not_written_to_disk():
    report = cleanup.saver_report(FakeSaver(live=["t1"]))
    assert report.count("NOT written to disk") == 3 and "blobs: 1 entries, dict" in report


class FakeCrons:
    """client.crons, with one clean-up job on the server already."""
    def __init__(self):
        self.jobs, self.searched, self.created = [{"cron_id": "old-1", "schedule": "0 0 * * *"}], [], []

    def search(self, assistant_id=None, limit=10, **kw):
        self.searched.append(assistant_id)
        return list(self.jobs)

    def delete(self, cron_id):
        self.jobs = [job for job in self.jobs if job["cron_id"] != cron_id]

    def create(self, assistant_id, schedule=None, input=None, **kw):
        self.created.append((assistant_id, schedule, input))
        self.jobs.append({"cron_id": "new-1", "schedule": schedule})
        return self.jobs[-1]


def cron_server():
    assistants = SimpleNamespace(search=lambda graph_id=None, limit=10: [{"assistant_id": f"id-of-{graph_id}"}])
    return SimpleNamespace(crons=FakeCrons(), assistants=assistants)


def test_a_second_setup_replaces_the_cleanup_job():
    client = cron_server()
    cleanup.set_job(client, "30 3 * * *", days=1)
    assert client.crons.searched == ["id-of-cleanup"]  # only the jobs of the clean-up graph
    assert [job["cron_id"] for job in client.crons.jobs] == ["new-1"]
    [(graph, schedule, job_input)] = client.crons.created
    assert (graph, schedule) == ("cleanup", "30 3 * * *")
    # The graph gets this input as its state: every key must be a field of it.
    assert set(job_input) <= set(vars(cleanup.CleanupJob()))


def test_delete_removes_the_cleanup_job_and_creates_none():
    client = cron_server()
    assert cleanup.set_job(client, None) is None
    assert client.crons.jobs == [] and client.crons.created == []
