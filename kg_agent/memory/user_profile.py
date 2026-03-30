from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class UserProfile:
    user_id: str
    attributes: dict[str, Any] = field(default_factory=dict)


class UserProfileStore:
    def __init__(self):
        self._profiles: dict[str, UserProfile] = {}
        self._lock = asyncio.Lock()

    async def get_profile(self, user_id: str | None) -> dict[str, Any]:
        if not user_id:
            return {}
        async with self._lock:
            profile = self._profiles.get(user_id)
        return dict(profile.attributes) if profile else {}

    async def update_profile(
        self, user_id: str | None, attributes: dict[str, Any]
    ) -> None:
        if not user_id:
            return
        async with self._lock:
            profile = self._profiles.setdefault(user_id, UserProfile(user_id=user_id))
            profile.attributes.update(attributes)
