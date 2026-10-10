from typing import Dict, List, Optional
from langchain_core.tools import BaseTool


def get_tools(tool_names: Optional[List[str]] = None) -> List[BaseTool]:
    """The tools named in `tool_names`, or all of them when it is None.

    An unknown name raises KeyError: it used to be dropped without a word, so a
    typo left the model without a tool.
    """
    from email_assistant.tools.default.email_tools import Done, Question
    from email_assistant.tools.gmail.gmail_tools import (
        cancel_meeting_tool,
        check_calendar_tool,
        schedule_meeting_tool,
        send_email_tool,
        update_meeting_tool,
    )

    all_tools = {
        "Done": Done,
        "Question": Question,
        "send_email_tool": send_email_tool,
        "check_calendar_tool": check_calendar_tool,
        "schedule_meeting_tool": schedule_meeting_tool,
        "update_meeting_tool": update_meeting_tool,
        "cancel_meeting_tool": cancel_meeting_tool,
    }
    if tool_names is None:
        return list(all_tools.values())
    return [all_tools[name] for name in tool_names]


def get_tools_by_name(tools: Optional[List[BaseTool]] = None) -> Dict[str, BaseTool]:
    """Get a dictionary of tools mapped by name."""
    if tools is None:
        tools = get_tools()
    return {tool.name: tool for tool in tools}
