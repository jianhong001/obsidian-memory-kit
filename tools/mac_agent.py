"""Install or remove a per-user, per-vault local inbox watcher on macOS."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

from memory import atomic, state_dir


def run():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=["install","remove"])
    parser.add_argument("--vault",required=True,type=Path)
    parser.add_argument("--inbox",type=Path)
    parser.add_argument("--python",default=sys.executable)
    args=parser.parse_args()
    if sys.platform!="darwin":parser.error("此后台安装器适用于 macOS")
    vault=args.vault.resolve()
    if not (vault/"00 关于我").exists():parser.error("库中缺少个人入口")
    directory=state_dir(vault)
    label="local.obsidian-memory-kit."+hashlib.sha256(str(vault).encode()).hexdigest()[:12]
    plist=Path.home()/"Library/LaunchAgents"/(label+".plist")
    domain="gui/"+str(os.getuid())
    if args.action=="install":
        if not args.inbox:parser.error("安装时需要 --inbox")
        inbox=args.inbox.expanduser().resolve()
        if inbox.is_relative_to(vault) or vault.is_relative_to(inbox):parser.error("请选择与库隔离的专用目录")
        inbox.mkdir(mode=0o700,parents=True,exist_ok=True)
        runtime=directory/"runtime"
        runtime.mkdir(mode=0o700,parents=True,exist_ok=True)
        for name in ("memory.py","formats.py"):
            atomic(runtime/name,(Path(__file__).parent/name).read_text())
        bundle=directory/"模型对话收件箱.app"
        contents=bundle/"Contents"
        binary=contents/"MacOS/MemoryInbox"
        binary.parent.mkdir(parents=True,exist_ok=True)
        resources=contents/"Resources"
        resources.mkdir(exist_ok=True)
        app_info={"CFBundleIdentifier":label+".app","CFBundleName":"模型对话收件箱",
                  "CFBundleDisplayName":"模型对话收件箱","CFBundleExecutable":"MemoryInbox",
                  "CFBundlePackageType":"APPL","CFBundleVersion":"1","LSUIElement":True,
                  "NSDocumentsFolderUsageDescription":"读取你投入收件箱的对话，并写入你指定的 Obsidian 库。"}
        atomic(contents/"Info.plist",plistlib.dumps(app_info).decode())
        settings={"python":args.python,"args":[str(runtime/"memory.py"),"--vault",str(vault),"scan",str(inbox),"--background"],
                  "vault":str(vault),"inbox":str(inbox),"status":str(directory/"status.json")}
        atomic(resources/"settings.json",json.dumps(settings,ensure_ascii=False))
        subprocess.run(["/usr/bin/swiftc","-O",str(Path(__file__).with_name("MemoryInbox.swift")),"-o",str(binary)],check=True)
        subprocess.run(["/usr/bin/codesign","--force","--sign","-",str(bundle)],check=True)
        config={"Label":label,"ProgramArguments":[str(binary)],
                "StartInterval":60,"RunAtLoad":True,"ProcessType":"Background","Umask":0o077,
                "StandardErrorPath":str(directory/"agent-errors.log")}
        plist.parent.mkdir(exist_ok=True,parents=True)
        atomic(plist,plistlib.dumps(config).decode())
    subprocess.run(["launchctl","bootout",domain+"/"+label],capture_output=True)
    if args.action=="remove":
        plist.unlink(missing_ok=True)
        print("已停用；收件箱、笔记和重试记录保留。")
        return
    subprocess.run(["/usr/bin/open","-g",str(bundle)],check=True)
    subprocess.run(["launchctl","bootstrap",domain,str(plist)],check=True)
    print("已加载",label,"；运行状态",directory/"status.json")


if __name__=="__main__":run()
