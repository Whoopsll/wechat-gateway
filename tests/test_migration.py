"""验证原始定义完整；已单独修复的函数由对应回归测试覆盖。"""

import ast
from html.parser import HTMLParser
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SourceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.line = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "code" and attrs.get("id") == "python-code":
            self.active = True
        if self.active and tag == "span" and attrs.get("class") == "line-code":
            self.line = True

    def handle_data(self, data):
        if self.active and self.line:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "span" and self.line:
            self.parts.append("\n")
            self.line = False
        if tag == "code":
            self.active = False


class MigrationTests(unittest.TestCase):
    def test_original_business_definitions_preserved(self):
        parser = SourceParser()
        parser.feed((ROOT / "微信网关代码.html").read_text())
        original = {
            node.name: ast.dump(node)
            for node in ast.parse("".join(parser.parts)).body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        }
        migrated = {}
        for path in (ROOT / "src/wechat_gateway").glob("*.py"):
            for node in ast.parse(path.read_text()).body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name != "main":
                    self.assertNotIn(node.name, migrated, f"重复定义：{node.name}")
                    migrated[node.name] = ast.dump(node)
        self.assertGreater(len(original), 80)
        self.assertFalse(original.keys() - migrated.keys(), "原始定义不得遗漏")
        self.assertEqual(migrated.keys() - original.keys(), {"CurlTransportError"})
        fixed = {
            "_parse_curl_headers",  # GW-5：正文边界
            "_curl_request", "_sendmessage",  # GW-3：发送不确定性
            "send_text", "send_reply",  # GW-1：认领代次
            "on_wechat_message", "save_updates_buf",  # GW-2：入库与游标
            "poll_loop",  # GW-2 / GW-4：重放和凭据刷新
        }
        # 已修复定义由专项回归覆盖；其余业务函数继续与原快照逐一对比。
        for name in original.keys() - fixed:
            with self.subTest(name=name):
                self.assertEqual(original[name], migrated[name])
