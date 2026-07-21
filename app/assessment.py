from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo
from .db import Database
from .schemas import QueueMessage


class Assessment:
    def __init__(self, db: Database, timezone: str):
        self.db, self.tz = db, ZoneInfo(timezone)

    async def handle(self, message: QueueMessage) -> str | None:
        text = re.sub(r"\s+", " ", message.body.strip())
        lower = text.lower()
        if lower == "help":
            return "Commands:\ntoday | week | overdue | load | focus | stuck\nstatus <task> | why <task> | history <task>\nblocked <task> because <reason> | unblock <task>\nprogress <goal|week|month|year> | at risk | tree <goal> | undo"
        if lower in {"today", "week", "overdue", "focus", "load", "stuck"}:
            return await self.list_view(message.user_id, lower)
        if lower == "at risk":
            return await self.at_risk(message.user_id)
        if lower == "undo":
            return await self.undo(message.user_id)
        match = re.match(r"^(status|why|history|unblock|tree|progress)\s+(.+)$", text, re.I)
        if match:
            command, reference = match.group(1).lower(), match.group(2).strip()
            if command == "status": return await self.status(message.user_id, reference)
            if command == "why": return await self.why(message.user_id, reference)
            if command == "history": return await self.history(message.user_id, reference)
            if command == "unblock": return await self.unblock(message.user_id, reference)
            if command == "tree": return await self.tree(message.user_id, reference)
            return await self.progress(message.user_id, reference)
        match = re.match(r"^blocked\s+(.+?)(?:\s+because\s+(.+))?$", text, re.I)
        if match:
            if not match.group(2): return "What is blocking it? Send: blocked <task> because <reason>."
            return await self.block(message.user_id, match.group(1), match.group(2))
        return None

    async def task(self, user_id: str, reference: str) -> dict | None:
        rows = await self.db.select("tasks", {"user_id": f"eq.{user_id}", "status": "in.(todo,doing,done)", "title": f"ilike.*{reference}*", "order": "created_at.desc", "limit": "2"})
        return rows[0] if len(rows) == 1 else None

    async def list_view(self, user_id: str, view: str) -> str:
        now = datetime.now(self.tz)
        if view == "load":
            rows = await self.db.select("tasks", {"user_id": f"eq.{user_id}", "status": "in.(todo,doing)"})
            overdue = sum(t.get("due_at") and t["due_at"] < now.isoformat() for t in rows)
            week = sum(t.get("due_at") and t["due_at"] < (now + timedelta(days=7)).isoformat() for t in rows)
            return f"Load: {overdue} overdue, {week} due in the next 7 days, {len(rows)} open."
        params = {"user_id": f"eq.{user_id}", "status": "in.(todo,doing)", "blocked_at": "is.null", "order": "due_at.asc,priority.asc", "limit": "10"}
        rows = await self.db.select("tasks", params)
        if view == "overdue": rows = [t for t in rows if t.get("due_at") and t["due_at"] < now.isoformat()]
        elif view in {"today", "focus"}: rows = [t for t in rows if t.get("due_at") and t["due_at"] < (now.replace(hour=23, minute=59, second=59)).isoformat()][:3 if view == "focus" else 10]
        elif view == "week": rows = [t for t in rows if t.get("due_at") and t["due_at"] < (now + timedelta(days=7)).isoformat()]
        elif view == "stuck":
            events = await self.db.select("task_events", {"user_id": f"eq.{user_id}", "event_type": "eq.rescheduled", "select": "task_id"})
            counts = {e["task_id"]: sum(x["task_id"] == e["task_id"] for x in events) for e in events}
            rows = [t for t in rows if counts.get(t["id"], 0) >= 2]
        if not rows: return "Nothing to show."
        heading = "Focus" if view == "focus" else view.title()
        return heading + ":\n" + "\n".join(f"{i}. [{t['priority']}] {t['title']}" for i, t in enumerate(rows, 1))

    async def status(self, user_id: str, reference: str) -> str:
        task = await self.task(user_id, reference)
        if not task: return "I couldn't find one unambiguous task. Use more of its title."
        reminders = await self.db.select("reminders", {"task_id": f"eq.{task['id']}", "select": "status"})
        fired = sum(r["status"] == "sent" for r in reminders)
        blocked = f"Blocked: {task['blocked_reason']}" if task.get("blocked_at") else "Not blocked"
        return f"{task['title']}\nPriority: {task['priority']} | Source: {task['source']}\nDue: {task.get('due_at') or 'none'}\n{blocked}\nReminders sent: {fired}"

    async def why(self, user_id: str, reference: str) -> str:
        task = await self.task(user_id, reference)
        if not task: return "I couldn't find one unambiguous task."
        if not task.get("goal_id"): return f"{task['title']} is a {task['source']} task, not linked to a goal."
        chain, goal_id = [], task["goal_id"]
        while goal_id:
            rows = await self.db.select("goals", {"id": f"eq.{goal_id}", "limit": "1"})
            if not rows: break
            goal = rows[0]; chain.append(f"{goal['level']}: {goal['title']}"); goal_id = goal.get("parent_goal_id")
        return f"{task['title']} exists because:\n" + "\n".join(chain)

    async def block(self, user_id: str, reference: str, reason: str) -> str:
        task = await self.task(user_id, reference)
        if not task: return "I couldn't find one unambiguous task."
        now = datetime.now(self.tz).isoformat()
        await self.db.update("tasks", {"id": f"eq.{task['id']}"}, {"blocked_at": now, "blocked_reason": reason})
        await self.db.insert("task_events", {"task_id": task["id"], "user_id": user_id, "event_type": "blocked", "details": {"reason": reason}})
        return f"Blocked {task['title']}. Reminders are paused until you send ‘unblock {task['title']}’."

    async def unblock(self, user_id: str, reference: str) -> str:
        task = await self.task(user_id, reference)
        if not task: return "I couldn't find one unambiguous task."
        await self.db.update("tasks", {"id": f"eq.{task['id']}"}, {"blocked_at": None, "blocked_reason": None})
        await self.db.insert("task_events", {"task_id": task["id"], "user_id": user_id, "event_type": "unblocked"})
        return f"Unblocked {task['title']}."

    async def history(self, user_id: str, reference: str) -> str:
        task = await self.task(user_id, reference)
        if not task: return "I couldn't find one unambiguous task."
        events = await self.db.select("task_events", {"task_id": f"eq.{task['id']}", "order": "created_at.desc", "limit": "10"})
        lines = [f"Created: {task['created_at']}"] + [f"{e['created_at']}: {e['event_type']}" for e in events]
        return task['title'] + "\n" + "\n".join(lines)

    async def progress(self, user_id: str, reference: str) -> str:
        goals = await self.db.select("goals", {"user_id": f"eq.{user_id}", "status": "eq.active"})
        key = reference.lower()
        if key in {"week", "month", "year"}: goals = [g for g in goals if g["level"] == {"week":"weekly","month":"monthly","year":"yearly"}[key]]
        else: goals = [g for g in goals if key in g["title"].lower()]
        if not goals: return "No matching active goal."
        goal_ids = {g['id'] for g in goals}; tasks = await self.db.select("tasks", {"user_id": f"eq.{user_id}"})
        related = [t for t in tasks if t.get('goal_id') in goal_ids]
        done = sum(t['status'] == 'done' for t in related); total = len(related)
        return f"Progress: {done}/{total} tasks complete ({round(100 * done / total) if total else 0}%)."

    async def at_risk(self, user_id: str) -> str:
        goals = await self.db.select("goals", {"user_id": f"eq.{user_id}", "status": "eq.active", "target_date": "not.is.null"})
        due = [g for g in goals if g['target_date'] < datetime.now(self.tz).date().isoformat()]
        return "At risk:\n" + "\n".join(g['title'] for g in due) if due else "No dated active goals are currently overdue."

    async def tree(self, user_id: str, reference: str) -> str:
        goals = await self.db.select("goals", {"user_id": f"eq.{user_id}", "status": "eq.active"})
        roots = [g for g in goals if reference.lower() in g['title'].lower()]
        if not roots: return "No matching active goal."
        root = roots[0]; by_parent = {}
        for goal in goals: by_parent.setdefault(goal.get('parent_goal_id'), []).append(goal)
        lines = []
        def visit(goal, depth=0):
            lines.append("  " * depth + f"- {goal['title']} ({goal['level']})")
            for child in by_parent.get(goal['id'], []): visit(child, depth + 1)
        visit(root); return "\n".join(lines)

    async def undo(self, user_id: str) -> str:
        events = await self.db.select("task_events", {"user_id": f"eq.{user_id}", "order": "created_at.desc", "limit": "1"})
        if not events: return "Nothing to undo yet."
        event = events[0]
        if event['event_type'] == 'blocked':
            await self.db.update("tasks", {"id": f"eq.{event['task_id']}"}, {"blocked_at": None, "blocked_reason": None})
            return "Undid the most recent block."
        if event['event_type'] == 'unblocked': return "The most recent action was unblock; re-block it with a reason if needed."
        return "That action cannot be undone yet."