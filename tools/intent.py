#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
intent — 把一句中文指令展开成确定的动作计划

设计依据：[[work/项目方案/第二大脑设计思路]] §5.2「意图要落成协议，不是交给模型猜」。

同一句话在任何一天执行，得到同一个候选清单。模型只在填空处工作：
这段讲什么、归哪个主题、一句话简介怎么写。计划本身不含任何模型调用。

用法:
    ./tools/intent.py "把最近的截图收一下"
    ./tools/intent.py "收一下这周的" --json
    ./tools/intent.py "整理一下"
    ./tools/intent.py "找点东西 毕业论文"
    ./tools/intent.py "收这个 ~/Desktop/某文章.md"
    ./tools/intent.py --list            # 列出全部指令与别名

退出码: 0 = 已展开；2 = 未命中协议（交给兜底执行器）；1 = 参数错误
"""

import argparse
import datetime as dt
import glob
import json
import os
import re
import sys

ROOT = os.environ.get("INTENT_VAULT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INBOX = os.environ.get("INTENT_INBOX") or os.path.expanduser("~/Documents/模型对话收件箱")

# ── 材料分类：按扩展名与目录判定，规则写死，不猜 ─────────────────────────────

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".heic", ".webp", ".gif", ".tiff", ".bmp"}
PDF_EXT = {".pdf"}
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".amr"}
TEXT_EXT = {".md", ".txt", ".markdown"}
DOC_EXT = {".docx", ".doc", ".pptx", ".xlsx", ".csv"}
CHAT_EXT = {".json", ".zip"}

KIND_RULES = [
    ("截图与照片", IMAGE_EXT),
    ("网页剪藏与 PDF", PDF_EXT),
    ("语音", AUDIO_EXT),
    ("文档", DOC_EXT),
    ("AI 对话导出", CHAT_EXT),
    ("纯文本", TEXT_EXT),
]

# 落在这些目录下的材料不再重复收（已经是库内材料）
ALREADY_IN_VAULT = ("raw/", "diary/", "work/", "wiki/", "Clippings/", "基础模块/", "扩展/", "tools/")
SKIP_DIRS = (".git", ".obsidian", ".claudian", ".claude", "node_modules", "__pycache__", "tools")

# 收件目标：分类 → 落点。raw/ 是证据层（不可变），diary/ 是个人记录层。
TARGETS = {
    "截图与照片": "raw/素材/截图",
    "网页剪藏与 PDF": "raw/Clippings",
    "AI 对话导出": "diary/模型记忆库",
    "文档": "raw",
    "纯文本": "raw",
    "语音": "raw/素材/语音",
}


def kind_of(path):
    ext = os.path.splitext(path)[1].lower()
    for name, exts in KIND_RULES:
        if ext in exts:
            return name
    return "其他"


def rel(p):
    try:
        return os.path.relpath(p, ROOT)
    except ValueError:
        return p


def walk_materials(roots, since, until):
    """在给定根目录下找时间窗内的材料文件；不含已在库内的路径。"""
    out = []
    for root in roots:
        root = os.path.expanduser(root)
        if not os.path.exists(root):
            continue
        if os.path.isfile(root):
            m = os.path.getmtime(root)
            if since <= dt.datetime.fromtimestamp(m) <= until:
                out.append(os.path.abspath(root))
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if fn.startswith(".") or fn == ".DS_Store":
                    continue
                p = os.path.join(dirpath, fn)
                m = dt.datetime.fromtimestamp(os.path.getmtime(p))
                if since <= m <= until:
                    out.append(os.path.abspath(p))
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return sorted(uniq)


def registered_sources():
    """已被 wiki/来源 的 sources 覆盖的材料路径（去掉 .md）。"""
    registered = set()
    for f in glob.glob(os.path.join(ROOT, "wiki/来源/*.md")):
        try:
            text = open(f, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        m = re.search(r"^sources:\s*(.+)$", text, re.M)
        if not m:
            continue
        for s in re.findall(r"[^\[\],\"]+", m.group(1)):
            s = s.strip()
            if s:
                registered.add(s[:-3] if s.endswith(".md") else s)
    return registered


def scan_clippings():
    """Clippings/ 与 raw/Clippings/ 下尚未登记的剪藏。"""
    reg = registered_sources()
    out = []
    for d in ("Clippings", "raw/Clippings"):
        base = os.path.join(ROOT, d)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS]
            for fn in filenames:
                if not fn.endswith(".md"):
                    continue
                p = os.path.join(dirpath, fn)
                key = rel(p)[:-3]
                if key not in reg:
                    out.append(p)
    return sorted(out)


# ── 时间窗解析：只认这几种写法，认不出就用默认值并说明 ──────────────────────

WINDOW_WORDS = [
    (r"今天|今日", 1),
    (r"最近|这几天|近期", 7),
    (r"这周|本周|这一周", 7),
    (r"上周|上一周", 14),
    (r"这个月|本月", 30),
    (r"最近一个月|这一个月", 30),
]


def parse_window(text, now=None):
    # INTENT_NOW 让测试与回放可以固定时钟；日常使用不设，取本地当前时间
    if now is None and os.environ.get("INTENT_NOW"):
        try:
            now = dt.datetime.fromisoformat(os.environ["INTENT_NOW"])
        except ValueError:
            now = None
    now = now or dt.datetime.now()
    days, label = 7, "最近 7 天"
    for pat, n in WINDOW_WORDS:
        if re.search(pat, text):
            days = n
            label = re.search(pat, text).group(0)
            break
    m = re.search(r"(\d+)\s*(天|日|周|个?月)", text)
    if m:
        num, unit = int(m.group(1)), m.group(2)
        days = num * (1 if unit in ("天", "日") else 7 if unit == "周" else 30)
        label = f"最近 {num}{unit}"
    since = now - dt.timedelta(days=days)
    # 时间窗按自然日对齐，保证同一天内重复执行结果一致
    return since.replace(hour=0, minute=0, second=0, microsecond=0), now, label, days


# ── 指令协议 ────────────────────────────────────────────────────────────────

def plan_collect(text, kwargs):
    since, until, label, days = parse_window(text, kwargs.get("now"))
    roots = [INBOX] + kwargs.get("extra_roots", [])
    mats = walk_materials(roots, since, until)
    clippings = [p for p in scan_clippings() if since <= dt.datetime.fromtimestamp(os.path.getmtime(p)) <= until]
    inbox = os.path.abspath(INBOX)

    def source_of(p):
        ap = os.path.abspath(p)
        if ap == inbox or ap.startswith(inbox + os.sep):
            return "收件箱"
        if ap.startswith(ROOT + os.sep):
            return "库内剪藏"
        return "外部目录"

    items = [{"path": rel(p) if os.path.abspath(p).startswith(ROOT + os.sep) else os.path.abspath(p),
              "kind": kind_of(p),
              "target": TARGETS.get(kind_of(p), "raw/待分类"),
              "source": source_of(p)}
             for p in mats + clippings]
    by_kind = {}
    for it in items:
        by_kind.setdefault(it["kind"], []).append(it["path"])
    return {
        "command": "收一下",
        "purpose": "把时间窗内的新材料纳入库内，按类型落到证据层",
        "window": {"from": since.date().isoformat(), "to": until.date().isoformat(), "label": label},
        "roots": [rel(r) if r.startswith(ROOT) else r for r in roots],
        "items": items,
        "summary": {k: len(v) for k, v in sorted(by_kind.items())},
        "writes": sorted({it["target"] for it in items}),
        "skip_rules": [
            "已在 raw/ diary/ work/ wiki/ 下的文件不重复收",
            ".DS_Store 与点文件跳过",
            "已有等价的 wiki/来源 页面时跳过（按 sources 字段判重）",
            "命中秘密模式的文件不读、不写、不回执",
        ],
        "needs_decision": _needs_decision(items),
    }


def _needs_decision(items):
    """只列出「需要人拍板」的情况；其余视为可自动执行。"""
    out = []
    if not items:
        out.append("时间窗内没有新材料：确认是否放宽时间范围或换目录")
    kinds = {it["kind"] for it in items}
    if "其他" in kinds:
        out.append("有无法按扩展名分类的文件，需指定落点")
    if "AI 对话导出" in kinds:
        out.append("对话导出需确认平台与课题名（同一课题要沿用已有名称）")
    if "语音" in kinds:
        out.append("语音需确认转写方式：手动听写或接国内 ASR（付费）")
    return out


def plan_tidy(text, kwargs):
    since, until, label, days = parse_window(text, kwargs.get("now"))
    issues = planned_checks()
    return {
        "command": "整理一下",
        "purpose": "把库内已有的结构问题列成可执行清单，先报后修",
        "window": {"from": since.date().isoformat(), "to": until.date().isoformat(), "label": label},
        "checks": issues["checks"],
        "writes": issues["writes"],
        "skip_rules": ["raw/ 与 diary/ 只在明确范围内整理", "冲突双方都保留，只更新判断"],
        "needs_decision": issues["needs_decision"],
    }


def planned_checks():
    """整理指令固定跑这几项；命令与 tools/vault 一致，避免两套规则。"""
    tool = os.path.join(ROOT, "tools", "vault")
    return {
        "checks": [
            {"cmd": f"{tool} check", "role": "全库健康检查，退出码 1 = 有 ERROR"},
            {"cmd": f"{tool} uningested", "role": "尚未摄入的剪藏"},
            {"cmd": f"{tool} links", "role": "Wiki-link 可解析性"},
            {"cmd": f"{tool} stubs", "role": "来源存根（空壳/偏薄）"},
            {"cmd": f"{tool} independence", "role": "来源独立性"},
        ],
        "writes": ["按检查结果分项修复；每项修复前先报告拟改范围"],
        "needs_decision": ["ERROR 项是否本轮修复，由本人一句话决定"],
    }


def plan_find(text, kwargs):
    m = re.search(r"(?:找点东西|找找|搜一下)\s*(.+)$", text)
    kw = (m.group(1).strip() if m else "").strip(" ：:，,")
    if not kw:
        return None, "缺少检索关键词"
    tool = os.path.join(ROOT, "tools", "vault")
    return {
        "command": "找点东西",
        "purpose": "只读检索，不写文件、不记日志",
        "keyword": kw,
        "checks": [{"cmd": f'{tool} find "{kw}"', "role": "按关键词定位库内材料"}],
        "writes": [],
        "skip_rules": ["只读；结果按原文路径交付，不改写"],
        "needs_decision": [],
    }, None


def plan_brief(text, kwargs):
    since, until, label, days = parse_window(text, kwargs.get("now"))
    return {
        "command": "更新简报",
        "purpose": "生成当日简报与待决清单",
        "window": {"from": since.date().isoformat(), "to": until.date().isoformat(), "label": label},
        "sections": [
            "昨日入库材料（按类型与落点计数）",
            "新增或修改的知识页",
            "待我拍板：概念页升级候选 / 旧结论冲突 / 画像变更 / 归属不明",
            "检查结果：ERROR 与 WARN 的增减",
            "本月 API 费用与剩余额度",
        ],
        "writes": ["work/项目方案/每日简报.md 的当日区块"],
        "skip_rules": ["简报只写结构与计数，不引原文"],
        "needs_decision": [],
    }


def plan_intake_one(text, kwargs):
    m = re.search(r"(?:收这个|收藏这个|收下这个)\s*(.+)$", text)
    target = (m.group(1).strip() if m else "").strip(" ：:，,")
    if not target:
        return None, "缺少要收录的对象（路径或链接）"
    target = os.path.expanduser(target)
    is_url = bool(re.match(r"^https?://", target))
    exists = os.path.exists(target)
    if not is_url and not exists:
        return None, f"路径不存在：{target}"
    kind = "网页剪藏与 PDF" if is_url else kind_of(target)
    return {
        "command": "收这个",
        "purpose": "单条材料的定向收录，不等时间窗",
        "items": [{"path": target if not target.startswith(ROOT) else rel(target),
                   "kind": kind,
                   "target": TARGETS.get(kind, "raw/待分类"),
                   "source": "链接" if is_url else "本机文件"}],
        "writes": [TARGETS.get(kind, "raw/待分类")],
        "skip_rules": ["按来源路径去重", "已有等价来源页时只追加，不新建"],
        "needs_decision": ["远程链接需确认：是否允许抓取（会访问外部服务）"] if is_url else [],
    }


COMMANDS = [
    (r"收这个|收藏这个|收下这个", plan_intake_one),
    (r"收一下|收一下最近|把.*收一下|收录|入库", plan_collect),
    (r"整理一下|整理整理|整理下|清理一下", plan_tidy),
    (r"找点东西|找找|搜一下", plan_find),
    (r"更新简报|生成简报|简报", plan_brief),
]


def expand(text, **kwargs):
    for pat, fn in COMMANDS:
        if re.search(pat, text):
            res = fn(text, kwargs)
            if isinstance(res, tuple):
                plan, err = res
                if err:
                    return {"command": None, "unhandled": True, "reason": err}
                return plan
            return res
    return {
        "command": None,
        "unhandled": True,
        "reason": "未命中固定协议，按设计交给兜底执行器（DSH headless）",
        "protocols": [p for p, _ in COMMANDS],
    }


def human(plan):
    if plan.get("unhandled"):
        return f"\n  未命中协议：{plan['reason']}\n  已注册指令：{'、'.join(plan.get('protocols', []))}\n"
    lines = [f"\n  {plan['command']} — {plan['purpose']}\n"]
    if "window" in plan:
        w = plan["window"]
        lines.append(f"  时间窗：{w['from']} → {w['to']}（{w['label']}）")
    if plan.get("items"):
        lines.append(f"  候选 {len(plan['items'])} 项：" + "，".join(f"{k} {v}" for k, v in plan.get("summary", {}).items()))
        for it in plan["items"][:20]:
            lines.append(f"    · [{it['kind']}] {it['path']} → {it['target']}")
        if len(plan["items"]) > 20:
            lines.append(f"    …另有 {len(plan['items']) - 20} 项")
    for c in plan.get("checks", []):
        lines.append(f"  · {c['cmd']}")
    if plan.get("writes"):
        lines.append("  将写入：" + "、".join(plan["writes"]))
    if plan.get("needs_decision"):
        lines.append("  需你拍板：")
        for n in plan["needs_decision"]:
            lines.append(f"    ? {n}")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="把一句中文指令展开成确定的动作计划")
    ap.add_argument("text", nargs="*", help="指令原文")
    ap.add_argument("--json", action="store_true", help="输出 JSON 计划")
    ap.add_argument("--list", action="store_true", help="列出全部指令与别名")
    ap.add_argument("--root", action="append", default=[], help="额外的材料扫描根目录（可重复）")
    a = ap.parse_args()

    if a.list:
        for pat, _ in COMMANDS:
            print("  " + pat)
        return 0
    if not a.text:
        ap.print_help()
        return 1

    plan = expand(" ".join(a.text), extra_roots=[os.path.expanduser(r) for r in a.root])
    if a.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print(human(plan))
    return 2 if plan.get("unhandled") else 0


if __name__ == "__main__":
    sys.exit(main())
