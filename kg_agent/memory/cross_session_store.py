from __future__ import annotations

from typing import Any


class CrossSessionStore:
    async def search(
        self, user_id: str | None, query: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        return []
