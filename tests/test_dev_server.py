"""The repairs of the dev server's saver (dev_server.py), against the installed server packages.

The server's own saver class cannot be made here: its `__init__` loads the
server's settings, which exist only in the running server. `ServerSaver`
below is built the way that `__init__` builds it, from the same parts: the
real dictionary class and the real flush list. One test pins that copy to
the installed code. The files go to a temporary folder, and no flush thread
starts.
"""
import asyncio
import gc
import inspect
import os
from collections import defaultdict
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver, PersistentDict
from langgraph_runtime_inmem import _persistence, checkpoint, ops

from email_assistant import cron, dev_server


class ServerSaver(InMemorySaver):
    """langgraph_runtime_inmem.checkpoint.InMemorySaver, without the server's settings."""
    def __init__(self):
        self.filename = os.path.join(".langgraph_api", ".langgraph_checkpoint.")
        made = 0

        def factory(*args):
            nonlocal made
            made += 1
            os.makedirs(".langgraph_api", exist_ok=True)
            part = PersistentDict(*args, filename=self.filename + str(made) + ".pckl")
            _persistence.register_persistent_dict(part)
            return part

        super().__init__(factory=factory)


def read_a_threads_state(memory):
    """What `checkpoint.Checkpointer(unpack_hook=...)` does for each read of a thread's state."""
    second = ServerSaver()
    second.writes, second.blobs, second.storage = memory.writes, memory.blobs, memory.storage
    del second
    gc.collect()


@pytest.fixture
def saver(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the saver makes .langgraph_api/ where the server runs
    monkeypatch.setattr(_persistence, "_stores", {})
    monkeypatch.setattr(_persistence, "_flush_thread", (None, None))  # not None, so no thread starts
    memory = ServerSaver()
    monkeypatch.setattr(checkpoint, "MEMORY", memory)  # what dev_server.server_saver reads
    return memory


def test_the_copy_of_the_servers_saver_matches_the_installed_code():
    """If a new version of the server changes these lines, read dev_server.py again."""
    init = inspect.getsource(checkpoint.InMemorySaver.__init__)
    assert 'os.path.join(".langgraph_api", ".langgraph_checkpoint.")' in init
    assert 'self.filename + str(i) + ".pckl"' in init and "__persistence_hook__(d)" in init
    made = inspect.getsource(InMemorySaver.__init__)  # the order of the three files: 1, 2, 3
    assert made.index("self.storage = factory") < made.index("self.writes = factory") < made.index("self.blobs = factory")
    second = inspect.getsource(checkpoint.Checkpointer)
    assert "saver.writes = MEMORY.writes" in second and "__persistence_hook__=register_persistent_dict" in second
    delete = inspect.getsource(ops._delete_checkpoints_for_thread)
    assert "checkpointer.writes = defaultdict(" in delete and "blobs" not in delete


def in_the_flush_list(saver) -> bool:
    return all(_persistence._stores.get(part.filename) is not None and _persistence._stores[part.filename]() is part
               for part in (saver.storage, saver.writes, saver.blobs))


def test_a_fresh_saver_is_in_the_flush_list_and_needs_no_repair(saver):
    assert in_the_flush_list(saver) and dev_server.keep_saving(saver) == 0
    # keep_saving rebuilds `writes` under this name: the saver's second file.
    assert saver.writes.filename == saver.filename + "2.pckl"
    assert (saver.storage.filename, saver.blobs.filename) == (saver.filename + "1.pckl", saver.filename + "3.pckl")


def test_after_a_read_of_a_threads_state_the_saver_is_written_to_disk_again(saver):
    """Live, 9 Oct: after one threads.get_state the three checkpoint files stopped changing."""
    read_a_threads_state(saver)
    assert not in_the_flush_list(saver)  # the fault
    assert dev_server.keep_saving(saver) == 3
    assert in_the_flush_list(saver)
    assert "NOT written" not in dev_server.saver_report(saver)


def test_after_a_thread_delete_the_writes_are_written_to_disk_again(saver):
    """Live, 9 Oct: after one threads.delete, `writes` was a plain dictionary, in no file."""
    saver.writes[("t1", "", "checkpoint-1")] = {"task": "a pending write"}
    saver.writes = defaultdict(dict, saver.writes)  # the line in ops._delete_checkpoints_for_thread
    gc.collect()
    assert dev_server.keep_saving(saver) >= 1
    assert isinstance(saver.writes, PersistentDict) and in_the_flush_list(saver)
    assert saver.writes[("t1", "", "checkpoint-1")] == {"task": "a pending write"}
    saver.writes.sync()
    assert Path(saver.filename + "2.pckl").exists()


def test_the_gmail_timer_run_repairs_the_saver(saver, monkeypatch):
    read_a_threads_state(saver)
    async def fetch(args):
        return 0
    monkeypatch.setattr(cron, "fetch_and_process_emails", fetch)
    out = asyncio.run(cron.graph.ainvoke({"email": "n@x.com"}))
    assert out["status"] == "success" and in_the_flush_list(saver)


def test_a_failed_repair_does_not_stop_the_gmail_check(monkeypatch):
    def broken(saver):
        raise RuntimeError("the server's saver changed")
    async def fetch(args):
        return 0
    monkeypatch.setattr(cron, "server_saver", lambda: object())
    monkeypatch.setattr(cron, "keep_saving", broken)
    monkeypatch.setattr(cron, "fetch_and_process_emails", fetch)
    out = asyncio.run(cron.graph.ainvoke({"email": "n@x.com"}))
    assert out["status"] == "success" and out["exit_code"] == 0
