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
    走代理时会有多组响应头（如先 "HTTP/1.1 200 Connection Established" 再是真正的响应），
    取最后一组以 HTTP/ 开头的头块为真正的响应头。"""
    marker = b"\nCURLSTATUS:"
    mi = out.rfind(marker)
    if mi < 0:
        raise ValueError("curl output missing CURLSTATUS trailer")
    status = int(out[mi + len(marker):].decode("ascii").strip())
    raw = out[:mi]
    # 找到最后一个以 HTTP/ 开头的头块
    # 头块之间以 \r\n\r\n 分隔；body 也可能包含它，但 body 在最后一个头块之后
    norm = raw.replace(b"\r\n", b"\n")
    # 按 \n\n 切分，找最后一个以 HTTP/ 开头的块
    parts = norm.split(b"\n\n")
    hdr_idx = -1
    for i, p in enumerate(parts):
        if p.startswith(b"HTTP/"):
            hdr_idx = i
    if hdr_idx < 0:
        raise ValueError("no HTTP status line in curl output")
    # 在原始字节流里定位最后一个头块的结束位置（body 是二进制，不能做 \r\n 替换）
    last_http = raw.rfind(b"HTTP/")
    hdr_end = raw.find(b"\r\n\r\n", last_http)
    sep_len = 4
    if hdr_end < 0:
        hdr_end = raw.find(b"\n\n", last_http)
        sep_len = 2
    if hdr_end < 0:
        raise ValueError("no header/body separator in curl output")
    header_block = raw[last_http:hdr_end].decode("latin-1").splitlines()
    headers = _CIHeaders()
    for line in header_block[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()
    return status, headers, raw[hdr_end + sep_len:]

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
