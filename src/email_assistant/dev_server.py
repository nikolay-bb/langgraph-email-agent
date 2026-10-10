"""What this project repairs in the dev server (`langgraph dev`), from inside it.

The graphs run inside the server's process, so they can reach the server's
checkpoint saver. Two faults of langgraph-runtime-inmem 0.34.1, both seen on
a running server on 9 Oct 2026:

1. The saver stops being written to disk. Each read of a thread's state
   (`threads.get_state`, which the bot does for a card) builds a second saver
   in `checkpoint.Checkpointer(unpack_hook=...)`. Its three new dictionaries
   take the places of the real ones in the flush list (`_persistence._stores`,
   one place per file name) and are thrown away at once, so the flush loop
   drops those places. From then on no checkpoint reaches the disk: a restart
   loses every step saved after it, an email that waits for an answer
   included. `keep_saving` puts the real dictionaries back. The `cron` graph
   calls it each minute, so about a minute of steps is the most that is
   unsaved.
2. A thread delete swaps `saver.writes` for a plain dictionary, which is
   never written (`ops._delete_checkpoints_for_thread`). `keep_saving` turns
   it back. The same delete leaves the thread's channel values in memory:
   see cleanup.free_leftovers.

Call these on the server's event loop, with no `await` inside: no run can
write a checkpoint in the middle.
"""


def server_saver():
    """The dev server's checkpoint saver, when this code runs inside that server; else None.

    Read, never made: `Checkpointer()` would create the saver and its files.
    """
    try:
        from langgraph_runtime_inmem import checkpoint
    except ImportError:
        return None
    return checkpoint.MEMORY


def keep_saving(saver) -> int:
    """Put the saver's three dictionaries back in the server's flush list.

    Returns how many were out of it; 0 when nothing was wrong.
    """
    from langgraph.checkpoint.memory import PersistentDict
    from langgraph_runtime_inmem import _persistence

    if not isinstance(saver.writes, PersistentDict):  # fault 2
        # The saver numbers its files in the order it makes them: storage 1, writes 2, blobs 3.
        writes = PersistentDict(dict, filename=saver.filename + "2.pckl")
        writes.update(saver.writes)
        saver.writes = writes
    repaired = 0
    for part in (saver.storage, saver.writes, saver.blobs):
        listed = _persistence._stores.get(part.filename)
        if listed is None or listed() is not part:  # fault 1
            _persistence.register_persistent_dict(part)
            repaired += 1
    return repaired


def saver_report(saver) -> str:
    """One log line: how much the saver holds, and if the server writes each part to disk."""
    from langgraph_runtime_inmem import _persistence
    flushed = {id(ref()) for ref in list(_persistence._stores.values()) if ref() is not None}
    return ", ".join(
        f"{name}: {len(part)} entries, {type(part).__name__}, {'written to disk' if id(part) in flushed else 'NOT written to disk'}"
        for name, part in (("storage", saver.storage), ("writes", saver.writes), ("blobs", saver.blobs))
    )
