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
                f.folder_id,
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
                    size, created_at, updated_at, hidden, locked, url, mime_class,
                    folder_id, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def list_file_ids(self, user_id: str) -> list[int]:
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                """
                SELECT fs.file_id
                FROM file_sync_state fs
                JOIN courses c ON c.user_id = fs.user_id AND c.id = fs.course_id
                WHERE fs.user_id = ? AND fs.active = 1 AND c.active = 1
                """,
                (user_id,),
            ).fetchall()
        return [int(row["file_id"]) for row in rows]

    def list_unnotified_files(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                """
                SELECT fs.*, c.name AS course_name
                FROM file_sync_state fs
                JOIN courses c ON c.user_id = fs.user_id AND c.id = fs.course_id
                WHERE fs.user_id = ? AND fs.active = 1 AND c.active = 1
                  AND fs.status NOT IN ('ignored')
                  AND NOT EXISTS (
                      SELECT 1 FROM notification_events e
                      WHERE e.user_id = fs.user_id
                        AND e.event_key = 'new_file:' || fs.file_id
                  )
                ORDER BY fs.first_seen_at DESC, fs.file_id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return _rows(rows)

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
        include_hidden: bool = False,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        clauses = ["f.user_id = ?", "c.active = 1"]
        if not include_hidden:
            clauses.append("f.hidden = 0")
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
                SELECT
                    mi.*,
                    c.name AS course_name,
                    m.name AS module_name,
                    m.position AS module_position
                FROM module_items mi
                JOIN courses c ON c.user_id = mi.user_id AND c.id = mi.course_id
                LEFT JOIN modules m
                    ON m.user_id = mi.user_id AND m.course_id = mi.course_id AND m.id = mi.module_id
                WHERE {where}
                ORDER BY mi.course_id, m.position, mi.position
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

    # ----------------------------- file sync -----------------------------

    def get_files_retry_at(self, user_id: str, course_id: int) -> str | None:
        with closing(self.db.connect()) as conn:
            row = conn.execute(
                "SELECT files_retry_at FROM file_course_sync WHERE user_id = ? AND course_id = ?",
                (user_id, course_id),
            ).fetchone()
        return str(row["files_retry_at"]) if row and row["files_retry_at"] else None

    def set_file_tab_status(
        self,
        user_id: str,
        course_id: int,
        status: str,
        *,
        error: str | None = None,
        retry_at: str | None = None,
        success: bool = False,
    ) -> None:
        now = _now_iso()
        with closing(self.db.connect()) as conn, conn:
            if success:
                conn.execute(
                    """
                    INSERT INTO file_course_sync (
                        user_id, course_id, files_tab_status, files_tab_error,
                        files_retry_at, last_files_attempt_at, last_files_success_at, updated_at
                    ) VALUES (?, ?, ?, NULL, NULL, ?, ?, ?)
                    ON CONFLICT (user_id, course_id) DO UPDATE SET
                        files_tab_status = excluded.files_tab_status,
                        files_tab_error = NULL,
                        files_retry_at = NULL,
                        last_files_attempt_at = excluded.last_files_attempt_at,
                        last_files_success_at = excluded.last_files_success_at,
                        updated_at = excluded.updated_at
                    """,
                    (user_id, course_id, status, now, now, now),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO file_course_sync (
                        user_id, course_id, files_tab_status, files_tab_error,
                        files_retry_at, last_files_attempt_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (user_id, course_id) DO UPDATE SET
                        files_tab_status = excluded.files_tab_status,
                        files_tab_error = excluded.files_tab_error,
                        files_retry_at = excluded.files_retry_at,
                        last_files_attempt_at = excluded.last_files_attempt_at,
                        updated_at = excluded.updated_at
                    """,
                    (user_id, course_id, status, error, retry_at, now, now),
                )

    def get_file_course_sync(self, user_id: str, course_id: int) -> dict[str, Any] | None:
        with closing(self.db.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM file_course_sync WHERE user_id = ? AND course_id = ?",
                (user_id, course_id),
            ).fetchone()
        return dict(row) if row else None

    def upsert_file_course_stats(
        self,
        user_id: str,
        course_id: int,
        *,
        files_seen: int,
        new_files: int,
        downloaded: int,
        unchanged: int,
        ignored: int,
        failed: int,
    ) -> None:
        now = _now_iso()
        with closing(self.db.connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO file_course_sync (
                    user_id, course_id, last_run_at, files_seen, new_files,
                    downloaded, unchanged, ignored, failed, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id, course_id) DO UPDATE SET
                    last_run_at = excluded.last_run_at,
                    files_seen = excluded.files_seen,
                    new_files = excluded.new_files,
                    downloaded = excluded.downloaded,
                    unchanged = excluded.unchanged,
                    ignored = excluded.ignored,
                    failed = excluded.failed,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    course_id,
                    now,
                    files_seen,
                    new_files,
                    downloaded,
                    unchanged,
                    ignored,
                    failed,
                    now,
                ),
            )

    def clear_file_retries(self, user_id: str) -> None:
        now = _now_iso()
        with closing(self.db.connect()) as conn, conn:
            conn.execute(
                """
                UPDATE file_course_sync
                SET files_retry_at = NULL, updated_at = ?
                WHERE user_id = ? AND files_retry_at IS NOT NULL
                """,
                (now, user_id),
            )
            conn.execute(
                """
                UPDATE file_sync_state
                SET next_retry_at = NULL, updated_at = ?
                WHERE user_id = ? AND next_retry_at IS NOT NULL
                """,
                (now, user_id),
            )

    def mark_file_sync_inactive(self, user_id: str, course_id: int) -> None:
        with closing(self.db.connect()) as conn, conn:
            conn.execute(
                "UPDATE file_sync_state SET active = 0 WHERE user_id = ? AND course_id = ?",
                (user_id, course_id),
            )

    def get_file_sync_states(self, user_id: str, course_id: int) -> dict[int, dict[str, Any]]:
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM file_sync_state WHERE user_id = ? AND course_id = ?",
                (user_id, course_id),
            ).fetchall()
        return {int(row["file_id"]): dict(row) for row in rows}

    def upsert_file_sync_states(self, user_id: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        payloads = [
            (
                user_id,
                r["file_id"],
                r["course_id"],
                r["display_name"],
                r.get("filename") or "",
                r.get("content_type"),
                r.get("size"),
                r.get("modified_at"),
                r.get("folder_id"),
                r.get("folder_name"),
                _bool(bool(r.get("locked"))),
                _bool(bool(r.get("hidden"))),
                r.get("module_id"),
                r.get("module_name"),
                r.get("source") or "files",
                r.get("download_url"),
                r.get("local_path"),
                r.get("sha256"),
                r.get("status") or "pending",
                r.get("skip_reason"),
                r.get("error"),
                int(r.get("attempts") or 0),
                r.get("last_attempt_at"),
                r.get("next_retry_at"),
                r.get("downloaded_at"),
                r["first_seen_at"],
                r["updated_at"],
                _bool(bool(r.get("active", 1))),
            )
            for r in rows
        ]
        with closing(self.db.connect()) as conn, conn:
            conn.executemany(
                """
                INSERT INTO file_sync_state (
                    user_id, file_id, course_id, display_name, filename, content_type,
                    size, modified_at, folder_id, folder_name, locked, hidden,
                    module_id, module_name, source, download_url, local_path, sha256,
                    status, skip_reason, error, attempts, last_attempt_at, next_retry_at,
                    downloaded_at, first_seen_at, updated_at, active
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT (user_id, file_id) DO UPDATE SET
                    course_id = excluded.course_id,
                    display_name = excluded.display_name,
                    filename = excluded.filename,
                    content_type = excluded.content_type,
                    size = excluded.size,
                    modified_at = excluded.modified_at,
                    folder_id = excluded.folder_id,
                    folder_name = excluded.folder_name,
                    locked = excluded.locked,
                    hidden = excluded.hidden,
                    module_id = excluded.module_id,
                    module_name = excluded.module_name,
                    source = excluded.source,
                    download_url = excluded.download_url,
                    local_path = excluded.local_path,
                    sha256 = excluded.sha256,
                    status = excluded.status,
                    skip_reason = excluded.skip_reason,
                    error = excluded.error,
                    attempts = excluded.attempts,
                    last_attempt_at = excluded.last_attempt_at,
                    next_retry_at = excluded.next_retry_at,
                    downloaded_at = excluded.downloaded_at,
                    updated_at = excluded.updated_at,
                    active = excluded.active
                """,
                payloads,
            )

    def update_file_sync_state(
        self,
        user_id: str,
        file_id: int,
        **fields: Any,
    ) -> None:
        allowed = {
            "status",
            "skip_reason",
            "error",
            "attempts",
            "last_attempt_at",
            "next_retry_at",
            "downloaded_at",
            "local_path",
            "sha256",
            "size",
            "modified_at",
            "download_url",
            "active",
            "folder_name",
        }
        filtered = {k: v for k, v in fields.items() if k in allowed}
        if not filtered:
            return
        filtered["updated_at"] = _now_iso()
        set_clauses = ", ".join(f"{col} = ?" for col in filtered)
        values = [*filtered.values(), user_id, file_id]
        with closing(self.db.connect()) as conn, conn:
            conn.execute(
                f"UPDATE file_sync_state SET {set_clauses} WHERE user_id = ? AND file_id = ?",
                values,
            )

    def get_file_sync_state(
        self,
        user_id: str,
        *,
        course_id: int | None = None,
        active_only: bool = True,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["fs.user_id = ?", "c.active = 1"]
        params: list[Any] = [user_id]
        if active_only:
            clauses.append("fs.active = 1")
        if course_id is not None:
            clauses.append("fs.course_id = ?")
            params.append(course_id)
        where = " AND ".join(clauses)
        limit_clause = f" LIMIT {int(limit)}" if limit else ""
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT fs.*, c.name AS course_name
                FROM file_sync_state fs
                JOIN courses c ON c.user_id = fs.user_id AND c.id = fs.course_id
                WHERE {where}
                ORDER BY c.name COLLATE NOCASE, fs.module_id, fs.display_name COLLATE NOCASE
                {limit_clause}
                """,
                params,
            ).fetchall()
        return _rows(rows)

    def file_sync_stats(self, user_id: str) -> dict[str, Any]:
        now = _now_iso()
        with closing(self.db.connect()) as conn:
            status_rows = conn.execute(
                """
                SELECT fs.status, COUNT(*) AS count
                FROM file_sync_state fs
                JOIN courses c ON c.user_id = fs.user_id AND c.id = fs.course_id
                WHERE fs.user_id = ? AND fs.active = 1 AND c.active = 1
                GROUP BY fs.status
                """,
                (user_id,),
            ).fetchall()
            status_counts = {str(row["status"]): int(row["count"]) for row in status_rows}
            total = sum(status_counts.values())

            bytes_row = conn.execute(
                """
                SELECT COALESCE(SUM(fs.size), 0) AS total_bytes
                FROM file_sync_state fs
                JOIN courses c ON c.user_id = fs.user_id AND c.id = fs.course_id
                WHERE fs.user_id = ? AND fs.active = 1 AND c.active = 1
                  AND fs.status = 'downloaded'
                """,
                (user_id,),
            ).fetchone()
            total_bytes = int(bytes_row["total_bytes"]) if bytes_row else 0

            blocked_row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM file_course_sync
                WHERE user_id = ? AND files_retry_at IS NOT NULL AND files_retry_at > ?
                """,
                (user_id, now),
            ).fetchone()
            blocked_courses = int(blocked_row["count"]) if blocked_row else 0

        return {
            "total": total,
            "downloaded": status_counts.get("downloaded", 0),
            "pending": status_counts.get("pending", 0),
            "ignored": status_counts.get("ignored", 0),
            "failed": status_counts.get("failed", 0),
            "bytes_downloaded": total_bytes,
            "blocked_courses": blocked_courses,
        }

    # ----------------------------- knowledge extraction & chunks -----------------------------

    def get_extracted_content(self, sha256: str) -> dict[str, Any] | None:
        with closing(self.db.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM extracted_content WHERE sha256 = ?",
                (sha256,),
            ).fetchone()
        return dict(row) if row else None

    def save_extracted_content(
        self,
        sha256: str,
        status: str,
        *,
        error_message: str | None = None,
        total_chunks: int = 0,
        total_characters: int = 0,
        empty_units_count: int = 0,
    ) -> None:
        now = _now_iso()
        with closing(self.db.connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO extracted_content (
                    sha256, status, error_message, total_chunks,
                    total_characters, empty_units_count, extracted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (sha256) DO UPDATE SET
                    status = excluded.status,
                    error_message = excluded.error_message,
                    total_chunks = excluded.total_chunks,
                    total_characters = excluded.total_characters,
                    empty_units_count = excluded.empty_units_count,
                    extracted_at = excluded.extracted_at
                """,
                (
                    sha256,
                    status,
                    error_message,
                    total_chunks,
                    total_characters,
                    empty_units_count,
                    now,
                ),
            )

    def replace_file_chunks(
        self,
        user_id: str,
        course_id: int,
        file_id: int,
        sha256: str,
        chunks: list[dict[str, Any]],
    ) -> None:
        now = _now_iso()
        rows = [
            (
                user_id,
                course_id,
                file_id,
                sha256,
                c["chunk_index"],
                c["location_type"],
                c["location_label"],
                c.get("location_start"),
                c.get("location_end"),
                c["text"],
                c["text_hash"],
                c["char_count"],
                c["token_estimate"],
                now,
            )
            for c in chunks
        ]
        with closing(self.db.connect()) as conn, conn:
            conn.execute(
                "DELETE FROM file_chunks WHERE user_id = ? AND file_id = ?",
                (user_id, file_id),
            )
            if rows:
                conn.executemany(
                    """
                    INSERT INTO file_chunks (
                        user_id, course_id, file_id, sha256, chunk_index, location_type,
                        location_label, location_start, location_end, text, text_hash,
                        char_count, token_estimate, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

    def delete_file_chunks(self, user_id: str, file_id: int) -> None:
        with closing(self.db.connect()) as conn, conn:
            conn.execute(
                "DELETE FROM file_chunks WHERE user_id = ? AND file_id = ?",
                (user_id, file_id),
            )

    def get_chunks_by_file(self, user_id: str, file_id: int) -> list[dict[str, Any]]:
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM file_chunks
                WHERE user_id = ? AND file_id = ?
                ORDER BY chunk_index ASC
                """,
                (user_id, file_id),
            ).fetchall()
        return _rows(rows)

    def get_chunks_by_sha256(self, sha256: str) -> list[dict[str, Any]]:
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM file_chunks
                WHERE sha256 = ?
                GROUP BY chunk_index
                ORDER BY chunk_index ASC
                """,
                (sha256,),
            ).fetchall()
        return _rows(rows)

    def copy_chunks_for_file(
        self,
        user_id: str,
        course_id: int,
        file_id: int,
        source_sha256: str,
    ) -> int:
        now = _now_iso()
        with closing(self.db.connect()) as conn, conn:
            conn.execute(
                "DELETE FROM file_chunks WHERE user_id = ? AND file_id = ?",
                (user_id, file_id),
            )
            cursor = conn.execute(
                """
                INSERT INTO file_chunks (
                    user_id, course_id, file_id, sha256, chunk_index, location_type,
                    location_label, location_start, location_end, text, text_hash,
                    char_count, token_estimate, created_at
                )
                SELECT
                    ?, ?, ?, sha256, chunk_index, location_type,
                    location_label, location_start, location_end, text, text_hash,
                    char_count, token_estimate, ?
                FROM (
                    SELECT * FROM file_chunks
                    WHERE sha256 = ?
                    GROUP BY chunk_index
                )
                ORDER BY chunk_index ASC
                """,
                (user_id, course_id, file_id, now, source_sha256),
            )
            return cursor.rowcount

    def get_downloaded_files_for_extraction(
        self,
        user_id: str,
        *,
        course_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = [
            "fs.user_id = ?",
            "fs.active = 1",
            "fs.status = 'downloaded'",
            "fs.sha256 IS NOT NULL",
            "c.active = 1",
        ]
        params: list[Any] = [user_id]
        if course_ids is not None:
            if not course_ids:
                return []
            placeholders = ",".join("?" for _ in course_ids)
            clauses.append(f"fs.course_id IN ({placeholders})")
            params.extend(course_ids)
        where = " AND ".join(clauses)

        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT
                    fs.*,
                    c.name AS course_name,
                    ec.status AS extraction_status,
                    ec.error_message AS extraction_error,
                    ec.total_chunks AS extraction_total_chunks,
                    ec.extracted_at AS extraction_at,
                    COUNT(fc.id) AS existing_chunks_count
                FROM file_sync_state fs
                JOIN courses c ON c.user_id = fs.user_id AND c.id = fs.course_id
                LEFT JOIN extracted_content ec ON ec.sha256 = fs.sha256
                LEFT JOIN file_chunks fc
                    ON fc.user_id = fs.user_id AND fc.file_id = fs.file_id
                WHERE {where}
                GROUP BY fs.file_id
                ORDER BY c.name COLLATE NOCASE, fs.file_id ASC
                """,
                params,
            ).fetchall()
        return _rows(rows)

    def reindex_fts(self) -> tuple[int, int]:
        """Reconstrói todo o índice FTS5 a partir de file_chunks."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("DELETE FROM chunks_fts")
            cursor = conn.execute(
                """
                INSERT INTO chunks_fts(rowid, filename, location_label, text)
                SELECT
                    fc.id,
                    COALESCE(fs.display_name, ''),
                    fc.location_label,
                    fc.text
                FROM file_chunks fc
                LEFT JOIN file_sync_state fs
                    ON fs.user_id = fc.user_id AND fs.file_id = fc.file_id
                """
            )
            total_chunks = conn.execute("SELECT count(*) FROM file_chunks").fetchone()[0]
            indexed_count = cursor.rowcount
            return indexed_count, total_chunks

    def fts_consistency_check(self) -> dict[str, int]:
        with closing(self.db.connect()) as conn:
            chunks_cnt = conn.execute("SELECT count(*) FROM file_chunks").fetchone()[0]
            fts_cnt = conn.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
            return {"chunks_count": chunks_cnt, "fts_count": fts_cnt}

    def get_chunk_by_id(self, chunk_id: int) -> dict[str, Any] | None:
        with closing(self.db.connect()) as conn:
            row = conn.execute(
                """
                SELECT
                    fc.*,
                    c.name AS course_name,
                    fs.display_name AS filename
                FROM file_chunks fc
                JOIN courses c ON c.user_id = fc.user_id AND c.id = fc.course_id
                JOIN file_sync_state fs ON fs.user_id = fc.user_id AND fs.file_id = fc.file_id
                WHERE fc.id = ?
                """,
                (chunk_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_neighbor_chunks(
        self,
        chunk_id: int,
        *,
        before: int = 1,
        after: int = 1,
    ) -> list[dict[str, Any]]:
        target = self.get_chunk_by_id(chunk_id)
        if not target:
            return []

        user_id = target["user_id"]
        file_id = target["file_id"]
        idx = target["chunk_index"]

        start_idx = max(1, idx - before)
        end_idx = idx + after

        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                """
                SELECT
                    fc.*,
                    c.name AS course_name,
                    fs.display_name AS filename
                FROM file_chunks fc
                JOIN courses c ON c.user_id = fc.user_id AND c.id = fc.course_id
                JOIN file_sync_state fs ON fs.user_id = fc.user_id AND fs.file_id = fc.file_id
                WHERE fc.user_id = ?
                  AND fc.file_id = ?
                  AND fc.chunk_index >= ?
                  AND fc.chunk_index <= ?
                ORDER BY fc.chunk_index ASC
                """,
                (user_id, file_id, start_idx, end_idx),
            ).fetchall()
            return _rows(rows)

    def knowledge_stats(self, user_id: str) -> dict[str, Any]:
        consistency = self.fts_consistency_check()
        with closing(self.db.connect()) as conn:
            status_rows = conn.execute(
                """
                SELECT
                    COALESCE(ec.status, 'pendente') AS ext_status,
                    COUNT(DISTINCT fs.file_id) AS file_count
                FROM file_sync_state fs
                JOIN courses c ON c.user_id = fs.user_id AND c.id = fs.course_id
                LEFT JOIN extracted_content ec ON ec.sha256 = fs.sha256
                WHERE fs.user_id = ? AND fs.active = 1 AND c.active = 1
                  AND fs.status = 'downloaded'
                GROUP BY ext_status
                """,
                (user_id,),
            ).fetchall()
            extraction_status_counts = {
                str(r["ext_status"]): int(r["file_count"]) for r in status_rows
            }

            # Chunks e caracteres por matéria
            course_rows = conn.execute(
                """
                SELECT
                    c.id AS course_id,
                    c.name AS course_name,
                    COUNT(DISTINCT fs.file_id) AS downloaded_files_count,
                    COUNT(fc.id) AS chunks_count,
                    COALESCE(SUM(fc.char_count), 0) AS total_chars
                FROM courses c
                JOIN file_sync_state fs
                    ON fs.user_id = c.user_id
                    AND fs.course_id = c.id
                    AND fs.active = 1
                    AND fs.status = 'downloaded'
                LEFT JOIN file_chunks fc
                    ON fc.user_id = c.user_id AND fc.file_id = fs.file_id
                WHERE c.user_id = ? AND c.active = 1
                GROUP BY c.id
                ORDER BY c.name COLLATE NOCASE
                """,
                (user_id,),
            ).fetchall()

            # Distribuição de caracteres dos chunks
            char_lengths = [
                int(row["char_count"])
                for row in conn.execute(
                    """
                    SELECT char_count FROM file_chunks
                    WHERE user_id = ?
                    ORDER BY char_count ASC
                    """,
                    (user_id,),
                ).fetchall()
            ]

        min_chars = char_lengths[0] if char_lengths else 0
        max_chars = char_lengths[-1] if char_lengths else 0
        if char_lengths:
            mid = len(char_lengths) // 2
            median_chars = (
                char_lengths[mid]
                if len(char_lengths) % 2 != 0
                else (char_lengths[mid - 1] + char_lengths[mid]) // 2
            )
        else:
            median_chars = 0

        total_chunks = len(char_lengths)
        avg_chars = sum(char_lengths) // total_chunks if total_chunks else 0

        courses_summary = [
            {
                "course_id": r["course_id"],
                "course_name": r["course_name"],
                "downloaded_files": r["downloaded_files_count"],
                "chunks": r["chunks_count"],
                "total_chars": r["total_chars"],
            }
            for r in course_rows
        ]

        return {
            "status_counts": extraction_status_counts,
            "courses": courses_summary,
            "total_chunks": total_chunks,
            "min_chars": min_chars,
            "median_chars": median_chars,
            "avg_chars": avg_chars,
            "max_chars": max_chars,
            "fts_consistency": consistency,
        }
