#!/usr/bin/env python
"""Telegram front end for the ambient email agent. Replaces Agent Inbox.

Long polling only. The bot calls OUT to Telegram and OUT to the LangGraph
server on localhost, so nothing has to listen on a public port. That is what
makes the deployed version safe without Tailscale or a reverse proxy.

Deliberately generic: it never hardcodes a tool name or an argument field.
Buttons come from the `config` flags the graph sends, and an edit echoes back
every field it was given, so a new tool needs no change here.

This file talks to Telegram and to the server and keeps the bot's memory
(BotData). What it shows - the text and buttons of each message - is built in
inbox_render.py, next to it.

Run:  uv run --group bot python scripts/telegram_inbox.py
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict, get_origin, get_type_hints

import httpx
from dotenv import load_dotenv
from langgraph_sdk import get_client
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# What the bot shows: pure functions, no Telegram or server calls.
from inbox_render import (
    CHUNK_LIMIT, FIELD_NOTES, FIELD_VALUE_LIMIT, SAFE_LIMIT, TELEGRAM_LIMIT,
    build_keyboard, chunk_text, coerce, edit_keyboard, esc, field_order, first_interrupt,
    format_value, full_text_pieces, parse_request, plain_outcome, preference_change,
    preference_preview, render_draft, render_list, render_message, reply_outcome, short_id,
)

load_dotenv()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
GRAPH_URL = os.getenv("LANGGRAPH_URL", "http://127.0.0.1:2024")
# The Gmail graph.
ASSISTANT_ID = os.getenv("TELEGRAM_ASSISTANT_ID", "email_assistant_hitl_memory_gmail")
# The small graph that shows or rewrites one preference profile on request.
# Run with no thread, so it never appears in the inbox.
PREFS_ASSISTANT_ID = os.getenv("TELEGRAM_PREFS_ASSISTANT_ID", "preferences")
# code -> (store namespace, button label). The code goes into callback data,
# which Telegram caps at 64 bytes; the namespace never does.
PREFS = {
    "cal": ("cal_preferences", "\U0001f4c5 Calendar"),
    "reply": ("response_preferences", "✉️ Replies"),
    "triage": ("triage_preferences", "\U0001f500 Triage"),
}
# The poll is only the safety net now: after you answer, `follow_run` reacts as
# soon as the graph settles. This interval decides how quickly a BRAND NEW email
# is noticed, and the server is on localhost, so a short one is cheap.
POLL_SECONDS = float(os.getenv("TELEGRAM_POLL_SECONDS", "5"))

# What to say the moment a button is pressed. Built by hand because
# f"{action.title()}ed." happened to work for accept and produced "Ignoreed."
# for ignore.
ACKNOWLEDGED = {"accept": "Accepted.", "ignore": "\U0001f6ab Email ignored."}
UNREACHABLE = "Could not reach the agent, so nothing was done. Try again in a moment."

# A double tap on a list button arrives as two presses about a second apart,
# and each one posted the whole email again. Repeats inside this window are
# ignored for the same question.
OPEN_DEBOUNCE_SECONDS = 5.0

# Where the agent keeps what it has learned. Only the root is named here - the
# individual namespaces under it are discovered, so a new one starts being
# reported without any change to this file.
MEMORY_NAMESPACE = os.getenv("TELEGRAM_MEMORY_NAMESPACE", "email_assistant")

# Which list message is current, kept on disk. Without it every restart posted
# the inbox again and left the previous list's buttons live, so the chat filled
# with duplicate lists. logs/ is gitignored.
STATE_FILE = Path(os.getenv("TELEGRAM_STATE_FILE", "logs/telegram_bot_state.json"))


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s", level=logging.INFO
)
# httpx logs the full request URL at INFO, and the Telegram API puts the bot
# token IN the URL. Left alone, every poll writes your token to the log - and on
# a server that means the token sits in a file on disk. Warnings still show.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("telegram_inbox")


# --------------------------------------------------------------------------
# Sending answers back
# --------------------------------------------------------------------------


async def resume(context: ContextTypes.DEFAULT_TYPE, thread_id: str, payload: dict):
    """Send one HITL answer. Always a list - the graph reads [0].

    Returns False if the server could not be reached. It used to raise, which
    died inside the button handler: the press did nothing and nobody was told.
    """
    ensure_state(context.bot_data)
    client = context.bot_data["client"]
    log.info("resuming %s with %s", thread_id, payload.get("type"))
    # Take the "before" reading FIRST. Without it the outcome can only say
    # that the run ended, not what it actually did or learned.
    # The memory reading is taken ONCE per email, at your first answer, and
    # carried across every later question on that thread. Taken fresh each
    # time, a Respond that led to a redraft had its learning silently lost:
    # the next answer's "before" reading already included it, so the outcome
    # never mentioned a preference at all.
    cycle_store = context.bot_data["cycle_store"]
    if thread_id not in cycle_store:
        cycle_store[thread_id] = await store_snapshot(client)
    mark = {
        "answer": payload.get("type"),
        "messages": await message_count(client, thread_id),
        "store": cycle_store[thread_id],
        "approved": set(context.bot_data["approved"].get(thread_id) or ()),
        "asked": set(context.bot_data["asked"].get(thread_id) or ()),
    }
    try:
        run = await client.runs.create(
            thread_id,
            ASSISTANT_ID,
            command={"resume": [payload]},
        )
    except Exception as exc:  # noqa: BLE001
        log.error("could not resume %s: %s", thread_id, exc, exc_info=bug_trace(exc))
        return False
    # FAULT 3. Keep watching, so we can say how the run ends. The bot used to
    # stop caring the moment you pressed a button: a failed run said nothing,
    # and a successful one said nothing either. On the Gmail agent that means
    # never learning whether the email actually went.
    #
    # FAULT 4. Nothing is discarded from `seen` any more. `seen` holds
    # INTERRUPT ids now, and the next interrupt has a different id, so it
    # announces itself. Discarding the thread id used to re-announce the
    # request you had just answered, because runs.create returns before the
    # server has moved the thread out of `interrupted`.
    context.bot_data["watching"][thread_id] = mark
    # It is no longer waiting on you. The list is NOT refreshed here - that
    # would land mid-cycle, before you had finished with this email.
    context.bot_data["pending"].pop(thread_id, None)

    # Follow this run instead of waiting for the next tick.
    run_id = (run or {}).get("run_id") if isinstance(run, dict) else None
    if run_id:
        asyncio.create_task(follow_run(context, thread_id, str(run_id)))
    return True


async def send_html(bot, chat_id: int, text: str, reply_markup=None):
    """Send a message, and fall back to plain text if Telegram refuses it.

    Two things make Telegram reject a message: it is over 4096 characters, or
    the HTML has a tag it cannot parse. Both are survivable - the words are
    still readable without the bold - and losing an interrupt is far worse
    than losing the formatting. Without this, a refused message was retried
    every poll and never got through.
    """
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    except BadRequest as exc:
        log.warning("Telegram refused the HTML message (%s), sending plain", exc)
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        if len(plain) > TELEGRAM_LIMIT - 100:
            plain = plain[: TELEGRAM_LIMIT - 100].rstrip() + "\n\n[... trimmed]"
        return await bot.send_message(
            chat_id=chat_id, text=plain, reply_markup=reply_markup
        )


# --------------------------------------------------------------------------
# Poller
# --------------------------------------------------------------------------


# What a call to the LangGraph server or to Telegram raises when the other side
# is down, slow or says no. The bot must live through these, so its handlers
# catch every exception, log it and carry on. An expected failure is one log
# line; anything else is a bug in this file, and its traceback is logged too
# (bug_trace), so it cannot hide behind "could not reach".
EXPECTED_ERRORS = (httpx.HTTPError, OSError, TelegramError)


def bug_trace(exc: BaseException) -> BaseException | None:
    """The `exc_info` for a log call: the traceback of a bug, none for an expected failure."""
    return None if isinstance(exc, EXPECTED_ERRORS) else exc


async def retry_stale(call):
    """Run one server call, retrying ONCE if the connection was already closed.

    The graph server (uvicorn) closes an idle connection after 5 seconds, and
    the bot polls every 5 seconds. Every so often the bot reuses a connection
    the server has just closed, and the request fails with httpx.ReadError -
    logged as "could not reach LangGraph" several times an hour while the
    server was perfectly healthy. A second attempt opens a fresh connection.
    A real outage still fails, just one attempt later.
    """
    try:
        return await call()
    except (httpx.ReadError, httpx.RemoteProtocolError):
        return await call()


async def poll_interrupts(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Announce anything the graph is waiting on.

    Returns False when the server could not be reached, so the caller knows
    the inbox it holds is STALE, not empty.
    """
    ensure_state(context.bot_data)
    client = context.bot_data["client"]
    seen: set[str] = context.bot_data["seen"]
    try:
        # Only this bot's graph. The server tags each thread with the graph
        # that last ran on it; a waiting thread of another graph once showed
        # up here and was resumed with the Gmail graph, which cannot run it.
        threads = await retry_stale(
            lambda: client.threads.search(
                status="interrupted", metadata={"graph_id": ASSISTANT_ID}, limit=20
            )
        )
    except Exception as exc:  # noqa: BLE001 - a dead server must not kill the bot
        # Name the exception type. Several of these carry an empty message,
        # which logged as "could not reach LangGraph at ...: " and nothing more.
        log.warning("could not reach LangGraph at %s: %s %s", GRAPH_URL, type(exc).__name__, exc,
                    exc_info=bug_trace(exc))
        return False

    for thread in threads:
        thread_id = thread["thread_id"]
        found = first_interrupt(thread)
        if found is None:
            continue
        interrupt_id, value = found
        # FAULT 4. Keyed on the interrupt, not the thread. Verified against a
        # live graph: re-reading one interrupt gives the same id, and the next
        # interrupt gives a different one.
        key = f"{thread_id}:{interrupt_id}"
        if key in seen:
            continue
        req = parse_request(value)
        req["interrupt_id"] = interrupt_id
        context.bot_data["pending"][thread_id] = req
        seen.add(key)
        # Remember that THIS tool needed your approval, so the outcome message
        # can report it and stay quiet about the agent's own working.
        context.bot_data["approved"].setdefault(thread_id, set()).add(req["action"])
        # A stop with no Accept button asks you something rather than doing
        # something - the agent's Question. The outcome leaves its result out.
        if not req["allowed"].get("allow_accept"):
            context.bot_data["asked"].setdefault(thread_id, set()).add(req["action"])
        # It stopped again rather than finishing, so there is no outcome to
        # report - this new question replaces it.
        context.bot_data["watching"].pop(thread_id, None)
        log.info("waiting: %s (%s)", thread_id, req["action"])
        # This is the email you are working on, so carry straight on to its
        # next question rather than sending you back to the list mid-cycle.
        if context.bot_data.get("focus") == thread_id:
            try:
                await send_html(
                    context.bot,
                    CHAT_ID,
                    render_message(req),
                    build_keyboard(thread_id, req["allowed"], req.get("interrupt_id", "")),
                )
            except Exception as exc:  # noqa: BLE001
                log.error("could not show the next step of %s: %s", thread_id, exc, exc_info=bug_trace(exc))
    return True


async def message_count(client, thread_id: str) -> int | None:
    """How many messages the thread holds right now, or None when it cannot be read.

    None, not 0. The outcome report shows the tool results after this count,
    and a 0 made it replay every old tool result of the thread as new.
    """
    try:
        state = await retry_stale(lambda: client.threads.get_state(thread_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read the state of %s: %s", thread_id, exc, exc_info=bug_trace(exc))
        return None
    return len((state.get("values") or {}).get("messages") or [])


async def store_snapshot(client) -> dict:
    """Everything the agent has learned, as {namespace/key: value}.

    The namespaces are DISCOVERED by one search rather than listed here, so
    this keeps working if the agent gains a fourth kind of preference.
    """
    try:
        found = await client.store.search_items([MEMORY_NAMESPACE], limit=100)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read the memory store: %s", exc, exc_info=bug_trace(exc))
        return {}
    out = {}
    for item in found.get("items") or []:
        name = "/".join(item.get("namespace") or [])
        out[f"{name}/{item.get('key')}"] = str(item.get("value"))
    return out


async def follow_run(context: ContextTypes.DEFAULT_TYPE, thread_id: str, run_id: str):
    """Wait for one run to settle, then react at once.

    Without this the bot only noticed on its next tick, so every answer felt
    like it took up to a full poll interval. `runs.join` returns as soon as the
    run finishes - interrupted again, idle, or failed - so the next question or
    the outcome lands in well under a second.

    If the join fails for any reason the regular poll still catches up, which is
    why this never raises.
    """
    client = context.bot_data["client"]
    try:
        await client.runs.join(thread_id, run_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not follow run %s: %s", run_id, exc, exc_info=bug_trace(exc))
        return
    await poll(context)


def load_list_state() -> dict:
    """The list message the previous run left behind, if any."""
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_list_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Record the current list, so a restart can pick up where this run left off.

    Only the real bot writes it (main() sets `persist_state`). Any other caller
    leaves it alone, so it cannot overwrite the running bot's record.
    """
    if not context.bot_data.get("persist_state"):
        return
    signature = context.bot_data.get("list_signature")
    record = {
        "list_message_id": context.bot_data.get("list_message_id"),
        "list_signature": [list(item) for item in signature] if signature is not None else None,
    }
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(record))
    except OSError as exc:
        log.warning("could not save the list state: %s", exc)


async def refresh_list(context: ContextTypes.DEFAULT_TYPE, force: bool = False):
    """Post the inbox list, but only when it has actually changed.

    The signature includes each interrupt's id, not just its thread, so a
    thread that moves on to its next question counts as a change and the list
    is reposted rather than left stale.
    """
    pending = context.bot_data["pending"]
    signature = tuple(sorted((tid, r.get("interrupt_id", "")) for tid, r in pending.items()))
    if not force and signature == context.bot_data.get("list_signature"):
        return
    context.bot_data["list_signature"] = signature

    # Take the buttons off the previous list so only the newest one is live.
    # By message id rather than a Message object, because the id survives a
    # restart and the object does not. Cosmetic, so a failure only warns.
    old_id = context.bot_data.get("list_message_id")
    if old_id is not None:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=CHAT_ID, message_id=old_id, reply_markup=None
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not retire the old list: %s", exc, exc_info=bug_trace(exc))
    context.bot_data["list_message_id"] = None

    if not pending:
        save_list_state(context)
        if force:
            await send_html(context.bot, CHAT_ID, "<b>Nothing waiting.</b>")
        return

    text, keyboard = render_list(pending)
    try:
        sent = await send_html(context.bot, CHAT_ID, text, keyboard)
        context.bot_data["list_message_id"] = getattr(sent, "message_id", None)
    except Exception as exc:  # noqa: BLE001
        # Do not remember the signature if the send failed, so the next poll
        # tries again rather than losing the notification silently.
        context.bot_data["list_signature"] = None
        log.error("could not post the list: %s", exc, exc_info=bug_trace(exc))
    save_list_state(context)


async def final_summary(client, thread_id: str, mark: dict) -> str:
    """Say what the run DID and what it LEARNED, in plain words.

    Both come from outside the graph: the tool results the graph already
    stores, and a before/after reading of the memory store. Nothing in the
    agent had to change to make this possible.
    """
    lines = []
    try:
        state = await client.threads.get_state(thread_id)
        messages = (state.get("values") or {}).get("messages") or []
        email = (state.get("values") or {}).get("email_input")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read the result of %s: %s", thread_id, exc, exc_info=bug_trace(exc))
        messages, email = [], None
        lines.append(f"The result could not be read: {esc(str(exc))}")

    # Which tool produced which result. Tool messages carry only the id of the
    # call they answer, so the name and arguments come from the AI message above.
    names, call_args = {}, {}
    for m in messages:
        if isinstance(m, dict) and m.get("type") == "ai":
            for call in m.get("tool_calls") or []:
                names[call.get("id")] = call.get("name")
                call_args[call.get("id")] = call.get("args") or {}

    # Report only tools you were ASKED to approve. Everything else is the
    # agent's own working - checking the calendar, for instance - and you did
    # not need to see it. `approved` is empty after a restart, and then we
    # would rather show too much than hide the result entirely.
    approved = mark.get("approved") or set()
    # A Question's result only repeats that you answered it ("User answered
    # the question, which can we can use..."). The email that follows is what
    # actually happened, so that is what is shown.
    asked = mark.get("asked") or set()

    # After an ignore, every tool result is the graph telling the MODEL what you
    # did - "User ignored this calendar meeting draft." Reading that back is
    # noise, and "Email ignored." already replaced the message the moment you
    # pressed the button. So an ignore reports memory changes only.
    start = mark.get("messages", 0)
    if mark.get("answer") != "ignore" and start is None:
        # The message count could not be read when you answered, so the
        # results of this answer cannot be told from older ones.
        lines.append("What the agent did could not be matched to your answer, so it is not shown. "
                     "LangGraph Studio shows the whole run.")
    elif mark.get("answer") != "ignore":
        # Keep the LAST result per tool. A tool that was redrafted leaves
        # earlier results behind, and only the final one is what happened.
        picked = {}
        for m in messages[start:]:
            if not (isinstance(m, dict) and m.get("type") == "tool" and m.get("content")):
                continue
            name = names.get(m.get("tool_call_id")) or ""
            if (approved and name not in approved) or name in asked:
                continue
            picked[name] = (str(m["content"]), call_args.get(m.get("tool_call_id")) or {})
        for name, (content, args) in picked.items():
            reply = None
            if name == "send_email_tool" and content.startswith("Email reply sent"):
                reply = reply_outcome(email, args.get("email_id"))
            lines.append(reply or "\u2705 " + esc(plain_outcome(content)))

    before = mark.get("store") or {}
    after = await store_snapshot(client)
    # Only keys that were there at your first answer. A key missing from the
    # "before" reading is the graph saving its default text on first use, or a
    # reading that failed - neither is something the agent learned from you.
    for key in sorted(k for k in before if k in after and before[k] != after[k]):
        if len(key.split("/")) < 2:
            continue
        change = preference_change(before[key], after[key])
        if not change:
            continue
        lines.append(f"\U0001f9e0 Preference noted ({esc(key.split('/')[-2])})")
        lines.append(f"<blockquote>{change}</blockquote>")

    if not lines:
        # The ignore was already confirmed on the button press. An empty
        # string tells the caller there is nothing more to send.
        if mark.get("answer") == "ignore":
            return ""
        lines.append("Nothing further was done.")
    return "\n".join(lines)


async def poll_outcomes(context: ContextTypes.DEFAULT_TYPE):
    """FAULT 3. Report how a run ended, once you have answered it."""
    ensure_state(context.bot_data)
    client = context.bot_data["client"]
    watching: dict = context.bot_data["watching"]
    for thread_id, mark in list(watching.items()):
        try:
            thread = await retry_stale(lambda: client.threads.get(thread_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not check %s: %s", thread_id, exc, exc_info=bug_trace(exc))
            continue

        status = thread.get("status")
        # Still running, or stopped again - the interrupt poller owns it now.
        if status in ("busy", "interrupted"):
            continue
        watching.pop(thread_id, None)
        context.bot_data["pending"].pop(thread_id, None)
        context.bot_data["approved"].pop(thread_id, None)
        context.bot_data["asked"].pop(thread_id, None)
        context.bot_data["draft"].pop(thread_id, None)
        context.bot_data["cycle_store"].pop(thread_id, None)

        if status == "error":
            log.error("run failed on %s", thread_id)
            text = (
                "\u274c <b>The run failed.</b>\n\nThe graph stopped with an error, "
                "so nothing further was done. The server log has the traceback."
            )
        else:
            text = await final_summary(client, thread_id, mark)
            log.info("reported outcome for %s (%s)", thread_id, status)
        # That email is done, so the next list is welcome again.
        if context.bot_data.get("focus") == thread_id:
            context.bot_data["focus"] = None
        if not text:
            continue
        try:
            await send_html(context.bot, CHAT_ID, text)
        except Exception as exc:  # noqa: BLE001
            log.error("could not report the outcome of %s: %s", thread_id, exc, exc_info=bug_trace(exc))


class BotData(TypedDict):
    """Everything the bot remembers, in `context.bot_data`, with its type.

    The one description of it: ensure_state() starts each key from its type
    (see starting_value). Only the list message is kept on disk (STATE_FILE);
    the rest is lost on a restart.
    """
    client: Any                     # the LangGraph server client; main() sets it
    seen: set[str]                  # "<thread_id>:<interrupt_id>" already announced
    pending: dict[str, dict]        # thread_id -> the request we showed you (parse_request)
    awaiting: dict[int, dict]       # prompt message_id -> {"thread_id", "mode", "field"} it waits for
    awaiting_last: int | None       # the newest of those prompts
    watching: dict[str, dict]       # thread_id -> the reading taken when you answered (resume)
    approved: dict[str, set[str]]   # thread_id -> tool names you were asked to approve
    asked: dict[str, set[str]]      # thread_id -> tools that only asked you something (no Accept)
    draft: dict[str, dict]          # thread_id -> args you are part-way through editing
    cycle_store: dict[str, dict]    # thread_id -> memory as it was at your FIRST answer
    opened: dict[str, tuple]        # thread_id -> (interrupt_id, when it was last opened)
    prefs_draft: dict[str, dict]    # prefs code -> {"based_on", "text"} shown and waiting for Apply
    list_message_id: int | None     # the list message whose buttons are live
    list_signature: tuple | None    # what that list showed: ((thread_id, interrupt_id), ...)
    focus: str | None               # the thread you have open; the list waits until it is done
    polling: bool                   # a poll is running
    poll_again: bool                # a poll was asked for while one ran
    persist_state: bool             # only the real bot writes STATE_FILE
    net_errors: int                 # Telegram reconnects so far (on_error)


BOT_DATA_TYPES = get_type_hints(BotData)


def starting_value(kind) -> Any:
    """What a BotData key starts as: an empty set or dict, False, 0, or None."""
    origin = get_origin(kind) or kind
    if origin in (set, dict):
        return origin()
    if kind is bool:
        return False
    if kind is int:
        return 0
    return None


def ensure_state(bot_data) -> None:
    """Make sure every store exists before anything reads it.

    main() sets these up, but the poll job runs on a timer and must never die
    from a missing key - a dead job means interrupts stop being announced at
    all, which is the worst failure this bot has.
    """
    for name, kind in BOT_DATA_TYPES.items():
        if name not in bot_data:
            bot_data[name] = starting_value(kind)


async def poll(context: ContextTypes.DEFAULT_TYPE):
    """One job, in order: new questions first, then outcomes.

    Guarded against overlap, because `follow_run` triggers a poll of its own
    and it must not run alongside the timer's - two at once could announce the
    same thing twice. A poll asked for while one runs is not dropped: the
    running one goes round once more. It used to be dropped, so the result of
    an answer sometimes waited for the next tick of the timer.
    """
    ensure_state(context.bot_data)
    if context.bot_data["polling"]:
        context.bot_data["poll_again"] = True
        return
    context.bot_data["polling"] = True
    try:
        while True:
            context.bot_data["poll_again"] = False
            reached = await poll_interrupts(context)
            await poll_outcomes(context)
            # One short list, rather than every request posted in full as it
            # arrives - and only once you are between emails. While you are
            # part-way through one, a new arrival waits quietly in `pending`
            # instead of interrupting you.
            # Only when the server answered THIS time. After a failed search
            # `pending` holds what we last knew - just after a restart, nothing -
            # and a list built from that retired the real list, then posted a
            # duplicate the moment the server came back.
            if reached and context.bot_data.get("focus") is None:
                await refresh_list(context)
            if not context.bot_data["poll_again"]:
                break
    finally:
        context.bot_data["polling"] = False


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


def mine(update: Update) -> bool:
    """Lock the bot to one chat. Without this anyone who finds the bot could
    read the email drafts and approve sends."""
    chat = update.effective_chat
    if chat is None or chat.id != CHAT_ID:
        log.warning("ignored message from chat %s", getattr(chat, "id", "?"))
        return False
    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not mine(update):
        return
    await update.message.reply_text(
        f"Connected.\nGraph: {ASSISTANT_ID}\nServer: {GRAPH_URL}\n"
        f"Polling every {POLL_SECONDS:.0f}s.\n\n/list shows what is waiting.\n"
        "/prefs shows what the agent has learned, and lets you change it."
    )


async def acknowledge(query):
    """Stop the spinner on a pressed button. Cosmetic, and allowed to fail."""
    try:
        await query.answer()
    except TelegramError as exc:  # noqa: BLE001
        log.warning("could not acknowledge the press: %s", exc)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the inbox on demand, even if nothing has changed.

    It is also the way out when you are lost. Every prompt still waiting for
    your text is closed, so a stray message cannot answer an email you have
    put down. Edit drafts and /prefs previews keep their buttons.
    """
    if not mine(update):
        return
    ensure_state(context.bot_data)
    context.bot_data["focus"] = None
    closed = len(context.bot_data["awaiting"])
    context.bot_data["awaiting"].clear()
    context.bot_data["awaiting_last"] = None
    if closed:
        log.info("/list closed %d open prompt(s)", closed)
        noun = "request that was" if closed == 1 else "requests that were"
        await update.message.reply_text(
            f"Closed {closed} {noun} waiting for your text. Nothing was sent."
        )
    await refresh_list(context, force=True)


async def drop_buttons(query):
    """Take the buttons off a message once it has been acted on.

    Without this the four buttons stayed live after Respond and Edit, so a
    second press sent another resume to a thread that was no longer waiting.
    The text is left on screen on purpose - you want to see the draft while
    you type the replacement. Telegram will not edit a message older than 48
    hours, and that is not worth failing the whole handler over.
    """
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest as exc:
        log.warning("could not remove the buttons: %s", exc)


async def remember_prompt(context, sent, thread_id: str, mode: str, field=None):
    """FAULT 5. Record what THIS prompt message is waiting for.

    There used to be one `awaiting` slot for the whole bot. Opening a second
    request silently threw the first away, and whatever you typed went to
    whichever was most recent. Each prompt is now keyed by its own message id,
    so replying to a prompt sends your text to that request and no other.
    """
    context.bot_data["awaiting"][sent.message_id] = {
        "thread_id": thread_id,
        "mode": mode,
        "field": field,
    }
    context.bot_data["awaiting_last"] = sent.message_id
    if len(context.bot_data["awaiting"]) > 1:
        await sent.reply_text(
            "More than one request is now waiting for your text. Reply "
            "directly to the one you mean, otherwise your message goes to "
            "this, the most recent."
        )


async def ask_for_field(message, context, thread_id: str, field: str, args: dict):
    """Ask for one field's new value, and record what we are waiting for."""
    current = format_value(args.get(field))
    if len(current) > FIELD_VALUE_LIMIT:
        current = current[:FIELD_VALUE_LIMIT].rstrip() + " ..."
    # Say what KIND of value is expected. A time typed as "2:30pm" or "1430"
    # would reach the tool unchanged, so the format is spelled out.
    original = args.get(field)
    if isinstance(original, list):
        hint = "\nSeparate several values with commas."
    elif isinstance(original, bool):
        hint = "\nExpects true or false."
    elif field in FIELD_NOTES:
        hint = "\n" + FIELD_NOTES[field]
    elif isinstance(original, int):
        hint = "\nExpects a whole number."
    else:
        hint = ""
    # <pre> is a full code block on Telegram - monospace, syntax colouring and
    # a copy button. Right for a draft email, absurd for the number 45. Short
    # single-line values get inline <code> and sit on the same line.
    escaped = esc(current)
    if not current:
        shown = "Current: (empty)"
    elif "\n" in current or len(current) > 80:
        shown = f"Current:\n<pre>{escaped}</pre>"
    else:
        shown = f"Current: <code>{escaped}</code>"
    sent = await message.reply_text(
        f"Send the new value for <b>{esc(field)}</b>. "
        f"Every other field stays as it is.{hint}\n\n{shown}",
        parse_mode=ParseMode.HTML,
    )
    await remember_prompt(context, sent, thread_id, "edit", field)


# --------------------------------------------------------------------------
# Preferences: see what the agent has learned, and change it in plain words
# --------------------------------------------------------------------------


def prefs_menu() -> tuple[str, InlineKeyboardMarkup]:
    # One button per row. Three in a row did not fit a phone screen, and
    # Telegram trimmed "Calendar" to "...ndar".
    rows = [
        [InlineKeyboardButton(label, callback_data=f"prefs|{code}|show")]
        for code, (_, label) in PREFS.items()
    ]
    return "<b>Your preferences.</b> Pick one to see it.", InlineKeyboardMarkup(rows)


async def prefs_run(context, payload: dict) -> dict | None:
    """One stateless run of the preferences graph.

    Returns the graph's output, or None when the server could not be reached.
    The graph reports its own problems as {"error": ...} rather than failing,
    so a None here really does mean "not reachable".
    """
    client = context.bot_data["client"]
    try:
        out = await client.runs.wait(None, PREFS_ASSISTANT_ID, input=payload)
    except Exception as exc:  # noqa: BLE001
        log.error("preferences run (%s) failed: %s", payload.get("mode"), exc, exc_info=bug_trace(exc))
        return None
    if not isinstance(out, dict):
        log.error("preferences run returned %s, not a dict", type(out).__name__)
        return None
    return out


async def cmd_prefs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not mine(update):
        return
    ensure_state(context.bot_data)
    text, keyboard = prefs_menu()
    await send_html(context.bot, CHAT_ID, text, keyboard)


async def send_lines(context, head: str, lines: list[str], tail: str, keyboard) -> None:
    """Send HTML lines under one heading, split between lines when they will
    not fit one message. The tail and the buttons go on the last part."""
    room = len(head) + len(tail) + 8
    chunks, current, size = [], [], room
    for line in lines:
        if current and size + len(line) + 1 > SAFE_LIMIT:
            chunks.append(current)
            current, size = [], room
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append(current)
    for number, chunk in enumerate(chunks, start=1):
        heading = head + (f" ({number}/{len(chunks)})" if len(chunks) > 1 else "")
        last = number == len(chunks)
        text = f"{heading}\n\n" + "\n".join(chunk) + (f"\n\n{tail}" if last else "")
        await send_html(context.bot, CHAT_ID, text, keyboard if last else None)


async def send_profile(context, heading: str, body: str, keyboard) -> None:
    """A profile can run past one Telegram message. Plain text, split at blank
    lines, with the buttons on the last part."""
    pieces = chunk_text(body, CHUNK_LIMIT)
    for number, piece in enumerate(pieces, start=1):
        head = f"<b>{esc(heading)}</b>" + (f" ({number}/{len(pieces)})" if len(pieces) > 1 else "")
        last = number == len(pieces)
        await send_html(context.bot, CHAT_ID, f"{head}\n\n{esc(piece)}", keyboard if last else None)


async def on_prefs_button(query, context, code: str, verb: str) -> None:
    if verb == "menu" or code not in PREFS:
        text, keyboard = prefs_menu()
        await send_html(context.bot, CHAT_ID, text, keyboard)
        return
    namespace, label = PREFS[code]

    if verb == "show":
        out = await prefs_run(context, {"namespace": namespace, "mode": "show"})
        if out is None:
            await query.message.reply_text(UNREACHABLE)
            return
        if out.get("error"):
            await query.message.reply_text(f"The agent said: {out['error']}")
            return
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✏️ Change", callback_data=f"prefs|{code}|change"),
            InlineKeyboardButton("← Back", callback_data="prefs|-|menu"),
        ]])
        await send_profile(context, label, out.get("current") or "(empty)", keyboard)
        return

    if verb == "change":
        sent = await query.message.reply_text(
            f"Tell me what to change in {label}, in plain words. For example: "
            "\"scoping calls are 30 minutes\" or \"remove the line about Fridays\"."
        )
        # Same route as Respond: keyed by prompt, so Telegram's Reply works.
        await remember_prompt(context, sent, code, "prefs")
        return

    if verb == "cancel":
        context.bot_data["prefs_draft"].pop(code, None)
        await drop_buttons(query)
        await query.message.reply_text("Cancelled. Nothing was changed.")
        return

    if verb == "apply":
        draft = context.bot_data["prefs_draft"].get(code)
        if draft is None:
            await drop_buttons(query)
            await query.message.reply_text("That proposal has expired. Start again with /prefs.")
            return
        out = await prefs_run(context, {
            "namespace": namespace, "mode": "apply",
            "text": draft["text"], "based_on": draft["based_on"],
        })
        if out is None:
            # Keep the draft and the button, so one more press is enough.
            await query.message.reply_text(UNREACHABLE + " Your proposal was kept - press Apply again.")
            return
        context.bot_data["prefs_draft"].pop(code, None)
        await drop_buttons(query)
        if out.get("saved"):
            await query.message.reply_text(f"✅ Saved. The next email uses the new {label} preferences.")
        elif out.get("error") == "changed meanwhile":
            await query.message.reply_text(
                f"Not saved: {label} changed while you were editing - the agent learned "
                "something from an email. Open it again with /prefs and look first."
            )
        else:
            await query.message.reply_text(f"Not saved. The agent said: {out.get('error') or 'unknown problem'}")
        return


async def propose_preference(update, context, key, job: dict, instruction: str) -> None:
    """Ask the agent to rewrite one profile, and show the result for approval.

    Nothing is saved here. The rewritten text sits in `prefs_draft` until Apply,
    together with the text it was based on - so a profile that an email's
    feedback rewrote in the meantime is never silently overwritten.
    """
    code = job["thread_id"]
    namespace, label = PREFS[code]
    await update.message.reply_text("Rewriting... this takes a few seconds.")
    out = await prefs_run(context, {"namespace": namespace, "mode": "propose", "instruction": instruction})
    if out is None:
        # Re-arm the prompt, so sending the same text again is all it takes.
        context.bot_data["awaiting"][key] = job
        context.bot_data["awaiting_last"] = key
        await update.message.reply_text(
            "Could not reach the agent, so nothing was changed. Send it again in a moment."
        )
        return
    if out.get("error"):
        await update.message.reply_text(f"The agent said: {out['error']}")
        return
    current, proposed = out.get("current") or "", out.get("proposed") or ""
    change = preference_change(current, proposed)
    if not change:
        await update.message.reply_text("That would change nothing - the text already says that.")
        return
    context.bot_data["prefs_draft"][code] = {"based_on": current, "text": proposed}
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Apply", callback_data=f"prefs|{code}|apply"),
        InlineKeyboardButton("✖ Cancel", callback_data=f"prefs|{code}|cancel"),
    ]])
    await send_lines(
        context,
        f"<b>✏️ {esc(label)} — proposed</b>",
        preference_preview(current, proposed),
        "<i>Struck through = removed · Bold = added. Nothing is saved until you press Apply.</i>",
        keyboard,
    )


# --------------------------------------------------------------------------
# Button presses and typed text
# --------------------------------------------------------------------------


@dataclass
class Press:
    """One button press, read from its callback data `action|target|extra`.

    `target` is a thread id, except on the prefs buttons, where it is a
    profile code. `req` is that thread's request as it was when the press
    arrived, or None when nothing is waiting on the thread any more.
    """
    query: Any
    context: ContextTypes.DEFAULT_TYPE
    action: str
    target: str
    extra: str | None
    req: dict | None


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send every button press to its handler in BUTTONS."""
    if not mine(update):
        return
    ensure_state(context.bot_data)
    query = update.callback_query
    # Log the press BEFORE anything can fail. Without this line a press that
    # died on its first await was indistinguishable from one that never
    # arrived, and there was no way to tell which from the log.
    log.info("button press: %s", query.data)
    # Fire and forget. This call only stops the spinner on the button, so the
    # real work must never queue behind it.
    asyncio.create_task(acknowledge(query))
    # `field` presses carry an extra part: field|<thread_id>|<index>. Thread
    # ids are uuids and never contain a pipe, so a plain split is safe.
    parts = query.data.split("|")
    action, target = parts[0], parts[1]
    extra = parts[2] if len(parts) > 2 else None
    handler = BUTTONS.get(action)
    if handler is None:
        log.warning("no handler for the button %s", query.data)
        return
    # No await before the handler starts, so a handler's own checks (the
    # double-tap guard in press_open) run before any other press is handled.
    await handler(Press(query, context, action, target, extra, context.bot_data["pending"].get(target)))


async def press_prefs(press: Press):
    """prefs|<code>|<verb>: the second part is a profile code, not a thread."""
    await on_prefs_button(press.query, press.context, press.target, press.extra or "menu")


async def press_list(press: Press):
    """Back to list. Stepping back to the list means you have put this email down."""
    press.context.bot_data["focus"] = None
    await refresh_list(press.context, force=True)


async def press_full(press: Press):
    """Full text: the whole request, where the announcement is trimmed on purpose."""
    req = press.req
    if req is None:
        await press.query.message.reply_text("That request is no longer waiting.")
        return
    raw = (req.get("description") or "").strip()
    if not raw:
        await press.query.message.reply_text("There is no more text to show.")
        return
    pieces = full_text_pieces(raw)
    for number, piece in enumerate(pieces, start=1):
        head = f"<b>Full text {number}/{len(pieces)}</b>\n\n" if len(pieces) > 1 else ""
        # The buttons go on the LAST chunk, so you can act without
        # scrolling back up past everything you have just read.
        last = number == len(pieces)
        await send_html(
            press.context.bot,
            CHAT_ID,
            head + piece,
            build_keyboard(press.target, req["allowed"], req.get("interrupt_id", "")) if last else None,
        )


async def press_open(press: Press):
    """A list button: show the email and what the agent proposes."""
    req = press.req
    if req is None:
        await press.query.message.reply_text("That request is no longer waiting.")
        return
    bot_data = press.context.bot_data
    # Ignore a repeat press for the SAME question within a few seconds. The
    # check and the record happen before the first await, so two presses
    # handled concurrently cannot both get through. A new question on the
    # same thread, or a later re-open, still works.
    current = req.get("interrupt_id", "")
    last = bot_data["opened"].get(press.target)
    now = time.monotonic()
    if last and last[0] == current and now - last[1] < OPEN_DEBOUNCE_SECONDS:
        log.info("ignored repeat open for %s", press.target)
        return
    bot_data["opened"][press.target] = (current, now)
    # Work this one through to the end before the list comes back.
    bot_data["focus"] = press.target
    await send_html(
        press.context.bot,
        CHAT_ID,
        render_message(req),
        build_keyboard(press.target, req["allowed"], req.get("interrupt_id", "")),
    )


async def still_waiting(press: Press) -> bool:
    """True when the question this answer button belongs to is still the one waiting.

    Otherwise the buttons are taken off and you are told. Two live copies of
    these buttons exist on purpose - the detail and the end of Full text - and
    old messages stay on screen after the graph moves on. Without this, Accept
    pressed twice resumed the thread twice, and an old Accept could answer the
    NEXT question on the same thread. A button with no id (sent before ids
    existed) is allowed only while its thread is still waiting.
    """
    current = short_id(press.req.get("interrupt_id", "")) if press.req else None
    if press.req is None or (press.extra is not None and press.extra != current):
        await drop_buttons(press.query)
        await press.query.message.reply_text("That question has already been answered.")
        return False
    return True


async def press_accept_or_ignore(press: Press):
    if not await still_waiting(press):
        return
    if not await resume(press.context, press.target, {"type": press.action, "args": None}):
        await press.query.message.reply_text(UNREACHABLE)
        return
    await press.query.edit_message_text(ACKNOWLEDGED.get(press.action, "Done."))


async def press_respond(press: Press):
    """Respond: ask for your text. on_text sends it to the graph."""
    if not await still_waiting(press):
        return
    await drop_buttons(press.query)
    sent = await press.query.message.reply_text("Send your feedback as a normal message.")
    await remember_prompt(press.context, sent, press.target, "respond")


async def press_edit(press: Press):
    """Edit: open a working copy of the arguments, with a button per field."""
    if not await still_waiting(press):
        return
    args = dict(press.req["args"])
    if not args:
        await press.query.message.reply_text("This request has no arguments to edit.")
        return
    # A working copy. Nothing goes to the graph until Send is pressed, so
    # you can correct several fields in one go.
    press.context.bot_data["draft"][press.target] = args
    await drop_buttons(press.query)
    await press.query.message.reply_text(
        render_draft(args),
        parse_mode=ParseMode.HTML,
        reply_markup=edit_keyboard(press.target, field_order(args)),
    )


async def request_expired(press: Press) -> bool:
    """True, after saying so, when nothing is waiting on the thread any more."""
    if press.req is None:
        await press.query.edit_message_text("That request has expired. Waiting for the next one.")
        return True
    return False


async def press_field(press: Press):
    """A field button on the edit panel: ask for that field's new value."""
    if await request_expired(press):
        return
    draft = press.context.bot_data["draft"].get(press.target)
    if draft is None:
        await press.query.message.reply_text("That draft has expired. Press Edit again.")
        return
    keys = field_order(draft)
    index = int(press.extra)
    if index >= len(keys):
        await press.query.message.reply_text("That field is no longer available.")
        return
    # The panel keeps its buttons on purpose - you will want them again.
    await ask_for_field(press.query.message, press.context, press.target, keys[index], draft)


async def press_send(press: Press):
    """Send on the edit panel: the whole edited draft goes to the graph."""
    if await request_expired(press):
        return
    drafts = press.context.bot_data["draft"]
    draft = drafts.pop(press.target, None)
    if draft is None:
        await press.query.message.reply_text("That draft has expired. Nothing was sent.")
        return
    if not await resume(
        press.context,
        press.target,
        {"type": "edit", "args": {"action": press.req.get("action", ""), "args": draft}},
    ):
        # Keep the edits and the Send button, so one more press is enough.
        drafts[press.target] = draft
        await press.query.message.reply_text(UNREACHABLE + " Your edits were kept.")
        return
    await drop_buttons(press.query)
    await press.query.message.reply_text("Edit sent.")


# Each button's action (the first part of its callback data) -> its handler.
# One short function per button, where there used to be one long if-chain.
BUTTONS = {
    "prefs": press_prefs,
    "list": press_list,
    "full": press_full,
    "open": press_open,
    "accept": press_accept_or_ignore,
    "ignore": press_accept_or_ignore,
    "respond": press_respond,
    "edit": press_edit,
    "field": press_field,
    "send": press_send,
}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not mine(update):
        return
    ensure_state(context.bot_data)
    awaiting: dict = context.bot_data["awaiting"]
    if not awaiting:
        # Nothing asked for text, often because /list closed the prompt. Say
        # so, rather than let the message vanish without a trace.
        await update.message.reply_text(
            "Nothing is waiting for your text. Press Respond on an email, "
            "or Change in /prefs, first."
        )
        return
    text = update.message.text

    # Telegram's own reply names the request exactly. Without one, fall back to
    # the most recent prompt - which is the right answer whenever only one is
    # open, and is why the bot warns you when a second one appears.
    reply_to = update.message.reply_to_message
    if reply_to is not None and reply_to.message_id in awaiting:
        key = reply_to.message_id
    elif context.bot_data.get("awaiting_last") in awaiting:
        key = context.bot_data["awaiting_last"]
    else:
        key = next(iter(awaiting))

    job = awaiting.pop(key)
    if context.bot_data.get("awaiting_last") == key:
        context.bot_data["awaiting_last"] = None
    thread_id = job["thread_id"]

    if job["mode"] == "prefs":
        # Not an email at all: thread_id holds a profile code.
        await propose_preference(update, context, key, job, text)
        return

    if job["mode"] == "respond":
        # args is a BARE STRING here. Not a dict. The graph does
        # user_input = response["args"] and drops it straight into a prompt.
        if not await resume(context, thread_id, {"type": "response", "args": text}):
            # Re-arm the prompt, so sending the same text again is all it takes.
            awaiting[key] = job
            context.bot_data["awaiting_last"] = key
            await update.message.reply_text(
                "Could not reach the agent, so nothing was sent. "
                "Send your feedback again in a moment."
            )
            return
        await update.message.reply_text("Feedback sent.")
        return

    # Edit. The value goes into the working copy, NOT to the graph. The whole
    # draft is sent only when you press Send, which is what makes changing a
    # second field possible.
    draft = context.bot_data["draft"].get(thread_id)
    field = job.get("field")
    if draft is None or field is None or field not in draft:
        await update.message.reply_text(
            "That draft has expired, so nothing was sent. Press Edit again."
        )
        return

    # coerce keeps the original type: a list stays a list, an int stays an int.
    value, problem = coerce(draft[field], text)
    if problem:
        extra = f" {FIELD_NOTES[field]}" if field in FIELD_NOTES else ""
        await update.message.reply_text(
            f"<b>Not changed.</b> {esc(field)} {esc(problem)}.{esc(extra)}\n\n"
            f"Try again, or pick a different field.",
            parse_mode=ParseMode.HTML,
        )
        # Re-arm the same prompt so a retry does not need another tap.
        await ask_for_field(update.message, context, thread_id, field, draft)
        return
    draft[field] = value

    # A fresh panel at the bottom of the chat, so the current state of the
    # draft is always the last thing you can see.
    await update.message.reply_text(
        render_draft(draft, changed=field),
        parse_mode=ParseMode.HTML,
        reply_markup=edit_keyboard(thread_id, field_order(draft)),
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """One readable line instead of a forty-line traceback."""
    err = context.error
    text = str(err)

    if "Chat not found" in text:
        log.error(
            "Telegram says 'Chat not found' for chat %s. A bot cannot start a "
            "conversation - open Telegram, find your bot and press Start, then "
            "this will work.",
            CHAT_ID,
        )
        return

    # BadRequest is a SUBCLASS of NetworkError in python-telegram-bot, so the
    # reconnect branch below swallowed every genuine rejection from Telegram -
    # a malformed keyboard, an over-long message, a stale callback. The bot
    # then looked like it was ignoring your button press. Real faults must be
    # loud, so this check has to come first.
    if isinstance(err, BadRequest):
        log.error("Telegram rejected the request: %s", text)
        return

    # Long polling holds a connection open with no traffic on it, and something
    # on the network path - a router or ISP - closes it on a timer, observed at
    # a steady ~40s. python-telegram-bot reopens it immediately and nothing is
    # lost, so this is a reconnect, not a failure. Logging each one as ERROR
    # made a healthy bot look broken, so count them and report occasionally.
    if isinstance(err, NetworkError):
        n = context.bot_data.get("net_errors", 0) + 1
        context.bot_data["net_errors"] = n
        if n == 1:
            log.warning(
                "connection to Telegram dropped and was reopened. Harmless if "
                "it stays periodic - button presses still arrive. Will report "
                "again every 20."
            )
        elif n % 20 == 0:
            log.warning("%d reconnects so far (still harmless)", n)
        return

    log.error("%s: %s", type(err).__name__, text)


def telegram_request(**overrides) -> HTTPXRequest:
    """Build the HTTP layer the bot uses to talk to Telegram, over IPv4 only.

    Measured on one network: IPv6 to api.telegram.org times out on roughly
    one connection in four, while IPv4 answered six out of six in 0.05s. When
    httpx happened to pick the IPv6 address, a button press stalled for the
    whole timeout - which looked exactly like the bot ignoring you. Binding
    the socket to 0.0.0.0, the IPv4 wildcard, makes an IPv6 destination
    unreachable, so only the address family that works is ever used.

    Timeouts are 10s rather than 30s. With IPv4 forced there is no reason for
    a call to take longer, and failing fast beats freezing.

    Set TELEGRAM_ALLOW_IPV6=1 to go back to the default behaviour.
    """
    settings = {
        "connect_timeout": 10.0,
        "read_timeout": 10.0,
        "write_timeout": 10.0,
        "pool_timeout": 10.0,
    }
    settings.update(overrides)
    if os.getenv("TELEGRAM_ALLOW_IPV6") == "1":
        return HTTPXRequest(**settings)
    return HTTPXRequest(
        httpx_kwargs={
            # retries=1 covers a connection that dies during the handshake.
            "transport": httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=1)
        },
        **settings,
    )


async def register_commands(app) -> None:
    """Put the commands in Telegram's menu button, so they can be found without
    remembering them. Cosmetic: a failure only warns."""
    try:
        await app.bot.set_my_commands([
            BotCommand("list", "What is waiting for you"),
            BotCommand("prefs", "See or change the agent's preferences"),
            BotCommand("start", "Check the connection"),
        ])
    except TelegramError as exc:
        log.warning("could not register the command menu: %s", exc)


def main():
    # python-telegram-bot defaults every timeout to 5 seconds, which the first
    # TLS handshake to api.telegram.org can exceed on a cold connection. That
    # shows up as `telegram.error.TimedOut` on get_me() before the bot ever
    # starts. Generous timeouts cost nothing while idle.
    app = (
        Application.builder()
        .token(TOKEN)
        .request(telegram_request())
        # Long polling holds this request open on purpose, so it needs its own,
        # much longer read timeout than ordinary calls.
        .get_updates_request(telegram_request(read_timeout=65.0))
        # Without this, updates are handled strictly one at a time, so one slow
        # press delays every press behind it.
        .concurrent_updates(True)
        .post_init(register_commands)
        .build()
    )
    app.bot_data["client"] = get_client(url=GRAPH_URL)
    # Pick up the list the previous run posted. If the inbox has not changed
    # since, the first poll sees the same signature and posts nothing.
    saved = load_list_state()
    app.bot_data["list_message_id"] = saved.get("list_message_id")
    if saved.get("list_signature") is not None:
        app.bot_data["list_signature"] = tuple(tuple(item) for item in saved["list_signature"])
    app.bot_data["persist_state"] = True
    ensure_state(app.bot_data)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("prefs", cmd_prefs))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(poll, interval=POLL_SECONDS, first=2)

    log.info("watching %s on %s, answering only chat %s", ASSISTANT_ID, GRAPH_URL, CHAT_ID)
    # Retry the initial handshake instead of dying on one slow connection.
    app.run_polling(bootstrap_retries=5)


if __name__ == "__main__":
    main()
