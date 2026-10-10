"""Show or change one of the agent's preference profiles, at the user's request.

A separate, tiny graph so that neither email graph has to change. It works on
the same three store entries both email graphs read at the start of every run:
("email_assistant", <namespace>) / "user_preferences". An edit made here is
picked up by the next email, with no restart.

Why the write happens HERE and not in the Telegram bot: the server's HTTP store
API validates values and rejects a plain string, but the email graphs keep the
profiles as plain strings and read them back as such. Inside a graph,
store.put is not validated, so this graph can keep exactly the same shape.

The bot runs it with no thread (a stateless run), so it never appears in the
bot's inbox and nothing is checkpointed.

Modes - the `mode` field of the input:
  show     -> {"current": text}                       no model call
  propose  -> {"current": text, "proposed": text}     one model call, writes nothing
  apply    -> {"saved": true} or {"saved": false, "error": "changed meanwhile"}
              writes `text`, but only if the store still holds `based_on`
Any problem comes back as {"error": "..."} rather than a failed run, so the
bot always has something to show.
"""

from typing import Literal

from langchain.chat_models import init_chat_model
from langgraph.graph import END, START, StateGraph
from langgraph.store.base import BaseStore
from typing_extensions import TypedDict

from email_assistant.prompts import (
    PREFERENCES_EDIT_INSTRUCTIONS,
    default_cal_preferences,
    default_response_preferences,
    default_triage_instructions,
)
from email_assistant.schemas import UserPreferences

ROOT = "email_assistant"
KEY = "user_preferences"

# The same defaults the email graphs save on first use (get_memory), so a
# profile that has never been written reads the same from here as from there.
DEFAULTS = {
    "triage_preferences": default_triage_instructions,
    "response_preferences": default_response_preferences,
    "cal_preferences": default_cal_preferences,
}


class PreferencesState(TypedDict, total=False):
    namespace: str      # one of DEFAULTS
    mode: str           # show | propose | apply
    instruction: str    # propose: what to change, in the user's words
    text: str           # apply: the full new profile
    based_on: str       # apply: the profile the user was shown before editing
    current: str
    proposed: str
    saved: bool
    error: str


def as_text(value) -> str:
    """The profiles are strings today; tolerate {"text": ...} if that ever changes."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return str(value)


def read_current(store: BaseStore, namespace: str) -> str:
    item = store.get((ROOT, namespace), KEY)
    return as_text(item.value) if item else DEFAULTS[namespace]


def rewrite(namespace: str, current: str, instruction: str) -> str:
    """One model call: the whole profile with the instruction applied.

    A module-level function on purpose, so tests can swap it for a stub and
    exercise the graph without a model.
    """
    llm = init_chat_model("openai:gpt-4.1", temperature=0.0).with_structured_output(UserPreferences)
    result = llm.invoke([
        {"role": "system", "content": PREFERENCES_EDIT_INSTRUCTIONS.format(namespace=namespace, current_profile=current)},
        {"role": "user", "content": instruction},
    ])
    return result.user_preferences


def problem(state: PreferencesState) -> str | None:
    """A plain-words reason the request cannot be served, or None."""
    namespace = state.get("namespace")
    if namespace not in DEFAULTS:
        return f"unknown namespace {namespace!r}; expected one of {sorted(DEFAULTS)}"
    mode = state.get("mode", "show")
    if mode not in ("show", "propose", "apply"):
        return f"unknown mode {mode!r}"
    if mode == "propose" and not (state.get("instruction") or "").strip():
        return "instruction is empty"
    if mode == "apply" and not (state.get("text") or "").strip():
        return "text is empty"
    return None


def show(state: PreferencesState, store: BaseStore):
    if err := problem(state):
        return {"error": err}
    return {"current": read_current(store, state["namespace"])}


def propose(state: PreferencesState, store: BaseStore):
    if err := problem(state):
        return {"error": err}
    current = read_current(store, state["namespace"])
    return {"current": current, "proposed": rewrite(state["namespace"], current, state["instruction"])}


def apply(state: PreferencesState, store: BaseStore):
    if err := problem(state):
        return {"error": err, "saved": False}
    current = read_current(store, state["namespace"])
    based_on = state.get("based_on")
    # An email answered between "propose" and "apply" may have rewritten the
    # profile through feedback. Never overwrite something the user has not seen.
    if based_on is not None and based_on.strip() != current.strip():
        return {"current": current, "saved": False, "error": "changed meanwhile"}
    # A plain string, the shape the email graphs' get_memory expects. The type
    # hint on put says dict, but in-process nothing validates - see the module
    # docstring.
    store.put((ROOT, state["namespace"]), KEY, state["text"])
    return {"current": state["text"], "saved": True}


def pick(state: PreferencesState) -> Literal["show", "propose", "apply"]:
    mode = state.get("mode", "show")
    return mode if mode in ("propose", "apply") else "show"


builder = StateGraph(PreferencesState)
builder.add_node("show", show)
builder.add_node("propose", propose)
builder.add_node("apply", apply)
builder.add_conditional_edges(START, pick, {"show": "show", "propose": "propose", "apply": "apply"})
builder.add_edge("show", END)
builder.add_edge("propose", END)
builder.add_edge("apply", END)

# The server injects its own store; tests compile `builder` with an InMemoryStore.
graph = builder.compile()
