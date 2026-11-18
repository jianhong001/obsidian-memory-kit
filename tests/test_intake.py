import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"tools"))
import memory
from formats import read


class Intake(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.v = self.base/"库"
        shutil.copytree(ROOT/"vault", self.v)
        self.s = self.base/"state"
        self.inbox=self.base/"in"
        self.inbox.mkdir()

    def cli(self,*args,success=True):
        p=subprocess.run([sys.executable,str(ROOT/"tools/memory.py"),"--vault",str(self.v),"--state-dir",str(self.s),*map(str,args)],capture_output=True,text=True)
        if success:self.assertEqual(p.returncode,0,p.stderr+p.stdout)
        else:self.assertNotEqual(p.returncode,0)
        return p.stdout+p.stderr

    def test_public_journey(self):
        a=self.inbox/"a.md"
        shutil.copy(ROOT/"examples/01-orchid.md",a)
        shutil.copy(ROOT/"examples/02-orchid.md",self.inbox/"b.md")
        self.cli("scan",self.inbox)
        topic=self.v/"02 模型对话/学习/兰花观察课.md"
        topic.write_text(topic.read_text()+"手写批注。\n")
        self.cli("scan",self.inbox)
        a.write_text(a.read_text()+"\n## 我 · a4\n我改为周五整理整周的记录。\n")
        self.cli("scan",self.inbox)
        self.assertEqual(len(list((self.v/"diary").rglob("*.md"))),2)
        text=topic.read_text()
        self.assertEqual(text.count("我改为周五"),1)
        self.assertIn("手写批注。",text)
        brief=self.cli("handoff","兰花观察课")
        self.assertIn("本人决定",brief)
        self.assertIn("本人回应",brief)
        self.assertIn("模型乙原话",brief)
        self.assertIn("兰花观察课",self.cli("find","兰花"))

    def test_exclusion_and_replay(self):
        a=self.inbox/"a.md"
        s=(ROOT/"examples/01-orchid.md").read_text()+"\n## 我 · a4\n私密测试文本\n\n## 助手 · a5\n复述私密测试文本\n"
        a.write_text(s)
        self.cli("import",a)
        a.write_text(s+"\n## 我 · a6\n这段别记\n\n## 助手 · a7\n复述私密测试文本\n")
        self.cli("import",a)
        a.write_text(s.replace("title:","title:")+"\n")
        self.cli("import",a)
        for p in self.v.rglob("*.md"):self.assertNotIn("私密测试文本",p.read_text())
        self.assertNotIn("私密测试文本",self.cli("handoff","兰花观察课"))
        self.assertNotIn("私密测试文本",(self.s/"state.json").read_text())

    def test_chatgpt_zip(self):
        import zipfile
        node=lambda i,parent,role,txt:{"parent":parent,"message":{"id":i,"author":{"role":role},"content":{"content_type":"text","parts":[txt]}}}
        c={"id":"demo-id","title":"课程计划","current_node":"b","mapping":{"a":node("a",None,"user","我计划学习写作。"),"b":node("b","a","assistant","好的"),"c":node("c","a","assistant","废弃分支")}}
        f=self.inbox/"exp.zip"
        with zipfile.ZipFile(f,"w") as z:z.writestr("conversations-001.json",json.dumps([c,dict(c,id="second"),dict(c,id="archived",is_archived=True)]))
        self.cli("import",f,success=False)
        self.assertFalse((self.v/"diary").exists())
        sig=hashlib.sha256(f.read_bytes()).hexdigest()
        self.assertIn(sig,self.cli("preview",f))
        self.cli("import",f,"--approve",sig)
        arcs=list((self.v/"diary").rglob("*.md"))
        self.assertEqual(len(arcs),2)
        self.assertTrue(all("废弃分支" not in p.read_text() for p in arcs))

    def test_recover_interruption(self):
        a=ROOT/"examples/01-orchid.md"
        real=memory.atomic
        def fail(path,body):
            if path.is_relative_to(self.v):raise OSError("simulated")
            real(path,body)
        with patch.object(memory,"atomic",side_effect=fail),patch.object(sys,"argv",["kit","--vault",str(self.v),"--state-dir",str(self.s),"import",str(a)]):
            self.assertEqual(memory.main(),1)
        self.assertTrue(json.loads((self.s/"state.json").read_text())["dirty"])
        self.cli("import",a)
        self.assertFalse(json.loads((self.s/"state.json").read_text())["dirty"])
        self.assertEqual(len(list((self.v/"diary").rglob("*.md"))),1)

    def test_legacy(self):
        src=ROOT/"examples/01-orchid.md"
        c=read(src)[0];ms=c["messages"]
        old=self.v/"diary/模型记忆库/模型甲/班级/旧笔记.md"
        old.parent.mkdir(parents=True)
        old.write_text("---\nconversation_id: orchid-a\n---\n\n<!-- message:abc -->\n### 我\n\n"+ms[0]["text"]+"\n\n<!-- end-of-transcript -->\n\n## 我的批注\n保留我。")
        self.cli("import",src)
        self.assertEqual(len(list((self.v/"diary").rglob("*.md"))),1)
        self.assertEqual(old.read_text().count(ms[0]["text"]),1)
        self.assertIn("保留我。",old.read_text())
        self.assertIn(ms[1]["text"],old.read_text())

    def test_audit_and_all_seven_entries(self):
        logs=self.v/"wiki/log.md"
        logs.parent.mkdir()
        logs.write_text("---\nupdated: 2020-01-01\n---\n\n# Wiki Log\n")
        checker=self.v/"tools/vault"
        checker.parent.mkdir()
        checker.write_text("#!/bin/sh\necho 'ERROR 3'\nexit 1\n")
        checker.chmod(0o700)
        self.cli("import",ROOT/"examples/01-orchid.md")
        self.cli("import",ROOT/"examples/01-orchid.md")
        self.assertEqual(logs.read_text().count("<!-- memory-kit-batch:"),1)
        self.assertIn("局部文件",logs.read_text())
        for page in self.v.glob("0[0-6] */*.md"):
            self.assertIn(str(page.relative_to(self.v)),self.cli("find",page.stem))

    def test_uncertain_no_write(self):
        f=self.inbox/"note.md";f.write_text("# 其他\n海蓝色。")
        self.cli("scan",self.inbox,success=False)
        self.assertTrue(json.loads((self.s/"status.json").read_text())["pending"])
        self.assertFalse((self.v/"diary").exists())
        self.cli("import",f,"--category","生活","--topic","我的颜色")
        self.assertTrue((self.v/"diary/主题整理/生活记录/我的颜色.md").exists())


if __name__=="__main__":unittest.main()
