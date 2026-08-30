from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from backend.config import get_config
from backend.memory import parse_shared_event


class Storage:
    def __init__(self, db_path: str | None = None) -> None:
        cfg = get_config()
        self.db_path = Path(db_path or cfg.storage.get("db_path", "data/conversations.db"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._local.conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                user_text TEXT,
                ai_text TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                completed_at REAL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_turns_status ON turns(status)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL CHECK(scope IN ('user', 'shared')),
                memory_key TEXT NOT NULL,
                subject TEXT NOT NULL,
                value TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active', 'superseded', 'deleted')),
                source_turn_id INTEGER,
                source_kind TEXT NOT NULL DEFAULT 'user_explicit',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                superseded_by INTEGER REFERENCES memories(id),
                FOREIGN KEY(source_turn_id) REFERENCES turns(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_active_key
            ON memories(scope, memory_key) WHERE status = 'active'
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_source_turn ON memories(source_turn_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status)
            """
        )
        conn.commit()
        self._migrate_legacy_shared_memories(conn)

    def _migrate_legacy_shared_memories(self, conn: sqlite3.Connection) -> None:
        """Normalize only legacy active shared rows that match the current event grammar."""
        rows = conn.execute(
            """
            SELECT id, memory_key, subject, value, updated_at
            FROM memories
            WHERE scope = 'shared' AND status = 'active' AND subject = 'shared_experience'
            ORDER BY id
            """
        ).fetchall()
        if not rows:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                memory_id, _, _, value, updated_at = row
                parsed = parse_shared_event(value)
                if parsed is None:
                    continue
                memory_key, subject = parsed
                conflict = conn.execute(
                    """
                    SELECT id, updated_at FROM memories
                    WHERE scope = 'shared' AND memory_key = ? AND status = 'active' AND id != ?
                    """,
                    (memory_key, memory_id),
                ).fetchone()
                if conflict is None:
                    conn.execute(
                        "UPDATE memories SET memory_key = ?, subject = ? WHERE id = ?",
                        (memory_key, subject, memory_id),
                    )
                    continue

                conflict_id, conflict_updated_at = conflict
                legacy_is_newer = (float(updated_at), int(memory_id)) >= (
                    float(conflict_updated_at), int(conflict_id)
                )
                if legacy_is_newer:
                    conn.execute(
                        """
                        UPDATE memories
                        SET status = 'superseded', superseded_by = ?
                        WHERE id = ? AND status = 'active'
                        """,
                        (memory_id, conflict_id),
                    )
                    conn.execute(
                        "UPDATE memories SET memory_key = ?, subject = ? WHERE id = ?",
                        (memory_key, subject, memory_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE memories
                        SET status = 'superseded', superseded_by = ?,
                            memory_key = ?, subject = ?
                        WHERE id = ? AND status = 'active'
                        """,
                        (conflict_id, memory_key, subject, memory_id),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def save_turn(self, turn_id: int, status: str, user_text: str | None = None,
                  ai_text: str | None = None, error: str | None = None,
                  completed: bool = False) -> int:
        conn = self._conn()
        now = time.time()
        completed_at = now if completed else None
        cursor = conn.execute(
            """
            INSERT INTO turns (turn_id, status, user_text, ai_text, error, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (turn_id, status, user_text or "", ai_text or "", error or "", now, completed_at),
        )
        persistent_id = cursor.lastrowid
        if persistent_id is None:
            conn.rollback()
            raise RuntimeError("save_turn did not return a persistent row id")
        conn.commit()
        return int(persistent_id)

    def complete_turn(self, turn_row_id: int, user_text: str, ai_text: str) -> None:
        conn = self._conn()
        now = time.time()
        cursor = conn.execute(
            """
            UPDATE turns
            SET status = 'completed', user_text = ?, ai_text = ?, completed_at = ?
            WHERE id = ?
            """,
            (user_text, ai_text, now, turn_row_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise RuntimeError("complete_turn expected exactly one persistent row")
        conn.commit()

    def update_turn_status(
        self, turn_row_id: int, status: str, error: str | None = None
    ) -> None:
        conn = self._conn()
        now = time.time()
        completed_at = now if status == "completed" else None
        cursor = conn.execute(
            """
            UPDATE turns
            SET status = ?, error = ?, completed_at = ?
            WHERE id = ?
            """,
            (status, error or "", completed_at, turn_row_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise RuntimeError("update_turn_status expected exactly one persistent row")
        conn.commit()

    def get_completed_turns(self, limit: int | None = None) -> list[dict[str, Any]]:
        cfg = get_config()
        limit = limit or cfg.storage.get("max_history_turns", 20)
        conn = self._conn()
        cur = conn.execute(
            """
            SELECT turn_id, user_text, ai_text, completed_at
            FROM turns
            WHERE status = 'completed' AND user_text IS NOT NULL AND ai_text IS NOT NULL
            ORDER BY completed_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = [
            {
                "turn_id": r[0],
                "user": r[1],
                "assistant": r[2],
                "completed_at": r[3],
            }
            for r in cur.fetchall()
        ]
        rows.reverse()
        return rows

    def get_recent_turns(self, limit: int | None = None) -> list[dict[str, Any]]:
        cfg = get_config()
        limit = limit or cfg.storage.get("max_history_turns", 20)
        conn = self._conn()
        cur = conn.execute(
            """
            SELECT turn_id, status, user_text, ai_text, error, created_at, completed_at
            FROM turns
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for r in cur.fetchall():
            rows.append({
                "turn_id": r[0],
                "status": r[1],
                "user": r[2],
                "assistant": r[3],
                "error": r[4],
                "created_at": r[5],
                "completed_at": r[6],
            })
        return rows

    def clear_history(self) -> int:
        conn = self._conn()
        cur = conn.execute("DELETE FROM turns")
        conn.commit()
        return cur.rowcount

    def upsert_memory(
        self,
        scope: str,
        memory_key: str,
        subject: str,
        value: str,
        source_turn_id: int,
        source_kind: str = "user_explicit",
    ) -> dict[str, Any]:
        """Insert an active memory version, superseding the previous active value."""
        if scope not in {"user", "shared"}:
            raise ValueError("invalid memory scope")
        if source_kind != "user_explicit":
            raise ValueError("memory source must be user_explicit")
        if not memory_key or not subject or not value:
            raise ValueError("memory fields must be non-empty")
        conn = self._conn()
        now = time.time()
        conn.execute("BEGIN IMMEDIATE")
        try:
            old = conn.execute(
                """
                SELECT id, value FROM memories
                WHERE scope = ? AND memory_key = ? AND status = 'active'
                """,
                (scope, memory_key),
            ).fetchone()
            if old is not None and old[1] == value:
                conn.commit()
                return {"action": "noop", "id": int(old[0]), "replaced_id": None}
            if old is not None:
                conn.execute(
                    """
                    UPDATE memories SET status = 'superseded', updated_at = ?
                    WHERE id = ? AND status = 'active'
                    """,
                    (now, old[0]),
                )
            cur = conn.execute(
                """
                INSERT INTO memories
                    (scope, memory_key, subject, value, status, source_turn_id,
                     source_kind, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (scope, memory_key, subject, value, source_turn_id, source_kind, now, now),
            )
            new_id = int(cur.lastrowid)
            if old is not None:
                conn.execute(
                    "UPDATE memories SET superseded_by = ? WHERE id = ?",
                    (new_id, old[0]),
                )
            conn.commit()
            return {
                "action": "updated" if old is not None else "inserted",
                "id": new_id,
                "replaced_id": int(old[0]) if old is not None else None,
            }
        except Exception:
            conn.rollback()
            raise

    def delete_memory(
        self,
        scope: str,
        memory_key: str,
        source_turn_id: int,
    ) -> dict[str, Any]:
        """Mark the current active version deleted without destroying audit history."""
        conn = self._conn()
        now = time.time()
        with conn:
            cur = conn.execute(
                """
                UPDATE memories SET status = 'deleted', source_turn_id = ?, updated_at = ?
                WHERE scope = ? AND memory_key = ? AND status = 'active'
                """,
                (source_turn_id, now, scope, memory_key),
            )
            return {"action": "deleted" if cur.rowcount else "noop", "count": cur.rowcount}

    def get_active_memories(self, limit: int = 100) -> list[dict[str, Any]]:
        conn = self._conn()
        rows = conn.execute(
            """
            SELECT id, scope, memory_key, subject, value, source_turn_id,
                   source_kind, created_at, updated_at
            FROM memories WHERE status = 'active'
            ORDER BY updated_at DESC, id DESC LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return [
            {
                "id": row[0], "scope": row[1], "memory_key": row[2],
                "subject": row[3], "value": row[4], "source_turn_id": row[5],
                "source_kind": row[6], "created_at": row[7], "updated_at": row[8],
            }
            for row in rows
        ]

    def count_active_memories(self) -> int:
        return int(self._conn().execute(
            "SELECT COUNT(*) FROM memories WHERE status = 'active'"
        ).fetchone()[0])

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
