"""What every bot check suite shares: the bot, a fake Telegram, a fake
LangGraph server, and the PASS/FAIL report.

Each suite in this folder is a script; tests/test_bot_suites.py runs each one
in its own process. A suite is a list of sections. Each section is a function
that builds its own World, so what one section sent or changed cannot reach
the next. An error inside a section is reported as a FAIL, and the sections
after it still run.

    from harness import World, check, section, run_all, t   # r: inbox_render

    @section("1. Something the bot must do")
    def _():
        w = World(interrupts=[waiting("T1", "i1", "write_email")])
        w.run(t.poll(w.context))
        check("a list was posted", w.lists())

    run_all()
"""
from __future__ import annotations

import asyncio
import pathlib
import runpy
import sys
import textwrap
import traceback
import types

from telegram.error import BadRequest

# tests/conftest.py sends the LangSmith traces to the tests' own project.
# pytest loads it for the pytest files; a bot suite is a script, so it is
# loaded here, before the imports below read .env.
runpy.run_path(str(pathlib.Path(__file__).resolve().parent.parent / "conftest.py"))

# The bot is two scripts in scripts/, not a package, so that folder goes on the
# path here. The editor finds them through [tool.pyright] in pyproject.toml.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import inbox_render as r  # noqa: E402  (what the bot shows: text and buttons)
import telegram_inbox as t  # noqa: E402  (the bot itself; both are scripts in scripts/)

__all__ = ["r", "t", "check", "section", "run_all", "World", "FakeMessage", "FakeBot", "FakeQuery",
           "FakeThreads", "FakeRuns", "FakeStore", "EVERY_BUTTON", "waiting", "request", "memory",
           "labels", "callbacks"]


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------

_sections = []
_failed = False


def check(label, condition, detail=""):
    """One line of the report: PASS or FAIL, the label, and a detail on request."""
    global _failed
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not condition:
        _failed = True


def section(title):
    """Add the function below it to the suite, under this title."""
    def add(function):
        _sections.append((title, function))
        return function
    return add


def run_all():
    """Run every section in the order written, then exit 0 only if every check passed."""
    global _failed
    for title, function in _sections:
        print(f"\n{title}")
        try:
            function()
        except Exception:  # noqa: BLE001 - report it, then go on to the next section
            _failed = True
            print("  FAIL  the section stopped with an error:")
            print(textwrap.indent(traceback.format_exc().rstrip(), "        "))
    print("\n" + ("FAILED" if _failed else "ALL PASS"))
    sys.exit(1 if _failed else 0)


# --------------------------------------------------------------------------
# A fake Telegram
# --------------------------------------------------------------------------

_message_ids = [1000]


class FakeMessage:
    """A Telegram message. Each one gets its own id, as in Telegram."""
    def __init__(self, world, text="", reply_to=None, reply_markup=None):
        self.world, self.text, self.reply_to_message = world, text, reply_to
        self.reply_markup = reply_markup
        _message_ids[0] += 1
        self.message_id = _message_ids[0]
        self.retired = False

    async def reply_text(self, text, reply_markup=None, **kw):
        self.world.sent.append((text, reply_markup))
        return FakeMessage(self.world, text, reply_markup=reply_markup)

    async def edit_reply_markup(self, reply_markup=None):
        self.retired = True


class FakeBot:
    """context.bot. With reject_html, it refuses every HTML message, as Telegram does a bad tag."""
    def __init__(self, world, reject_html=False):
        self.world, self.reject_html = world, reject_html

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, **kw):
        if self.reject_html and parse_mode:
            raise BadRequest("Can't parse entities: unclosed tag")
        self.world.sent.append((text, reply_markup))
        return FakeMessage(self.world, text, reply_markup=reply_markup)

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.world.retired.append(message_id)


class FakeQuery:
    """A button press: its callback data, and the message the button was on."""
    def __init__(self, world, data, message=None):
        self.world, self.data = world, data
        self.message = message or FakeMessage(world)

    async def answer(self):
        pass

    async def edit_message_text(self, text, **kw):
        self.world.sent.append((text, None))

    async def edit_message_reply_markup(self, reply_markup=None):
        self.world.buttons_removed += 1


# --------------------------------------------------------------------------
# A fake LangGraph server
# --------------------------------------------------------------------------

class FakeThreads:
    def __init__(self, world):
        self.world = world

    async def search(self, **kw):
        if self.world.search_fails:
            raise ConnectionError("server unreachable")
        return self.world.interrupts

    async def get(self, thread_id):
        return {"status": self.world.status.get(thread_id, self.world.default_status)}

    async def get_state(self, thread_id):
        # The world's own list, so it can grow as the run goes on, like the
        # real thing. A fixed copy made every result look older than the
        # mark, so nothing was ever reported.
        return {"values": {"messages": self.world.messages, "email_input": self.world.email_input}}


class FakeRuns:
    def __init__(self, world):
        self.world = world

    async def create(self, thread_id, assistant_id, command=None, **kw):
        if self.world.create_fails:
            raise ConnectionError("server unreachable")
        self.world.commands.append((thread_id, command))
        return dict(self.world.run_result)

    async def join(self, thread_id, run_id):
        self.world.joined.append((thread_id, run_id))
        await asyncio.sleep(0.01)  # the graph finishing
        return {}


class FakeStore:
    def __init__(self, world):
        self.world = world

    async def search_items(self, namespace, limit=100):
        return {"items": self.world.store_items}


# --------------------------------------------------------------------------
# One fresh world per section
# --------------------------------------------------------------------------

class World:
    """A fresh fake Telegram and LangGraph server, and the bot's memory using them.

    interrupts     what threads.search returns: the waiting threads (see waiting())
    status         thread_id -> what threads.get says; others get default_status
    messages       the thread's messages that threads.get_state returns
    email_input    the thread's email that threads.get_state returns
    store_items    what the memory store holds (see memory())
    run_result     what runs.create returns, e.g. {"run_id": "R1"}
    reject_html    Telegram refuses every HTML message
    """
    def __init__(self, interrupts=(), status=None, default_status="busy", messages=None,
                 store_items=(), run_result=None, reject_html=False, email_input=None):
        self.interrupts = list(interrupts)
        self.status = dict(status or {})
        self.default_status = default_status
        self.messages = list(messages or [])
        self.email_input = email_input
        self.store_items = list(store_items)
        self.run_result = dict(run_result or {})
        self.search_fails = False
        self.create_fails = False
        self.sent = []            # (text, buttons) of every message the bot sent or edited to
        self.commands = []        # (thread_id, command) of every runs.create
        self.joined = []          # (thread_id, run_id) of every runs.join
        self.retired = []         # message ids whose buttons the bot took off (the old list)
        self.buttons_removed = 0  # times the bot took the buttons off a pressed message
        self.client = types.SimpleNamespace(threads=FakeThreads(self), runs=FakeRuns(self), store=FakeStore(self))
        self.bot_data = {"client": self.client}
        t.ensure_state(self.bot_data)
        self.context = types.SimpleNamespace(bot_data=self.bot_data, bot=FakeBot(self, reject_html))

    # --- driving the bot ---

    @staticmethod
    def run(coroutine):
        """Run one bot call to the end, as Telegram's event loop would."""
        return asyncio.run(coroutine)

    def message(self, text="", reply_to=None):
        return FakeMessage(self, text, reply_to=reply_to)

    def press(self, data, message=None, chat=None):
        """Press the button whose callback data is `data`."""
        self.run(t.on_button(self.update(query=FakeQuery(self, data, message), chat=chat), self.context))

    def type(self, text, reply_to=None, chat=None):
        """Send a normal text message, or a Telegram Reply to `reply_to`."""
        self.run(t.on_text(self.update(message=self.message(text, reply_to), chat=chat), self.context))

    def command(self, handler, text, chat=None):
        """Send a command such as /list, handled by `handler` (t.cmd_list)."""
        self.run(handler(self.update(message=self.message(text), chat=chat), self.context))

    def poll(self):
        self.run(t.poll(self.context))

    def update(self, query=None, message=None, chat=None):
        return types.SimpleNamespace(callback_query=query, message=message,
                                     effective_chat=types.SimpleNamespace(id=t.CHAT_ID if chat is None else chat))

    # --- reading what happened ---

    def texts(self):
        return [text for text, _ in self.sent]

    def lists(self):
        """The inbox lists the bot posted."""
        return [text for text in self.texts() if text.startswith("<b>") and "waiting" in text[:20]]

    def details(self):
        """The opened emails the bot posted."""
        return [text for text in self.texts() if "Proposed:" in text]

    def resumes(self):
        """(thread_id, the answer's type) of every answer sent to the graph."""
        return [(thread_id, command["resume"][0]["type"]) for thread_id, command in self.commands]

    def answers(self):
        """(thread_id, the whole answer) of every answer sent to the graph."""
        return [(thread_id, command["resume"][0]) for thread_id, command in self.commands]

    def clear(self):
        """Forget what was sent so far; the bot's memory stays."""
        self.sent.clear()
        self.commands.clear()


# --------------------------------------------------------------------------
# Test data
# --------------------------------------------------------------------------

EVERY_BUTTON = {"allow_accept": True, "allow_ignore": True, "allow_respond": True, "allow_edit": True}


def waiting(thread_id, interrupt_id, action, who="Sarah Chen <s@x.com>", subject="Hello",
            allowed=None, body="body"):
    """One thread from threads.search that waits for your answer, as the server gives it."""
    return {"thread_id": thread_id, "interrupts": {"k": [{"id": interrupt_id, "value": [{
        "action_request": {"action": action, "args": {"subject": subject}},
        "config": allowed or EVERY_BUTTON,
        "description": f"**From**: {who}\n**Subject**: {subject}\n---\n{body}"}]}]}}


def request(action="write_email", args=None, allowed=None, description="", interrupt_id=None):
    """One entry of bot_data["pending"]: a request the bot has shown you."""
    req = {"action": action, "args": dict(args or {}), "allowed": allowed or {}, "description": description}
    if interrupt_id is not None:
        req["interrupt_id"] = interrupt_id
    return req


def memory(profile, value):
    """One memory store item, as store.search_items gives it."""
    return {"namespace": ["email_assistant", profile], "key": "user_preferences", "value": value}


def labels(markup):
    """The text of every button."""
    return [b.text for row in markup.inline_keyboard for b in row] if markup else []


def callbacks(markup):
    """The callback data of every button."""
    return [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []
