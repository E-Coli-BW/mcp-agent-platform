"""OpenAI-compatible chat request models."""

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str
    content: str


class ActiveFileContext(BaseModel):
    """Context about the file currently open in the user's editor."""

    path: str
    visible_start: int | None = None
    visible_end: int | None = None


class ChatRequest(BaseModel):
    model: str = "coding-agent"
    messages: list[ChatMessage]
    stream: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    prompt_version: str | None = None
    session_id: str | None = None
    active_file: ActiveFileContext | None = None