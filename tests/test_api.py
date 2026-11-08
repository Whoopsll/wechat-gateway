"""curl 响应头与正文边界的回归测试，不访问真实网络。"""

import http.client
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wechat_gateway import api


def curl_output(raw, status=200):
    return raw + f"\nCURLSTATUS:{status:03d}".encode("ascii")


class CurlHeaderTests(unittest.TestCase):
    def test_plain_response_and_case_insensitive_headers(self):
        raw = b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\r\n{}"
        status, headers, body = api._parse_curl_headers(curl_output(raw))
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(body, b"{}")

    def test_getupdates_text_containing_http_does_not_block_parsing(self):
        # 按故障描述构造脱敏响应；并非线上捕获的原始响应。
        payload = {"ret": 0, "get_updates_buf": "next-cursor", "msgs": [{
            "item_list": [{"type": 1, "text_item": {
                "text": "在整个数据中搜索最后一个 HTTP/，文件内容包含这类字节时会出错。"
            }}]
        }]}
        body = json.dumps(payload, ensure_ascii=False).encode()
        raw = b"HTTP/1.1 200 Connection established\r\n\r\n"
        raw += b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n" + body
        proc = types.SimpleNamespace(returncode=0, stdout=curl_output(raw), stderr=b"")
        with patch.object(api.subprocess, "run", return_value=proc):
            result = api.api_post("/ilink/bot/getupdates", {}, "fake-token")
        self.assertEqual(result, payload)

    def test_body_http_occurrences_are_preserved(self):
        for body in (b"HTTP/", b"prefix HTTP/1.1 404 Nope\r\nX-Fake: yes\r\n\r\nend",
                     b"HTTP/1.1 200 OK\r\nX-Fake: yes\r\n\r\nend",
                     b"HTTP/1.1 200 Connection established\r\n\r\nend",
                     b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\n\r\nend"):
            with self.subTest(body=body):
                raw = b"HTTP/1.1 200 OK\r\nX-Real: yes\r\n\r\n" + body
                _, headers, parsed = api._parse_curl_headers(curl_output(raw))
                self.assertEqual(parsed, body)
                self.assertEqual(headers.get("X-Real"), "yes")
                self.assertIsNone(headers.get("X-Fake"))

    def test_binary_body_and_embedded_curl_trailer_are_preserved(self):
        body = bytes(range(256)) + b"\r\n\r\nHTTP/\x00\n\nCURLSTATUS:404\r\n"
        raw = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        self.assertEqual(api._parse_curl_headers(curl_output(raw))[2], body)

    def test_lf_crlf_and_empty_body(self):
        for sep in (b"\n", b"\r\n"):
            for body in (b"", b"body\r\n\n\r"):
                with self.subTest(sep=sep, body=body):
                    raw = sep.join((b"HTTP/1.1 200 OK", b"X-Test: yes", b"", b"")) + body
                    self.assertEqual(api._parse_curl_headers(curl_output(raw))[2], body)

    def test_connect_and_informational_headers_are_skipped(self):
        prefixes = (
            b"HTTP/1.1 200 Connection established\r\nProxy-Agent: test\r\n\r\n",
            b"HTTP/1.1 200 Connection Established\n\n",
            b"HTTP/1.1 100 Continue\r\n\r\n",
            b"HTTP/1.1 103 Early Hints\r\nLink: </test>\r\n\r\n",
            b"HTTP/1.1 200 Connection established\r\n\r\n"
            b"HTTP/1.1 100 Continue\r\n\r\n"
            b"HTTP/1.1 103 Early Hints\r\nLink: </test>\r\n\r\n",
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                raw = prefix + b"HTTP/1.1 206 Partial Content\r\nContent-Length: 4\r\n\r\ndata"
                status, headers, body = api._parse_curl_headers(curl_output(raw, 206))
                self.assertEqual(status, 206)
                self.assertEqual(headers.get("Content-Length"), "4")
                self.assertIsNone(headers.get("Proxy-Agent"))
                self.assertIsNone(headers.get("Link"))
                self.assertEqual(body, b"data")

    def test_supported_curl_status_line_versions(self):
        for line in (b"HTTP/1.0 200 OK", b"HTTP/1.1 200 OK", b"HTTP/2 200", b"HTTP/3 200"):
            with self.subTest(line=line):
                self.assertEqual(api._parse_curl_headers(curl_output(line + b"\r\n\r\nbody"))[2], b"body")

    def test_protocol_switch_is_final_response(self):
        body = b"HTTP/1.1 200 OK\r\n\r\nupgraded protocol data"
        raw = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n" + body
        status, _, parsed = api._parse_curl_headers(curl_output(raw, 101))
        self.assertEqual(status, 101)
        self.assertEqual(parsed, body)

    def test_error_response_body_is_preserved(self):
        body = b"HTTP/ error details"
        raw = b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: text/plain\r\n\r\n" + body
        proc = types.SimpleNamespace(returncode=0, stdout=curl_output(raw, 500), stderr=b"")
        with patch.object(api.subprocess, "run", return_value=proc):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                api._curl_request("GET", "https://example.invalid")
        self.assertEqual(raised.exception.code, 500)
        self.assertEqual(raised.exception.read(), body)

    def test_partial_download_keeps_bytes_for_resume(self):
        body = b"partial HTTP/ bytes"
        raw = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + body
        proc = types.SimpleNamespace(returncode=18, stdout=curl_output(raw), stderr=b"partial transfer")
        with patch.object(api.subprocess, "run", return_value=proc):
            with self.assertRaises(http.client.IncompleteRead) as raised:
                api._curl_request("GET", "https://example.invalid")
        self.assertEqual(raised.exception.partial, body)
        self.assertEqual(raised.exception.expected, 100 - len(body))

    def test_malformed_status_line_and_headers_are_rejected(self):
        for raw in (b"junk HTTP/1.1 200 OK\r\n\r\n", b"HTTP/1.1 2000 OK\r\n\r\n",
                    b"HTTP/1.1 200 OK\r\nNot-a-header\r\n\r\n",
                    b"HTTP/1.1 200 OK\r\n: empty-name\r\n\r\n",
                    b"HTTP/1.1 200 OK\r\nBad Name: value\r\n\r\n",
                    b"HTTP/1.1 200 OK\r\nX-Test: bad\x00value\r\n\r\n"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    api._parse_curl_headers(curl_output(raw))

    def test_incomplete_or_missing_headers_are_rejected(self):
        for raw in (b"", b"HTTP/1.1 200 OK", b"HTTP/1.1 200 OK\r\nX-Test: value",
                    b"HTTP/1.1 200 Connection established\r\n\r\n",
                    b"HTTP/1.1 100 Continue\r\n\r\n"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    api._parse_curl_headers(curl_output(raw))

    def test_invalid_trailer_and_status_mismatch_are_rejected(self):
        raw = b"HTTP/1.1 200 OK\r\n\r\nbody"
        for out in (raw, raw + b"\nCURLSTATUS:", raw + b"\nCURLSTATUS:abc",
                    raw + b"\nCURLSTATUS:000", curl_output(raw, 404)):
            with self.subTest(out=out):
                with self.assertRaises(ValueError):
                    api._parse_curl_headers(out)
