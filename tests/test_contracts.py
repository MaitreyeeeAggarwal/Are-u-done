from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from app.schemas import Intent, IntentResult


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
