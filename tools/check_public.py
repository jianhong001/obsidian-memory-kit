"""Check the entire template release tree against an explicit distribution list."""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
ALLOW = set("""README.md
LICENSE
.gitignore
tools/check_public.py
tools/memory.py
tools/formats.py
tools/mac_agent.py
tools/MemoryInbox.swift
tools/intent.py
tools/worker.py
tools/brief.py
tests/test_intake.py
tests/test_intent.py
tests/test_brief.py
docs/设计思路.md
examples/01-orchid.md
examples/02-orchid.md
vault/AGENTS.md
vault/00 关于我/从这里开始.md
vault/01 健康与病历/健康档案.md
vault/02 模型对话/主题记录.md
vault/03 AI 对我的理解/观察与回应.md
vault/04 我的文章与想法/写作入口.md
vault/05 工作记录/工作入口.md
vault/06 生活记录/生活入口.md""".splitlines())
seen=set()
problems=[]
for p in ROOT.rglob("*"):
    path=p.relative_to(ROOT)
    if path.parts[0]==".git" or "__pycache__" in path.parts:continue
    if p.is_symlink():
        problems.append("符号链接 "+str(path))
        continue
    if not p.is_file():continue
    seen.add(path.as_posix())
    if path.as_posix() not in ALLOW:continue
    s=p.read_text()
    if re.search(r"/(?:Users|home)/[A-Za-z0-9_-]+/|-----BEGIN (?:OPENSSH|RSA|EC) PRIVATE KEY-----|\b(?:ghp_|sk-proj-)[A-Za-z0-9]{20,}",s):
        problems.append("请复核 "+str(path))
problems+=["清单差异 "+s for s in sorted(ALLOW^seen)]
for msg in problems:print(msg)
if not problems:print(f"检查通过，发行清单 {len(seen)} 个文件")
sys.exit(bool(problems))
