#!/usr/bin/env python3
"""微信网关源码入口，支持在未安装的源码目录运行。"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from wechat_gateway.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
