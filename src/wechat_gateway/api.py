"""微信 API 和 curl HTTP 传输。"""

import base64
import http.client
import io
import json
import os
import random
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from .config import ACCOUNT_ID, BASE_URL, ILINK_APP_CLIENT_VERSION, ILINK_APP_ID, STATE_DIR

# ---------- 微信 API ----------
def load_token():
    with open(os.path.join(STATE_DIR, f"{ACCOUNT_ID}.json")) as f:
        return json.load(f)["token"]

def random_uin():
    return base64.b64encode(str(random.getrandbits(32)).encode()).decode()

def headers(token):
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token}",
        "X-WECHAT-UIN": random_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }

def base_info():
    return {"channel_version": "2.4.10", "bot_agent": "Muse"}

class _CIHeaders(dict):
    """大小写不敏感的响应头字典（curl 原样返回头名，代码里用 .get("Content-Length") 等访问）"""
    def get(self, key, default=None):
        lk = key.lower()
        for k, v in self.items():
            if k.lower() == lk:
                return v
        return default

def _parse_curl_headers(out):
    """解析 curl -i 的输出，返回 (status_code, _CIHeaders, body_bytes)。
    从头顺序消费代理 CONNECT 和 1xx 临时响应，读到最终响应后固定正文起点。
    正文可能包含 HTTP 状态行甚至完整的伪响应头，不能再从正文中寻找头块。
    当前调用方固定 HTTP/1.1、不跟随重定向；CONNECT 按常见的
    "200 Connection established" 状态行识别。"""
    import re

    marker = b"\nCURLSTATUS:"
    mi = out.rfind(marker)
    if mi < 0:
        raise ValueError("curl output missing CURLSTATUS trailer")
    trailer = out[mi + len(marker):].strip()
    if not re.fullmatch(rb"[1-5][0-9]{2}", trailer):
        raise ValueError("invalid CURLSTATUS trailer")
    status = int(trailer)
    raw = out[:mi]
    offset = 0

    def read_line():
        nonlocal offset
        end = raw.find(b"\n", offset)
        if end < 0:
            raise ValueError("no header/body separator in curl output")
        line = raw[offset:end].removesuffix(b"\r")
        offset = end + 1
        return line

    while offset < len(raw):
        match = re.fullmatch(
            rb"HTTP/(?:1\.[01]|2|3) ([1-5][0-9]{2})(?: ([\t\x20-\x7e\x80-\xff]*))?",
            read_line())
        if match is None:
            raise ValueError("invalid HTTP status line in curl output")
        response_status = int(match[1])
        reason = (match[2] or b"").strip().lower()
        headers = _CIHeaders()
        while True:
            line = read_line()
            if not line:
                break
            name, colon, value = line.partition(b":")
            if (not colon or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                    or not re.fullmatch(rb"[\t\x20-\x7e\x80-\xff]*", value)):
                raise ValueError("invalid HTTP header in curl output")
            headers[name.decode("ascii")] = value.strip().decode("latin-1")
        # 101 切换协议后也是正文；不能把新协议的数据继续当作 HTTP 头。
        informational = 100 <= response_status < 200 and response_status != 101
        connect = response_status == 200 and reason == b"connection established"
        if informational or connect:
            continue
        if response_status != status:
            raise ValueError("HTTP status does not match CURLSTATUS trailer")
        return status, headers, raw[offset:]
    raise ValueError("no final HTTP response in curl output")

def _curl_request(method, url, headers=None, data=None, timeout=30):
    """用 curl 子进程发 HTTP 请求（urllib 会被代理按 TLS 指纹 403，curl 不会）。

    返回 (status_code, headers, body_bytes)。
    抛出的异常类型与原来 urllib 版本保持一致，调用方的 except 不用改：
      urllib.error.HTTPError  -> HTTP 4xx/5xx（.code / .headers 可用）
      urllib.error.URLError   -> 连接失败（DNS、拒绝连接、SSL 等）
      TimeoutError            -> 超时
      http.client.IncompleteRead -> 传了一半断开（curl exit 18），.partial 是已拿到的数据
    """
    cmd = ["curl", "-sS", "-i", "--http1.1", "--no-progress-meter",
           "--max-time", str(int(timeout)),
           "-w", "\nCURLSTATUS:%{http_code}",
           "-X", method, url]
    for k, v in (headers or {}).items():
        cmd.extend(["-H", f"{k}: {v}"])
    if data is not None:
        # 请求体经 stdin 传入（下方 subprocess input=data）；少了这个参数 curl 会
        # 直接丢掉请求体，服务端只回 {"ret":-1}——2026-10-17 换 curl 时漏掉的坑，
        # 症状与"会话过期"一模一样，2026-10-22 才定位到。
        cmd.extend(["--data-binary", "@-"])
    try:
        proc = subprocess.run(cmd, input=data, capture_output=True,
                              timeout=timeout + 15)
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(f"curl subprocess timeout {timeout}s {url[:80]}") from e
    rc = proc.returncode
    err_tail = proc.stderr.decode(errors="replace")[-200:]
    if rc == 28:
        raise TimeoutError(f"curl max-time {timeout}s exceeded {url[:80]}")
    if rc == 18:
        # 传了一半断开：尽量解析已拿到的数据，走调用方的 IncompleteRead 重试逻辑
        try:
            _, resp_headers, partial = _parse_curl_headers(proc.stdout)
        except ValueError:
            partial, resp_headers = b"", _CIHeaders()
        cl = resp_headers.get("Content-Length")
        expected = int(cl) - len(partial) if cl and cl.isdigit() else None
        raise http.client.IncompleteRead(partial, expected)
    if rc in (6, 7, 35, 51, 52, 60):
        # 6 解析失败 / 7 连接失败 / 35,51,52,60 各类 SSL/连接问题
        raise urllib.error.URLError(f"curl exit {rc}: {err_tail}")
    if rc != 0:
        raise urllib.error.URLError(f"curl exit {rc}: {err_tail}")
    status, resp_headers, body = _parse_curl_headers(proc.stdout)
    if status >= 400:
        raise urllib.error.HTTPError(url, status, f"HTTP {status}",
                                     resp_headers, io.BytesIO(body))
    return status, resp_headers, body

def api_post(endpoint, body, token, timeout=35):
    _, _, resp_body = _curl_request("POST", BASE_URL + endpoint,
                                   headers=headers(token),
                                   data=json.dumps(body).encode(),
                                   timeout=timeout)
    return json.loads(resp_body.decode())
