"""Edges no one has pressed yet: strangers, stale buttons, dead servers, restarts."""
import asyncio
import pathlib
import tempfile

import httpx
from telegram.error import BadRequest

from harness import FakeQuery, FakeThreads, World, callbacks, check, labels, request, run_all, section, r, t, waiting

UUID = "01900000-0000-7000-8000-000000000001"


@section("1. A stranger's chat is ignored completely")
def _():
    w = World()
    w.bot_data["pending"]["T1"] = request()
    w.press("accept|T1", chat=999)
    w.command(t.cmd_list, "/list", chat=999)
    w.bot_data["awaiting"][1] = {"thread_id": "T1", "mode": "respond", "field": None}
    w.type("send it", chat=999)
    check("no resume from a stranger", not w.resumes(), str(w.resumes()))
    check("nothing sent to a stranger", not w.sent)


@section("2. /list with nothing waiting says so")
def _():
    w = World()
    w.command(t.cmd_list, "/list")
    check("says Nothing waiting", w.sent and "Nothing waiting" in w.texts()[-1])


@section("3. A Question offers only Respond and Ignore")
def _():
    kb = r.build_keyboard("T1", {"allow_ignore": True, "allow_respond": True, "allow_edit": False, "allow_accept": False})
    check("no Accept", "Accept" not in labels(kb), str(labels(kb)))
    check("Respond and Ignore present", {"Respond", "Ignore"} <= set(labels(kb)))


@section("4. Graph server down - the poll survives")
def _():
    w = World()
    w.search_fails = True
    try:
        w.poll()
        survived = True
    except Exception:
        survived = False
    check("poll did not raise", survived)
    check("poll guard released", w.bot_data["polling"] is False)


@section("5. Bot restart - open interrupts come back as a list")
def _():
    w = World(interrupts=[waiting("T1", "i1", "write_email"), waiting("T2", "i2", "schedule_meeting")])
    w.poll()  # brand-new memory, as after a restart
    check("both recovered", set(w.bot_data["pending"]) == {"T1", "T2"})
    check("list posted", w.sent and "2 waiting" in w.texts()[-1])


@section("6. Opening something no longer waiting")
def _():
    w = World()
    w.press("open|GONE")
    check("says it is gone", w.sent and "no longer waiting" in w.texts()[-1])
    check("focus not grabbed", w.bot_data["focus"] is None)


@section("7. Pressing Accept twice must resume ONCE")
def _():
    # Full text re-sends the action buttons, so two live copies of Accept exist.
    w = World()
    w.bot_data["pending"]["T1"] = request()
    w.press("accept|T1")
    w.press("accept|T1")
    check("only one resume", len(w.resumes()) == 1, f"{len(w.resumes())} resumes")
    w.press("ignore|T1")
    check("a late Ignore is refused too", len(w.resumes()) == 1, f"{len(w.resumes())} resumes")


@section("8. Server down when you press a button")
def _():
    w = World()
    w.create_fails = True
    w.bot_data["pending"]["T1"] = request()
    try:
        w.press("accept|T1")
        raised = False
    except Exception:
        raised = True
    check("handler did not crash", not raised)
    check("you are told it failed", any("reach" in s.lower() or "try again" in s.lower() for s in w.texts()),
          str([s[:40] for s in w.texts()]))
    check("request still waiting, so you can retry", "T1" in w.bot_data["pending"])


@section("9. Telegram rejects the HTML - plain text still arrives")
def _():
    w = World(reject_html=True)
    w.run(t.send_html(w.context.bot, t.CHAT_ID, "<b>broken <i>tag</b>"))
    check("delivered as plain text", w.sent and w.texts()[-1] == "broken tag", repr(w.texts()[-1]) if w.sent else "")


@section("10. A failed run releases focus and says so")
def _():
    w = World(status={"T1": "error"})
    w.bot_data["focus"] = "T1"
    w.bot_data["watching"]["T1"] = {"answer": "accept", "messages": 0, "store": {}, "approved": set()}
    w.poll()
    check("failure reported", any("failed" in s.lower() for s in w.texts()))
    check("focus released", w.bot_data["focus"] is None)


@section("11. While focused on A, B arrives quietly; list returns after A")
def _():
    a = waiting("TA", "a1", "write_email")
    b = waiting("TB", "b1", "schedule_meeting", who="Ana <a@x.com>")
    w = World(interrupts=[a])
    w.poll()
    w.press("open|TA")
    w.clear()
    w.interrupts = [a, b]
    w.poll()
    check("B queued", "TB" in w.bot_data["pending"])
    check("no list while focused", not w.lists())
    w.bot_data["watching"]["TA"] = {"answer": "accept", "messages": 0, "store": {}, "approved": set()}
    w.bot_data["pending"].pop("TA")
    w.interrupts = [b]
    w.status["TA"] = "idle"
    w.poll()
    check("list returned with B", any("1 waiting" in s and "Ana" in s for s in w.texts()))


@section("12. Text with nothing waiting does nothing")
def _():
    w = World()
    w.type("hello?")
    check("no resume", not w.resumes())


@section("13. Every button fits Telegram's 64-byte limit")
def _():
    kbs = [r.build_keyboard(UUID, {"allow_accept": 1, "allow_edit": 1, "allow_respond": 1, "allow_ignore": 1},
                            "0123456789abcdef0123456789abcdef"),   # worst case: id attached
           r.edit_keyboard(UUID, [f"field_{i}" for i in range(12)]),
           r.render_list({UUID: {"action": "Email Assistant: notify", "sender": "A Very Long Sender Name Indeed",
                                 "subject": "s", "interrupt_id": "x"}})[1]]
    sizes = [len(data.encode()) for kb in kbs for data in callbacks(kb)]
    check("largest callback fits", max(sizes) <= 64, f"max {max(sizes)} bytes")


@section("14. A full inbox still fits one message")
def _():
    many = {f"{UUID[:-2]}{i:02d}": {"action": "Email Assistant: notify", "sender": "N" * 200 + " <x@y.z>",
                                    "subject": "S" * 200, "interrupt_id": str(i),
                                    "description": "**From**: x\n**Subject**: S\n---\n" + "long & <odd> text && " * 50}
            for i in range(20)}
    text, _ = r.render_list(many)
    check("20 long entries within SAFE_LIMIT", len(text) <= r.SAFE_LIMIT, f"{len(text)} chars")
    check("each still shows some of its email", text.count("<i>") == 20, f"{text.count('<i>')} previews")


@section("15. An old button cannot answer the NEXT question on the same thread")
def _():
    w = World()
    w.bot_data["pending"]["T1"] = request(interrupt_id="bbbbbbbb22223333")
    kb = r.build_keyboard("T1", {"allow_accept": True}, "bbbbbbbb22223333")
    check("buttons carry the question id", "accept|T1|bbbbbbbb" in callbacks(kb))
    w.press("accept|T1|aaaaaaaa")
    check("old question's Accept refused", not w.resumes(), str(w.resumes()))
    check("told it was already answered", any("already been answered" in s for s in w.texts()))
    w.press("accept|T1|bbbbbbbb")
    check("current question's Accept works", w.resumes() == [("T1", "accept")], str(w.resumes()))


@section("16. Server down while sending feedback - the prompt is kept")
def _():
    w = World()
    w.create_fails = True
    w.bot_data["pending"]["T1"] = request()
    prompt = w.message("Send your feedback")
    w.bot_data["awaiting"][prompt.message_id] = {"thread_id": "T1", "mode": "respond", "field": None}
    w.bot_data["awaiting_last"] = prompt.message_id
    w.type("shorter please")
    check("told it was not sent", any("nothing was sent" in s for s in w.texts()))
    check("prompt re-armed for a retry", prompt.message_id in w.bot_data["awaiting"])
    w.create_fails = False
    w.type("shorter please")
    check("the retry goes through", w.resumes() == [("T1", "response")], str(w.resumes()))


def restarted(interrupts, signature=(("T1", "i1"),)):
    """A world just after a restart: the list the previous run posted is message 42."""
    w = World(interrupts=interrupts)
    w.bot_data["list_message_id"] = 42
    w.bot_data["list_signature"] = signature
    return w


@section("17. A restart with the SAME inbox must not post another list")
def _():
    w = restarted([waiting("T1", "i1", "schedule_meeting")])  # same inbox as now
    w.poll()
    check("no duplicate list after restart", not w.lists(), f"{len(w.sent)} posted")


@section("18. A restart with a CHANGED inbox retires the old list, posts one new one")
def _():
    w = restarted([waiting("T1", "i1", "schedule_meeting"), waiting("T2", "i2", "write_email")])
    w.poll()
    check("old list's buttons retired by id", w.retired == [42], str(w.retired))
    check("exactly one new list", len(w.lists()) == 1)


@section("19. State is written only by the real bot, and reads back intact")
def _():
    original = t.STATE_FILE
    # A folder of its own that is removed afterwards; the old check left one
    # behind in the system's temp folder on every run.
    with tempfile.TemporaryDirectory() as folder:
        t.STATE_FILE = pathlib.Path(folder) / "state.json"
        try:
            w = World(interrupts=[waiting("T1", "i1", "schedule_meeting")])
            w.poll()
            check("tests never write the file", not t.STATE_FILE.exists())
            w = World(interrupts=[waiting("T1", "i1", "schedule_meeting")])
            w.bot_data["persist_state"] = True
            w.poll()
            saved = t.load_list_state()
            check("real bot writes it", t.STATE_FILE.exists())
            check("signature reads back", saved.get("list_signature") == [["T1", "i1"]], str(saved))
            check("message id recorded", saved.get("list_message_id") is not None)
        finally:
            t.STATE_FILE = original
    check("the temp folder is removed", not pathlib.Path(folder).exists())


@section("20. Server unreachable on the first poll after a restart: no retire, no duplicate")
def _():
    w = restarted([waiting("T1", "i1", "schedule_meeting")])
    w.search_fails = True
    w.poll()
    check("old list NOT retired while the server is down", w.retired == [], str(w.retired))
    check("nothing posted while down", not w.sent, f"{len(w.sent)} posted")
    check("saved signature kept", w.bot_data["list_signature"] == (("T1", "i1"),), str(w.bot_data["list_signature"]))
    w.search_fails = False
    w.poll()
    check("server back, same inbox: still no duplicate", not w.lists(), f"{len(w.sent)} posted")
    check("and still not retired", w.retired == [], str(w.retired))


@section("21. A double tap on a list button opens the email ONCE")
def _():
    w = World()
    w.bot_data["pending"]["T1"] = request(action="schedule_meeting", allowed={"allow_accept": True},
                                          interrupt_id="iii")

    async def double_tap():
        await asyncio.gather(t.on_button(w.update(query=FakeQuery(w, "open|T1")), w.context),
                             t.on_button(w.update(query=FakeQuery(w, "open|T1")), w.context))
    w.run(double_tap())
    check("only one detail sent", len(w.details()) == 1, f"{len(w.details())} sent")
    w.bot_data["pending"]["T1"]["interrupt_id"] = "jjj"            # the thread moved to a new question
    w.press("open|T1")
    check("a new question opens straight away", len(w.details()) == 2, f"{len(w.details())} sent")
    w.bot_data["opened"]["T1"] = ("jjj", t.time.monotonic() - 60)  # same question, a minute later
    w.press("open|T1")
    check("re-opening later still works", len(w.details()) == 3, f"{len(w.details())} sent")


class FlakyThreads(FakeThreads):
    """threads.search that fails with a closed connection the first `failures` times."""
    def __init__(self, world, failures):
        super().__init__(world)
        self.failures, self.calls = failures, 0

    async def search(self, **kw):
        self.calls += 1
        if self.calls <= self.failures:
            raise httpx.ReadError("connection closed by server")
        return self.world.interrupts


@section("22. A stale connection (ReadError) is retried once, not reported as an outage")
def _():
    w = World(interrupts=[waiting("T1", "i1", "schedule_meeting")])
    w.client.threads = FlakyThreads(w, failures=1)
    w.poll()
    check("retried once and got through", w.client.threads.calls == 2, f"{w.client.threads.calls} calls")
    check("list posted, so the poll counted as reached", w.lists())

    w = World(interrupts=[waiting("T1", "i1", "schedule_meeting")])
    w.client.threads = FlakyThreads(w, failures=99)
    w.poll()
    check("a real outage gives up after two tries", w.client.threads.calls == 2, f"{w.client.threads.calls} calls")
    check("and posts nothing", not w.sent, f"{len(w.sent)} posted")


@section("23. A button the bot does not know is logged and ignored")
def _():
    w = World()
    w.bot_data["pending"]["T1"] = request()
    w.press("unknown|T1")
    check("nothing sent", not w.sent, str(w.texts()))
    check("nothing resumed", not w.resumes(), str(w.resumes()))


@section("24. A server or Telegram failure logs one line; a bug logs its traceback")
def _():
    expected = [ConnectionError("server unreachable"), httpx.ReadError("closed"), BadRequest("bad tag")]
    check("an expected failure gets no traceback", all(t.bug_trace(e) is None for e in expected))
    bug = KeyError("pending")
    check("a bug gets its traceback", t.bug_trace(bug) is bug)


run_all()
