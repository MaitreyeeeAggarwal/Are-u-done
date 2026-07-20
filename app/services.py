from datetime import datetime, timedelta
import logging
import re
from uuid import uuid4
from zoneinfo import ZoneInfo
from twilio.rest import Client as TwilioClient
from .config import Settings
from .db import Database
from .llm import LLMUnavailable, NvidiaLLM
from .onboarding import Onboarding
from .schemas import Intent, IntentResult, QueueMessage


SLOW_INTENTS = {Intent.ADD_GOAL, Intent.ADD_EXAM, Intent.REVIEW_REPLY}
logger = logging.getLogger(__name__)


class Messenger:
    MAX_WHATSAPP_BODY = 1400

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token) if settings.twilio_account_sid else None

    async def send(self, to: str, body: str) -> None:
        if not self.client:
            raise RuntimeError("Twilio is not configured")
        # Twilio rejects WhatsApp bodies longer than 1,600 characters. Keep a
        # conservative margin and favor newline/word boundaries for plans.
        for part in self._chunks(body):
            # The SDK is synchronous; this is intentionally deferred to the delivery worker.
            self.client.messages.create(from_=self.settings.twilio_whatsapp_from, to=to, body=part)

    @classmethod
    def _chunks(cls, body: str) -> list[str]:
        if len(body) <= cls.MAX_WHATSAPP_BODY:
            return [body]
        chunks: list[str] = []
        remaining = body.strip()
        while len(remaining) > cls.MAX_WHATSAPP_BODY:
            boundary = max(remaining.rfind("\n", 0, cls.MAX_WHATSAPP_BODY), remaining.rfind(" ", 0, cls.MAX_WHATSAPP_BODY))
            if boundary <= 0:
                boundary = cls.MAX_WHATSAPP_BODY
            chunks.append(remaining[:boundary].rstrip())
            remaining = remaining[boundary:].lstrip()
        if remaining:
            chunks.append(remaining)
        return chunks


class Agent:
    def __init__(self, db: Database, llm: NvidiaLLM, settings: Settings):
        self.db, self.llm, self.settings = db, llm, settings
        self.tz = ZoneInfo(settings.timezone)
        self.onboarding = Onboarding(db, llm, settings.timezone)

    async def queue_reply(self, user_id: str, to: str, body: str, kind: str = "reply") -> None:
        await self.db.insert("outbox", {"user_id": user_id, "to_number": to, "body": body, "kind": kind})

    async def process(self, message: QueueMessage) -> None:
        onboarding_state = await self.onboarding.state(message.user_id)
        if self.is_generated_goal_reset_command(message.body):
            await self.reset_generated_goals(message)
            return
        setup_command = message.body.strip().lower() in {"setup", "start over", "start setup"}
        if setup_command or (onboarding_state is None and await self.onboarding.should_start(message.user_id, message.body)):
            await self.queue_reply(message.user_id, message.from_number, await self.onboarding.start(message.user_id))
            return
        if onboarding_state and onboarding_state["stage"] != "complete":
            if onboarding_state["stage"] == "awaiting_yearly":
                await self.queue_reply(message.user_id, message.from_number, "Working on your yearly goals…")
            try:
                replies = await self.onboarding.handle(message.user_id, message.body)
            except Exception:
                logger.exception("Onboarding flow failed")
                await self.queue_reply(message.user_id, message.from_number, "Setup hit a temporary problem. Please send the same goals again in a moment.")
                return
            if replies:
                for reply in replies:
                    await self.queue_reply(message.user_id, message.from_number, reply)
                return
        now = datetime.now(self.tz)
        states = await self.db.select("conversation_state", {"user_id": f"eq.{message.user_id}", "expires_at": f"gt.{now.isoformat()}"})
        if message.body.strip().lower() in {"cancel", "start over", "reset"}:
            if states:
                await self.db.delete("conversation_state", {"user_id": f"eq.{message.user_id}"})
                await self.queue_reply(message.user_id, message.from_number, "Cancelled the pending plan. Send a new goal whenever you’re ready.")
            else:
                await self.queue_reply(message.user_id, message.from_number, "There is no pending plan to cancel.")
            return
        if states and states[0]["kind"] == "goal_confirmation" and message.body.strip().lower() in {"confirm", "yes", "approve"}:
            await self.db.rpc("activate_goal_proposal", {"p_user_id": message.user_id})
            await self.queue_reply(message.user_id, message.from_number, "Your goal plan is active. I’ll schedule its work as it becomes relevant.")
            return
        if states and states[0]["kind"] == "goal_details":
            lower = message.body.lower()
            level = next((item for item in ("life", "yearly", "monthly", "weekly", "daily") if item in lower), None)
            if level:
                previous = states[0]["payload"]
                intent = IntentResult.model_validate({**previous, "intent": Intent.ADD_GOAL, "goal_level": level})
                await self.propose_goal(message, intent)
                return
        headed_goal = self.headed_goal_intent(message.body)
        if headed_goal:
            await self.queue_reply(message.user_id, message.from_number, "Working on that…")
            await self.propose_goal(message, headed_goal)
            return
        direct_list = self.direct_list_intent(message.body)
        if direct_list:
            await self.show_tasks(message, direct_list)
            return
        try:
            intent = await self.llm.classify(message.body, now.isoformat())
        except LLMUnavailable as error:
            logger.warning("NVIDIA intent classification unavailable: %s", error)
            await self.queue_reply(message.user_id, message.from_number, "I’m having trouble understanding that right now. Please try again in a moment.")
            return

        # Explicit list ranges are cheap and unambiguous.  Preserve them even if
        # the model's generic view field is overly broad (for example, “today”).
        normalized_body = message.body.lower()
        if intent.intent == Intent.LIST_TASKS:
            if "week" in normalized_body:
                intent.view = "week"
            elif "today" in normalized_body:
                intent.view = "today"
        if intent.intent == Intent.ADD_GOAL:
            self.enrich_goal_from_message(intent, message.body)

        if intent.intent in SLOW_INTENTS:
            await self.queue_reply(message.user_id, message.from_number, "Working on that…")
        if intent.intent in {Intent.ADD_TASK, Intent.ADD_URGENT_TASK}:
            await self.add_task(message, intent)
        elif intent.intent == Intent.LIST_TASKS:
            await self.show_tasks(message, intent)
        elif intent.intent == Intent.COMPLETE_TASK:
            await self.complete_task(message, intent)
        elif intent.intent == Intent.RESCHEDULE:
            await self.reschedule(message, intent)
        elif intent.intent == Intent.ADD_GOAL:
            await self.propose_goal(message, intent)
        elif intent.intent == Intent.ADD_EXAM:
            await self.add_exam(message, intent)
        elif intent.intent == Intent.REVIEW_REPLY:
            await self.record_review(message)
        else:
            await self.queue_reply(message.user_id, message.from_number, "I can add tasks, plan goals and exams, or show today’s list. Try: ‘add finish report tomorrow’." )

    @staticmethod
    def enrich_goal_from_message(intent: IntentResult, body: str) -> None:
        """Keep the useful contents of WhatsApp-style headed/numbered goal lists."""
        header = re.match(r"^\s*(life|yearly|monthly|weekly|daily)\s+goals?\s*:?\s*", body, re.IGNORECASE)
        if not header:
            return
        intent.goal_level = header.group(1).lower()
        remainder = body[header.end() :].strip()
        items = [re.sub(r"^\s*\d+[.)]\s*", "", line).strip() for line in remainder.splitlines()]
        items = [item for item in items if item]
        if items:
            intent.title = "; ".join(items)

    @staticmethod
    def headed_goal_intent(body: str) -> IntentResult | None:
        header = re.match(r"^\s*(life|yearly|monthly|weekly|daily)\s+goals?\s*:?\s*", body, re.IGNORECASE)
        if not header:
            return None
        remainder = body[header.end() :].strip()
        items = [re.sub(r"^\s*\d+[.)]\s*", "", line).strip() for line in remainder.splitlines()]
        items = [item for item in items if item]
        if not items:
            return None
        return IntentResult(intent=Intent.ADD_GOAL, goal_level=header.group(1).lower(), title="; ".join(items))

    @staticmethod
    def direct_list_intent(body: str) -> IntentResult | None:
        """Handle common task-list requests without depending on the LLM."""
        normalized = re.sub(r"\s+", " ", body.strip().lower())
        today_requests = {
            "today", "show today", "today's task", "today's tasks",
            "todays task", "todays tasks", "show my today's inbox",
            "show my todays inbox", "today's inbox", "todays inbox",
            "my today's inbox", "my todays inbox", "show today's tasks",
            "show todays tasks", "show my tasks today", "my tasks today",
        }
        if normalized in today_requests:
            return IntentResult(intent=Intent.LIST_TASKS, view="today")
        return None
    @staticmethod
    def is_generated_goal_reset_command(body: str) -> bool:
        normalized = re.sub(r"\s+", " ", body.strip().lower())
        return normalized in {"reset generated goals", "reset my generated goals"}

    async def reset_generated_goals(self, message: QueueMessage) -> None:
        result = await self.db.rpc("reset_generated_goals", {"p_user_id": message.user_id})
        deleted_tasks = result[0]["deleted_task_count"] if result else 0
        await self.queue_reply(
            message.user_id,
            message.from_number,
            f"Reset complete � removed {deleted_tasks} generated goal task(s). Your manual tasks are still safe. Send �setup� when you�re ready to build a new plan.",
        )
    async def add_task(self, message: QueueMessage, intent: IntentResult) -> None:
        if not intent.title:
            await self.queue_reply(message.user_id, message.from_number, "What would you like me to add?")
            return
        urgent = intent.intent == Intent.ADD_URGENT_TASK
        due = intent.due_at
        priority = "urgent" if urgent else intent.priority
        task = (await self.db.rpc("create_task_with_reminders", {
            "p_user_id": message.user_id, "p_title": intent.title, "p_description": intent.description,
            "p_list_name": intent.list_name or "Inbox", "p_due_at": due.isoformat() if due else None,
            "p_priority": priority, "p_source": "urgent" if urgent else "manual",
        }))[0]
        text = f"Added: {task['title']}" + (" — moved to the top of your priorities." if urgent else ".")
        await self.queue_reply(message.user_id, message.from_number, text)

    async def show_tasks(self, message: QueueMessage, intent: IntentResult) -> None:
        tasks = await self.db.rpc("prioritized_open_tasks", {"p_user_id": message.user_id, "p_view": intent.view or "today"})
        if not tasks:
            await self.queue_reply(message.user_id, message.from_number, "No open tasks in that view.")
            return
        ids = [item["id"] for item in tasks]
        snapshot = (await self.db.insert("task_list_snapshots", {
            "user_id": message.user_id, "task_ids": ids, "expires_at": (datetime.now(self.tz) + timedelta(hours=24)).isoformat(),
        }))[0]
        lines = [f"{i}. [{task['priority']}] {task['title']}" for i, task in enumerate(tasks, 1)]
        await self.queue_reply(message.user_id, message.from_number, "Your tasks:\n" + "\n".join(lines) + "\n\nReply ‘done 2’ to complete an item.")
        await self.db.update("task_list_snapshots", {"id": f"eq.{snapshot['id']}"}, {"shown_at": datetime.now(self.tz).isoformat()})

    async def complete_task(self, message: QueueMessage, intent: IntentResult) -> None:
        if intent.task_number:
            result = await self.db.rpc("complete_snapshot_task", {"p_user_id": message.user_id, "p_number": intent.task_number})
            if result:
                await self.queue_reply(message.user_id, message.from_number, f"Done — {result[0]['title']}.")
            else:
                await self.queue_reply(message.user_id, message.from_number, "That list number has expired. Say ‘show today’ and try again.")
            return
        candidates = await self.db.rpc("find_open_task", {"p_user_id": message.user_id, "p_query": intent.task_reference or intent.title or ""})
        if len(candidates) == 1:
            await self.db.rpc("complete_task", {"p_task_id": candidates[0]["id"]})
            await self.queue_reply(message.user_id, message.from_number, f"Done — {candidates[0]['title']}.")
        elif len(candidates) > 1:
            await self.db.rpc("set_conversation_state", {"p_user_id": message.user_id, "p_kind": "task_clarification", "p_payload": {"candidates": candidates}})
            await self.queue_reply(message.user_id, message.from_number, "I found more than one match. Please reply with the task number from ‘show today’.")
        else:
            await self.queue_reply(message.user_id, message.from_number, "I couldn’t find an open task matching that.")

    async def reschedule(self, message: QueueMessage, intent: IntentResult) -> None:
        if not intent.due_at or not (intent.task_reference or intent.title):
            await self.queue_reply(message.user_id, message.from_number, "Tell me which task and when to move it to.")
            return
        candidates = await self.db.rpc("find_open_task", {"p_user_id": message.user_id, "p_query": intent.task_reference or intent.title})
        if len(candidates) != 1:
            await self.queue_reply(message.user_id, message.from_number, "I need a more specific task name to reschedule it.")
            return
        await self.db.rpc("reschedule_task_with_reminders", {"p_task_id": candidates[0]["id"], "p_due_at": intent.due_at.isoformat()})
        await self.queue_reply(message.user_id, message.from_number, f"Moved {candidates[0]['title']} to {intent.due_at.strftime('%d %b, %I:%M %p')}.")

    async def propose_goal(self, message: QueueMessage, intent: IntentResult) -> None:
        states = await self.db.select("conversation_state", {"user_id": f"eq.{message.user_id}"})
        if states and states[0]["kind"] == "goal_details":
            previous = states[0]["payload"]
            intent = IntentResult.model_validate({**previous, **intent.model_dump(exclude_none=True)})
        if not intent.title or intent.goal_level not in {"life", "yearly", "monthly", "weekly", "daily"}:
            await self.db.rpc("set_conversation_state", {"p_user_id": message.user_id, "p_kind": "goal_details", "p_payload": intent.model_dump(mode="json")})
            await self.queue_reply(message.user_id, message.from_number, "Tell me the goal and whether it is life, yearly, monthly, weekly, or daily.")
            return
        next_level = {"life":"yearly", "yearly":"monthly", "monthly":"weekly", "weekly":"daily", "daily":"daily"}[intent.goal_level]
        try:
            plan = await self.llm.decompose(f"Break this {intent.goal_level} goal into concrete {next_level} items: {intent.title}. Return dated items only when justified.")
        except LLMUnavailable:
            await self.queue_reply(message.user_id, message.from_number, "I saved nothing because planning is temporarily unavailable. Please try again shortly.")
            return
        proposal_id = str(uuid4())
        payload = {"goal": intent.model_dump(mode="json"), "items": plan.model_dump(mode="json")}
        await self.db.rpc("set_conversation_state", {"p_user_id": message.user_id, "p_kind": "goal_confirmation", "p_payload": payload | {"proposal_id": proposal_id}})
        lines = "\n".join(f"{i}. {item.title}" for i, item in enumerate(plan.items, 1))
        await self.queue_reply(message.user_id, message.from_number, f"Proposed plan for {intent.title}:\n{lines}\n\nReply ‘confirm’ to activate it or tell me what to change.")

    async def add_exam(self, message: QueueMessage, intent: IntentResult) -> None:
        states = await self.db.select("conversation_state", {"user_id": f"eq.{message.user_id}"})
        if states and states[0]["kind"] == "exam_details":
            previous = states[0]["payload"]
            intent = IntentResult.model_validate({**previous, **intent.model_dump(exclude_none=True)})
        if not intent.subject or not intent.target_date or not intent.topics:
            payload = intent.model_dump(mode="json")
            await self.db.rpc("set_conversation_state", {"p_user_id": message.user_id, "p_kind": "exam_details", "p_payload": payload})
            await self.queue_reply(message.user_id, message.from_number, "I need the exam subject, date, and topics/chapters before I can make a study plan.")
            return
        try:
            plan = await self.llm.study_plan(f"Create dated study sessions for {intent.subject}, exam {intent.target_date}, topics: {', '.join(intent.topics)}. Front-load hard topics and reserve final days for review.")
        except LLMUnavailable:
            await self.queue_reply(message.user_id, message.from_number, "I couldn’t generate the study plan right now. Please try again shortly.")
            return
        await self.db.rpc("create_exam_plan", {"p_user_id": message.user_id, "p_subject": intent.subject, "p_exam_date": intent.target_date.isoformat(), "p_sessions": [s.model_dump(mode="json") for s in plan.sessions]})
        await self.queue_reply(message.user_id, message.from_number, f"Created {len(plan.sessions)} study sessions for your {intent.subject} exam.")

    async def record_review(self, message: QueueMessage) -> None:
        await self.db.rpc("record_review_reply", {"p_user_id": message.user_id, "p_raw_reply": message.body})
        await self.queue_reply(message.user_id, message.from_number, "Thanks — I’ve recorded your review and will use it to adjust your next plan.")
