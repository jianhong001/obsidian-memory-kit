#!/usr/bin/env python3
"""Local, retryable conversation intake for an Obsidian vault (Python 3.9+)."""
import argparse
from collections import defaultdict
from contextlib import contextmanager
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import tempfile
import zipfile

from formats import Pending, digest, read

HOME = Path.home()
INDEX = "02 模型对话/主题记录.md"
ROOTS = {"工作": "work/项目方案", "学习": "02 模型对话/学习", "生活": "diary/主题整理/生活记录"}
INCOMING = {".zip", ".md", ".json"}
TERMS = {"工作": ("实习", "交付", "项目排期", "工作", "客户"),
         "学习": ("论文", "课程", "学习", "访谈提纲", "作业", "课题", "文献"),
         "生活": ("日记", "生活", "散步", "周末", "出游", "晚饭")}
SKIP = re.compile(r"这(?:一)?段(?:别记|不要记|不要保存|不记录)|不要记录这(?:一)?段")
CREDENTIAL = re.compile(r"-----BEGIN [^\n]*PRIVATE KEY-----|\b(?:sk-(?:proj-)?[\w-]{18,}|github_pat_[\w]{20,}|gh[pousr]_[\w]{18,})|(?:密码|恢复码|access_token|api_key)\s*[:=：]\s*[^\s]{4,}", re.I)
START, END = "<!-- memory-kit:start -->", "<!-- memory-kit:end -->"


def safe(s):
    s = re.sub(r'[\x00-\x1f/\\:*?"<>|#\[\]^]', " ", str(s or ""))
    return re.sub(r"\s+", " ", s).strip(" .")[:64] or "未命名"


def literal(s):
    return str(s).replace("<!--", "&lt;!--").replace("![[", "&#33;[[").replace("![", "&#33;[").replace("<script", "&lt;script").replace("<iframe", "&lt;iframe").replace("<img", "&lt;img")


def atomic(path, text):
    path.parent.mkdir(mode=0o700, exist_ok=True, parents=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".memory-")
    try:
        with os.fdopen(fd, "w", encoding="utf8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def local(vault, relative):
    p = vault / relative
    if not p.resolve().is_relative_to(vault) or any(a.is_symlink() for a in (p, *p.parents) if a.is_relative_to(vault)):
        raise Pending("目标路径越出库或经过符号链接")
    return p


def state_dir(vault):
    return HOME / "Library/Application Support/ObsidianMemoryKit" / digest(str(vault))


@contextmanager
def locked(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "intake.lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def filehash(p):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def classify(chat, msgs):
    if chat.get("category"):
        if chat["category"] not in ROOTS:
            raise Pending("category 请选择工作、学习或生活；其他主题需指定合适的私人入口")
        return chat["category"]
    title = (chat.get("topic") or "") + " " + chat["title"]
    user = "\n".join(m["text"] for m in msgs if m["role"] in ("user", "document"))
    scores = {c: sum(3 * (t in title) + (t in user) for t in words) for c, words in TERMS.items()}
    best = max(scores.values())
    if not best or list(scores.values()).count(best) != 1:
        raise Pending("分类不明确，请在来源中填写 category 或在导入时指定")
    return max(scores, key=scores.get)


def clean(chat, old):
    messages = chat["messages"]
    ids = [m["id"] for m in messages]
    if len(set(ids)) != len(ids):
        raise Pending("消息标识重复")
    excluded = set(old.get("excluded", [])) | {str(i) for i in chat["omit"]}
    previous_user = next((m["id"] for m in reversed(old.get("messages", [])) if m["role"] == "user"), None)
    session = []
    dropping = False
    for m in messages:
        if m["role"] in ("user", "document"):
            if SKIP.search(m["text"]):
                excluded.update(session)
                if not session and previous_user:
                    # A control-only delta can refer to the previous exchange on disk.
                    old_ids = [x["id"] for x in old.get("messages", [])]
                    excluded.update(old_ids[old_ids.index(previous_user):])
                excluded.add(m["id"])
                dropping = True
            else:
                session, dropping = [m["id"]], False
        else:
            session.append(m["id"])
            if dropping:
                excluded.add(m["id"])
    combined = {m["id"]: m for m in old.get("messages", []) if m["id"] not in excluded}
    for m in messages:
        if m["id"] in excluded:
            continue
        if CREDENTIAL.search(m["text"]):
            raise Pending("检测到可能的凭据，请提供脱敏副本")
        if m["id"] in combined and combined[m["id"]]["text"] != m["text"]:
            raise Pending("同标识原文已改变；请以带新消息标识的更正追加")
        if m["id"] not in combined:
            combined[m["id"]] = m
    return list(combined.values()), sorted(excluded)


def legacy_path(vault, chat):
    root = local(vault, "diary/模型记忆库/" + safe(chat["platform"]))
    if root.exists():
        for p in root.glob("*/*.md"):
            with local(vault, p.relative_to(vault)).open(encoding="utf8") as f:
                if f.readline().strip() != "---":
                    continue
                for _, line in zip(range(18), f):
                    if line.strip() == "---":
                        break
                    if line.startswith("conversation_id:") and line.split(":", 1)[1].strip().strip('"') == chat["cid"]:
                        return p.relative_to(vault).as_posix()
    return None


def legacy_overlap(vault, path, messages):
    # Read only the matched archive. Compare ordered visible text; never guess a join.
    text = local(vault, path).read_text(encoding="utf8").split("<!-- end-of-transcript -->", 1)[0]
    blocks = re.split(r'(?m)^<!-- message:[^\n]+ -->\n', text)[1:]
    values = []
    for block in blocks:
        heading, sep, body = block.partition("\n")
        body = body.split("\n\n## 附件\n", 1)[0].strip()
        values.append(("user" if heading == "### 我" else "assistant", body))
    new = [(m["role"], m["text"].strip()) for m in messages]
    if values and new[:len(values)] == values:
        return len(values)
    raise Pending("旧归档内容与来源不能逐条对齐；已保留原件，请核对分支、附件或曾经脱敏的条目")


def stage(vault, db, chat, args):
    key = digest(chat["platform"] + "\0" + chat["cid"])
    old = db["chats"].get(key, {})
    msgs, excluded = clean(chat, old)
    if CREDENTIAL.search(chat["title"] + (chat.get("topic") or "")):
        raise Pending("标题或主题可能含凭据")
    entry = dict(old, messages=msgs, excluded=excluded)
    if not old.get("path") and msgs:
        legacy = legacy_path(vault, chat)
        if legacy:
            cut = legacy_overlap(vault, legacy, chat["messages"])
            entry["legacy_ids"] = [m["id"] for m in chat["messages"][:cut]]
        cat = classify(dict(chat, category=args.category or chat.get("category")), msgs)
        topic = (args.topic or chat.get("topic") or chat.get("project") or chat["title"]).strip()
        platform = safe(chat["platform"])
        date = chat["date"] or next((m["date"] for m in msgs if m.get("date")), None) or dt.date.today().isoformat()
        path = f"diary/模型记忆库/{platform}/{safe(chat.get('project') or '未分项目')}/{date} {safe(chat['title'])} {key[:8]}.md"
        topicpath = ROOTS[cat] + "/" + safe(topic) + ".md"
        peers = [c for c in db["chats"].values() if c.get("topic_path") == topicpath]
        if peers and any(c["topic"] != topic for c in peers):
            topicpath = topicpath[:-3] + "-" + digest(topic)[:8] + ".md"
        entry.update(path=legacy or path, topic_path=topicpath, category=cat, topic=topic, platform=platform,
                     cid=chat["cid"], title=chat["title"], date=date)
    if set(entry.get("legacy_ids", [])) & set(excluded):
        raise Pending("排除范围包含旧版手工归档的消息，需确认原文位置后同步移除")
    db["chats"][key] = entry


def note(vault, path, header, generated, existing_ok):
    target = local(vault, path)
    block = START + "\n" + generated.rstrip() + "\n" + END
    if target.exists():
        before = target.read_text(encoding="utf8")
        if START in before or END in before:
            if before.count(START) != 1 or before.count(END) != 1 or before.index(START) > before.index(END):
                raise Pending("笔记的自动记录边界已修改")
            after = before[:before.index(START)] + block + before[before.index(END)+len(END):]
        elif existing_ok and "<!-- end-of-transcript -->" in before:
            after = before.replace("<!-- end-of-transcript -->", block + "\n\n<!-- end-of-transcript -->", 1)
        elif existing_ok:
            after = before.rstrip() + "\n\n" + block + "\n"
        else:
            raise Pending("同名对话笔记已存在，无法安全合并")
    else:
        before = None
        after = header.rstrip() + "\n\n" + block + "\n\n## 我的批注\n"
    return target, after, after != before


def link(path, label):
    return "[[" + path[:-3] + "|" + safe(label) + "]]"


def evidence(m):
    s = m["text"].strip()
    if m["role"] == "assistant":
        return "模型回应"
    if m["role"] == "document":
        return "来源文本（说话者待核实）"
    if re.match(r"^(?:我(?:最后|最终)?决定|我选择|我选定|决定采用)", s):
        return "本人决定"
    if re.match(r"^(?:我不同意|我不采用|我拒绝|我不选)", s):
        return "本人回应"
    if re.search("我改为|更正|我改主意", s):
        return "本人更正"
    if re.match(r"^(?:我还没决定|待定|还有一个问题|还没解决)", s):
        return "尚未解决"
    if re.match(r"^(?:下一步|我准备|我计划)", s):
        return "本人计划"
    return "本人原话"


def snippets(chat):
    for m in chat["messages"]:
        if not m["text"].strip():
            continue
        s = " ".join(m["text"].split())
        if s.strip("！!。 .") in {"你好","谢谢","好的","好","收到","嗯"}:
            continue
        anchor = "msg-" + digest(m["id"])
        where = "[[" + chat["path"][:-3] + ("" if m["id"] in chat.get("legacy_ids",[]) else "#^" + anchor) + "|" + chat["platform"] + "原话]]"
        yield "- " + (m.get("date") or chat["date"]) + " · " + evidence(m) + " · " + literal(s[:240]) + ("…" if len(s)>240 else "") + "　" + where


def project(vault, db, keys):
    notes = []
    groups = defaultdict(list)
    for k, c in db["chats"].items():
        if c.get("path"):
            groups[c["topic_path"]].append(c)
            if k in keys:
                content = []
                for m in c["messages"]:
                    if m["id"] in c.get("legacy_ids", []):
                        continue
                    role = {"user":"我","assistant":c["platform"],"document":"来源文本（说话者待核实）"}[m["role"]]
                    content.append("### " + role + "\n\n" + literal(m["text"]) + "\n\n^msg-" + digest(m["id"]))
                header = "---\nvisibility: private\nconversation_id: " + json.dumps(c["cid"]) + "\n---\n\n# " + safe(c["title"])
                notes.append(note(vault, c["path"], header, "\n\n".join(content), bool(c.get("legacy_ids"))))
    affected = {db["chats"][k].get("topic_path") for k in keys}
    for path, chats in groups.items():
        if path in affected:
            lines = []
            for c in sorted(chats, key=lambda c:(c["date"],c["path"])):
                if c["messages"]:
                    lines += ["\n### " + link(c["path"], c["title"]), *snippets(c)]
            notes.append(note(vault,path,"---\nvisibility: private\n---\n\n# "+safe(chats[0]["topic"]), "\n".join(lines),True))
    index = ["- "+cs[0]["category"]+" · "+link(p,cs[0]["topic"]) for p,cs in sorted(groups.items()) if any(c["messages"] for c in cs)]
    notes.append(note(vault,INDEX,"---\nvisibility: private\n---\n\n# 主题记录", "\n".join(index), True))
    return notes


def checkpoint(directory, db):
    atomic(directory / "state.json", json.dumps(db, ensure_ascii=False, separators=(",",":")))


def audit(vault, db):
    checker = vault / "tools/vault"
    log = vault / "wiki/log.md"
    if not checker.is_file() or not log.is_file():
        return
    marker = "<!-- memory-kit-batch:" + db["done"][-1] + " -->"
    check = subprocess.run([str(checker), "check"], cwd=vault, capture_output=True, text=True, timeout=120)
    if check.returncode not in (0, 1) or (check.returncode == 1 and "ERROR" not in check.stdout):
        raise Pending("记录已写入；全库检查未能运行，下次将恢复验证")
    text = log.read_text(encoding="utf8")
    if marker in text:
        return
    heading = re.search(r"(?m)^# [^\n]+$", text)
    if not heading:
        raise Pending("记录已写入；需核对日志页主标题")
    date = dt.date.today().isoformat()
    verdict = "全库检查通过。" if check.returncode == 0 else "全库检查仍有错误，本次仅记录局部文件回读结果。"
    entry = f"\n\n## [{date}] update | 本地收件箱归档\n\n- 本批原文及主题记录已写入并回读。{verdict}\n{marker}\n"
    updated = text[:heading.end()] + entry + text[heading.end():]
    updated = re.sub(r"(?m)^updated: [^\n]+$", "updated: " + date, updated, count=1)
    if log.read_text(encoding="utf8") != text:
        raise Pending("日志同时发生变化，下次将重试")
    atomic(log, updated)


def flush(vault, directory, db):
    keys = db.get("dirty", [])
    if keys:
        changes = project(vault, db, keys) if any(db["chats"][k].get("path") for k in keys) else []
        for path, data, changed in changes:
            if changed:
                atomic(path, data)
            if path.read_text(encoding="utf8") != data:
                raise Pending("落盘校验失败，下次将重试")
        if changes:
            audit(vault,db)
        db["dirty"] = []
        checkpoint(directory, db)


def process(vault, directory, db, path, args):
    if path.is_symlink() or not path.is_file():
        raise Pending("来源必须为本机普通文件")
    signature = filehash(path)
    if signature in db["done"] and args.command != "preview":
        return "重复，已跳过"
    chats = read(path)
    if filehash(path) != signature:
        raise Pending("来源正在更新，稍后重试")
    if not chats:
        raise Pending("没有匹配到合格对话")
    if args.command == "preview":
        for c in chats:
            print(c["platform"],safe(c["title"]),len(c["messages"]),"条")
        print("批次指纹",signature)
        return "只读预览"
    if len(chats)>1 and args.approve != signature:
        raise Pending("批量导入需先 preview 并确认清单，再用 --approve 加批次指纹")
    candidate = copy.deepcopy(db)
    keys = []
    for chat in chats:
        stage(vault,candidate,chat,args)
        keys.append(digest(chat["platform"]+"\0"+chat["cid"]))
    # Preflight every managed region before committing. Dirty keys allow crash recovery.
    project(vault,candidate,keys)
    candidate["dirty"] = sorted({k for k in keys if candidate["chats"][k] != db["chats"].get(k)})
    candidate["done"].append(signature)
    checkpoint(directory,candidate)
    db.clear()
    db.update(candidate)
    flush(vault,directory,db)
    return f"已核对 {len(chats)} 条对话"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vault", type=Path, required=True)
    ap.add_argument("--state-dir", type=Path, help="本机私有状态目录，必须在库外")
    ap.add_argument("command", choices=["preview","import","scan","handoff","find","status"])
    ap.add_argument("target", nargs="?")
    ap.add_argument("--category",choices=ROOTS)
    ap.add_argument("--topic")
    ap.add_argument("--approve")
    ap.add_argument("--background",action="store_true", help="等文件稳定后再归档，仅在异常状态改变时提醒")
    args=ap.parse_args()
    vault=args.vault.resolve()
    if not (vault/"00 关于我").is_dir():
        ap.error("请指定第二大脑库根目录")
    directory=(args.state_dir or state_dir(vault)).resolve()
    if directory.is_relative_to(vault):
        ap.error("状态目录应在库外")
    if args.command not in ("status",) and not args.target:
        ap.error("缺少文件、目录、关键词或主题名")
    if args.command=="find":
        query=args.target.casefold()
        for base in sorted(vault.glob("0[0-6] *")):
            for path in sorted(base.rglob("*.md")):
                if query in path.stem.casefold() or query in local(vault,path.relative_to(vault)).read_text(encoding="utf8").casefold():
                    print(path.relative_to(vault))
        return 0
    with locked(directory):
        p=directory/"state.json"
        db=json.loads(p.read_text()) if p.exists() else {"chats":{},"done":[],"dirty":[]}
        if args.command not in ("preview","status","handoff"):
            flush(vault,directory,db)
        if args.command=="status":
            status=directory/"status.json"
            print(status.read_text() if status.exists() else "尚无扫描记录")
            return 0
        if args.command=="handoff":
            cs=[c for c in db["chats"].values() if c.get("topic")==args.target and c.get("messages") and (not args.category or c.get("category")==args.category)]
            if len({c["category"] for c in cs}) > 1:
                raise Pending("同名主题分属多个分类，请用 --category 指定本次范围")
            if not cs:
                raise Pending("没有找到该主题")
            print("# "+safe(args.target)+" · 接续说明（本机预览）")
            for c in sorted(cs,key=lambda c:c["date"]):
                print("\n"+link(c["path"],c["title"]))
                lines = list(snippets(c))
                focus = [line for line in lines if any(t in line for t in (" · 本人决定 · ", " · 本人更正 · ", " · 本人回应 · ", " · 尚未解决 · ", " · 本人计划 · "))]
                selected = set(focus[-6:] + lines[-2:])
                for line in lines:
                    if line in selected:
                        print(line)
            return 0
        source=Path(args.target).expanduser().resolve()
        if args.command=="scan" and (not source.is_dir() or source.is_relative_to(vault)):
            raise Pending("收件箱应为已存在的库外目录")
        pending={}
        statfile=directory/"status.json"
        last=json.loads(statfile.read_text()) if statfile.exists() else {}
        observed={}
        paths=sorted(source.iterdir()) if args.command=="scan" else [source]
        finished=[]
        for f in paths:
            if args.command=="scan" and (not f.is_file() or f.name.startswith(".") or f.suffix.lower() not in INCOMING):
                continue
            try:
                if args.command=="scan" and args.background:
                    st=f.stat()
                    observed[f.name]=[st.st_size,st.st_mtime_ns,st.st_ino]
                    if last.get("observed",{}).get(f.name)!=observed[f.name]:
                        continue
                    if f.name in last.get("finished",[]) and not db["dirty"]:
                        finished.append(f.name)
                        continue
                if db["dirty"]:
                    flush(vault,directory,db)
                result=process(vault,directory,db,f,args)
                finished.append(f.name)
                if args.command!="scan" or not result.startswith("重复"):
                    print(f.name,result)
            except (Pending,ValueError,OSError,KeyError,TypeError,zipfile.BadZipFile) as e:
                pending[f.name]=str(e) if isinstance(e,Pending) else "文件无法解析，请检查源文件和本机权限"
                if args.command!="scan": print(pending[f.name],file=sys.stderr)
        if args.command=="scan":
            status={"last_scan":dt.datetime.now().astimezone().isoformat(timespec="seconds"),"pending":pending,"observed":observed,"finished":finished}
            atomic(statfile,json.dumps(status,ensure_ascii=False,indent=2))
            if args.background and pending and pending!=last.get("pending") and sys.platform=="darwin":
                subprocess.run(["/usr/bin/osascript","-e",'display notification "有对话需要核对，请让本地助手查看收件箱状态。" with title "模型对话收件箱"'],capture_output=True,timeout=10)
        return int(bool(pending))


if __name__=="__main__":
    try:
        sys.exit(main())
    except (ValueError,OSError,Pending) as exc:
        print("处理暂停：",exc,file=sys.stderr)
        sys.exit(1)
