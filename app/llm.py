import asyncio
import json
from typing import TypeVar
import httpx
from pydantic import BaseModel, ValidationError
from .config import Settings
from .schemas import DecompositionResult, GoalItems, IntentResult, StudyPlan

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(Exception):
    pass


class NvidiaLLM:
    def __init__(self, config: Settings):
        self.config = config
        self.client = httpx.AsyncClient(base_url=config.nvidia_base_url.rstrip("/"), timeout=55)

    async def close(self) -> None:
        await self.client.aclose()

    async def _json(self, model: str, prompt: str, schema: type[T], *, timeout: float = 55) -> T:
        if not self.config.nvidia_api_key:
            raise LLMUnavailable("NVIDIA_API_KEY is not configured")
        payload = {
            "model": model,
            "temperature": 0,
            "max_tokens": 2048,
            "messages": [
                {"role": "system", "content": "/no_think\nReturn only valid JSON. Never use Markdown."},
                {"role": "user", "content": f"{prompt}\nJSON schema: {json.dumps(schema.model_json_schema())}"},
            ],
        }
        headers = {"Authorization": f"Bearer {self.config.nvidia_api_key}"}
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self.client.post("/chat/completions", headers=headers, json=payload, timeout=timeout)
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                if not content:
                    raise ValueError("NVIDIA returned an empty structured response")
                # Some compatible endpoints preserve an empty thinking tag or a
                # Markdown fence despite the instruction. Extract the JSON object
                # before Pydantic validates it; validation still remains strict.
                content = content.replace("<think></think>", "").strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                first, last = content.find("{"), content.rfind("}")
                if first >= 0 and last >= first:
                    content = content[first : last + 1]
                return schema.model_validate_json(content)
            except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as error:
                last_error = error
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
        raise LLMUnavailable(str(last_error))

    async def classify(self, body: str, now_iso: str) -> IntentResult:
        return await self._json(
            self.config.nvidia_nano_model,
            "Classify this WhatsApp message into one intent and extract only stated facts. "
            f"Current time: {now_iso}. Message: {body}", IntentResult,
        )

    async def decompose(self, context: str) -> DecompositionResult:
        return await self._json(self.config.nvidia_super_model, context, DecompositionResult)

    async def extract_goals(self, message: str, level: str) -> GoalItems:
        return await self._json(
            self.config.nvidia_super_model,
            f"Extract the distinct {level} goals the user explicitly stated. Preserve their meaning; "
            f"do not invent goals. Message: {message}",
            GoalItems,
        )

    async def study_plan(self, context: str) -> StudyPlan:
        return await self._json(self.config.nvidia_super_model, context, StudyPlan)
