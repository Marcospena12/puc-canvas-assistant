from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from .db import Database
from .models import (
    Announcement,
    Assignment,
    Course,
    CourseFile,
    Module,
    ModuleItem,
    PlannerItem,
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _bool(value: bool) -> int:
    return 1 if value else 0


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _rows(rows: list[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ----------------------------- writes -----------------------------

    def upsert_courses(self, user_id: str, courses: list[Course]) -> None:
        now = _now_iso()
        rows = [
            (
                user_id,
                c.id,
                c.name,
                c.course_code,
                c.workflow_state,
                c.term_id,
                c.term_name,
                c.term_start_at,
                c.term_end_at,
                c.current_score,
                c.current_grade,
                _json(c.teachers),
                _json(c.tabs),
                _bool(c.is_subject),
                1,
                now,
            )
            for c in courses
        ]
        with closing(self.db.connect()) as conn, conn:
            conn.execute("UPDATE courses SET active = 0 WHERE user_id = ?", (user_id,))
            conn.executemany(
                """
                INSERT INTO courses (
                    user_id, id, name, course_code, workflow_state, term_id, term_name,
                    term_start_at, term_end_at, current_score, current_grade,
                    teachers_json, tabs_json, is_subject, active, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id, id) DO UPDATE SET
                    name = excluded.name,
                    course_code = excluded.course_code,
                    workflow_state = excluded.workflow_state,
                    term_id = excluded.term_id,
                    term_name = excluded.term_name,
                    term_start_at = excluded.term_start_at,
                    term_end_at = excluded.term_end_at,
                    current_score = excluded.current_score,
                    current_grade = excluded.current_grade,
                    teachers_json = excluded.teachers_json,
                    tabs_json = excluded.tabs_json,
                    is_subject = excluded.is_subject,
                    active = 1,
                    synced_at = excluded.synced_at
                """,
                rows,
            )

    def replace_assignments(
        self, user_id: str, course_id: int, assignments: list[Assignment]
    ) -> None:
        now = _now_iso()
        rows = [
            (
                user_id,
                a.id,
                a.course_id,
                a.name,
                a.due_at,
                a.lock_at,
                a.unlock_at,
                a.points_possible,
                _json(a.submission_types),
                _bool(a.is_quiz),
                a.html_url,
                _bool(a.published),
                a.submission_state,
                a.submission_score,
                a.submission_grade,
                a.submitted_at,
                a.graded_at,
                _bool(a.late),
                _bool(a.missing),
                _bool(a.excused),
                now,
            )
            for a in assignments
        ]
        with closing(self.db.connect()) as conn, conn:
            conn.execute(
                "DELETE FROM assignments WHERE user_id = ? AND course_id = ?",
                (user_id, course_id),
            )
            conn.executemany(
                """
                INSERT INTO assignments (
                    user_id, id, course_id, name, due_at, lock_at, unlock_at,
                    points_possible, submission_types_json, is_quiz, html_url,
                    published, submission_state, submission_score, submission_grade,
                    submitted_at, graded_at, late, missing, excused, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def upsert_planner_items(self, user_id: str, items: list[PlannerItem]) -> None:
        now = _now_iso()
        rows = [
            (
                user_id,
                i.plannable_type,
                i.plannable_id,
                i.course_id,
                i.context_name,
                i.title,
                i.plannable_date,
                i.due_at,
                i.points_possible,
                i.html_url,
                _bool(i.submitted),
                _bool(i.graded),
                _bool(i.late),
                _bool(i.missing),
                _bool(i.new_activity),
                now,
            )
            for i in items
        ]
        with closing(self.db.connect()) as conn, conn:
            conn.executemany(
                """
                INSERT INTO planner_items (
                    user_id, plannable_type, plannable_id, course_id, context_name,
                    title, plannable_date, due_at, points_possible, html_url,
                    submitted, graded, late, missing, new_activity, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id, plannable_type, plannable_id) DO UPDATE SET
                    course_id = excluded.course_id,
                    context_name = excluded.context_name,
                    title = excluded.title,
                    plannable_date = excluded.plannable_date,
                    due_at = excluded.due_at,
                    points_possible = excluded.points_possible,
                    html_url = excluded.html_url,
                    submitted = excluded.submitted,
                    graded = excluded.graded,
                    late = excluded.late,
                    missing = excluded.missing,
                    new_activity = excluded.new_activity,
                    synced_at = excluded.synced_at
                """,
                rows,
            )

    def upsert_announcements(self, user_id: str, announcements: list[Announcement]) -> None:
        now = _now_iso()
        rows = [
            (
                user_id,
                a.id,
                a.course_id,
                a.title,
                a.message_html,
                a.author,
                a.posted_at,
                a.url,
                a.read_state,
                now,
            )
            for a in announcements
        ]
        with closing(self.db.connect()) as conn, conn:
            conn.executemany(
                """
                INSERT INTO announcements (
                    user_id, id, course_id, title, message_html, author,
                    posted_at, url, read_state, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id, id) DO UPDATE SET
                    title = excluded.title,
                    message_html = excluded.message_html,
                    author = excluded.author,
                    posted_at = excluded.posted_at,
                    url = excluded.url,
                    read_state = excluded.read_state,
                    synced_at = excluded.synced_at
                """,
                rows,
            )

    def replace_files(self, user_id: str, course_id: int, files: list[CourseFile]) -> None:
        now = _now_iso()
        unique = {f.id: f for f in files}
        rows = [
            (
                user_id,
                f.id,
                f.course_id,
                f.display_name,
                f.filename,
                f.content_type,
                f.size,
                f.created_at,
                f.updated_at,
                _bool(f.hidden),
                _bool(f.locked),
                f.url,
                f.mime_class,
                now,
            )
            for f in unique.values()
        ]
        with closing(self.db.connect()) as conn, conn:
            conn.execute(
                "DELETE FROM files WHERE user_id = ? AND course_id = ?",
                (user_id, course_id),
            )
            conn.executemany(
                """
                INSERT INTO files (
                    user_id, id, course_id, display_name, filename, content_type,
                    size, created_at, updated_at, hidden, locked, url, mime_class, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def replace_modules(
        self,
        user_id: str,
        course_id: int,
        modules: list[Module],
        items: list[ModuleItem],
    ) -> None:
        now = _now_iso()
        module_rows = [
            (
                user_id,
                m.course_id,
                m.id,
                m.name,
                m.position,
                m.state,
                m.unlock_at,
                _bool(m.require_sequential_progress),
                m.items_count,
                now,
            )
            for m in modules
        ]
        item_rows = [
            (
                user_id,
                i.course_id,
                i.module_id,
                i.id,
                i.title,
                i.type,
                i.position,
                i.html_url,
                i.url,
                i.external_url,
                i.page_url,
                i.content_id,
                now,
            )
            for i in items
        ]
        with closing(self.db.connect()) as conn, conn:
            conn.execute(
                "DELETE FROM modules WHERE user_id = ? AND course_id = ?",
                (user_id, course_id),
            )
            conn.executemany(
                """
                INSERT INTO modules (
                    user_id, course_id, id, name, position, state, unlock_at,
                    require_sequential_progress, items_count, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                module_rows,
            )
            conn.executemany(
                """
                INSERT INTO module_items (
                    user_id, course_id, module_id, id, title, type, position,
                    html_url, url, external_url, page_url, content_id, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                item_rows,
            )

    # ----------------------------- sync issues -----------------------------

    def clear_issues(self, user_id: str, resource: str, scope_id: int = 0) -> None:
        with closing(self.db.connect()) as conn, conn:
            conn.execute(
                "DELETE FROM sync_issues WHERE user_id = ? AND resource = ? AND scope_id = ?",
                (user_id, resource, scope_id),
            )

    def add_issue(
        self,
        user_id: str,
        resource: str,
        message: str,
        scope_id: int = 0,
        status_code: int | None = None,
    ) -> None:
        with closing(self.db.connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO sync_issues
                    (user_id, resource, scope_id, status_code, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, resource, scope_id, status_code, message, _now_iso()),
            )

    def list_issues(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM sync_issues WHERE user_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return _rows(rows)

    # ----------------------------- notifications -----------------------------

    def get_meta(self, key: str) -> str | None:
        with closing(self.db.connect()) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with closing(self.db.connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO schema_meta (key, value) VALUES (?, ?)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def add_notification_events(
        self,
        user_id: str,
        events: Sequence[tuple[str, str, str | None]],
    ) -> None:
        if not events:
            return
        now = _now_iso()
        rows = [(user_id, key, kind, resource_id, now) for key, kind, resource_id in events]
        with closing(self.db.connect()) as conn, conn:
            conn.executemany(
                """
                INSERT INTO notification_events
                    (user_id, event_key, kind, resource_id, sent_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (user_id, event_key) DO NOTHING
                """,
                rows,
            )

    def has_notification_event(self, user_id: str, event_key: str) -> bool:
        with closing(self.db.connect()) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM notification_events
                WHERE user_id = ? AND event_key = ?
                LIMIT 1
                """,
                (user_id, event_key),
            ).fetchone()
        return row is not None

    def get_notification_event_keys(self, user_id: str, event_keys: Sequence[str]) -> set[str]:
        if not event_keys:
            return set()
        placeholders = ",".join("?" for _ in event_keys)
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT event_key FROM notification_events
                WHERE user_id = ? AND event_key IN ({placeholders})
                """,
                [user_id, *event_keys],
            ).fetchall()
        return {str(row["event_key"]) for row in rows}

    def list_assignment_ids(self, user_id: str) -> list[int]:
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                """
                SELECT a.id
                FROM assignments a
                JOIN courses c ON c.user_id = a.user_id AND c.id = a.course_id
                WHERE a.user_id = ? AND c.active = 1
                """,
                (user_id,),
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def list_announcement_ids(self, user_id: str) -> list[int]:
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                """
                SELECT a.id
                FROM announcements a
                JOIN courses c ON c.user_id = a.user_id AND c.id = a.course_id
                WHERE a.user_id = ? AND c.active = 1
                """,
                (user_id,),
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def list_unnotified_assignments(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                """
                SELECT a.*, c.name AS course_name
                FROM assignments a
                JOIN courses c ON c.user_id = a.user_id AND c.id = a.course_id
                WHERE a.user_id = ? AND a.published = 1 AND c.active = 1
                  AND NOT EXISTS (
                      SELECT 1 FROM notification_events e
                      WHERE e.user_id = a.user_id
                        AND e.event_key = 'new_assignment:' || a.id
                  )
                ORDER BY (a.due_at IS NULL), a.due_at ASC, a.id ASC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return _rows(rows)

    def list_unnotified_announcements(self, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                """
                SELECT a.*, c.name AS course_name, c.is_subject
                FROM announcements a
                JOIN courses c ON c.user_id = a.user_id AND c.id = a.course_id
                WHERE a.user_id = ? AND c.active = 1
                  AND NOT EXISTS (
                      SELECT 1 FROM notification_events e
                      WHERE e.user_id = a.user_id
                        AND e.event_key = 'new_announcement:' || a.id
                  )
                ORDER BY a.posted_at DESC, a.id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return _rows(rows)

    # ----------------------------- reads -----------------------------

    def get_courses(
        self,
        user_id: str,
        *,
        active_only: bool = True,
        current_only: bool = False,
        include_non_subjects: bool = True,
        now_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if active_only:
            clauses.append("active = 1")
        if current_only:
            clauses.append(
                "term_start_at IS NOT NULL AND term_end_at IS NOT NULL "
                "AND term_start_at <= ? AND term_end_at >= ?"
            )
            now = now_iso or _now_iso()
            params.extend([now, now])
        if not include_non_subjects:
            clauses.append("is_subject = 1")
        where = " AND ".join(clauses)
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM courses WHERE {where} ORDER BY name COLLATE NOCASE",
                params,
            ).fetchall()
        return _rows(rows)

    def get_course(self, user_id: str, course_id: int) -> dict[str, Any] | None:
        with closing(self.db.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM courses WHERE user_id = ? AND id = ?",
                (user_id, course_id),
            ).fetchone()
        return dict(row) if row else None

    def get_upcoming_assignments(
        self,
        user_id: str,
        start_iso: str,
        end_iso: str,
        course_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = [
            "a.user_id = ?",
            "a.due_at IS NOT NULL",
            "a.due_at >= ?",
            "a.due_at < ?",
            "a.published = 1",
            "c.active = 1",
        ]
        params: list[Any] = [user_id, start_iso, end_iso]
        if course_ids is not None:
            if not course_ids:
                return []
            placeholders = ",".join("?" for _ in course_ids)
            clauses.append(f"a.course_id IN ({placeholders})")
            params.extend(course_ids)
        where = " AND ".join(clauses)
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT a.*, c.name AS course_name, c.course_code
                FROM assignments a
                JOIN courses c ON c.user_id = a.user_id AND c.id = a.course_id
                WHERE {where}
                ORDER BY a.due_at ASC
                """,
                params,
            ).fetchall()
        return _rows(rows)

    def get_assignments(
        self,
        user_id: str,
        course_id: int,
        *,
        status: str = "pending",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clauses = ["a.user_id = ?", "a.course_id = ?", "a.published = 1", "c.active = 1"]
        params: list[Any] = [user_id, course_id]
        if status == "pending":
            clauses.append("a.submitted_at IS NULL AND a.excused = 0")
        elif status == "graded":
            clauses.append("(a.submission_state = 'graded' OR a.graded_at IS NOT NULL)")
        where = " AND ".join(clauses)
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT a.*, c.name AS course_name
                FROM assignments a
                JOIN courses c ON c.user_id = a.user_id AND c.id = a.course_id
                WHERE {where}
                ORDER BY (a.due_at IS NULL), a.due_at ASC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return _rows(rows)

    def get_grades(self, user_id: str, course_ids: list[int] | None = None) -> list[dict[str, Any]]:
        clauses = ["c.user_id = ?", "c.active = 1"]
        params: list[Any] = [user_id]
        if course_ids is not None:
            if not course_ids:
                return []
            placeholders = ",".join("?" for _ in course_ids)
            clauses.append(f"c.id IN ({placeholders})")
            params.extend(course_ids)
        where = " AND ".join(clauses)
        now = _now_iso()
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT
                    c.id, c.name, c.course_code, c.current_score, c.term_name,
                    COUNT(a.id) AS assignments_total,
                    COALESCE(SUM(CASE WHEN a.submission_state = 'graded'
                        OR a.graded_at IS NOT NULL THEN 1 ELSE 0 END), 0) AS graded_count,
                    COALESCE(SUM(CASE WHEN a.missing = 1 THEN 1 ELSE 0 END), 0) AS missing_count,
                    COALESCE(SUM(CASE WHEN a.due_at IS NOT NULL AND a.due_at < ?
                        AND a.submitted_at IS NULL AND a.excused = 0
                        THEN 1 ELSE 0 END), 0) AS overdue_count
                FROM courses c
                LEFT JOIN assignments a
                    ON a.user_id = c.user_id AND a.course_id = c.id AND a.published = 1
                WHERE {where}
                GROUP BY c.id
                ORDER BY c.name COLLATE NOCASE
                """,
                [now, *params],
            ).fetchall()
        return _rows(rows)

    def get_announcements(
        self,
        user_id: str,
        *,
        since_iso: str,
        course_ids: list[int] | None = None,
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        clauses = ["a.user_id = ?", "a.posted_at IS NOT NULL", "a.posted_at >= ?", "c.active = 1"]
        params: list[Any] = [user_id, since_iso]
        if course_ids is not None:
            if not course_ids:
                return []
            placeholders = ",".join("?" for _ in course_ids)
            clauses.append(f"a.course_id IN ({placeholders})")
            params.extend(course_ids)
        where = " AND ".join(clauses)
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT a.*, c.name AS course_name, c.is_subject
                FROM announcements a
                JOIN courses c ON c.user_id = a.user_id AND c.id = a.course_id
                WHERE {where}
                ORDER BY a.posted_at DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return _rows(rows)

    def get_files(
        self,
        user_id: str,
        *,
        course_ids: list[int] | None = None,
        query: str | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        clauses = ["f.user_id = ?", "c.active = 1", "f.hidden = 0"]
        params: list[Any] = [user_id]
        if course_ids is not None:
            if not course_ids:
                return []
            placeholders = ",".join("?" for _ in course_ids)
            clauses.append(f"f.course_id IN ({placeholders})")
            params.extend(course_ids)
        if query:
            clauses.append("(f.display_name LIKE ? OR f.filename LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like])
        where = " AND ".join(clauses)
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT f.*, c.name AS course_name
                FROM files f
                JOIN courses c ON c.user_id = f.user_id AND c.id = f.course_id
                WHERE {where}
                ORDER BY f.updated_at DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return _rows(rows)

    def get_module_file_items(
        self,
        user_id: str,
        *,
        course_ids: list[int] | None = None,
        limit: int = 60,
    ) -> list[dict[str, Any]]:
        clauses = ["mi.user_id = ?", "mi.type = 'File'", "c.active = 1"]
        params: list[Any] = [user_id]
        if course_ids is not None:
            if not course_ids:
                return []
            placeholders = ",".join("?" for _ in course_ids)
            clauses.append(f"mi.course_id IN ({placeholders})")
            params.extend(course_ids)
        where = " AND ".join(clauses)
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT mi.*, c.name AS course_name
                FROM module_items mi
                JOIN courses c ON c.user_id = mi.user_id AND c.id = mi.course_id
                WHERE {where}
                ORDER BY mi.course_id, mi.module_id, mi.position
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return _rows(rows)

    def cache_stats(self, user_id: str) -> dict[str, Any]:
        tables = ("courses", "assignments", "planner_items", "announcements", "files", "modules")
        stats: dict[str, Any] = {}
        with closing(self.db.connect()) as conn:
            for table in tables:
                count = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE user_id = ?",
                    (user_id,),
                ).fetchone()["n"]
                last = conn.execute(
                    f"SELECT MAX(synced_at) AS last FROM {table} WHERE user_id = ?",
                    (user_id,),
                ).fetchone()["last"]
                stats[table] = {"count": count, "last_synced_at": last}
        return stats

    def is_empty(self, user_id: str) -> bool:
        with closing(self.db.connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM courses WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return row["n"] == 0
