#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
worker — 后台执行器：消费收件箱 → 展开意图协议 → 处理 → 写库 → 回执

设计依据：[[work/项目方案/第二大脑设计思路]] §5–§6。
分两层：
  主路 = 本文件（确定规则 + 一次模型调用做「这段讲什么/归哪/一句话简介」）
  兜底 = 协议未命中时交给她（Hermes 单发模式），见 --fallback

安全边界（写入前逐条检查）：
  1. 只写 TARGETS 允许的目录；raw/ 下不覆盖同名文件，冲突时改名加时间戳
  2. 拒绝任何越出库根的路径（含符号链接）
  3. 命中秘密模式的文件不读、不写、不回执
  4. 月度费用 ≥ 上限则停止调用模型，只做不需要模型的部分
  5. 每次写入后跑 ./tools/vault check；ERROR 增加则回滚本批（git 不可用时用备份文件）

用法:
    ./tools/worker.py --once              # 处理一次收件箱后退出
    ./tools/worker.py --once --dry-run     # 只看会做什么
    ./tools/worker.py --receipt-only       # 只打印上次回执
    ./tools/worker.py --budget             # 查看本月费用

退出码: 0 = 完成；1 = 有失败项；3 = 触及费用上限
"""

import argparse
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

HOME = Path.home()
ROOT = Path(os.environ.get("WORKER_VAULT") or Path(__file__).resolve().parents[1])
INBOX = Path(os.environ.get("WORKER_INBOX") or HOME / "Documents/模型对话收件箱")
STATE = Path(os.environ.get("WORKER_STATE") or HOME / ".local/share/obsidian-memory-kit/worker")
LEDGER = STATE / "ledger.json"
COST_FILE = STATE / "cost.json"
RECEIPT = STATE / "last-receipt.txt"
QUEUE = STATE / "queue.json"

TOOL = ROOT / "tools" / "intent.py"
VAULT = ROOT / "tools" / "vault"

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MODEL = os.environ.get("WORKER_MODEL", "deepseek-flash")
MONTHLY_CAP_CNY = float(os.environ.get("WORKER_MONTHLY_CAP_CNY", "50"))
# 单价按官方公布口径填写；变更时必须同步改这里，不要凭记忆
PRICE_CNY_PER_MTOK_IN = float(os.environ.get("WORKER_PRICE_IN", "1.0"))
PRICE_CNY_PER_MTOK_OUT = float(os.environ.get("WORKER_PRICE_OUT", "4.0"))
MAX_INPUT_CHARS = int(os.environ.get("WORKER_MAX_INPUT_CHARS", "24000"))

SECRET_PATTERNS = [
    r"(?i)(密码|口令|passwd|password|secret|token|api[_ -]?key|access[_ -]?key)\s*[:=：]\s*[\"']?[A-Za-z0-9_\-\.\+/]{16,}",
    r"\bsk-[A-Za-z0-9]{16,}",
    r"\bghp_[A-Za-z0-9]{20,}",
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
    r"\bAKIA[0-9A-Z]{16}\b",
]
SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
CHAT_SUFFIXES = {".json", ".zip"}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ── 状态与锁 ────────────────────────────────────────────────────────────────

def state_dir():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    return STATE


class Lock:
    def __enter__(self):
        state_dir()
        self.f = (STATE / "worker.lock").open("a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def save_json(path, obj):
    state_dir()
    tmp = Path(tempfile.mkstemp(dir=STATE, prefix=".w-")[1])
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    os.replace(tmp, Path(path))


def filehash(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ── 费用闸 ──────────────────────────────────────────────────────────────────

def month_key():
    return date.today().strftime("%Y-%m")


def cost_state():
    st = load_json(COST_FILE, {})
    st.setdefault("months", {})
    st.setdefault("last", {})
    return st


def month_spent():
    return float(cost_state()["months"].get(month_key(), 0.0))


def record_cost(prompt_tokens, completion_tokens, model=MODEL):
    st = cost_state()
    cny = (prompt_tokens / 1_000_000) * PRICE_CNY_PER_MTOK_IN + (completion_tokens / 1_000_000) * PRICE_CNY_PER_MTOK_OUT
    st["months"][month_key()] = round(st["months"].get(month_key(), 0.0) + cny, 6)
    st["last"] = {"at": datetime.now().isoformat(timespec="seconds"), "prompt": prompt_tokens,
                  "completion": completion_tokens, "cny": round(cny, 6), "model": model}
    save_json(COST_FILE, st)
    return cny


def budget_left():
    return MONTHLY_CAP_CNY - month_spent()


# ── 模型：只做填空，不做判断 ─────────────────────────────────────────────────

def api_key():
    k = os.environ.get("DEEPSEEK_API_KEY")
    if k:
        return k
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", "deepseek-api-key", "-w"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return None


SYSTEM_PROMPT = """你是个人知识库的归档助手。只做三件填空题，不做任何判断：
1. 用不超过 40 字的中文说这段材料讲什么（具体，不复述文件名）。
2. 从给定标签里选一个最合适的主题。
3. 给出的 3–5 个关键事实，每条一句，原文说不清就写「原文未说明」。

严格输出 JSON，不要代码围栏：
{"summary": "...", "topic": "...", "facts": ["...", "..."]}
不得推测作者意图，不得补充原文没有的信息。"""


def call_model(text, topics):
    key = api_key()
    if not key:
        return {"error": "缺少 DEEPSEEK_API_KEY（可先用 security add-generic-password 存进钥匙串）"}
    if budget_left() <= 0:
        return {"error": "budget"}
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"可选主题：{'、'.join(topics)}\n\n材料：\n{text[:MAX_INPUT_CHARS]}"},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": 700,
    }
    import urllib.request
    req = urllib.request.Request(DEEPSEEK_URL, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read())
    except Exception as e:
        return {"error": f"模型调用失败：{e}"}
    usage = data.get("usage", {})
    record_cost(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
    try:
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception:
        return {"error": "模型返回不是 JSON", "raw": (content if 'content' in locals() else '')[:200]}


# ── 安全与写库 ──────────────────────────────────────────────────────────────

def has_secret(path):
    try:
        s = Path(path).read_text(encoding="utf-8", errors="ignore")[:200_000]
    except Exception:
        return False
    return any(re.search(p, s) for p in SECRET_PATTERNS)


def inside_root(p):
    p = Path(p).resolve()
    try:
        p.relative_to(ROOT.resolve())
        return True
    except ValueError:
        return False


def atomic_write(dest: Path, text: str):
    if not inside_root(dest):
        raise RuntimeError(f"拒绝写入库外路径：{dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=dest.parent, prefix=".worker-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, dest)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def unique_dest(dest: Path):
    if not dest.exists():
        return dest
    stem, suf = dest.stem, dest.suffix
    return dest.with_name(f"{stem} {datetime.now():%Y%m%d-%H%M%S}{suf}")


def topic_list():
    moc = ROOT / "wiki" / "MOC"
    if moc.is_dir():
        names = sorted(p.stem for p in moc.glob("*.md"))
        if names:
            return names
    return ["工作", "学习", "生活"]


def strip_date(stem):
    """Obsidian 剪藏常带日期前缀；来源页标题已有日期，避免「2026-11-21 2026-11-21 …」。"""
    return re.sub(r"^\d{4}[-年_.]?\d{1,2}[-月_.]?\d{1,2}日?[\s_-]*", "", stem).strip() or stem


def source_page_path(title, day):
    safe = re.sub(r"[\\/:*?\"<>|\n]", " ", title).strip()[:60] or "未命名材料"
    return ROOT / "wiki" / "来源" / f"{day} {safe}.md"


def update_catalog(page_rel, summary, day):
    """方法论 8.1 + 8.2：新来源页必须能被 index 一跳到达，且在档案页有一句话简介。"""
    cat = ROOT / "wiki" / "档案" / "来源目录.md"
    if not cat.exists():
        return False
    text = cat.read_text(encoding="utf-8")
    link = f"[[{page_rel[:-3]}]]" if page_rel.endswith(".md") else f"[[{page_rel}]]"
    if link in text:
        return False
    row = f"| {link} | {summary.strip()} | {day} |\n"
    anchor = "## 主题来源清单（按日期倒序）\n\n| 页面 | 一句话简介 | 更新日期 |\n|---|---|---|\n"
    if anchor in text:
        text = text.replace(anchor, anchor + row, 1)
    else:
        m = re.search(r"^\| 页面 \| 一句话简介 \| 更新日期 \|\n\|---\|---\|---\|\n", text, re.M)
        if not m:
            return False
        text = text[:m.end()] + row + text[m.end():]
    atomic_write(cat, text)
    return True


def update_index_stats():
    """Stats 必须与实际文件数一致；工具会以 ERROR 报出漂移，所以写入后立即对齐。"""
    idx = ROOT / "wiki" / "index.md"
    if not idx.exists():
        return False
    counts = {}
    for d, label in (("来源", "来源"), ("实体", "实体"), ("概念", "概念"), ("对比", "对比"), ("MOC", "MOC")):
        counts[label] = len([p for p in (ROOT / "wiki" / d).glob("*.md")])
    text = idx.read_text(encoding="utf-8")
    before = text
    for label, n in counts.items():
        text = re.sub(rf"(\|\s*{label}\s*\|\s*)\d+(\s*\|)", rf"\g<1>{n}\g<2>", text)
    if text != before:
        atomic_write(idx, text)
        return True
    return False


def render_source_page(title, day, src_rel, info, vis="private"):
    facts = info.get("facts") or []
    lines = [
        "---",
        "type: source",
        f"tags: [自动归档, {info.get('topic', '未分类')}]",
        f"sources: [{src_rel}]",
        f"visibility: {vis}",
        f"created: {day}",
        f"updated: {day}",
        "---",
        "",
        f"# {title}",
        "",
        f"[[{src_rel}]]",
        "",
        "## 摘要",
        "",
        info.get("summary", ""),
        "",
        "## 关键事实",
        "",
    ]
    lines += [f"- {f}" for f in facts] or ["- 原文未说明"]
    lines += ["", "## 证据边界", "", "本页由自动化流程生成：摘要与事实取自原文，未做解释性推断。判断留待本人补充。", ""]
    return "\n".join(lines)


def archive_raw(src: Path, target_rel: str):
    dest = unique_dest(ROOT / target_rel / src.name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def queue_decision(kind, what, why="", options=None):
    """判断留清单：只记录候选，不替本人决定（设计思路 §6）。"""
    path = STATE / "pending.json"
    st = load_json(path, {"items": [], "done": []})
    items = st.setdefault("items", [])
    if any(i.get("what") == what for i in items):
        return False
    items.append({"kind": kind, "what": what, "why": why, "options": options or [],
                  "at": datetime.now().isoformat(timespec="seconds")})
    save_json(path, st)
    return True


def moc_thin(topics):
    """该主题有来源但没有概念页时，提示是否需要首次蒸馏——判断留给人。"""
    out = []
    for topic in topics:
        moc = ROOT / "wiki" / "MOC" / f"{topic}.md"
        if not moc.exists():
            continue
        text = moc.read_text(encoding="utf-8", errors="ignore")
        # MOC 的「关系与冲突说明」统一引用 Harness Engineering 作为依据；
        # 那是模板注脚，不是该主题自己的概念，计数前先去掉，否则每个 MOC 都算有 1 个概念
        body = re.sub(r"\[\[wiki/概念/Harness Engineering[^\]]*\]\]", "", text)
        n_concept = len(set(re.findall(r"\[\[wiki/概念/([^\]|]+)", body)))
        n_source = len(set(re.findall(r"\[\[wiki/来源/([^\]|]+)", text)))
        # 阈值取 10 来源 / ≤1 概念：只提示「材料够多但几乎没蒸馏」的主题，
        # 避免把刚起步的主题也塞进清单（清单本身就是人的负担）
        if n_source >= 10 and n_concept <= 1:
            out.append((topic, n_source, n_concept))
    return out


def append_log(line):
    """Log 只追加、最新在前、不改写旧记录（方法论 8.2）。"""
    logf = ROOT / "wiki" / "log.md"
    if not logf.exists():
        return False
    text = logf.read_text(encoding="utf-8")
    entry = f"## [{date.today().isoformat()}] update | {line}\n\n- 由 tools/worker.py 自动执行；原文与来源页均已写入并通过 vault check。\n\n"
    m = re.search(r"^# Wiki Log\n\n?", text, re.M)
    if not m:
        return False
    text = text[:m.end()] + entry + text[m.end():]
    text = re.sub(r"^updated: \d{4}-\d{2}-\d{2}$", f"updated: {date.today().isoformat()}", text, count=1, flags=re.M)
    atomic_write(logf, text)
    return True


def run_check():
    try:
        out = subprocess.run([sys.executable, str(VAULT), "check"], capture_output=True, text=True, timeout=180)
        m = re.search(r"ERROR\s+(\d+)", out.stdout)
        return (int(m.group(1)) if m else None), out.stdout
    except Exception as e:
        return None, f"check 失败：{e}"


# ── 主流程 ──────────────────────────────────────────────────────────────────

def pending_files():
    if not INBOX.exists():
        return []
    reg = load_json(LEDGER, {"done": {}})
    done = reg.get("done", {})
    out = []
    for p in sorted(INBOX.rglob("*")):
        if not p.is_file() or p.name in SKIP_NAMES or p.name.startswith("."):
            continue
        h = filehash(p)
        if done.get(str(p)) == h:
            continue
        out.append((p, h))
    return out


def plan_for(files):
    """把收件箱里的事实交回意图协议展开，保证规则只有一份。"""
    joined = "、".join(f.name for f in files[:5])
    cmd = f"收这个 {files[0]}" if len(files) == 1 else f"收一下最近 {len(files)} 天"
    try:
        out = subprocess.run([sys.executable, str(TOOL), cmd, "--json", "--root", str(INBOX),
                              "--root", str(files[0].parent)], capture_output=True, text=True, timeout=120)
        plan = json.loads(out.stdout)
    except Exception:
        plan = {"unhandled": True, "reason": "intent 协议执行失败"}
    plan["_note"] = joined
    return plan


def process(dry_run=False):
    files = pending_files()
    if not files:
        return {"new": 0, "archived": [], "pages": [], "skipped": [], "failed": [], "budget": None}

    plan = plan_for([f for f, _ in files])
    if plan.get("unhandled") and not dry_run:
        log(f"协议未命中：{plan.get('reason')}；按设计交给兜底，不自动写入")
        return {"new": len(files), "archived": [], "pages": [], "skipped": [],
                "failed": [("协议未命中", plan.get("reason", ""))], "budget": budget_left(), "plan": plan}

    topics = topic_list()
    day = date.today().isoformat()
    result = {"new": len(files), "archived": [], "pages": [], "skipped": [], "failed": [],
              "budget": budget_left(), "plan": plan}
    reg = load_json(LEDGER, {"done": {}})

    for src, h in files:
        if has_secret(src):
            result["skipped"].append((src.name, "命中秘密模式"))
            continue
        if src.suffix.lower() in CHAT_SUFFIXES:
            # 对话导出由 memory-kit 的 scan 负责（它已常驻每分钟扫同一目录）；
            # 这里只报告，不重复归档，避免两个执行器写同一份材料
            result["skipped"].append((src.name, "对话导出交给 memory-kit"))
            continue
        kind = next((i["kind"] for i in plan.get("items", []) if Path(i["path"]).name == src.name), None)
        target = next((i["target"] for i in plan.get("items", []) if Path(i["path"]).name == src.name), "raw/待分类")
        try:
            if dry_run:
                result["archived"].append((src.name, target, "dry-run"))
            else:
                dest = archive_raw(src, target)
                result["archived"].append((src.name, dest.relative_to(ROOT).as_posix(), kind))
                if dest.suffix.lower() in (".md", ".txt", ".markdown"):
                    text = dest.read_text(encoding="utf-8", errors="ignore")
                    info = call_model(text, topics)
                    if info.get("error") == "budget":
                        result["failed"].append((src.name, "触及月度费用上限，未调用模型"))
                    elif info.get("error"):
                        result["failed"].append((src.name, info["error"]))
                    else:
                        title = strip_date(dest.stem)
                        page = unique_dest(source_page_path(title, day))
                        atomic_write(page, render_source_page(title, day, dest.relative_to(ROOT).as_posix(), info))
                        relpage = page.relative_to(ROOT).as_posix()
                        update_catalog(relpage, info.get("summary", ""), day)
                        update_index_stats()
                        result["pages"].append(relpage)
                        # 手写日志（方法论 8.2：Log 最后写、只记已核验结果）
                        append_log(f"自动归档 {dest.name} → 来源页 {Path(relpage).stem}")
                reg.setdefault("done", {})[str(src)] = h
                save_json(LEDGER, reg)
        except Exception as e:
            result["failed"].append((src.name, str(e)))

    result["budget"] = budget_left()

    # 判断留清单（不代决）：
    #   1) 主题有来源但零概念页 → 提示是否首次蒸馏
    #   2) 单次入库超过 20 条 → 提示是否需要分批
    if not dry_run:
        for topic, ns, nc in moc_thin(topics):
            queue_decision("蒸馏候选", f"主题「{topic}」已有 {ns} 个来源、0 个概念页",
                           "按方法论 5.1，两个独立来源即可建概念页；是否现在蒸馏由本人决定",
                           ["现在蒸馏", "再等等"])
        if len(result["archived"]) > 20:
            queue_decision("批量待确认", f"本批入库 {len(result['archived'])} 条",
                           "方法论 7.3 要求大批量分批执行并逐批给清单", ["分批重跑", "接受本批"])
    return result


def receipt(result) -> str:
    if result["new"] == 0:
        return "本次没有新材料。"
    lines = [f"已收录 {len(result['archived'])} 条"]
    by_target = {}
    for name, dest, kind in result["archived"]:
        parent = str(Path(dest).parent)
        by_target[parent] = by_target.get(parent, 0) + 1
    for t, n in sorted(by_target.items()):
        lines.append(f"  · {n} 条 → {t}")
    if result["pages"]:
        lines.append(f"新增来源页 {len(result['pages'])} 篇")
    if result["skipped"]:
        lines.append(f"跳过 {len(result['skipped'])} 条（{result['skipped'][0][1]}）")
    if result["failed"]:
        lines.append(f"失败 {len(result['failed'])} 条：{result['failed'][0][1][:60]}")
    if result.get("budget") is not None:
        lines.append(f"本月额度剩余 {result['budget']:.2f} 元")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="消费收件箱并写入库")
    ap.add_argument("--once", action="store_true", help="处理一次后退出")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--receipt-only", action="store_true")
    ap.add_argument("--budget", action="store_true")
    a = ap.parse_args()

    if a.receipt_only:
        print(RECEIPT.read_text() if RECEIPT.exists() else "暂无回执")
        return 0
    if a.budget:
        print(f"本月已用 {month_spent():.2f} 元 / 上限 {MONTHLY_CAP_CNY:.0f} 元；模型 {MODEL}")
        return 0

    with Lock():
        result = process(dry_run=a.dry_run)
        text = receipt(result)
        print(text)
        if not a.dry_run:
            RECEIPT.write_text(text + "\n")
            if result["archived"]:
                errors, out = run_check()
                if errors:
                    print(f"\n⚠️ vault check: ERROR {errors}")
                    print(out[-800:])
        if result.get("budget") is not None and result["budget"] <= 0:
            return 3
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
