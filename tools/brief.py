#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
brief — 每日简报 + 待决清单

设计依据：[[work/项目方案/第二大脑设计思路]] §6「留人的部分必须便宜」。
两版输出：
  库内版 → work/项目方案/每日简报.md（可以写结构与路径）
  飞书版 → 只写计数与目录，不出现任何原文（用 --feishu 打印）

用法:
    ./tools/brief.py                # 写入库内简报并打印飞书摘要
    ./tools/brief.py --feishu       # 只打印飞书摘要
    ./tools/brief.py --decide 2 ok  # 处理待决清单第 2 条（同意）
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HOME = Path.home()
ROOT = Path(os.environ.get("WORKER_VAULT") or Path(__file__).resolve().parents[1])
STATE = Path(os.environ.get("WORKER_STATE") or HOME / ".local/share/obsidian-memory-kit/worker")
PENDING = STATE / "pending.json"
LEDGER = STATE / "ledger.json"
COST = STATE / "cost.json"
BRIEF = ROOT / "work" / "项目方案" / "每日简报.md"
VAULT = ROOT / "tools" / "vault"

CAP_CNY = float(os.environ.get("WORKER_MONTHLY_CAP_CNY", "50"))


def load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def save(path, obj):
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def vault_check():
    try:
        out = subprocess.run([sys.executable, str(VAULT), "check"], capture_output=True, text=True, timeout=180)
        text = out.stdout
    except Exception as e:
        return {"error": str(e), "errors": None, "warns": []}
    err = re.search(r"ERROR\s+(\d+)", text)
    warns = []
    m = re.search(r"WARN\s+(\d+)\n((?:\s+·.*\n)+)", text)
    if m:
        warns = [line.strip().lstrip("· ").strip() for line in m.group(2).splitlines() if line.strip()]
    details = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("· "):
            details.append(s[2:])
    return {"errors": int(err.group(1)) if err else None, "warns": warns, "details": details}


def uningested_count():
    try:
        out = subprocess.run([sys.executable, str(VAULT), "uningested"], capture_output=True, text=True, timeout=120)
        m = re.search(r"共\s*(\d+)\s*项待处理", out.stdout)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def today_archived():
    led = load(LEDGER, {"done": {}})
    today = date.today().isoformat()
    rows = []
    for path, h in (led.get("done") or {}).items():
        try:
            m = datetime.fromtimestamp(Path(path).stat().st_mtime)
        except Exception:
            continue
        if m.date().isoformat() == today:
            rows.append(Path(path).name)
    return len(rows), rows


def pending_items():
    return load(PENDING, {"items": []}).get("items", [])


def month_spent():
    st = load(COST, {})
    return float((st.get("months") or {}).get(date.today().strftime("%Y-%m"), 0.0))


def render(feishu_only=False):
    chk = vault_check()
    n_today, names = today_archived()
    pending = pending_items()
    uning = uningested_count()
    spent = month_spent()
    now = datetime.now()

    if feishu_only:
        lines = [f"今日归档 {n_today} 条，待我拍板 {len(pending)} 条。"]
        if chk["errors"] is not None:
            lines.append(f"检查：ERROR {chk['errors']}。")
        lines.append(f"本月 API 费用 {spent:.2f} 元（上限 {CAP_CNY:.0f}）。")
        if pending:
            lines.append("回「第 N 条 同意/不行」即可处理。")
        return "\n".join(lines)

    L = [f"# 每日简报 · {now:%Y-%m-%d}", "", f"> 自动生成于 {now:%H:%M}。本页只写结构与计数，不引原文。", "",
         "## 今日入库", ""]
    L.append(f"- 新材料 {n_today} 条" + ("：" + "、".join(names[:10]) if names else ""))
    if uning is not None:
        L.append(f"- 尚未摄入的剪藏：{uning} 项")
    L += ["", "## 检查结果", ""]
    if chk["errors"] is None:
        L.append("- 检查未执行成功")
    else:
        L.append(f"- ERROR {chk['errors']} 项")
        for w in chk["warns"]:
            L.append(f"- WARN：{w}")
    L += ["", "## 待我拍板", ""]
    if not pending:
        L.append("- 暂无。")
    else:
        for i, it in enumerate(pending, 1):
            L.append(f"{i}. **{it.get('kind', '待定')}** · {it.get('what', '')}")
            if it.get("why"):
                L.append(f"   - 理由：{it['why']}")
            if it.get("options"):
                L.append(f"   - 可选：{' / '.join(it['options'])}")
    L += ["", "## 费用", "", f"- 本月已用 {spent:.2f} 元，上限 {CAP_CNY:.0f} 元。", ""]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feishu", action="store_true")
    ap.add_argument("--decide", nargs="+", help="第 N 条 + 决定")
    a = ap.parse_args()

    if a.decide:
        idx = int(a.decide[0]) - 1
        decision = " ".join(a.decide[1:]) or "同意"
        st = load(PENDING, {"items": [], "done": []})
        items = st.get("items", [])
        if not (0 <= idx < len(items)):
            print(f"没有第 {idx + 1} 条"); return 1
        it = items.pop(idx)
        st.setdefault("done", []).append({"at": datetime.now().isoformat(timespec="seconds"),
                                          "item": it, "decision": decision})
        save(PENDING, st)
        print(f"已记录决定：第 {idx + 1} 条 → {decision}。需要执行的动作已排队，下次运行处理。")
        return 0

    text = render(feishu_only=a.feishu)
    print(text)
    if not a.feishu:
        BRIEF.parent.mkdir(parents=True, exist_ok=True)
        tmp = BRIEF.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, BRIEF)
    return 0


if __name__ == "__main__":
    sys.exit(main())
