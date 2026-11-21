# Obsidian Memory Kit

**想留下和 AI 聊过的事，又懒得整理笔记？**

继续在你习惯的应用里聊。把导出的对话交给本地助手，或放进设置好的收件箱。下次想知道「我当时为什么改主意」，打开一篇主题笔记，就能看到当时谁提了建议、你怎么回应，以及每句话的出处。

[使用这个模板](https://github.com/jianhong001/obsidian-memory-kit/generate) · [先看一次找回原话的结果](#一次找回原话的结果) · [让助手带我试](#让助手带我试) · [设计思路](docs/设计思路.md)

> **English (short)**: A local-first Obsidian vault kit where an LLM keeps a wiki of what you actually said. Exports and inbox drops become sourced topic notes; `tools/intent.py` expands one Chinese sentence into a deterministic action plan; `tools/worker.py` files material and writes source pages; `tools/brief.py` reports what changed and what still needs *your* judgement. No cloud, no vendor lock-in — swap in any OpenAI-compatible model. Design rationale: [docs/设计思路.md](docs/设计思路.md).

## 一次找回原话的结果

**「我最后选了几点观察兰花？我听过每天中午拍照的建议，后来采纳了吗？」**

仓库里有两段虚构的课程讨论。导入后，「兰花观察课」主题页能找到这三句原话：

| 哪一天、谁说的 | 原话 |
|---|---|
| 3 月 1 日 · 模型甲提议 | 「建议每天中午拍一张照片比较。」 |
| 3 月 1 日 · 我回应 | 「我不采用每天中午拍照的建议，那时我在上课。」 |
| 3 月 2 日 · 我在模型乙的对话里决定 | 「我决定每天早上八点记录兰花的状态，因为出门上课前能坚持做。」 |

所以，**中午是模型的建议，早上八点是我自己的决定**。程序把带出处的摘录交给助手，助手就有依据回答上面的问题。两段对话各保留一份完整的可读原文，在 Obsidian 里点主题页的引用即可跳到对应发言。[第一段原文](examples/01-orchid.md) · [第二段原文](examples/02-orchid.md)

## 让助手带我试

在 macOS 或 Linux 上准备好 Python 3.9 或更新版本。[下载 ZIP](https://github.com/jianhong001/obsidian-memory-kit/archive/refs/heads/main.zip) 并解压，让能读写本机文件的 AI 助手打开解压后的文件夹，把下面这段话发给它。第一次只用仓库自带的示例，原有笔记用不到。

> 按 README 的演示命令，新建一个独立的 Obsidian 演示库，把 examples 中的两段对话都导进去。回答我：我最后为什么选早上八点？请分别引用模型的建议和我自己的原话。最后给我主题笔记的位置，让我能点回去看。

<details>
<summary>自己在终端试（无需 API 密钥）</summary>

在解压后的项目目录运行以下命令。每次都会建立一个新的演示目录：

```sh
demo="$(mktemp -d "$HOME/MemoryKit-Demo.XXXXXX")"
cp -R vault/. "$demo/"
python3 tools/memory.py --vault "$demo" import examples/01-orchid.md
python3 tools/memory.py --vault "$demo" import examples/02-orchid.md
python3 tools/memory.py --vault "$demo" handoff 兰花观察课
printf '%s\n' "$demo"
```

在 Obsidian 选择「打开本地仓库」，打开最后一行显示的文件夹，再点「02 模型对话 → 主题记录 → 兰花观察课」。再次导入同一份文件即可验证去重。

</details>

## 接入自己的聊天后，我还要做什么？

| 平时在哪里聊 | 你要做的一步 | 接下来会发生什么 |
|---|---|---|
| 能读写本机的助手 | 把独立的库交给它，读库里的 `AGENTS.md`，明确启用持续记录 | 助手可在话题有进展时投递对话并调用导入工具。同一课题沿用相同分类与主题名，发言就能汇在一篇主题笔记里。 |
| ChatGPT 或其他官方应用 | 从 ChatGPT 导出 JSON／ZIP，或让其他模型提供[通用 Markdown](#通用-markdown) | 文件可由本地助手导入。含多条对话的历史包先列清单，经你核对再写入；纯网页助手只能处理你交给它的材料。 |
| Mac 上经常有新文件 | 首次安装收件箱任务，以后只把文件放进约定的文件夹 | 登录后每分钟检查一次，文件在两次检查中保持稳定才处理。分类不明或需要确认的条目留待核对。 |

分类支持 **工作、学习、生活**。每条对话在本工具里归入一个主题；跨模型续聊时，同一个项目沿用相同的 `category` 和 `topic`。已有笔记和新生成的内容可以在 Obsidian 里继续编辑，手写感想放在「我的批注」区。个人库请放在公开仓库之外。

## 一句话 → 确定清单 → 自动归档

对话导入解决「找回原话」。如果你还想让**别的杂乱材料**（网页剪藏、PDF、截图、导出包）也自动进库，用这一组命令。它们只依赖 Python 标准库；模型只在填空处出现（这段讲什么、归哪个主题），判断留给你。

```sh
# 1) 看看一句话会被展开成什么（不写任何文件）
python3 tools/intent.py "收一下这周的"
python3 tools/intent.py "找点东西 毕业论文" --json

# 2) 正式处理收件箱：归档 + 生成来源页 + 更新索引与日志 + 自检
python3 tools/worker.py --once
python3 tools/worker.py --budget        # 本月已用费用

# 3) 每日简报：库内版，以及只含计数与目录的外部通道版
python3 tools/brief.py
python3 tools/brief.py --feishu
python3 tools/brief.py --decide 2 同意  # 处理待决清单第 2 条
```

| 文件 | 为什么这样拆 |
|---|---|
| `tools/intent.py` | 同一句话永远展开成同一个候选清单；未命中就明确返回 `unhandled`，不猜 |
| `tools/worker.py` | 只做「可撤销」的写入；命中凭据模式跳过；每月费用上限默认 50 元，超限停止调用模型 |
| `tools/brief.py` | 自动化的产出必须便宜到能每天看：几行计数 + 一份待决清单 |

`worker.py` 需要模型时读取环境变量 `DEEPSEEK_API_KEY`（或任意兼容接口，改 `WORKER_MODEL` 与价格变量）。没有 key 也能跑：它会完成归档与检查，只跳过摘要与来源页。

默认收件箱：`~/Documents/模型对话收件箱`；状态与费用账本：`~/.local/share/obsidian-memory-kit/worker`（可用 `WORKER_INBOX`、`WORKER_STATE` 覆盖）。

## 日常使用

以下命令均从项目目录运行，`$demo` 指向上面建好的演示库。正式使用时，将它换成你基于空白模板建立的私人库路径。

| 想做的事 | 如何操作 |
|---|---|
| 看一批历史里有哪些聊天 | `python3 tools/memory.py --vault "$demo" preview "导出.zip"`。看标题、条数和批次指纹；确认后运行 `python3 tools/memory.py --vault "$demo" import "导出.zip" --approve "上一步给出的指纹"`。 |
| 给一段聊天指定课题 | `python3 tools/memory.py --vault "$demo" import "对话.md" --category 学习 --topic "毕业论文"` |
| 接着和另一个助手聊 | `python3 tools/memory.py --vault "$demo" handoff "毕业论文"`。先在本机预览有出处的摘录，再选择愿意分享的内容。 |
| 找一个入口或查看待处理文件 | `python3 tools/memory.py --vault "$demo" find "学习"` 搜索七个个人入口；`python3 tools/memory.py --vault "$demo" status` 查看收件箱上次扫描结果。 |

**安装 macOS 收件箱。** 安装需要 Apple 命令行开发工具。请选择库外的专用文件夹，macOS 询问时允许收件箱应用访问指定位置。

```sh
python3 tools/mac_agent.py install --vault "$demo" --inbox "$HOME/模型对话收件箱"
```

随时可以用 `python3 tools/mac_agent.py remove --vault "$demo"` 停用。在 Linux 上，先创建专用目录，再手动或定时运行 `python3 tools/memory.py --vault "$demo" scan "$HOME/模型对话收件箱"`。源文件会保留在收件箱里。

### 通用 Markdown

让模型沿用同一个对话标识、按顺序标出双方发言。下面这段也能导入「兰花观察课」：

```markdown
---
platform: 示例平台
conversation_id: orchid-week-2
category: 学习
topic: 兰花观察课
date: 2025-03-03
---
# 讨论记录频率

## 我 · m01
我决定周一到周五都记录。

## 助手 · m02
周末需要单独注明。
```

稳定的对话和消息标识有助于去重、续写和定向排除。省略时使用标题和发言位置，适合只从末尾追加的记录。无法判断发言者的普通 Markdown 将标为「来源文本（说话者待核实）」；日期缺失时可能使用导入日。ChatGPT 导出只读当前可见分支，已归档对话与内部工具消息跳过，非文本附件登记状态。

### 改主意，或有一段不想留

- **改主意了**：追加一句「我改为……」并保留日期。以后能看到前后两次说法。
- **这段别记**：在按上述格式标明说话者的记录里，单独用一条用户发言写「这段别记」。它会排除紧邻的上一轮、这条指令及随后到下一条用户发言前的模型回应。要指定更早的消息，可在属性区写 `omit: m01 m02`，列全涉及的标识。
- **排除的范围**包括工具管理的原文、主题摘录、接续说明及本机状态。收件箱源文件、手写批注、历史备份和已经分享给别人的内容需单独处理。凭据筛查只覆盖常见格式，请在投递前检查标题、元数据和正文。
- **备份**时一并保存私人库和本机状态目录。默认状态在 `~/Library/Application Support/ObsidianMemoryKit/`；命令行可用 `--state-dir` 指定库外的私有位置。中断后可用相同文件重试。

## 复用或贡献

仓库自带空白模板、虚构示例和标准库 Python 工具。发布自己的版本前，可以在干净目录运行下面两项检查。个人对话、运行状态和私人库的 Git 历史应留在本机。

```sh
python3 -m unittest discover -s tests -v
python3 tools/check_public.py
```

模板和工具采用 [MIT 许可证](LICENSE)。
