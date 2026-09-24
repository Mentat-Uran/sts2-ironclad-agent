from __future__ import annotations

from typing import Any

import httpx


class GameApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class GameApi:
    def __init__(self, base_url: str, timeout_seconds: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds)

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.TimeoutException as exc:
            raise GameApiError(f"Game bridge timed out on {method} {path}; no action was retried.") from exc
        except httpx.HTTPError as exc:
            raise GameApiError(f"Game bridge unavailable on {method} {path}: {type(exc).__name__}.") from exc
        if response.status_code >= 400:
            excerpt = response.text[:400]
            raise GameApiError(f"Game bridge returned HTTP {response.status_code}: {excerpt}", response.status_code)
        try:
            data = response.json()
        except ValueError as exc:
            raise GameApiError(f"Game bridge returned non-JSON on {method} {path}.", response.status_code) from exc
        if not isinstance(data, dict):
            raise GameApiError(f"Game bridge returned an unexpected payload on {method} {path}.", response.status_code)
        return data

    async def ping(self) -> dict[str, Any]:
        return await self._request("GET", "/")

    async def read_state(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/singleplayer", params={"format": "json"})

    async def act(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Never retry a game action: a network timeout leaves the outcome unknown.
        return await self._request("POST", "/api/v1/singleplayer", json=payload)
