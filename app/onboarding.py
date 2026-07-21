"""Long-lived, stage-scoped setup flow for building a goal hierarchy."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from .db import Database
from .llm import LLMUnavailable, NvidiaLLM
from .schemas import DecompositionItem


LEVELS = ("yearly", "monthly", "weekly", "daily")
MAX_PARENTS_PER_TRANSITION = 5
MAX_CHILDREN_PER_PARENT = 5
CHILD = {"yearly": "monthly", "monthly": "weekly", "weekly": "daily"}
CONFIRM_STAGE = {
    "monthly": "awaiting_monthly_confirmation",
    "weekly": "awaiting_weekly_confirmation",
    "daily": "awaiting_daily_confirmation",
}
ADD_STAGE = {
    "monthly": "awaiting_monthly_additions",
    "weekly": "awaiting_weekly_additions",
    "daily": "awaiting_daily_additions",
}


class Onboarding:
    def __init__(self, db: Database, llm: NvidiaLLM, timezone: str):
        self.db, self.llm, self.tz = db, llm, ZoneInfo(timezone)

    async def state(self, user_id: str) -> dict | None:
        rows = await self.db.select("onboarding_state", {"user_id": f"eq.{user_id}"})
        return rows[0] if rows else None

    async def should_start(self, user_id: str, body: str) -> bool:
        if body.strip().lower() in {"setup", "start over", "start setup"}:
            return True
        current = await self.state(user_id)
        if current:
            return current["stage"] != "complete"
        goals = await self.db.select("goals", {"user_id": f"eq.{user_id}", "limit": "1"})
        return not goals

    async def start(self, user_id: str) -> str:
        await self.db.rpc("set_onboarding_state", {"p_user_id": user_id, "p_stage": "awaiting_yearly", "p_working_data": {}})
        return "Let’s set up your goal structure. What are your main goals for this year? You can list a few — I’ll break each one down."

    async def handle(self, user_id: str, body: str) -> list[str] | None:
        state = await self.state(user_id)
        if not state:
            return None
        if body.strip().lower() in {"setup", "start over", "start setup"}:
            return [await self.start(user_id)]
        stage, data = state["stage"], state["working_data"]
        if stage == "complete":
            return None
        if stage == "awaiting_yearly":
            return await self._capture_yearly(user_id, body)
        if stage.endswith("_confirmation"):
            return await self._confirmation(user_id, body, stage, data)
        if stage.endswith("_additions"):
            return await self._additions(user_id, body, stage, data)
        return ["I’m resuming setup. Please tell me your yearly goals."]

    async def _capture_yearly(self, user_id: str, body: str) -> list[str]:
        try:
            items = await self.llm.extract_goals(body, "yearly")
        except LLMUnavailable as error:
            return [f"Goal planning is unavailable right now: {error}. Please try again shortly."]
        parents = await self.db.rpc("create_active_goals", {"p_user_id": user_id, "p_level": "yearly", "p_items": [item.model_dump(mode="json") for item in items.items]})
        return await self._propose_children(user_id, parents, "monthly")

    async def _propose_children(self, user_id: str, parents: list[dict], child_level: str) -> list[str]:
        selected_parents = parents[:MAX_PARENTS_PER_TRANSITION]
        proposed_ids: list[str] = []
        lines: list[str] = []

        async def create_for_parent(parent: dict) -> tuple[str, list[dict]] | None:
            try:
                plan = await self.llm.decompose(f"Break this {parent['level']} goal into practical {child_level} goals: {parent['title']}")
            except LLMUnavailable:
                return None
            children = await self.db.rpc("create_proposed_children", {"p_user_id": user_id, "p_parent_goal_id": parent["id"], "p_level": child_level, "p_items": [item.model_dump(mode="json") for item in plan.items[:MAX_CHILDREN_PER_PARENT]]})
            return parent["title"], children

        # A bounded concurrent batch prevents a long hierarchy from blocking the
        # single-user worker for dozens of sequential model calls.
        results = await asyncio.gather(*(create_for_parent(parent) for parent in selected_parents))
        for result in results:
            if not result:
                continue
            title, children = result
            proposed_ids.extend(child["id"] for child in children)
            lines.append(f"{title}:\n" + "\n".join(f"• {child['title']}" for child in children))
        if not proposed_ids:
            return ["I couldn’t generate that breakdown right now. Please try again shortly."]
        await self.db.rpc("set_onboarding_state", {"p_user_id": user_id, "p_stage": CONFIRM_STAGE[child_level], "p_working_data": {"child_level": child_level, "proposed_ids": proposed_ids}})
        note = f"\n\nI focused this setup pass on {len(selected_parents)} priority parent goals." if len(parents) > len(selected_parents) else ""
        return ["Here’s your " + child_level + " breakdown:\n\n" + "\n\n".join(lines) + note + "\n\nReply ‘yes’ to confirm, or tell me what to change."]

    async def _confirmation(self, user_id: str, body: str, stage: str, data: dict) -> list[str]:
        if body.strip().lower() not in {"yes", "confirm", "approve"}:
            return ["Reply ‘yes’ to accept this breakdown, or send the changes you want. I’ll keep this setup stage ready for you."]
        level = data["child_level"]
        ids = data["proposed_ids"]
        await self.db.rpc("activate_goal_ids", {"p_user_id": user_id, "p_goal_ids": ids})
        if level == "daily":
            await self.db.rpc("materialize_daily_tasks", {"p_user_id": user_id, "p_goal_ids": ids})
        await self.db.rpc("set_onboarding_state", {"p_user_id": user_id, "p_stage": ADD_STAGE[level], "p_working_data": {"level": level}})
        return [f"Want to add any other {level} goals not covered by these? Send them with a rough deadline, or reply ‘no’ to continue."]

    async def _additions(self, user_id: str, body: str, stage: str, data: dict) -> list[str]:
        level = data["level"]
        if body.strip().lower() not in {"no", "none", "continue", "done"}:
            try:
                items = await self.llm.extract_goals(body, level)
            except LLMUnavailable:
                return ["I couldn’t read that addition. Please send it again, or reply ‘no’ to continue."]
            await self.db.rpc("create_active_goals", {"p_user_id": user_id, "p_level": level, "p_items": [item.model_dump(mode="json") for item in items.items]})
            return [f"Added. Send another {level} goal, or reply ‘no’ to continue."]
        if level == "daily":
            tasks = await self.db.rpc("prioritized_open_tasks", {"p_user_id": user_id, "p_view": "today"})
            await self.db.rpc("set_onboarding_state", {"p_user_id": user_id, "p_stage": "complete", "p_working_data": {}})
            rendered = "\n".join(f"{i}. {task['title']}" for i, task in enumerate(tasks, 1)) or "No tasks are due today."
            return ["Setup done. Your goals are structured top to bottom. I’ll check in daily, weekly, and monthly from here — you can also text me anytime to add, complete, or check tasks.\n\nToday:\n" + rendered]
        next_level = CHILD[level]
        parents = await self.db.select("goals", {"user_id": f"eq.{user_id}", "level": f"eq.{level}", "status": "eq.active", "order": "created_at.desc", "limit": str(MAX_PARENTS_PER_TRANSITION)})
        return await self._propose_children(user_id, parents, next_level)
