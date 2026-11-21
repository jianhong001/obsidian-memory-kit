#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
intent 协议的固定用例。

原则：同一指令、同一时间点，结果必须完全一致；时间窗边界必须确定；
认不出的指令必须明确返回 unhandled，而不是猜。

运行：python3 tools/tests/test_intent.py
"""

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOL = os.path.join(REPO, "tools", "intent.py")
FIXED_NOW = dt.datetime(2026, 11, 21, 12, 0, 0)


def run(text, now=None, roots=()):
    """以固定时钟调用 CLI，返回 (exit_code, plan_dict)。"""
    env = dict(os.environ)
    if now:
        env["INTENT_NOW"] = now.isoformat()
    args = [sys.executable, TOOL, text, "--json"]
    for r in roots:
        args += ["--root", r]
    p = subprocess.run(args, capture_output=True, text=True, env=env)
    try:
        plan = json.loads(p.stdout)
    except json.JSONDecodeError:
        plan = {"_stdout": p.stdout, "_stderr": p.stderr}
    return p.returncode, plan


class Determinism(unittest.TestCase):
    def test_same_command_same_plan(self):
        a = run("整理一下", FIXED_NOW)[1]
        b = run("整理一下", FIXED_NOW)[1]
        self.assertEqual(a, b)

    def test_collect_is_stable(self):
        a = run("把最近的截图收一下", FIXED_NOW)[1]
        b = run("把最近的截图收一下", FIXED_NOW)[1]
        self.assertEqual(a["items"], b["items"])


class Windows(unittest.TestCase):
    def test_default_is_seven_days(self):
        _, plan = run("收一下", FIXED_NOW)
        self.assertEqual(plan["window"]["from"], "2026-11-14")
        self.assertEqual(plan["window"]["to"], "2026-11-21")

    def test_explicit_days(self):
        _, plan = run("收录最近 3 天的", FIXED_NOW)
        self.assertEqual(plan["window"]["from"], "2026-11-18")

    def test_explicit_week(self):
        _, plan = run("收一下最近 2 周", FIXED_NOW)
        self.assertEqual(plan["window"]["from"], "2026-11-07")

    def test_month(self):
        _, plan = run("整理一下这个月的", FIXED_NOW)
        self.assertEqual(plan["window"]["from"], "2026-10-22")


class Dispatch(unittest.TestCase):
    def test_unknown_is_unhandled(self):
        code, plan = run("帮我把论文那个东西想一下", FIXED_NOW)
        self.assertEqual(code, 2)
        self.assertTrue(plan["unhandled"])

    def test_find_requires_keyword(self):
        code, plan = run("找点东西", FIXED_NOW)
        self.assertEqual(code, 2)
        self.assertTrue(plan["unhandled"])

    def test_find_with_keyword(self):
        code, plan = run("找点东西 毕业论文", FIXED_NOW)
        self.assertEqual(code, 0)
        self.assertEqual(plan["keyword"], "毕业论文")

    def test_tidy_is_read_first(self):
        _, plan = run("整理一下", FIXED_NOW)
        self.assertEqual(plan["writes"][0][:4], "按检查结")  # 先报后修：writes 不是具体文件

    def test_missing_path_is_unhandled(self):
        code, plan = run("收这个 /definitely/not/here.md", FIXED_NOW)
        self.assertEqual(code, 2)
        self.assertTrue(plan["unhandled"])


class Collect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def touch(self, name, age_days):
        p = os.path.join(self.tmp, name)
        open(p, "w").write("x")
        t = (FIXED_NOW - dt.timedelta(days=age_days)).timestamp()
        os.utime(p, (t, t))
        return p

    def test_only_window_files(self):
        self.touch("new.png", 1)
        self.touch("old.png", 30)
        _, plan = run("收一下", FIXED_NOW, roots=[self.tmp])
        names = [os.path.basename(i["path"]) for i in plan["items"]]
        self.assertIn("new.png", names)
        self.assertNotIn("old.png", names)

    def test_kind_targets(self):
        self.touch("a.png", 1)
        self.touch("b.pdf", 1)
        self.touch("c.m4a", 1)
        self.touch("d.zip", 1)
        _, plan = run("收一下", FIXED_NOW, roots=[self.tmp])
        targets = {os.path.basename(i["path"]): i["target"] for i in plan["items"]}
        self.assertEqual(targets["a.png"], "raw/素材/截图")
        self.assertEqual(targets["b.pdf"], "raw/Clippings")
        self.assertEqual(targets["c.m4a"], "raw/素材/语音")
        self.assertEqual(targets["d.zip"], "diary/模型记忆库")

    def test_empty_result_asks_for_decision(self):
        # 测试库为空且清空库内剪藏影响时不适用；改用根本不存在的新目录
        empty = os.path.join(self.tmp, "nothing")
        os.makedirs(empty)
        _, plan = run("收一下 最近 1 天", FIXED_NOW, roots=[empty])
        self.assertTrue(any("没有新材料" in d for d in plan["needs_decision"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
