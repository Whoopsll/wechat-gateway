"""媒体下载、AES 加解密和消息内容解析。"""

import base64
import http.client
import json
import os
import time
import urllib.request
import urllib.error
import urllib.parse
from .api import _curl_request
from .config import CDN_BASE, MEDIA_DIR, MEDIA_DOWNLOAD_MAX, MEDIA_RETRY_SLEEP, WATCH_DIR

# ---------- 媒体：加解密 ----------
def _aes_ecb(key, data, encrypt):
    """AES-128-ECB + PKCS7。优先 pycryptodome，没有就用 cryptography"""
    try:
        from Crypto.Cipher import AES
        c = AES.new(key, AES.MODE_ECB)
        if encrypt:
            pad = 16 - len(data) % 16
            return c.encrypt(data + bytes([pad]) * pad)
        out = c.decrypt(data)
    except ImportError:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        c = Cipher(algorithms.AES(key), modes.ECB())
        if encrypt:
            pad = 16 - len(data) % 16
            e = c.encryptor()
            return e.update(data + bytes([pad]) * pad) + e.finalize()
        d = c.decryptor()
        out = d.update(data) + d.finalize()
    pad = out[-1] if out else 0
    if not 1 <= pad <= 16 or out[-pad:] != bytes([pad]) * pad:
        raise ValueError("bad padding (wrong key?)")
    return out[:-pad]

def _aes_ecb_block(key, block16):
    """只解一块、不管 padding，用来看密钥对不对（解出来是不是文件头）"""
    if len(block16) < 16:
        return b""
    try:
        from Crypto.Cipher import AES
        return AES.new(key, AES.MODE_ECB).decrypt(block16[:16])
    except ImportError:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        d = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        return d.update(block16[:16]) + d.finalize()

def _key_candidates(item, media):
    """收到的媒体可能的密钥，按优先级列出（去重）。
    image_item.aeskey 是 hex；media.aes_key 是 base64，解出来可能是 16 字节原始 key，也可能是 32 位 hex 文本"""
    out = []
    def add(name, key):
        if key and len(key) == 16 and all(key != k for _, k in out):
            out.append((name, key))
    hexkey = (item or {}).get("aeskey")
    if hexkey:
        try:
            add("image_item.aeskey", bytes.fromhex(hexkey))
        except ValueError:
            pass
    b64 = (media or {}).get("aes_key")
    if b64:
        try:
            raw = base64.b64decode(b64 + "=" * (-len(b64) % 4))
            add("media.aes_key(raw)", raw)
            if len(raw) == 32:
                add("media.aes_key(hex)", bytes.fromhex(raw.decode()))
        except (ValueError, UnicodeDecodeError):
            pass
    return out

def _media_key(item, media):
    """兼容旧调用：返回第一个候选密钥"""
    c = _key_candidates(item, media)
    if not c:
        raise ValueError("no usable aes key")
    return c[0][1]

def _url_candidates(media):
    urls = []
    if media.get("full_url"):
        urls.append(("full_url", media["full_url"]))
    if media.get("encrypt_query_param"):
        urls.append(("encrypt_query_param", f"{CDN_BASE}/download?encrypted_query_param="
                                            f"{urllib.parse.quote(media['encrypt_query_param'], safe='')}"))
    return urls

def _looks_valid(data, is_image):
    if not is_image:
        return True
    return _guess_image_ext(data) != ".jpg" or data[:3] == b"\xff\xd8\xff"

RANGE_CHUNK = 64 * 1024     # 断点续传时每段的大小
GET_MAX_STALLS = 5          # 连续多少次没有任何进展就放弃

def _http_get(url, timeout=30):
    """下载 CDN 内容；测试里会替换。
    微信 CDN 经常传到一百多 KB 就断（实测多次截在 126~139KB）。拿到 Content-Length 后，
    断了就用 Range 从断点接着下，每段 64KB；服务器不认 Range 就退回整体重下。"""
    data, total, stalls, requests_made, last = b"", None, 0, 0, None
    while stalls < GET_MAX_STALLS and requests_made < 200:
        requests_made += 1
        resume = bool(data) and total is not None
        headers = {"User-Agent": "Mozilla/5.0"}
        if resume:
            headers["Range"] = f"bytes={len(data)}-{min(len(data) + RANGE_CHUNK, total) - 1}"
        before = len(data)
        try:
            status, resp_headers, body = _curl_request("GET", url, headers=headers,
                                                      timeout=timeout)
            cl = resp_headers.get("Content-Length")
            body = body[:MEDIA_DOWNLOAD_MAX + 1]
        except urllib.error.HTTPError as e:
            if e.code == 416 and total is not None and len(data) >= total:
                return data[:total]
            if e.code < 500:      # 4xx（过期、无权限）重试也没用
                raise
            last, stalls = e, stalls + 1
            time.sleep(MEDIA_RETRY_SLEEP)
            continue
        except http.client.IncompleteRead as e:
            # urllib 自己发现长度不够：partial 是已经拿到的部分，expected 是还差多少
            body, last = e.partial or b"", e
            status = 206 if resume else 200
            missing = e.expected if isinstance(e.expected, int) else None
            cl = str(len(body) + missing) if (missing is not None and not resume) else None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as e:
            last, stalls = e, stalls + 1
            time.sleep(MEDIA_RETRY_SLEEP)
            continue

        if resume and status == 206:
            data += body
        else:                                    # 第一次请求，或者服务器不认 Range 给了整个文件
            data = body
            total = int(cl) if cl and cl.isdigit() else total if resume else None
        if len(data) > MEDIA_DOWNLOAD_MAX:
            raise ValueError("media too large")
        if total is None:
            return data                          # 不知道总长，只能相信它（解密那一层还会再查）
        if len(data) >= total:
            if requests_made > 1:
                print(f"[media] 分 {requests_made} 次下完 {total} 字节", flush=True)
            return data[:total]
        if len(data) > before:
            stalls = 0                           # 有进展就不算失败
        else:
            stalls += 1
            time.sleep(MEDIA_RETRY_SLEEP)
        last = http.client.IncompleteRead(data, total - len(data))
    raise last or RuntimeError("download failed")

def _cdn_upload(url, data, timeout=None):
    """上传密文到 CDN，返回响应头里的 x-encrypted-param；测试里会替换。
    超时按大小放宽（按 64KB/s 的慢速上行估算，最少 30 秒），失败时把每次尝试的错误都带上"""
    if timeout is None:
        timeout = max(30, len(data) // (64 * 1024))
    last, errors = None, []
    for attempt in range(3):
        try:
            _, resp_headers, _ = _curl_request(
                "POST", url, data=data, timeout=timeout,
                headers={"Content-Type": "application/octet-stream",
                         "User-Agent": "Mozilla/5.0"})
            param = resp_headers.get("x-encrypted-param")
            if not param:
                raise RuntimeError(f"no x-encrypted-param, x-error-message={resp_headers.get('x-error-message')}")
            return param
        except urllib.error.HTTPError as e:
            if e.code < 500:   # 4xx 不重试
                raise RuntimeError(f"cdn upload {e.code} {e.headers.get('x-error-message')}")
            last = e
            errors.append(f"#{attempt + 1} HTTP {e.code} {e.headers.get('x-error-message')}")
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError) as e:
            # RemoteDisconnected 是 ConnectionResetError + HTTPException，不会被包成 URLError
            last = e
            errors.append(f"#{attempt + 1} {type(e).__name__}: {e}")
        if attempt < 2:
            time.sleep(1 + attempt)
    raise RuntimeError(f"cdn upload failed after 3 tries ({len(data)} bytes, timeout {timeout}s): "
                       + "; ".join(errors))

def _guess_image_ext(data):
    if data[:3] == b"\xff\xd8\xff": return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n": return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"): return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP": return ".webp"
    return ".jpg"

def _safe_name(name):
    name = os.path.basename(name or "file").replace("\x00", "")
    return "".join(c if c.isalnum() or c in "._-（）()" or ord(c) > 127 else "_" for c in name)[:80] or "file"

def _summarize_item(sub):
    """失败时记录 item 结构：长字段截断，看得出有哪些字段、长度多少"""
    def cut(v):
        if isinstance(v, str):
            return v[:20] + f"…(len={len(v)})" if len(v) > 20 else v
        if isinstance(v, dict):
            return {k: cut(x) for k, x in v.items()}
        return v
    return cut(sub or {})

def download_media(item, idx, mid):
    """下载并解密一个媒体 item，存到 MEDIA_DIR，返回放进消息内容里的文字标记。
    依次尝试 (下载地址 × 密钥) 的组合，第一个解得开的就用；全失败则把密文和结构存进 media/failed/ 方便排查"""
    t = item.get("type")
    sub = item.get("image_item") if t == 2 else item.get("file_item")
    media = (sub or {}).get("media") or {}
    keys = _key_candidates(sub, media)
    urls = _url_candidates(media)
    attempts, cipher_seen = [], None
    data = fallback = None   # fallback：padding 对但文件头不认识（比如 HEIC），没有更好的就用它
    for url_name, url in urls:
        for dl in range(3):
            try:
                cipher = _http_get(url)
            except Exception as e:
                attempts.append(f"{url_name}#{dl + 1}: 下载失败 {type(e).__name__}: {e}")
                break
            cipher_seen = cipher
            if len(cipher) % 16:
                attempts.append(f"{url_name}#{dl + 1}: 长度 {len(cipher)} 不是 16 的倍数，数据被截断，重下")
                time.sleep(MEDIA_RETRY_SLEEP)
                continue
            truncated = False
            for key_name, key in keys:
                head = _aes_ecb_block(key, cipher[:16])
                head_ok = _looks_valid(head, True) if t == 2 else None
                try:
                    plain = _aes_ecb(key, cipher, encrypt=False)
                except ValueError as e:
                    attempts.append(f"{url_name}#{dl + 1}+{key_name}: {e} (len={len(cipher)}, "
                                    f"首块解出 {head[:4].hex()}{' = 图片头，密钥是对的' if head_ok else ''})")
                    truncated = truncated or bool(head_ok)   # 密钥对、padding 坏 → 尾部被截断
                    continue
                if not _looks_valid(plain, t == 2):
                    attempts.append(f"{url_name}#{dl + 1}+{key_name}: 解出来文件头不认识 head={plain[:8].hex()}")
                    fallback = fallback or plain
                    continue
                if (url_name, key_name, dl) != (urls[0][0], keys[0][0], 0):
                    print(f"[media] id={mid[:12]} 解开: {url_name}+{key_name}（第 {dl + 1} 次下载）", flush=True)
                data = plain
                break
            if data is not None or not truncated:
                break                       # 成功，或者不是截断（换下一个地址）
            attempts[-1] += "，判定为截断，重下"
            time.sleep(MEDIA_RETRY_SLEEP)
        if data is not None:
            break
    data = data if data is not None else fallback
    if data is None:
        os.makedirs(os.path.join(MEDIA_DIR, "failed"), exist_ok=True)
        base = os.path.join(MEDIA_DIR, "failed", _safe_name(mid.replace("hash:", "h")) + f"_{idx}")
        if cipher_seen is not None:
            with open(base + ".bin", "wb") as f:
                f.write(cipher_seen)
        diag = {"type": t, "item": _summarize_item(sub),
                "sizes_in_item": {k: (sub or {}).get(k) for k in ("mid_size", "hd_size", "thumb_size", "len")
                                  if (sub or {}).get(k) is not None},
                "keys": [n for n, _ in keys],
                "urls": [n for n, _ in urls], "attempts": attempts,
                "cipher_len": len(cipher_seen) if cipher_seen is not None else None,
                "cipher_head": cipher_seen[:16].hex() if cipher_seen else None}
        with open(base + ".json", "w") as f:
            json.dump(diag, f, ensure_ascii=False, indent=2)
        print(f"[media] decrypt failed id={mid[:12]} 详情见 {base}.json: {attempts}", flush=True)
        raise ValueError("all key/url combinations failed")

    os.makedirs(MEDIA_DIR, exist_ok=True)
    base = _safe_name(mid.replace("hash:", "h")) + f"_{idx}"
    if t == 2:
        path = os.path.join(MEDIA_DIR, base + _guess_image_ext(data))
        label = "图片"
    else:
        fname = _safe_name(sub.get("file_name"))
        path = os.path.join(MEDIA_DIR, f"{base}_{fname}")
        label = f"文件 {fname}"
    with open(path, "wb") as f:
        f.write(data)
    return f"[{label}: {path}]"

def message_content(msg, mid):
    """把一条微信消息转成文字内容：文本原样，图片/文件下载后变成 [图片: 路径]，其他类型给占位"""
    parts = []
    for idx, item in enumerate(msg.get("item_list") or []):
        t = item.get("type")
        if t == 1:
            txt = (item.get("text_item") or {}).get("text")
            if txt:
                parts.append(str(txt))
        elif t in (2, 4):
            try:
                parts.append(download_media(item, idx, mid))
            except Exception as e:
                print(f"[media] download failed id={mid[:12]}: {e}", flush=True)
                parts.append("[图片接收失败，请让对方重发]" if t == 2 else "[文件接收失败，请让对方重发]")
        else:
            voice_text = (item.get("voice_item") or {}).get("text") if t == 3 else None
            if voice_text:
                parts.append(f"[语音转文字] {voice_text}")
            else:
                parts.append({3: "[语音消息，暂不支持]", 5: "[视频消息，暂不支持]"}.get(t, f"[暂不支持的消息类型 {t}]"))
                # 记录原始结构，方便以后支持
                try:
                    with open(os.path.join(WATCH_DIR, "raw_media.jsonl"), "a") as f:
                        f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                except OSError:
                    pass
    return "\n".join(parts)
