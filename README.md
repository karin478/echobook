<p align="center"><img src="kokoro_reader/static/icon.svg" width="96" alt="Echobook icon"></p>

# Echobook（回声书）

**Echobook is a local read-along audiobook reader for Chinese-speaking learners of English.** Drop in an EPUB and it voices every chapter offline with [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M), highlights each word as it is spoken, lets you drill sentence by sentence (play one, stop, replay), shows a Chinese translation under the current sentence, and explains any phrase you select *in its context*, like a tutor would. Everything you save (fragment, sentence, translation, explanation, audio clip) goes into a vocabulary list for Anki. Audio runs on your own Mac; only the Chinese side calls an LLM, with your own OpenRouter key. The UI is in Chinese.

在 Mac 本机运行的「边看边听」英文原版书阅读器，给用读英文小说练英语的中文母语者：

- **有声书**：把 EPUB 拖进书架，本地 Kokoro TTS 生成自然的朗读音频，逐词高亮、自动滚动，点哪个词就从哪里读。
- **逐句精听**：「一句一停」模式每句播完就停，`↑` 重听、`←` `→` 换句，复刻逐句精听法；「连续」模式一直读下去。
- **逐句中文**：当前句的中文显示在段落下方，`↓` 开关。全书先建术语表，人名地名保持英文、称呼和专名译法统一。
- **划词**：划选一个词、一个短语或一整句，马上弹出结果；只划了半个词或短语动词的一半（turned / gave it），会自动补成整词和整个短语（turned out / gave it up）。「翻译」用免费机器翻译秒出中文（不花 token）；「讲解」像英语老师一对一精讲：先说这段在这里的意思，再拆成最小的短语/搭配/句型逐个讲，带例句。
- **收藏 → Anki**：划词卡片里 ⭐ 收藏片段（讲解里每个 ▸ 小块右边的 ☆ 能单独收藏那一块），或点中文那行末尾的 ☆ / 按 `F` 收藏整句；英文、中文、讲解和句子音频一起进生词本。生词本里一键（或全部）推到 Anki：正面是划选的词 + 整句（词加粗）+ 句子音频，背面是这句的中文 + 翻译/讲解 + 出处。
- **阅读体验**：Aa 面板调字号、字体、行距和正文宽度（只改正文，菜单栏不跟着放大）；浅色/深色手动切换。

> 状态：作者自用、持续迭代中。主要在 macOS（Apple Silicon）+ Chrome 上开发和测试；其它系统没有验证过。
>
> 名字：echo 是书被读出来的声音，也是逐句精听时的「再听一遍」。代码里的 `kokoro_reader`、`KOKORO_*` 环境变量、数据目录 `~/Documents/Kokoro Reader/` 和 `~/.config/kokoro-reader/` 是早期名字，来自它用的 [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) 语音模型。

## 快速开始

需要 Python 3.12、[uv](https://github.com/astral-sh/uv)、ffmpeg（`brew install ffmpeg`）。

```bash
git clone https://github.com/karin478/echobook.git && cd echobook
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
# spaCy 英文模型（misaki 分词要用，不在 PyPI 上）
uv pip install --python .venv/bin/python https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
.venv/bin/python -m kokoro_reader      # 前台运行，Ctrl+C 停止
```

然后在 Chrome 打开 <http://127.0.0.1:7788>。Kokoro 模型第一次生成音频时会从 Hugging Face 下载到缓存。

### 中文翻译和讲解（可选，需要密钥）

逐句中文、划词讲解、术语表都通过 [OpenRouter](https://openrouter.ai/) 调用模型（默认 `deepseek/deepseek-v4.1-flash-1017`，便宜的快速模型）。没有密钥时阅读器照样能用，只是没有中文。

```bash
mkdir -p ~/.config/kokoro-reader
cp .env.example ~/.config/kokoro-reader/.env    # 然后在里面填 OPENROUTER_API_KEY=
```

- 密钥只放在你自己的 `~/.config/kokoro-reader/.env`（或环境变量 `OPENROUTER_API_KEY`），仓库里只有不带值的 `.env.example`，`.env` 已被 `.gitignore` 忽略。也可以用 `KOKORO_TRANSLATE_ENV` 指向别的文件。
- 可选：`KOKORO_TRANSLATE_MODEL` 换模型，`OPENROUTER_API_BASE` 换任何 OpenAI 兼容的地址。
- 密钥只在服务端使用，不会发到网页；出错信息里的密钥会被替换成 `***`。

## 用法

| 按键 | 作用 |
|---|---|
| `Space` | 播放 / 暂停（一句一停时：重播或继续本句） |
| `←` `→` | 上一句 / 下一句 |
| `↑`（或 `R`） | 从头重听本句 |
| `↓` | 中文开 / 关（会记住） |
| `S` | 连续 / 一句一停（会记住） |
| `F` | 收藏本句（英文 + 中文 + 句子音频），再按取消 |
| `T` | 划词模式：翻译 / 讲解（会记住） |
| `[` `]` | 语速 ±0.05 |
| `Esc` | 关掉划词卡片 / 阅读设置 |

- 点某个词：选中那句并从这个词开始读。
- 章节「准备」= 音频 + 中文一起排队；打开阅读页会自动准备本章和下一章；书页「准备整本书」一次排完。章节没生成音频时也能读，只是没有声音。
- 生词本（书架右上角）可以回放每条收藏的句子音频。
- **预计时间和大小**：书页「准备整本书」旁边、每章「准备」按钮的提示里都有「预计 约 N 分钟 · 音频约 X MB」，准备过程中显示「音频还剩约 …」。速度按这台电脑最近 30 章的实际用时算，没转过时用默认值（Apple GPU 上约每秒 85 个字符）。

## 磁盘占用和删除

- 音频是 48 kbps 单声道 mp3，约 **每小时 22 MB**；一本 10 小时的小说约 220 MB。书架右上角显示总占用，书页上显示这本书的占用。
- 删除在书架上（鼠标悬停或右键封面 → ×），二选一：
  - **只删音频**：章节回到「未准备」，文字、中文翻译、阅读进度都保留，以后想听再生成；
  - **删除整本书**：书、音频、中文都删掉。
  两种都**不动生词本**：收藏的词句和它们的句子音频一直在。
- 服务每次启动会顺手清掉残留：不属于任何书的文件夹、中断导入/转换留下的临时文件、没有生词指向的句子音频。

## 推到 Anki

需要 Anki 桌面版开着，并装好 [AnkiConnect](https://ankiweb.net/shared/info/2055492159) 插件（默认 `127.0.0.1:8765`，`KOKORO_ANKI_URL` 可改）。

- 第一次在生词本顶部选一个**已有的**牌组和卡片类型；卡片类型的前两个字段分别放正面和背面（Basic 的 Front/Back 就行）。选择存在数据目录的 `anki.json`。
- 不新建牌组或卡片类型，不触发同步（自己在 Anki 里同步）；同一牌组里正面相同的卡不会重复添加。
- 推过的条目会记住（显示「已在 Anki」），再点不会重复推。卡片带 `echobook` 和日期两个标签。

## 隐私和安全

- 服务只监听 `127.0.0.1`，局域网访问不到；只认 `127.0.0.1` / `localhost` 主机名（防 DNS rebinding）；写操作带了别的网站的 Origin 一律 403。
- 书、音频、进度、生词都在 `~/Documents/Kokoro Reader/`（`KOKORO_READER_HOME` 可改），数据库是其中的 `kokoro.db`。
- 外发请求只有翻译相关：发给 OpenRouter 的是要翻译的段落（和术语表、讲解用的少量上下文）；划词「翻译」把划选的那一小段发给 Google（不通时 MyMemory）。不发任何其它数据。
- EPUB 上传上限 300 MB，解压后超过 1 GB 或压缩比异常的拒收（防压缩炸弹）；封面只认 JPEG/PNG/GIF/WebP。

## 开机自启（launchd，macOS）

```bash
scripts/install-launchd.sh             # 安装并立即启动；以后登录自动运行，崩溃自动拉起
scripts/install-launchd.sh uninstall   # 停止并卸载
```

- 安装：把 `launchd/com.kokoro-reader.plist` 里的 `__REPO__` / `__HOME__` 换成真实路径，写到 `~/Library/LaunchAgents/`，再 `launchctl bootstrap gui/$UID`。改了代码之后重新运行一次即可重启服务。
- 卸载：`launchctl bootout`，并把 plist 移到 `~/Library/LaunchAgents.disabled/`（只 bootout 的话重启后会回来）。
- 日志：`~/Library/Logs/kokoro-reader.log`；状态：`launchctl print gui/$UID/com.kokoro-reader | head`。
- launchd 的环境里没有 `/opt/homebrew/bin`，plist 里已把 `PATH` 设好；ffmpeg 按 `KOKORO_READER_FFMPEG` → Homebrew → `PATH` 的顺序找。

## Mac App（双击打开）

```bash
scripts/build-mac-app.sh               # 生成 ~/Applications/Echobook.app（可拖到 Dock）
```

双击时先确认后台服务在跑（没在跑就用 launchd 拉起来，最多等 20 秒），再用 Chrome 打开一个没有地址栏的独立窗口；服务起不来时弹窗提示去看日志。App 只是启动器（`macapp/launcher.sh`），图标由 `macapp/make_icon.swift` 生成。

上传 EPUB 请在 Chrome 或这个 App 窗口里做：有些内嵌浏览器（比如编辑器自带的预览面板）弹不出系统选文件窗口，也接不住从 Finder 拖进来的文件。

## 工作原理

- **音频**：单线程后台转换，转换线程的 QoS 降到 Utility（macOS），不拖慢网页和播放。TTS 默认用 Apple GPU（`mps`），没有时用 CPU，`KOKORO_READER_DEVICE=cpu|mps` 可强制。Apple Silicon 上实测 GPU 比 CPU 快约四成；服务启动后的第一章明显慢一些（GPU 要为新的句子长度现编译）。服务重启时还在排队的章节变回「未准备」，已生成的音频和中文不受影响。
- **翻译**：章节一被「准备」就和音频同时在后台翻（整章约 1 分钟，最多 4 章并行；刚打开的章节插队）。先建一次**全书术语表**：本地统计全书大写专名，把前几百个连一行上下文发给模型一次定下统一译名（人名地名保留英文，称呼 Mom → 妈妈 等给固定中文），之后每批只带本批出现的条目。先按**段落**整段翻译，再把中文对齐到 spaCy 分出的英文句子：能按中文句末标点直接切就不调模型，否则让模型切；切分结果必须拼回原译文、逐字不差才采用，否则这一段整段作为一个单元。结果存在 `books/<id>/zh/`。
- **智能扩选**：查询前先在本地把选区补成整词，再用 spaCy 句法分析找短语动词的小品词（turned *out*、gave it *up*），加一张常见介词动词表（look *after*、put *up with*），不调模型。
- **划词**：「翻译」先走免费机器翻译——Google 的 `clients5.google.com` dict-chrome-ex 端点（几十毫秒，不要密钥；`translate.googleapis.com` 的 gtx 端点对共享宽带 IP 常回 429，所以不用它），不通时用 MyMemory，两个都不通才调模型；失败的通道冷却 10 分钟。这两个都不是官方付费 API，可能随时变化。机器翻译只翻划选的这一段、不看语境，要语境义请用「讲解」。「讲解」带上选区、所在句子和前后约 600 字的上下文调模型。同一选区同一模式的结果缓存在内存里，再查不重复请求。「讲解」的输出格式（`▸` 短语块、`例：`、`注意：`）由前端逐条排版。
- 测试用 `KOKORO_READER_FAKE_TRANSLATE=1` 换成假翻译。

## 测试

```bash
.venv/bin/pytest            # 默认：单元 + API（假 TTS、假翻译，几十秒）
.venv/bin/pytest -m e2e     # Playwright 端到端（用系统 Chrome，不下载浏览器；假 TTS）
.venv/bin/pytest -m slow    # 真实 Kokoro：短句 token 覆盖 + 真书冒烟（几分钟）
```

所有测试都用临时目录做数据目录，不碰你的书架；`tests/conftest.py` 强制假翻译并拦掉一切非本机网络请求，测试永远不会调用 OpenRouter。真书冒烟读环境变量 `KOKORO_READER_SMOKE_BOOKS` 指向的文件夹里的 EPUB（自己放几本你有权使用的书），没设置时自动 skip。

## 致谢和许可

- 语音：[hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)（Apache-2.0）和 [misaki](https://github.com/hexgrad/misaki) G2P；分句：[spaCy](https://spacy.io/)。
- 本项目代码以 [MIT](LICENSE) 许可发布。只用它读你有权使用的书。
