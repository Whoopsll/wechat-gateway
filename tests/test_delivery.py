"""GW-1 / GW-3：真实 SQLite + 模拟 curl 的回复归属与防重验证。"""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wechat_gateway import api, claims, cli, files, messages, reminders, storage


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="wechat-delivery-")))
        self.stack.enter_context(patch.object(storage, "DB_PATH", str(self.root / "test.db")))
        self.stack.enter_context(patch.object(storage, "_local", threading.local()))
        self.stack.enter_context(patch.object(messages, "load_token", return_value="fake-token"))
        self.conn = storage.db()
        self.addCleanup(self.conn.close)

    def seed(self, mid="m1", chat="chat-A"):
        self.conn.execute(
            "INSERT INTO inbox(msg_id,chat_id,content,created_at,ctx_token) VALUES(?,?,?,?,?)",
            (mid, chat, mid, time.time(), "fake-context"))

    def response(self, code=0, body=b'{"ret":0}', status=200):
        stdout = (f"HTTP/1.1 {status} Result\r\nContent-Type: application/json\r\n\r\n".encode()
                  + body + f"\nCURLSTATUS:{status:03d}".encode())
        return types.SimpleNamespace(returncode=code, stdout=stdout, stderr=b"simulated curl")

    def sent_exists(self, key):
        return bool(self.conn.execute("SELECT 1 FROM sent WHERE reply_to=?", (key,)).fetchone())

    def test_reclaimed_worker_cannot_send_or_finish_new_batch(self):
        self.seed()
        old = claims.claim_batch()
        self.conn.execute("UPDATE inbox SET claimed_at=?", (time.time() - claims.STALE_SECS - 1,))
        self.seed("m2")
        new = claims.claim_batch()
        with patch.object(messages, "_post_text") as send:
            result = messages.send_reply(old["reply_to"], "chat-A", "旧答案", claim_id=old["claim_id"])
            self.assertEqual(result, "superseded")
            send.assert_not_called()
            self.assertFalse(self.sent_exists(old["reply_to"]))
            self.assertEqual(self.conn.execute("SELECT DISTINCT status FROM inbox").fetchall(), [("processing",)])
            self.assertEqual(messages.send_reply(new["reply_to"], "chat-A", "新答案", claim_id=new["claim_id"]), "ok")
            send.assert_called_once_with("chat-A", "新答案")
        self.assertEqual(self.conn.execute("SELECT DISTINCT status FROM inbox").fetchall(), [("replied",)])

    def test_wrong_chat_message_or_claim_is_rejected(self):
        self.seed()
        batch = claims.claim_batch()
        for reply_to, chat, claim in (("m1", "chat-B", batch["claim_id"]),
                                      ("missing", "chat-A", batch["claim_id"]),
                                      ("m1", "chat-A", "old-claim")):
            with self.subTest(reply_to=reply_to, chat=chat, claim=claim), patch.object(messages, "_post_text") as send:
                self.assertEqual(messages.send_reply(reply_to, chat, "text", claim_id=claim), "superseded")
                send.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sent").fetchone()[0], 0)

    def test_reply_renews_claim_before_network_and_queued_message_stays_pending(self):
        self.seed()
        batch = claims.claim_batch()
        self.conn.execute("UPDATE inbox SET claimed_at=?", (time.time() - claims.STALE_SECS + 1,))
        self.seed("m2")
        def network(*_):
            # 不在网络调用期间持有 SQLite 写事务。
            with sqlite3.connect(storage.DB_PATH, isolation_level=None, timeout=0.1) as other:
                other.execute("UPDATE inbox SET content=content WHERE msg_id='m2'")
            self.assertIsNone(claims.claim_batch())
        with patch.object(messages, "_post_text", side_effect=network):
            self.assertEqual(messages.send_reply("m1", "chat-A", "answer", claim_id=batch["claim_id"]), "ok")
        self.assertEqual(self.conn.execute("SELECT msg_id,status FROM inbox ORDER BY msg_id").fetchall(),
                         [("m1", "replied"), ("m2", "pending")])

    def test_handoff_during_transport_cannot_finish_replacement_claim(self):
        self.seed()
        old = claims.claim_batch()
        replacement = []
        def network(*_):
            claims.unclaim("chat-A")
            self.seed("m2")
            replacement.append(claims.claim_batch())
        with patch.object(messages, "_post_text", side_effect=network):
            messages.send_reply("m1", "chat-A", "already in flight", claim_id=old["claim_id"])
        self.assertNotEqual(old["claim_id"], replacement[0]["claim_id"])
        self.assertEqual(self.conn.execute("SELECT DISTINCT status FROM inbox").fetchall(), [("processing",)])

    def test_reply_transport_uncertainty_finishes_only_its_claim(self):
        self.seed()
        batch = claims.claim_batch()
        self.seed("m2")
        with patch.object(api.subprocess, "run", return_value=self.response(52)) as run:
            self.assertEqual(messages.send_reply("m1", "chat-A", "answer", claim_id=batch["claim_id"]), "uncertain")
            self.assertEqual(messages.send_reply("m1", "chat-A", "answer", claim_id=batch["claim_id"]), "already_replied")
            self.assertEqual(run.call_count, 1)
        self.assertTrue(self.sent_exists("m1"))
        self.assertEqual(self.conn.execute("SELECT msg_id,status FROM inbox ORDER BY msg_id").fetchall(),
                         [("m1", "replied"), ("m2", "pending")])

    def test_reply_definite_failure_keeps_batch_available_for_retry(self):
        self.seed()
        batch = claims.claim_batch()
        with patch.object(api.subprocess, "run", return_value=self.response(6)):
            with self.assertRaises(api.CurlTransportError):
                messages.send_reply("m1", "chat-A", "answer", claim_id=batch["claim_id"])
        self.assertEqual(self.conn.execute("SELECT status FROM inbox").fetchone()[0], "processing")
        self.assertFalse(self.sent_exists("m1"))
        with patch.object(api.subprocess, "run", return_value=self.response()):
            self.assertEqual(messages.send_reply("m1", "chat-A", "answer", claim_id=batch["claim_id"]), "ok")

    def test_uncertain_curl_errors_keep_record_and_block_second_request(self):
        for code in (18, 28, 52, 55, 56, 92, 99):
            with self.subTest(code=code), patch.object(api.subprocess, "run", return_value=self.response(code)) as run:
                key = f"uncertain-{code}"
                self.assertEqual(messages.send_text("chat-A", "text", key), "uncertain")
                self.assertTrue(self.sent_exists(key))
                self.assertEqual(messages.send_text("chat-A", "text", key), "already_replied")
                self.assertEqual(run.call_count, 1)

    def test_pre_request_failures_allow_retry_and_remain_url_errors(self):
        for code in (3, 5, 6, 7, 35, 60, 77):
            with self.subTest(code=code):
                key = f"retry-{code}"
                with patch.object(api.subprocess, "run", return_value=self.response(code)):
                    with self.assertRaises(api.CurlTransportError) as error:
                        messages.send_text("chat-A", "text", key)
                self.assertIsInstance(error.exception, urllib.error.URLError)
                self.assertFalse(error.exception.request_may_have_been_sent)
                self.assertFalse(self.sent_exists(key))
                with patch.object(api.subprocess, "run", return_value=self.response()):
                    self.assertEqual(messages.send_text("chat-A", "text", key), "ok")

    def test_bad_or_unconfirmed_response_is_uncertain(self):
        for index, body in enumerate((b"not JSON", b"\xff", b"[]", b"null",
                                      b'{"ret":"0"}', b'{"ret":false}')):
            with self.subTest(body=body), patch.object(api.subprocess, "run", return_value=self.response(body=body)) as run:
                key = f"bad-response-{index}"
                self.assertEqual(messages.send_text("chat-A", "text", key), "uncertain")
                self.assertTrue(self.sent_exists(key))
                self.assertEqual(messages.send_text("chat-A", "text", key), "already_replied")
                run.assert_called_once()

    def test_success_response_preserves_optional_ret_compatibility(self):
        for index, body in enumerate((b"{}", b'{"ret":null}', b'{"ret":0}')):
            with self.subTest(body=body), patch.object(api.subprocess, "run", return_value=self.response(body=body)) as run:
                key = f"success-{index}"
                self.assertEqual(messages.send_text("chat-A", "text", key), "ok")
                self.assertEqual(messages.send_text("chat-A", "text", key), "already_replied")
                run.assert_called_once()

    def test_bad_http_headers_are_uncertain_after_request(self):
        proc = self.response()
        proc.stdout = b"HTTP/1.1 200 OK\r\nBrokenHeader\r\n\r\n{}\nCURLSTATUS:200"
        with patch.object(api.subprocess, "run", return_value=proc):
            self.assertEqual(messages.send_text("chat-A", "text", "broken-headers"), "uncertain")
        self.assertTrue(self.sent_exists("broken-headers"))

    def test_http_error_classification(self):
        for status in (408, 500, 502, 503):
            with self.subTest(status=status), patch.object(api.subprocess, "run", return_value=self.response(status=status)):
                key = f"http-{status}"
                self.assertEqual(messages.send_text("chat-A", "text", key), "uncertain")
                self.assertTrue(self.sent_exists(key))
        for status in (400, 401, 403, 429):
            with self.subTest(status=status), patch.object(api.subprocess, "run", return_value=self.response(status=status)):
                key = f"http-{status}"
                with self.assertRaises(urllib.error.HTTPError):
                    messages.send_text("chat-A", "text", key)
                self.assertFalse(self.sent_exists(key))

    def test_explicit_api_rejection_allows_retry(self):
        with patch.object(api.subprocess, "run", return_value=self.response(body=b'{"ret":-1}')):
            with self.assertRaisesRegex(RuntimeError, "ret=-1"):
                messages.send_text("chat-A", "text", "rejected")
        self.assertFalse(self.sent_exists("rejected"))

    def test_missing_credentials_are_not_misreported_as_delivery_uncertain(self):
        with patch.object(messages, "load_token", side_effect=FileNotFoundError("credentials missing")), \
             patch.object(api.subprocess, "run") as run:
            with self.assertRaises(FileNotFoundError):
                messages.send_text("chat-A", "text", "no-credentials")
            run.assert_not_called()
        self.assertFalse(self.sent_exists("no-credentials"))

    def test_unclassified_url_error_is_conservatively_uncertain(self):
        with patch.object(messages, "api_post", side_effect=urllib.error.URLError("unknown stage")):
            self.assertEqual(messages.send_text("chat-A", "text", "unknown-stage"), "uncertain")
        self.assertTrue(self.sent_exists("unknown-stage"))

    def test_say_and_notify_also_retain_uncertain_records(self):
        self.seed()
        batch = claims.claim_batch()
        with patch.object(api.subprocess, "run", return_value=self.response(56)) as run:
            self.assertEqual(messages.say(batch["claim_id"], "progress"), "uncertain")
            self.assertTrue(self.sent_exists(f"say:{batch['claim_id']}:0"))
            self.assertEqual(reminders.notify("chat-A", "reminder", "rid"), "uncertain")
            self.assertEqual(reminders.notify("chat-A", "reminder", "rid"), "duplicate")
            self.assertEqual(run.call_count, 2)

    def test_existing_media_resend_policy_is_explicitly_preserved(self):
        self.seed()
        batch = claims.claim_batch()
        path = self.root / "report.txt"
        path.write_text("report")
        with patch.object(files, "upload_media", return_value=({}, 6, 16, "md5")), \
             patch.object(files.time, "sleep"), \
             patch.object(api.subprocess, "run", side_effect=[self.response(52), self.response()]) as run:
            self.assertEqual(files.send_file(batch["claim_id"], str(path)), "ok")
            self.assertEqual(files.send_file(batch["claim_id"], str(path)), "already_sent")
            self.assertEqual(run.call_count, 2)

    def test_cli_requires_claim_and_accepts_stdin_or_inline_text(self):
        with patch.object(cli, "WATCH_DIR", str(self.root / "watch")), \
             patch.object(cli, "STATE_DIR", str(self.root / "accounts")), \
             patch.object(cli, "send_reply", return_value="ok") as send, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            for args in (["reply", "m1", "chat-A"], ["reply", "m1", "chat-A", "legacy text"]):
                with self.assertRaises(SystemExit) as error:
                    cli.main(args)
                self.assertEqual(error.exception.code, 2)
            send.assert_not_called()
            with patch.object(sys, "stdin", io.StringIO("stdin text")):
                cli.main(["reply", "m1", "chat-A", "--claim-id", "claim-A"])
            send.assert_called_with("m1", "chat-A", "stdin text", claim_id="claim-A")
            cli.main(["reply", "m1", "chat-A", "--claim-id", "claim-A", "inline text"])
            send.assert_called_with("m1", "chat-A", "inline text", claim_id="claim-A")

    def test_source_entry_rejects_old_syntax_and_stale_claim_without_network(self):
        self.seed()
        claims.claim_batch()
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, WECHAT_WATCH_DIR=str(self.root), WECHAT_STATE_DIR=str(self.root / "accounts"))
        command = [sys.executable, str(root / "wechat_gateway.py"), "reply", "m1", "chat-A"]
        old = subprocess.run(command, input="answer", env=env, text=True, capture_output=True)
        self.assertEqual(old.returncode, 2)
        self.assertIn("--claim-id", old.stderr)
        stale = subprocess.run([*command, "--claim-id", "old-claim"], input="answer",
                               env=env, text=True, capture_output=True, check=True)
        self.assertEqual(stale.stdout.strip(), "superseded")
        self.assertEqual(self.conn.execute("SELECT status FROM inbox").fetchone()[0], "processing")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sent").fetchone()[0], 0)
