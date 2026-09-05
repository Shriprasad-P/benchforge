from dataclasses import dataclass
from typing import Literal
from pydantic import BaseModel, Field


@dataclass(frozen=True)
class PublicTask:
    id: str
    title: str
    description: str
    constraints: list[str]


@dataclass(frozen=True)
class ModelContext:
    files: dict[str, str]


@dataclass(frozen=True)
class EvaluationSpec:
    task_command: list[str]
    regression_command: list[str]
    allowed_files: tuple[str, ...] = ("calculator.py",)


class RunRequest(BaseModel):
    adapter: Literal[
        "mock-fixed",
        "mock-unchanged",
        "chat-completions",
        "openai-compatible",
        "anthropic-messages",
    ] = "mock-fixed"
    benchmark_id: str = "tiny-v0.1"
    trials: int = Field(default=1, ge=1, le=10)
