from pydantic import BaseModel
from langchain_core.tools import tool

@tool
class Done(BaseModel):
    """The work on this email is finished."""
    done: bool

@tool
class Question(BaseModel):
      """Question to ask user."""
      content: str
