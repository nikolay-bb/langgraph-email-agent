"""What the agent is told after you press Respond, and when it may say "booked".

25 Sep: on a meeting card you answered "I want it to be this Saturday". The
agent was told only "User gave feedback, which can we incorporate into the
meeting request". It did not propose the meeting again; it went straight on to
a reply that said the meeting was scheduled, and no meeting existed.

The handler tests run the graph's real interrupt_handler with interrupt(), the
memory update and the Google read for a card faked, so no model, server or
Google call is made.
"""
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from email_assistant import email_assistant_hitl_memory_gmail as gmail_graph
from email_assistant.prompts import (CANCEL_FEEDBACK, EMAIL_FEEDBACK, MEETING_FEEDBACK, MEMORY_UPDATE_INSTRUCTIONS,
                                     MOVE_FEEDBACK, agent_system_prompt_hitl_memory)
from email_assistant.schemas import UserPreferences

GMAIL_EMAIL = {"from": "Test <r@x.com>", "to": "n@x.com", "subject": "Saturday catch-up",
               "body": "Are you free this Saturday at 11:00?", "id": "m1"}

CASES = [
    ("schedule_meeting_tool", MEETING_FEEDBACK, "cal_preferences",
     {"attendees": ["r@x.com"], "title": "Catch-up", "start_time": "2026-09-27T11:00:00",
      "end_time": "2026-09-27T11:30:00", "organizer_email": "n@x.com", "timezone": "Europe/London"}),
    ("send_email_tool", EMAIL_FEEDBACK, "response_preferences",
     {"email_id": "m1", "response_text": "Saturday works.", "email_address": "n@x.com"}),
    ("update_meeting_tool", MOVE_FEEDBACK, "cal_preferences",
     {"event_id": "ev1", "start_time": "2026-09-26T11:00:00", "end_time": "2026-09-26T11:30:00"}),
    ("cancel_meeting_tool", CANCEL_FEEDBACK, "cal_preferences", {"event_id": "ev1"}),
]

@pytest.fixture(autouse=True)
def no_google(monkeypatch):
    """A move or cancel card reads the event from Google; here it gets fixed text."""
    monkeypatch.setattr(gmail_graph, "event_card_lines", lambda event_id: f"**Meeting**: event {event_id}")


def stop_at(monkeypatch, answer):
    """Fake interrupt(): record the request shown to you, give back `answer`."""
    shown = []
    def interrupt(requests):
        shown.extend(requests)
        return [answer]
    monkeypatch.setattr(gmail_graph, "interrupt", interrupt)
    return shown


def proposal(tool, args, email=GMAIL_EMAIL):
    return {"email_input": email,
            "messages": [AIMessage(content="", tool_calls=[{"name": tool, "args": args, "id": "call-1"}])]}


@pytest.mark.parametrize("tool, template, profile, args", CASES, ids=[c[0] for c in CASES])
def test_respond_tells_the_model_the_tool_did_not_run(monkeypatch, tool, template, profile, args):
    monkeypatch.setattr(gmail_graph, "interrupt", lambda requests: [{"type": "response", "args": "I want it to be this Saturday"}])
    learned, told = [], []
    def update_memory(store, namespace, messages):
        learned.append(namespace)
        told.append(messages[-1]["content"])
    monkeypatch.setattr(gmail_graph, "update_memory", update_memory)

    command = gmail_graph.interrupt_handler(proposal(tool, args), store=None)

    assert command.goto == "llm_call"  # back to the model, which must propose again
    [message] = command.update["messages"]
    assert message["tool_call_id"] == "call-1"
    assert message["content"] == template.format(feedback="I want it to be this Saturday", tool=tool)
    assert "was NOT" in message["content"] and f"call {tool} again" in message["content"]
    assert learned == [("email_assistant", profile)]
    # P6, 25 Sep: "I want it to be this Saturday" taught a weekend rule.
    assert "if it is only about this" in told[0] and "return the profile unchanged" in told[0]


def test_the_agent_may_say_booked_only_after_the_tool_did_it():
    assert ("Never write in an email that a meeting is scheduled, booked, confirmed, moved or cancelled "
            "unless the tool that does it said it was done") in agent_system_prompt_hitl_memory


def test_the_agent_moves_a_meeting_instead_of_making_a_second_one():
    assert "Never schedule a second meeting for one that already exists: move it" in agent_system_prompt_hitl_memory
    assert "update_meeting_tool" in gmail_graph.tools_by_name and "cancel_meeting_tool" in gmail_graph.tools_by_name


@pytest.mark.parametrize("tool, args, heading, edit", [
    ("update_meeting_tool", {"event_id": "ev1", "start_time": "2026-09-28T11:00:00", "end_time": "2026-09-28T11:30:00"},
     "# Move Meeting", True),
    ("cancel_meeting_tool", {"event_id": "ev1"}, "# Cancel Meeting", False),
])
def test_move_and_cancel_stop_for_your_answer(monkeypatch, tool, args, heading, edit):
    shown = stop_at(monkeypatch, {"type": "ignore", "args": None})
    # The card that read_event_cards saved for this tool call
    state = {**proposal(tool, args), "event_cards": {"call-1": "**Meeting**: event ev1"}}
    command = gmail_graph.interrupt_handler(state, store=None)
    [request] = shown
    assert request["action_request"] == {"action": tool, "args": args}
    assert heading in request["description"] and "**Meeting**: event ev1" in request["description"]
    assert request["config"] == {"allow_ignore": True, "allow_respond": True, "allow_edit": edit, "allow_accept": True}
    # Ignore: nothing runs, and the run ends
    assert command.goto == END
    assert command.update["messages"][0]["content"].startswith("User ignored this meeting")


def test_the_handler_never_reads_google_for_a_card(monkeypatch):
    """interrupt_handler runs again from the top on each resume, so a read in it would repeat.

    It used to read the event itself when no card was saved (a thread that
    stopped before read_event_cards existed). Now such a card shows the ID.
    """
    reads = []
    monkeypatch.setattr(gmail_graph, "event_card_lines", lambda event_id: reads.append(event_id) or "read again")
    shown = stop_at(monkeypatch, {"type": "ignore", "args": None})
    gmail_graph.interrupt_handler(proposal("cancel_meeting_tool", {"event_id": "ev1"}), store=None)
    assert reads == []
    assert "**Event ID**: ev1" in shown[0]["description"]


def test_the_card_reads_google_once_not_again_on_resume(monkeypatch):
    """The whole Gmail graph, with a checkpointer, stopped at a Cancel card and resumed.

    A node runs again from the top on resume. The Google read for the card is
    its own node (read_event_cards), before the stop, so it is not run again.
    """
    reads = []
    monkeypatch.setattr(gmail_graph, "event_card_lines",
                        lambda event_id: reads.append(event_id) or f"**Meeting**: event {event_id}")
    monkeypatch.setattr(gmail_graph, "llm_router",
                        SimpleNamespace(invoke=lambda messages: SimpleNamespace(classification="respond")))
    cancel = AIMessage(content="", tool_calls=[{"name": "cancel_meeting_tool", "args": {"event_id": "ev1"}, "id": "call-1"}])
    monkeypatch.setattr(gmail_graph, "llm_with_tools", SimpleNamespace(invoke=lambda messages: cancel))
    graph = gmail_graph.overall_workflow.compile(checkpointer=InMemorySaver(), store=InMemoryStore())
    config = {"configurable": {"thread_id": "t1"}}

    stopped = graph.invoke({"email_input": GMAIL_EMAIL}, config)
    assert "**Meeting**: event ev1" in stopped["__interrupt__"][0].value[0]["description"]
    assert reads == ["ev1"]

    graph.invoke(Command(resume=[{"type": "ignore", "args": None}]), config)
    assert reads == ["ev1"]  # not read again on resume
    assert graph.get_state(config).next == ()  # Ignore ended the run


def done(*names):
    return {"messages": [AIMessage(content="", tool_calls=[{"name": n, "args": {}, "id": f"c{i}"} for i, n in enumerate(names)])]}


def test_should_continue_looks_at_every_tool_call():
    assert gmail_graph.should_continue(done("Done")) == "mark_as_read_node"
    assert gmail_graph.should_continue(done("send_email_tool")) == "interrupt_handler"
    # A reply next to Done must still stop for your answer, not be dropped.
    assert gmail_graph.should_continue(done("Done", "send_email_tool")) == "interrupt_handler"
    # A move or a cancel reads its meeting from Google first, for the card.
    assert gmail_graph.should_continue(done("update_meeting_tool")) == "read_event_cards"
    assert gmail_graph.should_continue(done("cancel_meeting_tool")) == "read_event_cards"


def test_should_continue_with_no_tool_call_says_so():
    with pytest.raises(ValueError, match="called no tool"):
        gmail_graph.should_continue(done())


def test_accept_runs_the_move(monkeypatch):
    stop_at(monkeypatch, {"type": "accept", "args": None})
    ran = []
    fake = SimpleNamespace(invoke=lambda args: ran.append(args) or "Meeting 'Pilot call' moved successfully")
    monkeypatch.setitem(gmail_graph.tools_by_name, "update_meeting_tool", fake)
    args = {"event_id": "ev1", "start_time": "2026-09-28T11:00:00", "end_time": "2026-09-28T11:30:00"}
    command = gmail_graph.interrupt_handler(proposal("update_meeting_tool", args), store=None)
    assert ran == [args]
    assert command.goto == "llm_call"
    assert command.update["messages"][0]["content"] == "Meeting 'Pilot call' moved successfully"


# --- The earlier emails of the conversation ---------------------------------

HISTORY = [{"from": "n@x.com", "to": "r@x.com", "date": "Wed", "body": "Booked: Thursday 14:00, event for the pilot."}]


def test_the_model_sees_the_earlier_emails(monkeypatch):
    decision = SimpleNamespace(classification="respond")
    asked = []
    monkeypatch.setattr(gmail_graph, "llm_router", SimpleNamespace(invoke=lambda messages: asked.append(messages) or decision))
    monkeypatch.setattr(gmail_graph, "get_memory", lambda store, namespace, default: default)
    command = gmail_graph.triage_router({"email_input": {**GMAIL_EMAIL, "history": HISTORY}}, store=None)
    assert "Booked: Thursday 14:00" in asked[0][1]["content"]  # triage
    assert "Booked: Thursday 14:00" in command.update["messages"][0]["content"]  # the response agent


def test_the_card_shows_only_the_newest_email(monkeypatch):
    shown = stop_at(monkeypatch, {"type": "ignore", "args": None})
    gmail_graph.interrupt_handler(
        proposal("send_email_tool", {"email_id": "m1", "response_text": "Yes.", "email_address": "n@x.com"},
                 email={**GMAIL_EMAIL, "history": HISTORY}), store=None)
    assert "Booked: Thursday 14:00" not in shown[0]["description"]


# A reply: the sender's mail program quoted the earlier email under it.
REPLY = {**GMAIL_EMAIL, "history": HISTORY,
         "body": "Can we make it 15:30?\n\nOn Wed, 23 Sep 2026 at 10:00, <n@x.com> wrote:\n"
                 "> Booked: Thursday 14:00, event for the pilot.\n"}


def triage(monkeypatch, email):
    """Run the triage node on `email`; returns what triage and the response agent are given."""
    asked = []
    monkeypatch.setattr(gmail_graph, "llm_router", SimpleNamespace(
        invoke=lambda messages: asked.append(messages) or SimpleNamespace(classification="respond")))
    monkeypatch.setattr(gmail_graph, "get_memory", lambda store, namespace, default: default)
    command = gmail_graph.triage_router({"email_input": email}, store=None)
    return asked[0][1]["content"], command.update["messages"][0]["content"]


def test_the_model_reads_the_earlier_emails_once(monkeypatch):
    # 5 Oct: the quote under a reply and the history held the same emails.
    for text in triage(monkeypatch, REPLY):
        assert "Can we make it 15:30?" in text
        assert "**Earlier emails in this conversation**" in text
        assert text.count("Booked: Thursday 14:00") == 1
        assert "wrote:" not in text and "\n>" not in text


def test_a_quote_the_history_does_not_hold_stays(monkeypatch):
    other = [{"from": "n@x.com", "to": "r@x.com", "date": "Tue", "body": "Another email."}]
    for text in triage(monkeypatch, {**REPLY, "history": other}):
        assert "> Booked: Thursday 14:00" in text and "wrote:" in text
    for text in triage(monkeypatch, {**REPLY, "history": []}):
        assert "> Booked: Thursday 14:00" in text and "wrote:" in text


def test_notify_gives_the_model_the_earlier_emails_once_and_the_card_the_quote(monkeypatch):
    shown = stop_at(monkeypatch, {"type": "ignore", "args": None})
    command = gmail_graph.triage_interrupt_handler({"email_input": REPLY, "classification_decision": "notify"}, store=None)
    told = command.update["messages"][0]["content"]
    assert told.count("Booked: Thursday 14:00") == 1 and "\n>" not in told
    assert "> Booked: Thursday 14:00" in shown[0]["description"]

    shown = stop_at(monkeypatch, {"type": "ignore", "args": None})
    gmail_graph.interrupt_handler(
        proposal("send_email_tool", {"email_id": "m1", "response_text": "Yes.", "email_address": "n@x.com"},
                 email=REPLY), store=None)
    assert "> Booked: Thursday 14:00" in shown[0]["description"]


def test_the_model_is_told_when_the_email_was_sent_and_the_card_is_not(monkeypatch):
    email = {**GMAIL_EMAIL, "date": "Sun, 04 Oct 2026 18:00:00 +0100"}
    line = "**Date**: Sunday 2026-10-04, 18:00 (UK time)"
    assert line in triage(monkeypatch, email)[1]  # the response agent

    shown = stop_at(monkeypatch, {"type": "ignore", "args": None})
    command = gmail_graph.triage_interrupt_handler({"email_input": email, "classification_decision": "notify"}, store=None)
    assert line in command.update["messages"][0]["content"]
    assert "**Date**" not in shown[0]["description"]

    shown = stop_at(monkeypatch, {"type": "ignore", "args": None})
    gmail_graph.interrupt_handler(
        proposal("send_email_tool", {"email_id": "m1", "response_text": "Yes.", "email_address": "n@x.com"},
                 email=email), store=None)
    assert "**Date**" not in shown[0]["description"]

    assert "**Date**" not in triage(monkeypatch, GMAIL_EMAIL)[1]  # an email ingested before the date was kept


# --- Memory learns rules, not one-off answers (P6) ---------------------------

def test_the_memory_prompt_says_a_one_off_change_is_not_a_preference():
    assert "A request about this one email or this one meeting is NOT a preference" in MEMORY_UPDATE_INSTRUCTIONS
    assert '"make it this Saturday"' in MEMORY_UPDATE_INSTRUCTIONS


class FakeStore:
    """value None = nothing stored under the namespace yet."""
    def __init__(self, value):
        self.value, self.puts = value, []

    def get(self, namespace, key):
        return None if self.value is None else SimpleNamespace(value=self.value)

    def put(self, namespace, key, value):
        self.puts.append(value)


def fake_memory_model(monkeypatch, answer):
    model = SimpleNamespace(invoke=lambda messages: UserPreferences(chain_of_thought="", user_preferences=answer))
    monkeypatch.setattr(gmail_graph, "init_chat_model",
                        lambda *a, **k: SimpleNamespace(with_structured_output=lambda schema: model))


def test_an_unchanged_profile_is_not_written(monkeypatch):
    store = FakeStore("\n30 minute meetings.\n")
    fake_memory_model(monkeypatch, "30 minute meetings.")
    gmail_graph.update_memory(store, ("email_assistant", "cal_preferences"), [])
    assert store.puts == []


def test_a_changed_profile_is_written(monkeypatch):
    store = FakeStore("30 minute meetings.")
    fake_memory_model(monkeypatch, "30 minute meetings.\nNever on Fridays.")
    gmail_graph.update_memory(store, ("email_assistant", "cal_preferences"), [])
    assert store.puts == ["30 minute meetings.\nNever on Fridays."]


def test_an_empty_namespace_is_an_empty_profile(monkeypatch):
    """It used to crash (None.value) unless get_memory had filled the namespace first."""
    store = FakeStore(None)
    fake_memory_model(monkeypatch, "Never on Fridays.")
    gmail_graph.update_memory(store, ("email_assistant", "cal_preferences"), [])
    assert store.puts == ["Never on Fridays."]
