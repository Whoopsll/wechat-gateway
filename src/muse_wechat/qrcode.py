"""纯标准库二维码编码和 PNG 输出。"""

import struct
import zlib

# ---------- 二维码生成（纯标准库：字节模式、纠错等级 M、版本 1~10） ----------
_QR_ECM_TABLE = {  # 版本 -> (每块纠错码字数, [(块数, 每块数据码字数), ...])
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]),
    5: (24, [(2, 43)]), 6: (16, [(4, 27)]), 7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)]),
}
_QR_ALIGN_CENTERS = {1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30), 6: (6, 34),
                     7: (6, 22, 38), 8: (6, 24, 42), 9: (6, 26, 46), 10: (6, 28, 50)}
_QR_REMAINDER_BITS = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0, 10: 0}

def _gf_mul(x, y):
    """GF(256) 乘法（本原多项式 0x11D），逐位算，不建表"""
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        if (y >> i) & 1:
            z ^= x
    return z

def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(len(result)):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < len(result):
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result

def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result

def _qr_data_codewords(raw, version):
    """把原始字节编码成带纠错、已交织的全部码字（字节模式）"""
    ec_len, groups = _QR_ECM_TABLE[version]
    total_data = sum(n * d for n, d in groups)
    bits = []

    def put(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    put(0b0100, 4)                                   # 字节模式
    put(len(raw), 8 if version <= 9 else 16)         # 字符计数
    for b in raw:
        put(b, 8)
    capacity = total_data * 8
    if len(bits) > capacity:
        raise ValueError(f"内容过长：{len(raw)} 字节放不进版本 {version}")
    put(0, min(4, capacity - len(bits)))             # 终止符
    put(0, -len(bits) % 8)                           # 对齐到字节
    pad = 0xEC
    while len(bits) < capacity:
        put(pad, 8)
        pad = 0x11 if pad == 0xEC else 0xEC
    data = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for bit in bits[i:i + 8]:
            byte = (byte << 1) | bit
        data.append(byte)
    blocks, ec_blocks, offset = [], [], 0
    divisor = _rs_divisor(ec_len)
    for n_blocks, dc in groups:
        for _ in range(n_blocks):
            block = bytes(data[offset:offset + dc])
            offset += dc
            blocks.append(block)
            ec_blocks.append(bytes(_rs_remainder(block, divisor)))
    out = bytearray()
    for i in range(max(len(b) for b in blocks)):
        for b in blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(ec_len):
        for eb in ec_blocks:
            out.append(eb[i])
    return bytes(out)

def _qr_draw_format(mod, size, mask, func=None):
    """画格式信息（纠错等级 M 的 format bits 为 0）；func 给定时同时标记功能模块"""
    rem = mask
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = ((mask << 10) | rem) ^ 0x5412

    def setf(x, y, dark):
        mod[y][x] = dark
        if func is not None:
            func[y][x] = True

    for i in range(6):
        setf(8, i, (bits >> i) & 1 == 1)
    setf(8, 7, (bits >> 6) & 1 == 1)
    setf(8, 8, (bits >> 7) & 1 == 1)
    setf(7, 8, (bits >> 8) & 1 == 1)
    for i in range(9, 15):
        setf(14 - i, 8, (bits >> i) & 1 == 1)
    for i in range(8):
        setf(size - 1 - i, 8, (bits >> i) & 1 == 1)
    for i in range(8, 15):
        setf(8, size - 15 + i, (bits >> i) & 1 == 1)
    setf(8, size - 8, True)                          # 暗模块

def _qr_draw_base(version):
    """画全部功能图案，返回 (模块图, 功能模块标记)；格式信息先用掩码 0 占位"""
    size = version * 4 + 17
    mod = [[False] * size for _ in range(size)]
    func = [[False] * size for _ in range(size)]

    def setf(x, y, dark):
        if 0 <= x < size and 0 <= y < size:
            mod[y][x] = dark
            func[y][x] = True

    for i in range(size):                            # 计时图案
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    for dx, dy in ((3, 3), (size - 4, 3), (3, size - 4)):   # 定位图案 + 分隔符
        for yy in range(-4, 5):
            for xx in range(-4, 5):
                setf(dx + xx, dy + yy, max(abs(xx), abs(yy)) not in (2, 4))
    centers = _QR_ALIGN_CENTERS[version]             # 校正图案
    for cy in centers:
        for cx in centers:
            if (cx, cy) in ((centers[0], centers[0]), (centers[0], centers[-1]),
                            (centers[-1], centers[0])):
                continue
            for yy in range(-2, 3):
                for xx in range(-2, 3):
                    setf(cx + xx, cy + yy, max(abs(xx), abs(yy)) != 1)
    _qr_draw_format(mod, size, 0, func)              # 格式信息占位 + 暗模块
    if version >= 7:                                 # 版本信息
        rem = version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = (version << 12) | rem
        for i in range(18):
            dark = (bits >> i) & 1 == 1
            setf(size - 11 + i % 3, i // 3, dark)
            setf(i // 3, size - 11 + i % 3, dark)
    return mod, func

def _qr_mask_bit(mask, r, c):
    if mask == 0:
        return (r + c) % 2 == 0
    if mask == 1:
        return r % 2 == 0
    if mask == 2:
        return c % 3 == 0
    if mask == 3:
        return (r + c) % 3 == 0
    if mask == 4:
        return (r // 2 + c // 3) % 2 == 0
    if mask == 5:
        return (r * c) % 2 + (r * c) % 3 == 0
    if mask == 6:
        return ((r * c) % 2 + (r * c) % 3) % 2 == 0
    return ((r + c) % 2 + (r * c) % 3) % 2 == 0

def _qr_finder_like(history):
    n = history[1]
    core = n > 0 and history[2] == n and history[3] == n * 3 \
        and history[4] == n and history[5] == n
    return ((1 if core and history[0] >= n * 4 and history[6] >= n else 0)
            + (1 if core and history[6] >= n * 4 and history[0] >= n else 0))

def _qr_penalty_line(line):
    """一条行/列的规则 1（连续同色）+ 规则 3（类定位图案）罚分"""
    size = len(line)
    score = 0
    run_color, run_len = False, 0
    history = [0] * 7

    def push(run):
        if history[0] == 0:
            run += size                              # 行首的虚拟白边
        history.pop()
        history.insert(0, run)

    for cell in line:
        if cell == run_color:
            run_len += 1
            if run_len == 5:
                score += 3
            elif run_len > 5:
                score += 1
        else:
            push(run_len)
            if not run_color:
                score += _qr_finder_like(history) * 40
            run_color, run_len = cell, 1
    if run_color:                                    # 收尾：最后的深色 run 和虚拟白边都计入
        push(run_len)
        run_len = 0
    push(run_len + size)
    score += _qr_finder_like(history) * 40
    return score

def _qr_penalty(mod):
    size = len(mod)
    score = 0
    for row in mod:
        score += _qr_penalty_line(row)
    for x in range(size):
        score += _qr_penalty_line([mod[y][x] for y in range(size)])
    for y in range(size - 1):                        # 规则 2：2x2 同色块
        for x in range(size - 1):
            if mod[y][x] == mod[y][x + 1] == mod[y + 1][x] == mod[y + 1][x + 1]:
                score += 3
    dark = sum(sum(row) for row in mod)              # 规则 4：黑白比例
    total = size * size
    k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
    return score + k * 10

def _qr_matrix(text, force_mask=None):
    """把文本编码成二维码模块图，返回 (模块图, 版本, 掩码)。模块图 [y][x]，True 为黑。
    已用插件自带 qrcode-terminal 的实现逐格交叉校验（强制同掩码时 10 个版本全一致）"""
    raw = text.encode("utf-8")
    version = None
    for v in range(1, 11):
        _, groups = _QR_ECM_TABLE[v]
        total_data = sum(n * d for n, d in groups)
        if 4 + (8 if v <= 9 else 16) + len(raw) * 8 <= total_data * 8:
            version = v
            break
    if version is None:
        raise ValueError(f"内容过长：{len(raw)} 字节超过版本 10-M 的容量")
    codewords = _qr_data_codewords(raw, version)
    bits = []
    for b in codewords:
        for i in range(7, -1, -1):
            bits.append((b >> i) & 1)
    bits += [0] * _QR_REMAINDER_BITS[version]
    base, func = _qr_draw_base(version)
    size = len(base)
    best = None
    for mask in (range(8) if force_mask is None else (force_mask,)):
        mod = [row[:] for row in base]
        i = 0
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    y = size - 1 - vert if ((right + 1) & 2) == 0 else vert
                    if not func[y][x]:
                        dark = i < len(bits) and bits[i] == 1
                        mod[y][x] = dark != _qr_mask_bit(mask, y, x)
                        i += 1
            right -= 2
        _qr_draw_format(mod, size, mask)
        score = _qr_penalty(mod)
        if best is None or score < best[0]:
            best = (score, mod, mask)
    return best[1], version, best[2]

def _write_qr_png(text, path, min_size=480, border=4):
    """把文本编码成二维码并写成灰度 PNG（纯标准库），边长不小于 min_size，返回边长像素数"""
    mod, _, _ = _qr_matrix(text)
    n = len(mod)
    scale = max(1, -(-min_size // (n + border * 2)))
    px = (n + border * 2) * scale
    raw = bytearray()
    for py in range(px):
        raw.append(0)                                # 每行过滤器类型 0
        my = py // scale - border
        for pxx in range(px):
            mx = pxx // scale - border
            dark = 0 <= mx < n and 0 <= my < n and mod[my][mx]
            raw.append(0 if dark else 255)

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", px, px, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    return px
