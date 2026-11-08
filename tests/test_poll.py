"""GW-2 / GW-4：收消息重放、原子落库、游标持久化与 token 刷新。"""

from contextlib import ExitStack
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wechat_gateway import api, inbox, media, poll, storage


class StopPolling(BaseException):
    """结束测试循环，不被生产代码的 Exception 重试路径捕获。"""


class PollTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="wechat-poll-")))
        self.stack.enter_context(patch.object(storage, "DB_PATH", str(self.root / "test.db")))
        self.stack.enter_context(patch.object(storage, "SYNC_FILE", str(self.root / "cursor.json")))
        self.stack.enter_context(patch.object(storage, "_local", threading.local()))
        self.stack.enter_context(patch.object(poll, "acquire_single_instance_lock", return_value=True))
        self.stack.enter_context(patch.object(poll.threading, "Thread"))
        self.stack.enter_context(patch.object(poll.time, "sleep"))
        self.stack.enter_context(patch.object(poll, "load_token", return_value="fake-token"))
        self.conn = storage.db()
        self.addCleanup(self.conn.close)
        storage.save_updates_buf("old-cursor")

    def message(self, mid="m1"):
        return {"message_id": mid, "from_user_id": "chat-A", "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": mid}}]}

    def run_rounds(self, responses):
        requests = []
        def api_post(endpoint, body, token, timeout):
            requests.append((body["get_updates_buf"], token, body["token"]))
            if len(requests) > len(responses):
                raise StopPolling()
            return responses[len(requests) - 1]
        with patch.object(poll, "api_post", side_effect=api_post):
            with self.assertRaises(StopPolling):
                poll.poll_loop()
        return requests

    def batch(self, messages=None):
        return {"ret": 0, "msgs": [self.message()] if messages is None else messages,
                "get_updates_buf": "next-cursor"}

    def test_handler_failure_replays_batch_without_duplicate_history(self):
        fail_once = [True]
        def handler(msg):
            if msg["message_id"] == "m1" and fail_once[0]:
                fail_once[0] = False
                raise sqlite3.OperationalError("simulated database write failure")
            inbox.on_wechat_message(msg)
        batch = self.batch([self.message("m1"), self.message("m2")])
        with patch.object(poll, "on_wechat_message", side_effect=handler):
            requests = self.run_rounds([batch, batch])
        self.assertEqual([request[0] for request in requests], ["old-cursor", "old-cursor", "next-cursor"])
        self.assertEqual(storage.get_updates_buf(), "next-cursor")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0], 2)

    def test_persistent_bad_message_keeps_cursor_and_processes_other_messages(self):
        bad = {"from_user_id": 123, "item_list": [{"type": 1}]}
        requests = self.run_rounds([self.batch([bad, self.message()])] * 2)
        self.assertEqual([request[0] for request in requests], ["old-cursor"] * 3)
        self.assertEqual(storage.get_updates_buf(), "old-cursor")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0], 1)

    def test_cursor_save_failure_keeps_in_memory_cursor_for_retry(self):
        real_save = storage.save_updates_buf
        saves = []
        def save(buf):
            saves.append(buf)
            if len(saves) == 1:
                raise OSError("simulated full disk")
            real_save(buf)
        with patch.object(poll, "save_updates_buf", side_effect=save):
            requests = self.run_rounds([self.batch(), self.batch()])
        self.assertEqual([request[0] for request in requests], ["old-cursor", "old-cursor", "next-cursor"])
        self.assertEqual(storage.get_updates_buf(), "next-cursor")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0], 1)

    def test_atomic_cursor_replacement_failure_preserves_old_file(self):
        with patch.object(storage.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                storage.save_updates_buf("new-cursor")
        self.assertEqual(storage.get_updates_buf(), "old-cursor")
        self.assertEqual(list(self.root.glob(".sync-*.tmp")), [])

    def test_inbox_and_history_commit_together_and_failure_is_retriable(self):
        self.conn.execute("CREATE TRIGGER fail_history BEFORE INSERT ON history "
                          "BEGIN SELECT RAISE(ABORT, 'simulated history failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            inbox.on_wechat_message(self.message())
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 0)
        self.conn.execute("DROP TRIGGER fail_history")
        inbox.on_wechat_message(self.message())
        inbox.on_wechat_message(self.message())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0], 1)

    def test_media_download_failure_still_stores_failure_placeholder(self):
        msg = self.message()
        msg["item_list"] = [{"type": 2, "image_item": {}}]
        with patch.object(media, "download_media", side_effect=OSError("download failed")):
            self.run_rounds([self.batch([msg])])
        text = self.conn.execute("SELECT content FROM inbox").fetchone()[0]
        self.assertIn("图片接收失败", text)
        self.assertEqual(storage.get_updates_buf(), "next-cursor")

    def test_same_account_token_refresh_is_used_on_next_request(self):
        account = self.root / "test-account.json"
        account.write_text(json.dumps({"token": "old-token"}))
        requests = []
        def api_post(endpoint, body, token, timeout):
            requests.append((token, body["token"]))
            if len(requests) == 1:
                account.write_text(json.dumps({"token": "new-token"}))
                return {"ret": -1}
            raise StopPolling()
        with patch.object(api, "STATE_DIR", str(self.root)), \
             patch.object(api, "ACCOUNT_ID", "test-account"), \
             patch.object(poll, "load_token", side_effect=api.load_token), \
             patch.object(poll, "api_post", side_effect=api_post):
            with self.assertRaises(StopPolling):
                poll.poll_loop()
        self.assertEqual(requests, [("old-token", "old-token"), ("new-token", "new-token")])
        self.assertEqual(storage.get_updates_buf(), "old-cursor")

    def test_temporary_credential_read_error_is_retried(self):
        with patch.object(poll, "load_token", side_effect=[FileNotFoundError("missing"), "new-token", "new-token"]):
            requests = self.run_rounds([self.batch([])])
        self.assertEqual(requests, [("old-cursor", "new-token", "new-token"),
                                    ("next-cursor", "new-token", "new-token")])
