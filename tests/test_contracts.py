from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from app.schemas import Intent, IntentResult
from app.services import Agent


def test_intent_invalid_priority_is_normalized() -> None:
    result = IntentResult(intent=Intent.ADD_TASK, title="Read", priority="asap")
    assert result.priority == "normal"


def test_numbered_completion_requires_positive_number() -> None:
    try:
        IntentResult(intent=Intent.COMPLETE_TASK, task_number=0)
    except ValueError:
        pass
    else:
        raise AssertionError("zero must not be a task-list number")


def test_timezone_aware_expiry() -> None:
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    assert (now + timedelta(hours=24)).tzinfo is not None

def test_common_today_requests_bypass_the_llm() -> None:
    for body in ("Show today", "Today's inbox", "Today's tasks", "my tasks today"):
        result = Agent.direct_list_intent(body)
        assert result == IntentResult(intent=Intent.LIST_TASKS, view="today")


def test_unrelated_message_is_not_treated_as_a_list_request() -> None:
    assert Agent.direct_list_intent("Add task today") is None
