#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日简报 + 待决清单的固定用例。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TOOLS = REPO / "tools"
vault = REPO / "vault"
STATE = Path(tempfile.mkdtemp(prefix="mk-state-"))
os.environ["WORKER_STATE"] = str(STATE)


def run(script, *args, env=None):
    e = dict(os.environ)
    e["WORKER_STATE"] = str(STATE)
    e["WORKER_VAULT"] = str(vault)
    e.update(env or {})
    return subprocess.run([sys.executable, str(TOOLS / script), *args],
                          capture_output=True, text=True, env=e, timeout=240)


class Brief(unittest.TestCase):
    def test_feishu_has_no_paths(self):
        out = run("brief.py", "--feishu").stdout
        self.assertNotIn("wiki/", out)
        self.assertNotIn("raw/", out)
        self.assertNotIn("diary/", out)
        self.assertRegex(out, r"今日归档 \d+ 条")

    def test_feishu_mentions_budget(self):
        out = run("brief.py", "--feishu").stdout
        self.assertIn("上限 50", out)

    def test_in_file_brief_written(self):
        brief = vault / "work" / "项目方案" / "每日简报.md"
        before = brief.read_text() if brief.exists() else None
        run("brief.py")
        self.assertTrue(brief.exists())
        text = brief.read_text()
        self.assertIn("# 每日简报", text)
        self.assertIn("## 待我拍板", text)
        if before is None:
            brief.unlink()


class Decide(unittest.TestCase):
    def setUp(self):
        self.state = STATE
        self.pending = self.state / "pending.json"
        self.backup = self.pending.read_text() if self.pending.exists() else None
        self.state.mkdir(parents=True, exist_ok=True)
        self.pending.write_text(json.dumps({"items": [
            {"kind": "蒸馏候选", "what": "测试条目一", "why": "", "options": []},
            {"kind": "归属不明", "what": "测试条目二", "why": "", "options": []},
        ]}, ensure_ascii=False))

    def tearDown(self):
        if self.backup is None:
            self.pending.unlink(missing_ok=True)
        else:
            self.pending.write_text(self.backup)

    def test_decide_removes_item(self):
        out = run("brief.py", "--decide", "1", "同意")
        self.assertEqual(out.returncode, 0)
        left = json.loads(self.pending.read_text())["items"]
        self.assertEqual([i["what"] for i in left], ["测试条目二"])

    def test_decide_out_of_range(self):
        out = run("brief.py", "--decide", "9", "同意")
        self.assertEqual(out.returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
