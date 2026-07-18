"""Small async PostgREST client; all state-changing multi-row operations use SQL RPCs."""
from typing import Any
import httpx


class Database:
    def __init__(self, url: str, service_key: str):
        self.base = f"{url.rstrip('/')}/rest/v1"
        self.headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }
        self.client = httpx.AsyncClient(timeout=20)

    async def close(self) -> None:
        await self.client.aclose()

    async def rpc(self, name: str, params: dict[str, Any]) -> Any:
        response = await self.client.post(f"{self.base}/rpc/{name}", headers=self.headers, json=params)
        response.raise_for_status()
        # PostgreSQL functions returning void are represented by Supabase as 204.
        return response.json() if response.content else None

    async def select(self, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        response = await self.client.get(f"{self.base}/{table}", headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    async def insert(self, table: str, value: dict[str, Any], *, ignore_duplicates: bool = False) -> list[dict[str, Any]]:
        headers = self.headers | {"Prefer": "return=representation"}
        if ignore_duplicates:
            headers["Prefer"] = "return=representation,resolution=ignore-duplicates"
        response = await self.client.post(f"{self.base}/{table}", headers=headers, json=value)
        response.raise_for_status()
        return response.json()

    async def update(self, table: str, filters: dict[str, str], value: dict[str, Any]) -> list[dict[str, Any]]:
        response = await self.client.patch(
            f"{self.base}/{table}", headers=self.headers | {"Prefer": "return=representation"},
            params=filters, json=value,
        )
        response.raise_for_status()
        return response.json()

    async def delete(self, table: str, filters: dict[str, str]) -> None:
        response = await self.client.delete(f"{self.base}/{table}", headers=self.headers, params=filters)
        response.raise_for_status()
