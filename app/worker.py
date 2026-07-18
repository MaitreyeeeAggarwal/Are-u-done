import asyncio
import logging
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from .config import settings
from .db import Database
from .llm import NvidiaLLM
from .schemas import QueueMessage
from .services import Agent, Messenger


logger = logging.getLogger(__name__)


class Worker:
    def __init__(self) -> None:
        self.settings = settings()
        self.db = Database(self.settings.supabase_url, self.settings.supabase_service_role_key)
        self.agent = Agent(self.db, NvidiaLLM(self.settings), self.settings)
        self.messenger = Messenger(self.settings)
        self.tz = ZoneInfo(self.settings.timezone)

    async def process_queue(self) -> None:
        messages = await self.db.rpc("claim_inbound_messages", {"p_worker_id": self.settings.worker_id, "p_lease_seconds": self.settings.queue_lease_seconds})
        for row in messages:
            message = QueueMessage.model_validate(row)
            try:
                await self.agent.process(message)
                await self.db.rpc("finish_inbound_message", {"p_queue_id": message.id, "p_worker_id": self.settings.worker_id})
            except Exception:
                # Lease expiry makes the item retryable. Do not mark successful on unexpected defects.
                await self.db.rpc("release_inbound_message", {"p_queue_id": message.id, "p_worker_id": self.settings.worker_id})

    async def deliver_outbox(self) -> None:
        rows = await self.db.rpc("claim_outbox", {"p_worker_id": self.settings.worker_id, "p_max_attempts": self.settings.max_delivery_attempts})
        for row in rows:
            try:
                await self.messenger.send(row["to_number"], row["body"])
                await self.db.rpc("complete_outbox", {"p_outbox_id": row["id"], "p_worker_id": self.settings.worker_id})
            except Exception as error:
                await self.db.rpc("fail_outbox", {"p_outbox_id": row["id"], "p_worker_id": self.settings.worker_id, "p_error": str(error), "p_max_attempts": self.settings.max_delivery_attempts})

    async def reminders_and_reviews(self) -> None:
        await self.db.rpc("queue_due_reminders", {"p_worker_id": self.settings.worker_id})
        await self.db.rpc("queue_due_reviews", {"p_timezone": self.settings.timezone})
        await self.db.rpc("expire_conversation_state", {})

    async def tick(self) -> None:
        try:
            await self.process_queue()
            await self.deliver_outbox()
        except Exception:
            # Supabase/network failures must not terminate the polling worker.
            # Leases expire, so a later successful poll safely reclaims the work.
            logger.exception("Worker tick failed; retrying on the next poll")

    async def scheduled_tick(self) -> None:
        try:
            await self.reminders_and_reviews()
            await self.deliver_outbox()
        except Exception:
            logger.exception("Scheduled worker tick failed; retrying on the next poll")

    async def close(self) -> None:
        await self.db.close()
        await self.agent.llm.close()


async def run() -> None:
    logging.basicConfig(level=logging.INFO)
    worker = Worker()
    scheduler = AsyncIOScheduler(timezone=worker.tz)
    scheduler.add_job(worker.tick, "interval", seconds=5, id="inbound-poll", max_instances=1, coalesce=True)
    scheduler.add_job(worker.scheduled_tick, "interval", minutes=5, id="durable-poll", max_instances=1, coalesce=True)
    scheduler.start()
    await worker.tick()
    await worker.scheduled_tick()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        scheduler.shutdown()
        await worker.close()


if __name__ == "__main__":
    asyncio.run(run())
