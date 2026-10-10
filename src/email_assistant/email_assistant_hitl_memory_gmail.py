from typing import Literal

from langchain.chat_models import init_chat_model

from langgraph.graph import StateGraph, START, END
from langgraph.store.base import BaseStore
from langgraph.types import interrupt, Command

from email_assistant.tools import get_tools, get_tools_by_name
from email_assistant.tools.gmail.prompt_templates import GMAIL_TOOLS_PROMPT
from email_assistant.tools.gmail.gmail_tools import mark_as_read, event_card_lines
from email_assistant.prompts import triage_system_prompt, triage_user_prompt, agent_system_prompt_hitl_memory, default_triage_instructions, default_background, default_response_preferences, default_cal_preferences, MEMORY_UPDATE_INSTRUCTIONS, MEMORY_UPDATE_INSTRUCTIONS_REINFORCEMENT, MEETING_FEEDBACK, MOVE_FEEDBACK, CANCEL_FEEDBACK, EMAIL_FEEDBACK
from email_assistant.schemas import State, RouterSchema, StateInput, UserPreferences
from email_assistant.utils import parse_gmail, format_for_display, format_gmail_markdown, format_history, dates_for_prompt, without_repeated_quote
from dotenv import load_dotenv

load_dotenv(".env")

# Get tools with Gmail tools
tools = get_tools(["send_email_tool", "schedule_meeting_tool", "update_meeting_tool", "cancel_meeting_tool",
                   "check_calendar_tool", "Question", "Done"])
tools_by_name = get_tools_by_name(tools)

# The three tools that change the calendar. Each stops for your answer, and
# your Respond or Edit on any of them can teach the calendar preferences.
MEETING_TOOLS = ("schedule_meeting_tool", "update_meeting_tool", "cancel_meeting_tool")
# The two that name an existing event by its ID only. Their cards show the
# event as it is now, read from Google by read_event_cards.
EVENT_TOOLS = ("update_meeting_tool", "cancel_meeting_tool")
# What the model is told after Respond on each: what did NOT happen, and what next.
MEETING_FEEDBACKS = {
    "schedule_meeting_tool": MEETING_FEEDBACK,
    "update_meeting_tool": MOVE_FEEDBACK,
    "cancel_meeting_tool": CANCEL_FEEDBACK,
}
# What the model is told after Ignore on each; the run then ends.
IGNORED = {
    "send_email_tool": "User ignored this email draft.",
    "schedule_meeting_tool": "User ignored this calendar meeting draft.",
    "update_meeting_tool": "User ignored this meeting change.",
    "cancel_meeting_tool": "User ignored this meeting cancellation.",
    "Question": "User ignored this question.",
}

# One model, used two ways: the router gets structured output, and the agent
# must call a tool (any of them), one at a time.
llm = init_chat_model("openai:gpt-4.1", temperature=0.0)
llm_router = llm.with_structured_output(RouterSchema)
llm_with_tools = llm.bind_tools(tools, tool_choice="required", parallel_tool_calls=False)

def get_memory(store, namespace, default_content=None):
    """Get memory from the store or initialize with default if it doesn't exist.
    
    Args:
        store: LangGraph BaseStore instance to search for existing memory
        namespace: Tuple defining the memory namespace, e.g. ("email_assistant", "triage_preferences")
        default_content: Default content to use if memory doesn't exist
        
    Returns:
        str: The content of the memory profile, either from existing memory or the default
    """
    # Search for existing memory with namespace and key
    user_preferences = store.get(namespace, "user_preferences")
    
    # If memory exists, return its content (the value)
    if user_preferences:
        return user_preferences.value
    
    # If memory doesn't exist, add it to the store and return the default content
    else:
        # Namespace, key, value
        store.put(namespace, "user_preferences", default_content)
        user_preferences = default_content
    
    # Return the default content
    return user_preferences 

def update_memory(store, namespace, messages):
    """Update memory profile in the store.
    
    Args:
        store: LangGraph BaseStore instance to update memory
        namespace: Tuple defining the memory namespace, e.g. ("email_assistant", "triage_preferences")
        messages: List of messages to update the memory with
    """

    # Get the existing memory. An empty namespace counts as an empty profile:
    # get_memory normally fills it first, but nothing here depends on that.
    item = store.get(namespace, "user_preferences")
    current = item.value if item else ""
    # Update the memory
    llm = init_chat_model("openai:gpt-4.1", temperature=0.0).with_structured_output(UserPreferences)
    result = llm.invoke(
        [
            {"role": "system", "content": MEMORY_UPDATE_INSTRUCTIONS.format(current_profile=current, namespace=namespace)},
        ] + messages
    )
    # Save the updated memory to the store. A one-off answer should leave the
    # profile as it is (MEMORY_UPDATE_INSTRUCTIONS); a copy that differs only
    # in blank lines at the ends is not written, so it is not reported as learned.
    if result.user_preferences.strip() != current.strip():
        store.put(namespace, "user_preferences", result.user_preferences)

# Nodes 
def triage_router(state: State, store: BaseStore) -> Command[Literal["triage_interrupt_handler", "response_agent", "__end__"]]:
    """Analyze email content to decide if we should respond, notify, or ignore.

    The triage step prevents the assistant from wasting time on:
    - Marketing emails and spam
    - Company-wide announcements
    - Messages meant for other teams
    """
    
    # Parse the email input
    author, to, subject, email_thread, email_id = parse_gmail(state["email_input"])
    # In this node only the model reads the email. The history holds the
    # earlier emails, so the same emails quoted at the end of this one are cut.
    email_thread = without_repeated_quote(email_thread, state["email_input"])
    user_prompt = triage_user_prompt.format(
        author=author, to=to, subject=subject, email_thread=email_thread
    ) + format_history(state["email_input"])

    # The email as markdown, for the model's first message
    email_markdown = format_gmail_markdown(subject, author, to, email_thread, email_id,
                                           date=state["email_input"].get("date"))

    # Search for existing triage_preferences memory
    triage_instructions = get_memory(store, ("email_assistant", "triage_preferences"), default_triage_instructions)

    # Format system prompt with background and triage instructions
    system_prompt = triage_system_prompt.format(
        background=default_background,
        triage_instructions=triage_instructions,
    )

    # Run the router LLM
    result = llm_router.invoke(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    )

    # Decision
    classification = result.classification

    # Process the classification decision
    if classification == "respond":
        print("📧 Classification: RESPOND - This email requires a response")
        # Next node
        goto = "response_agent"
        # Update the state
        update = {
            "classification_decision": result.classification,
            "messages": [{"role": "user",
                            "content": f"Respond to the email: {email_markdown}{format_history(state['email_input'])}"
                        }],
        }
        
    elif classification == "ignore":
        print("🚫 Classification: IGNORE - This email can be safely ignored")

        # Next node
        goto = END
        # Update the state
        update = {
            "classification_decision": classification,
        }

    elif classification == "notify":
        print("🔔 Classification: NOTIFY - This email contains important information") 

        # Next node
        goto = "triage_interrupt_handler"
        # Update the state
        update = {
            "classification_decision": classification,
        }

    else:
        raise ValueError(f"Invalid classification: {classification}")
    
    return Command(goto=goto, update=update)

def triage_interrupt_handler(state: State, store: BaseStore) -> Command[Literal["response_agent", "__end__"]]:
    """Handles interrupts from the triage step"""
    
    # Parse the email input
    author, to, subject, email_thread, email_id = parse_gmail(state["email_input"])

    # Create email markdown for Agent Inbox in case of notification
    email_markdown = format_gmail_markdown(subject, author, to, email_thread, email_id)

    # The model's copy: the earlier emails are in the history, so the same
    # emails quoted at the end of this one are cut. The card keeps them. It
    # also says when the email was sent.
    model_markdown = format_gmail_markdown(
        subject, author, to, without_repeated_quote(email_thread, state["email_input"]), email_id,
        date=state["email_input"].get("date"))

    # Create messages
    messages = [{"role": "user",
                "content": f"Email to notify user about: {model_markdown}{format_history(state['email_input'])}"
                }]

    # Create interrupt for Agent Inbox
    request = {
        "action_request": {
            "action": f"Email Assistant: {state['classification_decision']}",
            "args": {}
        },
        "config": {
            "allow_ignore": True,  
            "allow_respond": True,
            "allow_edit": False, 
            "allow_accept": False,  
        },
        # Email to show in Agent Inbox
        "description": email_markdown,
    }

    # Send to Agent Inbox and wait for response
    response = interrupt([request])[0]

    # If user provides feedback, go to response agent and use feedback to respond to email   
    if response["type"] == "response":
        # Add feedback to messages 
        user_input = response["args"]
        messages.append({"role": "user",
                        "content": f"User wants to reply to the email. Use this feedback to respond: {user_input}"
                        })
        # Update memory with feedback
        update_memory(store, ("email_assistant", "triage_preferences"), [{
            "role": "user",
            "content": "The user decided to respond to the email, so update the triage preferences to capture this."
        }] + messages)

        goto = "response_agent"

    # If user ignores email, go to END
    elif response["type"] == "ignore":
        # Make note of the user's decision to ignore the email
        messages.append({"role": "user",
                        "content": "The user decided to ignore the email even though it was classified as notify."
                        })
        # Ignore does NOT rewrite triage preferences (changed 14 September 2026).
        # It used to, so one Ignore could move a whole category of email to
        # `ignore` - where it is then dropped silently, with no message at all.
        goto = END

    # Catch all other responses
    else:
        raise ValueError(f"Invalid response: {response}")

    # Update the state 
    update = {
        "messages": messages,
    }

    return Command(goto=goto, update=update)

def llm_call(state: State, store: BaseStore):
    """LLM decides whether to call a tool or not"""
    
    # Search for existing cal_preferences memory
    cal_preferences = get_memory(store, ("email_assistant", "cal_preferences"), default_cal_preferences)
    
    # Search for existing response_preferences memory
    response_preferences = get_memory(store, ("email_assistant", "response_preferences"), default_response_preferences)

    return {
        "messages": [
            llm_with_tools.invoke(
                [
                    {"role": "system", "content": agent_system_prompt_hitl_memory.format(
                        tools_prompt=GMAIL_TOOLS_PROMPT,
                        background=default_background,
                        response_preferences=response_preferences, 
                        cal_preferences=cal_preferences,
                        **dates_for_prompt(),
                    )}
                ]
                + state["messages"]
            )
        ]
    }
    
def should_continue(state: State) -> Literal["read_event_cards", "interrupt_handler", "mark_as_read_node"]:
    """Route to tool handler, or end if Done tool called.

    Every tool call is looked at, not only the first: Done ends the email only
    when nothing else was called with it, so a reply called next to Done is
    never dropped. The model must call a tool (tool_choice="required"), so a
    message with none is a model or API fault. It raises a clear error; it
    used to return None, which the graph cannot route.
    """
    tool_calls = state["messages"][-1].tool_calls
    if not tool_calls:
        raise ValueError("The model called no tool, although tool_choice='required' makes it call one.")
    if all(tool_call["name"] == "Done" for tool_call in tool_calls):
        # TODO: Here, we could update the background memory with the email-response for follow up actions.
        return "mark_as_read_node"
    if any(tool_call["name"] in EVENT_TOOLS for tool_call in tool_calls):
        return "read_event_cards"
    return "interrupt_handler"


def read_event_cards(state: State):
    """Read from Google the meetings that a move or a cancel names, for their cards.

    Its own node, before interrupt_handler stops for your answer. A node runs
    again from the top when it is resumed, so the read used to run once more
    for every answer. A finished node does not run again: its result is saved
    in the state. (A LangGraph task inside interrupt_handler was tried first.
    In LangGraph 1.2.11 a task inside a subgraph, as response_agent is, runs
    again on resume, so it did not help.)
    """
    return {"event_cards": {
        tool_call["id"]: event_card_lines(tool_call["args"].get("event_id"))
        for tool_call in state["messages"][-1].tool_calls
        if tool_call["name"] in EVENT_TOOLS
    }}


def interrupt_handler(state: State, store: BaseStore) -> Command[Literal["llm_call", "__end__"]]:
    """Creates an interrupt for human review of tool calls"""
    
    # Store messages
    result = []

    # Go to the LLM call node next
    goto = "llm_call"

    # Iterate over the tool calls in the last message
    for tool_call in state["messages"][-1].tool_calls:
        
        # Allowed tools for HITL
        hitl_tools = ["send_email_tool", *MEETING_TOOLS, "Question"]
        
        # If tool is not in our HITL list, execute it directly without interruption
        if tool_call["name"] not in hitl_tools:

            # Execute search_memory and other tools without interruption
            tool = tools_by_name[tool_call["name"]]
            observation = tool.invoke(tool_call["args"])
            result.append({"role": "tool", "content": observation, "tool_call_id": tool_call["id"]})
            continue
            
        # Get original email from email_input in state
        email_input = state["email_input"]
        author, to, subject, email_thread, email_id = parse_gmail(email_input)
        original_email_markdown = format_gmail_markdown(subject, author, to, email_thread, email_id)
        
        # Format tool call for display and prepend the original email. A move
        # or a cancel names the event by ID only, so the card shows the event
        # as read_event_cards found it in Google. This node never reads Google
        # for a card: it runs again from the top on each resume, so the read
        # would repeat. With no saved card, the card shows the event's ID.
        current_event = (state.get("event_cards") or {}).get(tool_call["id"])
        tool_display = format_for_display(tool_call, current_event)
        description = original_email_markdown + tool_display

        # Configure what actions are allowed in Agent Inbox
        if tool_call["name"] == "send_email_tool":
            config = {
                "allow_ignore": True,
                "allow_respond": True,
                "allow_edit": True,
                "allow_accept": True,
            }
        elif tool_call["name"] in ("schedule_meeting_tool", "update_meeting_tool"):
            config = {
                "allow_ignore": True,
                "allow_respond": True,
                "allow_edit": True,
                "allow_accept": True,
            }
        elif tool_call["name"] == "cancel_meeting_tool":
            # Only the event's ID to edit, so no Edit.
            config = {
                "allow_ignore": True,
                "allow_respond": True,
                "allow_edit": False,
                "allow_accept": True,
            }
        elif tool_call["name"] == "Question":
            config = {
                "allow_ignore": True,
                "allow_respond": True,
                "allow_edit": False,
                "allow_accept": False,
            }
        else:
            raise ValueError(f"Invalid tool call: {tool_call['name']}")

        # Create the interrupt request
        request = {
            "action_request": {
                "action": tool_call["name"],
                "args": tool_call["args"]
            },
            "config": config,
            "description": description,
        }

        # Send to Agent Inbox and wait for response
        response = interrupt([request])[0]

        # Handle the responses 
        if response["type"] == "accept":

            # Execute the tool with original args
            tool = tools_by_name[tool_call["name"]]
            observation = tool.invoke(tool_call["args"])
            result.append({"role": "tool", "content": observation, "tool_call_id": tool_call["id"]})
                        
        elif response["type"] == "edit":

            # Tool selection 
            tool = tools_by_name[tool_call["name"]]
            initial_tool_call = tool_call["args"]
            
            # Get edited args from Agent Inbox
            edited_args = response["args"]["args"]

            # Update the AI message's tool call with edited content (reference to the message in the state)
            ai_message = state["messages"][-1] # Get the most recent message from the state
            current_id = tool_call["id"] # Store the ID of the tool call being edited
            
            # Create a new list of tool calls by filtering out the one being edited and adding the updated version
            # This avoids modifying the original list directly (immutable approach)
            updated_tool_calls = [tc for tc in ai_message.tool_calls if tc["id"] != current_id] + [
                {"type": "tool_call", "name": tool_call["name"], "args": edited_args, "id": current_id}
            ]

            # Create a new copy of the message with updated tool calls rather than modifying the original
            # This ensures state immutability and prevents side effects in other parts of the code
            result.append(ai_message.model_copy(update={"tool_calls": updated_tool_calls}))

            # Save feedback in memory and update the send_email_tool call with the edited content from Agent Inbox
            if tool_call["name"] == "send_email_tool":
                
                # Execute the tool with edited args
                observation = tool.invoke(edited_args)
                
                # Add only the tool response message
                result.append({"role": "tool", "content": observation, "tool_call_id": current_id})

                # This is new: update the memory
                update_memory(store, ("email_assistant", "response_preferences"), [{
                    "role": "user",
                    "content": f"User edited the email response. Here is the initial email generated by the assistant: {initial_tool_call}. Here is the edited email: {edited_args}. Follow all instructions above, and remember: {MEMORY_UPDATE_INSTRUCTIONS_REINFORCEMENT}."
                }])
            
            # Save feedback in memory and update the meeting tool call with the edited content from Agent Inbox
            elif tool_call["name"] in ("schedule_meeting_tool", "update_meeting_tool"):
                
                # Execute the tool with edited args
                observation = tool.invoke(edited_args)
                
                # Add only the tool response message
                result.append({"role": "tool", "content": observation, "tool_call_id": current_id})

                # This is new: update the memory
                update_memory(store, ("email_assistant", "cal_preferences"), [{
                    "role": "user",
                    "content": f"User edited the calendar invitation. Here is the initial calendar invitation generated by the assistant: {initial_tool_call}. Here is the edited calendar invitation: {edited_args}. Follow all instructions above, and remember: {MEMORY_UPDATE_INSTRUCTIONS_REINFORCEMENT}."
                }])
            
            # Catch all other tool calls
            else:
                raise ValueError(f"Invalid tool call: {tool_call['name']}")

        elif response["type"] == "ignore":

            if tool_call["name"] not in IGNORED:
                raise ValueError(f"Invalid tool call: {tool_call['name']}")
            # Don't execute the tool, and tell the agent how to proceed
            result.append({"role": "tool", "content": f"{IGNORED[tool_call['name']]} Ignore this email and end the workflow.", "tool_call_id": tool_call["id"]})
            # Go to END
            goto = END
            # No memory update: Ignore ends the email and teaches nothing.

        elif response["type"] == "response":
            # User provided feedback
            user_feedback = response["args"]
            if tool_call["name"] == "send_email_tool":
                # Don't execute the tool, and add a message with the user feedback to incorporate into the email
                result.append({"role": "tool", "content": EMAIL_FEEDBACK.format(feedback=user_feedback, tool=tool_call["name"]), "tool_call_id": tool_call["id"]})
                # This is new: update the memory
                update_memory(store, ("email_assistant", "response_preferences"), state["messages"] + result + [{
                    "role": "user",
                    "content": f"User gave feedback on this email. Update the response preferences only if the feedback states a lasting preference; if it is only about this email, return the profile unchanged. Follow all instructions above, and remember: {MEMORY_UPDATE_INSTRUCTIONS_REINFORCEMENT}."
                }])

            elif tool_call["name"] in MEETING_TOOLS:
                # Don't execute the tool, and tell the model what did NOT happen and what to do next
                result.append({"role": "tool", "content": MEETING_FEEDBACKS[tool_call["name"]].format(feedback=user_feedback, tool=tool_call["name"]), "tool_call_id": tool_call["id"]})
                # This is new: update the memory
                update_memory(store, ("email_assistant", "cal_preferences"), state["messages"] + result + [{
                    "role": "user",
                    "content": f"User gave feedback on this meeting. Update the calendar preferences only if the feedback states a lasting preference; if it is only about this meeting (its day, time or length), return the profile unchanged. Follow all instructions above, and remember: {MEMORY_UPDATE_INSTRUCTIONS_REINFORCEMENT}."
                }])

            elif tool_call["name"] == "Question":
                # Don't execute the tool, and add a message with the user feedback to incorporate into the email
                result.append({"role": "tool", "content": f"User answered the question, which can we can use for any follow up actions. Feedback: {user_feedback}", "tool_call_id": tool_call["id"]})

            else:
                raise ValueError(f"Invalid tool call: {tool_call['name']}")

    # Update the state 
    update = {
        "messages": result,
    }

    return Command(goto=goto, update=update)

# Conditional edge function

def mark_as_read_node(state: State):
    email_input = state["email_input"]
    author, to, subject, email_thread, email_id = parse_gmail(email_input)
    mark_as_read(email_id)

# Build workflow
agent_builder = StateGraph(State)

# Add nodes - with store parameter
agent_builder.add_node("llm_call", llm_call)
agent_builder.add_node("read_event_cards", read_event_cards)
agent_builder.add_node("interrupt_handler", interrupt_handler)
agent_builder.add_node("mark_as_read_node", mark_as_read_node)

# Add edges
agent_builder.add_edge(START, "llm_call")
agent_builder.add_conditional_edges(
    "llm_call",
    should_continue,
    {
        "read_event_cards": "read_event_cards",
        "interrupt_handler": "interrupt_handler",
        "mark_as_read_node": "mark_as_read_node",
    },
)
agent_builder.add_edge("read_event_cards", "interrupt_handler")
agent_builder.add_edge("mark_as_read_node", END)

# Compile the agent
response_agent = agent_builder.compile()

# Build overall workflow with store and checkpointer
overall_workflow = (
    StateGraph(State, input_schema=StateInput)
    .add_node(triage_router)
    .add_node(triage_interrupt_handler)
    .add_node("response_agent", response_agent)
    .add_node("mark_as_read_node", mark_as_read_node)
    .add_edge(START, "triage_router")
    .add_edge("mark_as_read_node", END)
)

email_assistant = overall_workflow.compile()