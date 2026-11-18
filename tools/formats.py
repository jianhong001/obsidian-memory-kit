"""Read locally supplied conversation exports. No network calls."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import zipfile

LIMIT = 160 * 1024 * 1024
ROLE = re.compile(r"^#{2,3}\s+(我|用户|助手|user|assistant|AI|ChatGPT|Claude|Gemini|Codex|模型)(?:\s*[·|｜]\s*([\w.-]+))?\s*$", re.I)
USER = {"我", "用户", "user"}


class Pending(ValueError):
    """The source needs a human decision or a supported representation."""


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def day(value):
    if isinstance(value, (float, int)):
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).astimezone().date().isoformat()
    if value:
        return dt.date.fromisoformat(str(value)[:10]).isoformat()
    return None


def markdown(path):
    text = path.read_text(encoding="utf-8-sig")
    meta = {}
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end < 0:
            raise Pending("Markdown 属性区缺少结束行")
        for line in text[4:end].splitlines():
            key, sep, val = line.partition(":")
            if sep:
                meta[key.strip()] = val.strip().strip('"').strip("'")
        text = text[end + 5:]
    title = meta.get("title") or next((s[2:].strip() for s in text.splitlines() if s.startswith("# ")), path.stem)
    platform = meta.get("platform") or "Markdown"
    cid = meta.get("conversation_id") or "md-" + digest(title)
    msgs, buffer, role, mid, fence = [], [], None, None, None

    def finish():
        if role:
            msgs.append({"id": mid or "line-" + str(len(msgs) + 1), "role": role,
                         "text": "\n".join(buffer).strip(), "date": day(meta.get("date"))})

    for line in text.splitlines():
        m = ROLE.match(line) if not fence else None
        if m:
            finish()
            buffer = []
            role = "user" if m[1].lower() in USER else "assistant"
            mid = m[2]
            continue
        fm = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fm:
            if fence and fm[1].startswith(fence) and not line.strip()[len(fm[1]):]:
                fence = None
            elif not fence:
                fence = fm[1]
        buffer.append(line)
    finish()
    if not msgs:
        # Preserve authorship uncertainty in Markdown without role headings.
        msgs = [{"id": "document", "role": "document", "text": text.strip(), "date": day(meta.get("date"))}]
    if not any(m["text"] for m in msgs):
        raise Pending("没有可读取的正文")
    return [{"cid": cid, "platform": platform, "title": title, "topic": meta.get("topic"),
             "category": meta.get("category"), "project": meta.get("project"), "messages": msgs,
             "omit": meta.get("omit", "").split(), "date": day(meta.get("date"))}]


def chatgpt(obj):
    if not isinstance(obj, dict) or not isinstance(obj.get("mapping"), dict):
        raise Pending("JSON 不是支持的 ChatGPT 对话导出")
    mapping, current = obj["mapping"], obj.get("current_node")
    if not current:
        raise Pending("缺少当前分支，无法确定可见对话顺序")
    chain, seen = [], set()
    while current:
        if current in seen or current not in mapping:
            raise Pending("分支记录不完整")
        seen.add(current)
        node = mapping[current]
        msg = node.get("message") or {}
        role = (msg.get("author") or {}).get("role")
        if (role in ("user", "assistant") and not (msg.get("metadata") or {}).get("is_visually_hidden_from_conversation")
                and msg.get("channel") not in ("analysis", "commentary") and msg.get("recipient") in (None, "all")):
            content = msg.get("content") or {}
            if content.get("content_type") in ("text", "multimodal_text"):
                parts = [v if isinstance(v, str) else "〔非文本附件，未导入原件〕" for v in content.get("parts", [])]
                if any(parts):
                    chain.append({"id": msg.get("id") or current, "role": role, "text": "\n".join(parts),
                                  "date": day(msg.get("create_time"))})
        current = node.get("parent")
    cid = obj.get("conversation_id") or obj.get("id")
    if not cid or not chain:
        raise Pending("缺少原对话标识或可读消息")
    return {"cid": str(cid), "platform": "ChatGPT", "title": obj.get("title") or "无标题",
            "category": None, "topic": None, "project": None, "messages": list(reversed(chain)),
            "omit": [], "date": day(obj.get("create_time"))}


def read(path):
    if path.is_symlink() or path.stat().st_size > LIMIT:
        raise Pending("需提供普通文件或拆分较大的导出")
    if path.suffix.lower() == ".md":
        return markdown(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            members = [i for i in z.infolist() if re.fullmatch(r"conversations(?:[-_]\d+)?\.json", Path(i.filename).name)]
            if not members or sum(i.file_size for i in members) > LIMIT:
                raise Pending("未找到 conversations JSON，或需拆分导出")
            docs = [json.loads(z.read(i)) for i in members]
    else:
        docs = [json.loads(path.read_bytes())]
    return [chatgpt(item) for doc in docs for item in (doc if isinstance(doc, list) else [doc])
            if not isinstance(item, dict) or not item.get("is_archived")]
