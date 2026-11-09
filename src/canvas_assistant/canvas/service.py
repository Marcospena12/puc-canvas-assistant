from __future__ import annotations

from typing import Any

from .client import CanvasClient


class CanvasService:
    """Métodos de domínio sobre os endpoints que usamos do Canvas."""

    def __init__(self, client: CanvasClient) -> None:
        self.client = client

    async def get_profile(self) -> dict[str, Any]:
        result = await self.client.get("/users/self/profile")
        return result if isinstance(result, dict) else {}

    async def get_courses(self) -> list[dict[str, Any]]:
        return await self.client.get_all(
            "/courses",
            {
                "enrollment_state": "active",
                "include[]": ["term", "total_scores", "teachers"],
            },
        )

    async def get_tabs(self, course_id: int) -> list[dict[str, Any]]:
        return await self.client.get_all(f"/courses/{course_id}/tabs")

    async def get_planner_items(self, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        return await self.client.get_all(
            "/planner/items",
            {"start_date": start_iso, "end_date": end_iso},
        )

    async def get_assignments(self, course_id: int) -> list[dict[str, Any]]:
        return await self.client.get_all(
            f"/courses/{course_id}/assignments",
            {"include[]": ["submission"]},
        )

    async def get_announcements(
        self, course_id: int, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        return await self.client.get_all(
            "/announcements",
            {
                "context_codes[]": [f"course_{course_id}"],
                "start_date": start_date,
                "end_date": end_date,
                "active_only": "true",
            },
        )

    async def get_files(self, course_id: int) -> list[dict[str, Any]]:
        return await self.client.get_all(f"/courses/{course_id}/files")

    async def get_file(self, course_id: int, file_id: int) -> dict[str, Any]:
        result = await self.client.get(f"/courses/{course_id}/files/{file_id}")
        return result if isinstance(result, dict) else {}

    async def get_folders(self, course_id: int) -> list[dict[str, Any]]:
        return await self.client.get_all(f"/courses/{course_id}/folders")

    async def get_modules(self, course_id: int) -> list[dict[str, Any]]:
        return await self.client.get_all(
            f"/courses/{course_id}/modules",
            {"include[]": ["items"]},
        )
