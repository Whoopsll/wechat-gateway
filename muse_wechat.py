#!/usr/bin/env python3
"""兼容旧脚本调用路径，也支持在未安装的源码目录运行。"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from muse_wechat.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
