"""Persistent turn identity regression tests using isolated temporary SQLite files."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config as config_module
from backend.config import Config
from backend.session import ConversationSession, TurnGuard
from backend.storage import Storage


def _configure(tmp_path: Path) -> None:
    cfg = Config()
    cfg._raw = {
        "app": {"host": "127.0.0.1", "port": 7870},
        "asr": {"model": "dummy", "ws_url": "", "format": "pcm", "sample_rate": 16000},
        "llm": {"model": "dummy", "base_url": "", "max_context_turns": 20},
        "tts": {"pcm_queue_limit": 10},
        "audio": {
            "sample_rate": 16000,
            "channels": 1,
            "frame_duration_ms": 20,
            "rms_threshold": 0.1,
            "rms_consecutive_frames": 2,
            "prebuffer_ms": 500,
        },
        "livetalking": {"http_url": "http://127.0.0.1:8010"},
        "storage": {"db_path": str(tmp_path / "turns.db"), "max_history_turns": 20},
        "logging": {"directory": str(tmp_path / "logs")},
    }
    cfg._secrets = {}
    config_module._config = cfg


def _rows(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT id, turn_id, status, user_text, ai_text, error "
            "FROM turns ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


class TestPersistentTurnIdentity(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        _configure(self.tmp)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_restart_collision_updates_only_new_row(self) -> None:
        first = Storage(self.tmp / "turns.db")
        row_a = first.save_turn(1, "completed", "A-user", "A-ai", completed=True)
        row_b = first.save_turn(1, "completed", "B-user", "B-ai", completed=True)
        first.close()

        restarted = Storage(self.tmp / "turns.db")
        row_c = restarted.save_turn(1, "active")
        self.assertNotIn(row_c, {row_a, row_b})
        restarted.complete_turn(row_c, "C-user", "C-ai")
        restarted.close()

        rows = _rows(self.tmp / "turns.db")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0], row_a)
        self.assertEqual(rows[0][1:5], (1, "completed", "A-user", "A-ai"))
        self.assertEqual(rows[1][0], row_b)
        self.assertEqual(rows[1][1:5], (1, "completed", "B-user", "B-ai"))
        self.assertEqual(rows[2][0], row_c)
        self.assertEqual(rows[2][1:5], (1, "completed", "C-user", "C-ai"))

    def test_complete_isolation_with_colliding_runtime_ids(self) -> None:
        storage = Storage(self.tmp / "turns.db")
        old_row = storage.save_turn(7, "active", "old-user")
        new_row = storage.save_turn(7, "active", "new-user")
        storage.complete_turn(new_row, "new-user", "new-ai")
        storage.close()

        rows = _rows(self.tmp / "turns.db")
        self.assertEqual(rows[0][0], old_row)
        self.assertEqual(rows[0][2:5], ("active", "old-user", ""))
        self.assertEqual(rows[1][0], new_row)
        self.assertEqual(rows[1][2:5], ("completed", "new-user", "new-ai"))

    def test_failed_status_isolation_with_colliding_runtime_ids(self) -> None:
        storage = Storage(self.tmp / "turns.db")
        old_row = storage.save_turn(3, "active", "old-user")
        new_row = storage.save_turn(3, "active", "new-user")
        storage.update_turn_status(new_row, "failed", "new-failure")
        storage.close()

        rows = _rows(self.tmp / "turns.db")
        self.assertEqual(rows[0][0], old_row)
        self.assertEqual(rows[0][2], "active")
        self.assertEqual(rows[0][5], "")
        self.assertEqual(rows[1][0], new_row)
        self.assertEqual(rows[1][2], "failed")
        self.assertEqual(rows[1][5], "new-failure")

    def test_completed_context_is_not_batch_mutated(self) -> None:
        storage = Storage(self.tmp / "turns.db")
        storage.save_turn(1, "completed", "old-user", "old-ai", completed=True)
        storage.save_turn(1, "completed", "middle-user", "middle-ai", completed=True)
        current = storage.save_turn(1, "active", "new-user")
        storage.complete_turn(current, "new-user", "new-ai")
        history = storage.get_completed_turns()
        storage.close()

        self.assertCountEqual(
            sorted((item["user"], item["assistant"]) for item in history),
            [("old-user", "old-ai"), ("middle-user", "middle-ai"), ("new-user", "new-ai")],
        )
        rows = _rows(self.tmp / "turns.db")
        self.assertEqual([row[3:5] for row in rows], [
            ("old-user", "old-ai"),
            ("middle-user", "middle-ai"),
            ("new-user", "new-ai"),
        ])

    def test_two_independent_storages_isolate_same_runtime_id(self) -> None:
        storage_a = Storage(self.tmp / "turns.db")
        storage_b = Storage(self.tmp / "turns.db")
        try:
            row_a = storage_a.save_turn(1, "active", "session-a")
            row_b = storage_b.save_turn(1, "active", "session-b")
            self.assertNotEqual(row_a, row_b)
            storage_b.complete_turn(row_b, "session-b", "reply-b")
            rows = _rows(self.tmp / "turns.db")
            by_id = {row[0]: row for row in rows}
            self.assertEqual(by_id[row_a][2:5], ("active", "session-a", ""))
            self.assertEqual(by_id[row_b][2:5], ("completed", "session-b", "reply-b"))
        finally:
            storage_a.close()
            storage_b.close()

    def test_missing_row_raises_and_does_not_commit(self) -> None:
        storage = Storage(self.tmp / "turns.db")
        try:
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                storage.complete_turn(999999, "secret-user", "secret-ai")
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                storage.update_turn_status(999999, "failed", "secret-error")
            self.assertEqual(_rows(self.tmp / "turns.db"), [])
        finally:
            storage.close()


class TestSessionCancellationIdentity(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        _configure(self.tmp)
        self.session = ConversationSession()

    async def asyncTearDown(self) -> None:
        await self.session.shutdown()
        self.tmp_dir.cleanup()

    async def _bind_active_turn(self) -> tuple[TurnGuard, int]:
        old_row = self.session._storage.save_turn(1, "completed", "old", "old-ai", completed=True)
        turn = TurnGuard(self.session._next_turn_id())
        turn.storage_id = self.session._storage.save_turn(turn.turn_id, "active", "current")
        self.session._current_turn = turn
        self.session._state = "listening"
        return turn, old_row

    async def test_pause_cancels_only_bound_persistent_row(self) -> None:
        turn, old_row = await self._bind_active_turn()
        await self.session.pause()
        rows = {row[0]: row for row in _rows(self.tmp / "turns.db")}
        self.assertEqual(rows[old_row][2:5], ("completed", "old", "old-ai"))
        self.assertEqual(rows[turn.storage_id][2], "interrupted")
        self.assertEqual(rows[turn.storage_id][5], "用户暂停")

    async def test_interrupt_cancels_only_bound_persistent_row(self) -> None:
        turn, old_row = await self._bind_active_turn()
        await self.session.interrupt()
        rows = {row[0]: row for row in _rows(self.tmp / "turns.db")}
        self.assertEqual(rows[old_row][2:5], ("completed", "old", "old-ai"))
        self.assertEqual(rows[turn.storage_id][2], "interrupted")
        self.assertEqual(rows[turn.storage_id][5], "用户打断")


if __name__ == "__main__":
    unittest.main()
