"""模块拆分后的离线流程验证，不调用真实网络。"""

from contextlib import ExitStack
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from muse_wechat import claims, files, inbox, login, mail, messages, reminders, storage


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="wechat-test-")))
        self.stack.enter_context(patch.object(storage, "DB_PATH", str(self.root / "test.db")))
        self.stack.enter_context(patch.object(storage, "_local", threading.local()))
        self.stack.enter_context(patch.object(claims, "WAIT_SESSION_FILE", str(self.root / "wait.json")))
        self.stack.enter_context(patch.object(reminders, "REMINDERS_DIR", str(self.root / "reminders")))
        self.conn = storage.db()
        self.addCleanup(self.conn.close)

    def seed(self, mid="m1", chat="chat-A"):
        self.conn.execute(
            "INSERT INTO inbox(msg_id,chat_id,content,created_at,ctx_token) VALUES(?,?,?,?,?)",
            (mid, chat, "你好", time.time(), "fake-context"),
        )

    def test_all_modules_import(self):
        for path in (ROOT / "src/muse_wechat").glob("*.py"):
            if not path.name.startswith("__"):
                importlib.import_module(f"muse_wechat.{path.stem}")

    def test_inbound_deduplicates_then_reply_finishes_batch(self):
        msg = {"message_id": "inbound-1", "from_user_id": "chat-A", "context_token": "ctx",
               "item_list": [{"type": 1, "text_item": {"text": "你好"}}]}
        with patch.object(inbox.threading, "Thread"):
            inbox.on_wechat_message(msg)
            inbox.on_wechat_message(msg)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 1)
        batch = claims.claim_batch()
        self.assertEqual(batch["messages"], ["你好"])
        self.assertEqual(claims.heartbeat(batch["claim_id"]), "ok")
        self.assertEqual(claims.hold(batch["claim_id"], 300), "ok")
        with patch.object(messages, "_post_text") as send:
            self.assertEqual(messages.say(batch["claim_id"], "收到"), "ok")
            self.assertEqual(messages.send_reply(batch["reply_to"], "chat-A", "你好呀"), "ok")
            self.assertEqual(messages.send_reply(batch["reply_to"], "chat-A", "你好呀"), "already_replied")
            self.assertEqual(send.call_count, 2)
        self.assertEqual(claims.heartbeat(batch["claim_id"]), "done")
        self.assertIsNone(claims.claim_batch())

    def test_stale_claim_is_reclaimed(self):
        self.seed()
        old = claims.claim_batch()
        self.conn.execute("UPDATE inbox SET claimed_at=?", (time.time() - claims.STALE_SECS - 1,))
        new = claims.claim_batch()
        self.assertNotEqual(old["claim_id"], new["claim_id"])
        self.assertEqual(claims.heartbeat(old["claim_id"]), "superseded")
        self.assertIn("reclaim", claims.list_events())

    def test_wait_claim_timeout_uses_injected_clock(self):
        clock = [100.0]
        def advance(seconds):
            clock[0] += seconds
        result = claims.wait_claim("segment", 2, claim_fn=lambda: None,
                                   now_fn=lambda: clock[0], sleep_fn=advance)
        self.assertEqual(result, {"messages": [], "timeout": True})
        self.assertFalse((self.root / "wait.json").exists())

    def test_reminder_dispatch_sends_once(self):
        self.seed()
        spec = reminders.remind_create("chat-A", "喝水啦", delay_seconds=60)
        with patch.object(reminders, "_post_text") as send:
            result = reminders.dispatch_reminders(now_fn=lambda: spec["due_epoch"] + 1)
            self.assertEqual(result["sent"], 1)
            self.assertEqual(reminders.dispatch_reminders()["sent"], 0)
            send.assert_called_once_with("chat-A", "喝水啦")
        done = self.root / "reminders/done" / (spec["id"] + ".json")
        self.assertTrue(done.exists())

    def test_file_send_preserves_deduplication(self):
        self.seed()
        batch = claims.claim_batch()
        path = self.root / "report.txt"
        path.write_text("report")
        with patch.object(files, "upload_media", return_value=({}, 6, 16, "fake-md5")), \
             patch.object(files, "_send_media_item", return_value="ok") as send:
            self.assertEqual(files.send_file(batch["claim_id"], str(path)), "ok")
            self.assertEqual(files.send_file(batch["claim_id"], str(path)), "already_sent")
            send.assert_called_once()

    def test_mail_adapter_preserves_deduplication(self):
        self.seed()
        batch = claims.claim_batch()
        with patch.object(mail, "MAIL_ALLOWED", {"test@example.com"}), \
             patch.object(mail, "_run_mail_cli", return_value=types.SimpleNamespace(returncode=0)) as send:
            self.assertEqual(mail.send_mail(batch["claim_id"], "test@example.com", "标题", "正文"), "ok")
            self.assertEqual(mail.send_mail(batch["claim_id"], "test@example.com", "标题", "正文"), "already_sent")
            send.assert_called_once()

    def test_login_saves_credentials_and_qr_under_configured_directory(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(login, "STATE_DIR", str(self.root / "accounts")))
            for name, filename in (("LOGIN_QR_PATH", "qr.png"), ("LOGIN_LOG_PATH", "login.log"),
                                   ("LOGIN_STATE_PATH", "login-state.json")):
                stack.enter_context(patch.object(login, name, str(self.root / filename)))
            stack.enter_context(patch.object(login, "fetch_login_qrcode", return_value={
                "qrcode": "fake-qr-id", "qrcode_img_content": "offline-test"}))
            stack.enter_context(patch.object(login, "poll_login_status", return_value={
                "status": "confirmed", "ilink_bot_id": "test@im.bot", "bot_token": "fake-token"}))
            result = login.qr_login(sleep_fn=lambda _: None)
        self.assertEqual(result, {"result": "connected", "account_id": "test-im-bot"})
        account = self.root / "accounts/test-im-bot.json"
        self.assertEqual(json.loads(account.read_text())["token"], "fake-token")
        self.assertEqual(account.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "qr.png").read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_cli_compatibility_and_module_entrypoint(self):
        env = dict(os.environ, WECHAT_WATCH_DIR=str(self.root / "watch"),
                   WECHAT_STATE_DIR=str(self.root / "accounts"),
                   WECHAT_ACCOUNT_ID="offline-test", PYTHONPATH=str(ROOT / "src"))
        for args in ((str(ROOT / "muse_wechat.py"), "--help"),
                     ("-m", "muse_wechat", "--help")):
            result = subprocess.run([sys.executable, *args], env=env, text=True, capture_output=True, check=True)
            self.assertIn("dispatch_reminders", result.stdout)
        self.assertFalse((self.root / "watch").exists())
        result = subprocess.run([sys.executable, str(ROOT / "muse_wechat.py"), "claim"],
                                env=env, text=True, capture_output=True, check=True)
        self.assertIsNone(json.loads(result.stdout))
        self.assertTrue((self.root / "watch/wechat.db").exists())
