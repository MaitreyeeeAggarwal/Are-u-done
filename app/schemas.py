from datetime import date, datetime
from enum import StrEnum
from typing import Any
from pydantic import BaseModel, Field, field_validator


class Intent(StrEnum):
    ADD_TASK = "add_task"
    ADD_URGENT_TASK = "add_urgent_task"
    ADD_GOAL = "add_goal"
    ADD_EXAM = "add_exam"
    COMPLETE_TASK = "complete_task"
    LIST_TASKS = "list_tasks"
    RESCHEDULE = "reschedule"
    REVIEW_REPLY = "review_reply"
    CASUAL = "casual"


class IntentResult(BaseModel):
    intent: Intent
    title: str | None = None
    description: str | None = None
    due_at: datetime | None = None
    priority: str = "normal"
    list_name: str | None = None
    task_number: int | None = Field(default=None, ge=1)
    task_reference: str | None = None
    goal_level: str | None = None
    target_date: date | None = None
    subject: str | None = None
    topics: list[str] = Field(default_factory=list)
    view: str | None = None
    raw_statuses: dict[str, str] = Field(default_factory=dict)

    @field_validator("priority")
    @classmethod
    def priority_is_valid(cls, value: str) -> str:
        if value not in {"low", "normal", "high", "urgent"}:
            return "normal"
        return value


class DecompositionItem(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    target_date: date | None = None
    priority: str = "normal"


class DecompositionResult(BaseModel):
    items: list[DecompositionItem] = Field(min_length=1, max_length=12)


class GoalItems(BaseModel):
    items: list[DecompositionItem] = Field(min_length=1, max_length=8)


class StudySession(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    due_at: datetime
    priority: str = "high"


class StudyPlan(BaseModel):
    sessions: list[StudySession] = Field(min_length=1, max_length=60)


class QueueMessage(BaseModel):
    id: str
    user_id: str
    message_sid: str
    body: str
    from_number: str
    received_at: datetime
    attempts: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
