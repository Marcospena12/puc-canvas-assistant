from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
    user_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    name TEXT NOT NULL,
    course_code TEXT NOT NULL DEFAULT '',
    workflow_state TEXT NOT NULL DEFAULT '',
    term_id INTEGER,
    term_name TEXT,
    term_start_at TEXT,
    term_end_at TEXT,
    current_score REAL,
    current_grade TEXT,
    teachers_json TEXT NOT NULL DEFAULT '[]',
    tabs_json TEXT NOT NULL DEFAULT '[]',
    is_subject INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (user_id, id)
);

CREATE TABLE IF NOT EXISTS assignments (
    user_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    course_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    due_at TEXT,
    lock_at TEXT,
    unlock_at TEXT,
    points_possible REAL,
    submission_types_json TEXT NOT NULL DEFAULT '[]',
    description_html TEXT,
    rubric_json TEXT NOT NULL DEFAULT '[]',
    is_quiz INTEGER NOT NULL DEFAULT 0,
    html_url TEXT,
    published INTEGER NOT NULL DEFAULT 1,
    submission_state TEXT,
    submission_score REAL,
    submission_grade TEXT,
    submitted_at TEXT,
    graded_at TEXT,
    late INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    excused INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (user_id, id),
    FOREIGN KEY (user_id, course_id) REFERENCES courses (user_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_assignments_due
    ON assignments (user_id, course_id, due_at);

CREATE TABLE IF NOT EXISTS planner_items (
    user_id TEXT NOT NULL,
    plannable_type TEXT NOT NULL,
    plannable_id TEXT NOT NULL,
    course_id INTEGER,
    context_name TEXT,
    title TEXT,
    plannable_date TEXT,
    due_at TEXT,
    points_possible REAL,
    html_url TEXT,
    submitted INTEGER NOT NULL DEFAULT 0,
    graded INTEGER NOT NULL DEFAULT 0,
    late INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    new_activity INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (user_id, plannable_type, plannable_id)
);
CREATE INDEX IF NOT EXISTS idx_planner_date ON planner_items (user_id, plannable_date);

CREATE TABLE IF NOT EXISTS announcements (
    user_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    course_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    message_html TEXT,
    author TEXT,
    posted_at TEXT,
    url TEXT,
    read_state TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (user_id, id),
    FOREIGN KEY (user_id, course_id) REFERENCES courses (user_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_announcements_posted
    ON announcements (user_id, course_id, posted_at);

CREATE TABLE IF NOT EXISTS files (
    user_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    course_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    filename TEXT NOT NULL DEFAULT '',
    content_type TEXT,
    size INTEGER,
    created_at TEXT,
    updated_at TEXT,
    hidden INTEGER NOT NULL DEFAULT 0,
    locked INTEGER NOT NULL DEFAULT 0,
    url TEXT,
    mime_class TEXT,
    folder_id INTEGER,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (user_id, id),
    FOREIGN KEY (user_id, course_id) REFERENCES courses (user_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_files_course ON files (user_id, course_id, updated_at);

CREATE TABLE IF NOT EXISTS modules (
    user_id TEXT NOT NULL,
    course_id INTEGER NOT NULL,
    id INTEGER NOT NULL,
    name TEXT NOT NULL,
    position INTEGER,
    state TEXT,
    unlock_at TEXT,
    require_sequential_progress INTEGER NOT NULL DEFAULT 0,
    items_count INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (user_id, course_id, id),
    FOREIGN KEY (user_id, course_id) REFERENCES courses (user_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS module_items (
    user_id TEXT NOT NULL,
    course_id INTEGER NOT NULL,
    module_id INTEGER NOT NULL,
    id INTEGER NOT NULL,
    title TEXT NOT NULL,
    type TEXT NOT NULL,
    position INTEGER,
    html_url TEXT,
    url TEXT,
    external_url TEXT,
    page_url TEXT,
    content_id INTEGER,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (user_id, course_id, id),
    FOREIGN KEY (user_id, course_id, module_id)
        REFERENCES modules (user_id, course_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_module_items_type
    ON module_items (user_id, course_id, type);

CREATE TABLE IF NOT EXISTS sync_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    scope_id INTEGER NOT NULL DEFAULT 0,
    status_code INTEGER,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_issues_user
    ON sync_issues (user_id, created_at);

CREATE TABLE IF NOT EXISTS notification_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    event_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    resource_id TEXT,
    sent_at TEXT NOT NULL,
    UNIQUE (user_id, event_key)
);
CREATE INDEX IF NOT EXISTS idx_notification_events_user
    ON notification_events (user_id, kind, sent_at);

CREATE TABLE IF NOT EXISTS file_sync_state (
    user_id TEXT NOT NULL,
    file_id INTEGER NOT NULL,
    course_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    filename TEXT NOT NULL DEFAULT '',
    content_type TEXT,
    size INTEGER,
    modified_at TEXT,
    folder_id INTEGER,
    folder_name TEXT,
    locked INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    module_id INTEGER,
    module_name TEXT,
    source TEXT NOT NULL DEFAULT 'files',
    download_url TEXT,
    local_path TEXT,
    sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    skip_reason TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    next_retry_at TEXT,
    downloaded_at TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, file_id),
    FOREIGN KEY (user_id, course_id) REFERENCES courses (user_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_file_sync_course
    ON file_sync_state (user_id, course_id, active, status);

CREATE TABLE IF NOT EXISTS file_course_sync (
    user_id TEXT NOT NULL,
    course_id INTEGER NOT NULL,
    files_tab_status TEXT NOT NULL DEFAULT 'unknown',
    files_tab_error TEXT,
    files_retry_at TEXT,
    last_files_attempt_at TEXT,
    last_files_success_at TEXT,
    last_run_at TEXT,
    files_seen INTEGER NOT NULL DEFAULT 0,
    new_files INTEGER NOT NULL DEFAULT 0,
    downloaded INTEGER NOT NULL DEFAULT 0,
    unchanged INTEGER NOT NULL DEFAULT 0,
    ignored INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, course_id),
    FOREIGN KEY (user_id, course_id) REFERENCES courses (user_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS extracted_content (
    sha256 TEXT PRIMARY KEY,
    status TEXT NOT NULL, -- extraido, sem_texto, falha
    error_message TEXT,
    total_chunks INTEGER NOT NULL DEFAULT 0,
    total_characters INTEGER NOT NULL DEFAULT 0,
    empty_units_count INTEGER NOT NULL DEFAULT 0,
    extracted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    course_id INTEGER NOT NULL,
    file_id INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'arquivo',
    source_ref TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    chunk_index INTEGER NOT NULL,
    location_type TEXT NOT NULL, -- pagina, slide, secao, etc.
    location_label TEXT NOT NULL, -- ex.: 'Página 3', 'Slides 4-6', 'Seção 1'
    location_start INTEGER,
    location_end INTEGER,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    token_estimate INTEGER NOT NULL,
    due_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id, course_id) REFERENCES courses (user_id, id) ON DELETE CASCADE,
    UNIQUE (user_id, course_id, source_type, source_ref, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_file_chunks_lookup
    ON file_chunks (user_id, course_id, source_type, source_ref);
CREATE INDEX IF NOT EXISTS idx_file_chunks_sha256
    ON file_chunks (sha256);
CREATE INDEX IF NOT EXISTS idx_file_chunks_type
    ON file_chunks (user_id, course_id, source_type);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    title,
    location_label,
    text,
    source_type UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON file_chunks BEGIN
    INSERT INTO chunks_fts(rowid, title, location_label, text, source_type)
    VALUES (new.id, new.title, new.location_label, new.text, new.source_type);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON file_chunks BEGIN
    DELETE FROM chunks_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON file_chunks BEGIN
    DELETE FROM chunks_fts WHERE rowid = old.id;
    INSERT INTO chunks_fts(rowid, title, location_label, text, source_type)
    VALUES (new.id, new.title, new.location_label, new.text, new.source_type);
END;

CREATE TABLE IF NOT EXISTS canvas_content_state (
    user_id TEXT NOT NULL,
    course_id INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    title TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    canvas_file_ids_json TEXT NOT NULL DEFAULT '[]',
    due_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (user_id, course_id, source_type, source_ref),
    FOREIGN KEY (user_id, course_id) REFERENCES courses (user_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_canvas_content_lookup
    ON canvas_content_state (user_id, course_id, source_type);

CREATE TABLE IF NOT EXISTS content_sync_state (
    user_id TEXT NOT NULL,
    course_id INTEGER NOT NULL,
    resource TEXT NOT NULL, -- pagina, tarefa, aviso, ementa, quiz
    status TEXT NOT NULL DEFAULT 'unknown',
    error TEXT,
    retry_at TEXT,
    last_attempt_at TEXT,
    last_success_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, course_id, resource),
    FOREIGN KEY (user_id, course_id) REFERENCES courses (user_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS briefing_cache (
    cache_key TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    assignment_id INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    briefing_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS study_cache (
    cache_key TEXT PRIMARY KEY,
    file_sha256 TEXT NOT NULL,
    location_range TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS study_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    command TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    context_blocks INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_study_usage_user_created ON study_usage (user_id, created_at);

CREATE TABLE IF NOT EXISTS review_cache (
    cache_key TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    assignment_id INTEGER NOT NULL,
    draft_hash TEXT NOT NULL,
    rubric_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    feedback_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_cache_user ON review_cache (user_id, created_at);
"""

SCHEMA_VERSION = 8


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        files_cols = {row["name"] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
        if files_cols and "folder_id" not in files_cols:
            conn.execute("ALTER TABLE files ADD COLUMN folder_id INTEGER")

        ext_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(extracted_content)").fetchall()
        }
        if ext_cols and "empty_units_count" not in ext_cols:
            conn.execute(
                "ALTER TABLE extracted_content "
                "ADD COLUMN empty_units_count INTEGER NOT NULL DEFAULT 0"
            )

        assignment_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(assignments)").fetchall()
        }
        if assignment_cols and "description_html" not in assignment_cols:
            conn.execute("ALTER TABLE assignments ADD COLUMN description_html TEXT")
        if assignment_cols and "rubric_json" not in assignment_cols:
            conn.execute(
                "ALTER TABLE assignments ADD COLUMN rubric_json TEXT NOT NULL DEFAULT '[]'"
            )

        # Migração v5 -> v6: generalização de chunks para qualquer tipo de conteúdo
        chunk_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(file_chunks)").fetchall()
        }
        chunk_indices = conn.execute("PRAGMA index_list(file_chunks)").fetchall()
        has_course_in_unique = False
        for idx_row in chunk_indices:
            if idx_row["unique"]:
                cols = [
                    c["name"]
                    for c in conn.execute(f"PRAGMA index_info({idx_row['name']})").fetchall()
                ]
                if "course_id" in cols and "source_type" in cols:
                    has_course_in_unique = True
                    break

        if chunk_cols and (not has_course_in_unique or "source_type" not in chunk_cols):
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS file_chunks_v6 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    course_id INTEGER NOT NULL,
                    file_id INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT NOT NULL DEFAULT '',
                    source_type TEXT NOT NULL DEFAULT 'arquivo',
                    source_ref TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    chunk_index INTEGER NOT NULL,
                    location_type TEXT NOT NULL,
                    location_label TEXT NOT NULL,
                    location_start INTEGER,
                    location_end INTEGER,
                    text TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    char_count INTEGER NOT NULL,
                    token_estimate INTEGER NOT NULL,
                    due_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id, course_id)
                        REFERENCES courses (user_id, id) ON DELETE CASCADE,
                    UNIQUE (user_id, course_id, source_type, source_ref, chunk_index)
                )
                """
            )
            if "source_type" in chunk_cols:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO file_chunks_v6 (
                        id, user_id, course_id, file_id, sha256, source_type, source_ref, title,
                        chunk_index, location_type, location_label, location_start, location_end,
                        text, text_hash, char_count, token_estimate, due_at, created_at
                    )
                    SELECT
                        id, user_id, course_id, file_id, sha256, source_type, source_ref, title,
                        chunk_index, location_type, location_label, location_start, location_end,
                        text, text_hash, char_count, token_estimate, due_at, created_at
                    FROM file_chunks
                    """
                )
            else:
                conn.execute(
                    """
                    INSERT INTO file_chunks_v6 (
                        id, user_id, course_id, file_id, sha256, source_type, source_ref, title,
                        chunk_index, location_type, location_label, location_start, location_end,
                        text, text_hash, char_count, token_estimate, due_at, created_at
                    )
                    SELECT
                        fc.id, fc.user_id, fc.course_id, fc.file_id, fc.sha256,
                        'arquivo', CAST(fc.file_id AS TEXT),
                        COALESCE(fs.display_name, ''),
                        fc.chunk_index, fc.location_type, fc.location_label,
                        fc.location_start, fc.location_end,
                        fc.text, fc.text_hash, fc.char_count, fc.token_estimate, NULL, fc.created_at
                    FROM file_chunks fc
                    LEFT JOIN file_sync_state fs
                        ON fs.user_id = fc.user_id AND fs.file_id = fc.file_id
                    """
                )
            conn.execute("DROP TRIGGER IF EXISTS chunks_ai")
            conn.execute("DROP TRIGGER IF EXISTS chunks_ad")
            conn.execute("DROP TRIGGER IF EXISTS chunks_au")
            conn.execute("DROP TABLE IF EXISTS chunks_fts")
            conn.execute("DROP TABLE file_chunks")
            conn.execute("ALTER TABLE file_chunks_v6 RENAME TO file_chunks")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_file_chunks_lookup "
                "ON file_chunks (user_id, course_id, source_type, source_ref)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_file_chunks_sha256 ON file_chunks (sha256)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_file_chunks_type "
                "ON file_chunks (user_id, course_id, source_type)"
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    title,
                    location_label,
                    text,
                    source_type UNINDEXED,
                    tokenize='unicode61 remove_diacritics 2'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO chunks_fts(rowid, title, location_label, text, source_type)
                SELECT id, title, location_label, text, source_type FROM file_chunks
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON file_chunks BEGIN
                    INSERT INTO chunks_fts(rowid, title, location_label, text, source_type)
                    VALUES (new.id, new.title, new.location_label, new.text, new.source_type);
                END;
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON file_chunks BEGIN
                    DELETE FROM chunks_fts WHERE rowid = old.id;
                END;
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON file_chunks BEGIN
                    DELETE FROM chunks_fts WHERE rowid = old.id;
                    INSERT INTO chunks_fts(rowid, title, location_label, text, source_type)
                    VALUES (new.id, new.title, new.location_label, new.text, new.source_type);
                END;
                """
            )
            conn.execute("PRAGMA foreign_keys = ON")

        # Garante população do FTS5 caso já existam chunks sem indexação
        fts_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
        ).fetchone()
        if fts_table and chunk_cols:
            fts_count = conn.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
            chunks_count = conn.execute("SELECT count(*) FROM file_chunks").fetchone()[0]
            if chunks_count > 0 and fts_count == 0:
                conn.execute(
                    """
                    INSERT INTO chunks_fts(rowid, title, location_label, text, source_type)
                    SELECT id, title, location_label, text, source_type FROM file_chunks
                    """
                )

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            self._migrate(conn)
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('version', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
