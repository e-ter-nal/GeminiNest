# GeminiNest

> **面向 Google Antigravity 原生多模态宿主的自主 B 站冲浪、本地视觉安检与持续记忆数字生命系统。**
>
> *An autonomous Bilibili surfing, local visual safety-gated, and bounded cross-platform memory digital life system built for Google Antigravity's native multimodal host — zero external LLM API keys, hot-swappable between v1 Pure-Audio (`48kbps m4a`) and v2 Sanitized-Video (`wd-tagger` DirectML GPU + Surgical Blackout MP4).*

[![Release](https://img.shields.io/badge/version-v2.0.0-blue.svg)](#v1-纯音频-vs-v2-可插拔视觉安检门)
[![Python](https://img.shields.io/badge/python-3.11-3776AB.svg)](#user--ai-agent-通用快速部署清单quickstart-checklist)
[![Tests](https://img.shields.io/badge/pytest-149%20passed%20(offline)-brightgreen.svg)](#step-4离线验货--dry-run-与-149-条单元测试)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 项目亮点与架构差异

传统的「AI 刷视频 / 自动化冲浪」脚本通常依赖浏览器轮询截图 + 循环调用外部视觉 API，容易导致多轮工具乒乓（Token 爆炸）、账单失控、误传违规画面，且记忆随会话结束清零。**GeminiNest** 从底层架构做了四项核心反转：

| 对比维度 | 传统截图轮询 / 外置 API 脚本 | GeminiNest（v2.0 架构） |
| :--- | :--- | :--- |
| **控制流与 Token 成本** | Agent 反复调工具抓页、截多图（280–1,120 Tok/张），多轮乒乓 Token 失控 | **控制权反转（IoC）**：本地 Python 批处理拉取、黑名单过滤、记忆预检索与媒体封装，Agent 压缩为 **「选片 + 鉴赏」极简两轮**，并拼装 `4,500–5,200 Tokens` 字节级稳定静态前缀命中隐式缓存 |
| **模型接入与费用** | 强依赖外置 LLM SDK 与按量付费 API Key，多模态流量费用高昂 | **Antigravity 宿主原生 `read_file` 零额外 API 费**：零外置 LLM SDK、零 Gemini API Key，直接复用 Antigravity Pro 订阅原生读取本地 `.m4a` / `.mp4` |
| **长期记忆与遗忘机制** | 堆叠全量历史聊天导致上下文膨胀，缺乏检索与遗忘衰减 | **SQLite FTS5 + 艾宾浩斯 Top-8 记忆引擎**：四列 BM25 加权（标题/简介/热评/新评）+ `<3` 字中文短词回退 + 印象分驱动的遗忘曲线衰减，每次注入严格封顶 Top-8（3 全局 + 5 关联） |
| **跨端同步与安全隔离** | 原图/原片或数据库直传云端，极易触发 `SAFETY` 截断或泄露私密文件 | **四重防护 + Google Drive 纯文本物理隔离闭环**：前置元数据硬黑名单 → 本地 `wd-tagger` 全帧安检与外科手术抹黑 → 临时媒体两阶段用完即焚 → 云盘纯文本白名单（仅 `.md`/`.json`/`.txt`）打通 **Antigravity ↔ Gemini Web** 正反向记忆闭环 |

---

## v1 纯音频 vs v2 可插拔视觉安检门

### 为什么采用「本地抽帧 + 音轨重封 MP4」与「强硬前置安检」？

1. **为什么不直接喂原片，也不传散装多图，而是抽帧重封净版 MP4？**
   - **顺应原生机理，榨干 Token 与带宽效率**：Gemini 原生处理视频本质上也是按固定帧率抽帧 + 音轨联合编码。本地提前按 `480p @ 1fps + 48kbps` 重封，既能把动辄几十上百 MB 的 B 站原片压到 **2–3 MB**（轻松落入 100 MB Inline 直传上限，省去上传带宽与外部存储链路），又能直接享受视频通道 **`70 Tokens/帧`** 的计费档位——相比散装多图输入（`280–1,120 Tokens/张`）**暴省 4–16 倍 Token**，同时保留原生音画时间轴对齐。
   - **严格帧一致安检（Frame Parity）**：直接传原片无法保证每一帧都安全；抽帧过检后再用过检帧重封，能 100% 保证「本地 `wd-tagger` 检过哪几帧，模型看到的 MP4 里就严格只有哪几帧」。

2. **为什么默认强硬屏蔽二游/擦边分区，并叠加本地安检与云盘纯文本隔离？**
   - **核心目的不是内容偏好，而是「保号防误判」**：Google 账号体系（含云盘与多模态通道）对未成年人保护 / CSAM（儿童色情）采用极严苛的自动化风控机制，而大量二次元/二游角色普遍带有**幼态画风（萝莉/少年体型）或清凉服饰**，极易触发自动化风控的**严重误判，轻则 `SAFETY` 截断，重则连坐导致整个 Google 主账号被永久封禁**。
   - 因此本项目构建了「**元数据黑名单前置硬拦截（零下载）→ 本地 `wd-tagger` 逐帧安检与外科抹黑/降级 → Google Drive 纯文本物理隔离**」三道保号防线；黑名单词库均配置在 `config.json` 中，使用者可在充分评估自身账号风控承受能力的前提下自行增删。

`config.json` 出厂默认采用零额外视觉依赖的 **v1 纯音频模式**；升级到 **v2 净版视频模式**无需修改任何业务代码或定时任务 Prompt，只需在本地私有 `config.local.json` 中热插拔切换开关即可。

| 维度 | v1 纯音频模式（`audio_only`） | v2 净版视频模式（`sanitized_video`） |
| :--- | :--- | :--- |
| **感知载体** | 元数据 + 字幕 + 精选评论 + **`48kbps` 单声道 `m4a`**（SponsorBlock 自动去广告） | 元数据 + 字幕 + 精选评论 + **480p 1fps 严格帧一致净版 MP4**（含 `48kbps` 单声道音轨） |
| **本地安检引擎** | `PassthroughSafetyGate`（零视觉依赖、零视觉合规风险） | **`wd-tagger` ONNX**（`SmilingWolf/wd-vit-tagger-v3`），支持 `DirectML`（Windows 通用 GPU）/ `CUDA` / `CPU` 自动路由 |
| **安全兜底机制** | 严守**纯听觉诚实红线**（禁止脑补未提供的画面细节） | 三重阈值（`general ≥ 0.20`、`sensitive ≤ 0.75`、`unsafe ≤ 0.05`）→ 违规帧原地**外科手术抹黑 `(0,0,0)`** → 抹黑率 `> 15%` 触发 **Early-Exit 提前熔断**并自动降级为 `downgraded_audio`（`E5001`） |
| **实测体积与估算 Token** | 110s 视频 → **`680 KB` `m4a`**；按 25 Tok/s 估算总计 `3,264 Tokens`（**本地公式估算 / 特定环境观测值**，非服务端官方计费遥测） | 133s（132 帧）视频 → **`2.73 MB` 净版 MP4**（`2,858,964` 字节）；按 70 Tok/帧 + 25 Tok/s 估算总计 `13,040 Tokens`（**本地公式估算 / 特定环境观测值**，非服务端官方计费遥测） |
| **实测管线耗时** | 秒级完成 FFmpeg 抽音与空降裁剪 | **Windows DirectML GPU 端到端 `21.77 s`**（下载 `2.51 s` + 抽帧 `5.61 s` + 132 帧推理 `12.43 s` + 重封 `0.67 s`）；单帧热态推理 **`51.75 ms`**（较 CPU `360.76 ms` 提速 **7.0×**，端到端较 CPU `291.15 s` 提速 **13.4×**） |

### 热插拔切换方式

在本地私有 `config.local.json` 中切换以下字段即可随时往返于 v1 / v2（定时任务 Prompt 完全通用，无需修改）：

```json
{
  "project": {
    "pipeline_mode": "sanitized_video"
  },
  "safety_alignment": {
    "wd_tagger_enabled": true,
    "wd_tagger_model_path": "data/models/model.onnx",
    "wd_tagger_device": "auto"
  }
}
```

> 切回 v1 时，仅需把 `"pipeline_mode"` 改回 `"audio_only"`、`"wd_tagger_enabled"` 设为 `false`。

---

## 系统架构流程图

```mermaid
flowchart TD
    subgraph Step1["阶段 1：本地控制层批处理 (prepare-feed)"]
        A["启动 .cat.lock PID 防撞车锁 & 清扫残留 temp"] --> B["Step 1: 吸收云盘 inbox/ 反向 [MEMORY_PATCH]"]
        B --> C["60s 非抢占 tkinter 预算小窗 (锁屏 TclError 静默兜底 0.35)"]
        C --> D["B 站只读 WBI 心跳 & 推荐流 / 30% 好奇心搜索"]
        D --> E["meta_filter 前置黑名单硬过滤 (二游/敏感分区零下载)"]
        E --> F["SQLite FTS5 四列 BM25 + 艾宾浩斯 Top-8 记忆召回"]
        F --> G["输出 SelectionPacket (4,500-5,200 Tok 静态前缀缓存)"]
    end

    subgraph Step2["阶段 2：Antigravity 宿主驱动两轮闭环"]
        G --> H["宿主小脑选片: 挑选 2-4 条高价值 BV"]
        H --> I["prepare-media: 按 config.local.json 热路由 v1 / v2"]
        I --> J{"pipeline_mode?"}
        J -- "v1: audio_only" --> K["FFmpeg + SponsorBlock -> 48kbps m4a"]
        J -- "v2: sanitized_video" --> L["FFmpeg 抽帧 -> wd-tagger ONNX (DirectML/CUDA/CPU)"]
        L -- "抹黑率 <= 15%" --> M["违规帧原地抹黑 -> 严格帧一致重封净版 MP4 (<= 95MB)"]
        L -- "抹黑率 > 15% (E5001)" --> N["Early-Exit 熔断焚帧 -> 自动降级 downgraded_audio"]
        K --> O["宿主原生 read_file 加载本地媒体并输出结构化手账 JSON"]
        M --> O
        N --> O
        O --> P["commit-appreciation: 写入记忆/账本/好奇心 & finally 物理焚毁临时目录"]
    end

    subgraph Step3["阶段 3：纯文本云盘正反向闭环 (sync)"]
        P --> Q["burn-temp --run-id <run_id> 隔离清扫 (当前轮次残留归零)"]
        Q --> R["从 SQLite 导出完整手账至 data/exports/ & 独立隐藏 Git 自动留痕"]
        R --> S["扩展名白名单校验 (.md/.json/.txt) -> 新修订轮转 1_yesterday_readonly/ & 刷新 0_active/"]
        S --> T["Gemini Web (Gem + Scheduled Actions) 读取 0_active/ 延续记忆"]
        T -. "Export to Docs -> Apps Script (.gdoc -> .txt)" .-> B
    end
```

---

## 项目目录结构（公版追踪 vs 本地私有隔离）

### 1. Git 追踪的公版文件（随仓库开源发布）

```text
GeminiNest/
├── .agent/rules/cat-soul.md          # 核心人设、感知诚实与审美规范（静态前缀缓存基石）
├── config.json                       # 全局非敏感默认配置（出厂默认 v1 audio_only）
├── config.local.example.json         # 本地路径与 v1/v2 私有覆盖示例模板
├── .env.example                      # B 站只读小号 Cookie 环境变量模板（零 Gemini API Key）
├── .gitignore                        # 严格屏蔽凭据、私有配置、数据库、模型权重与临时媒体
├── pyproject.toml                    # pytest 路径与离线测试配置
├── requirements.txt                  # v1 最小依赖（yt-dlp / httpx / python-dotenv / winotify）
├── requirements-v2.txt               # v2 视觉安检与冷备依赖（onnxruntime / pillow / numpy / py7zr）
├── requirements-dev.txt              # 离线单元测试依赖（pytest）
├── LICENSE                           # MIT 开源许可证
├── README.md                         # 项目门面与通用部署清单
├── ARCHITECTURE.md                   # 全局架构设计白皮书
├── SPEC_AND_TASKS.md                 # 模块契约、18 项错误码表与分阶段里程碑
├── HANDOFF_LOG.md                    # 架构决策与阶段验收交接记录
├── docs/
│   └── SETUP_AND_RUNTIME.md          # v1/v2 运维部署、Antigravity 定时 Prompt 与 Web 联动手册
├── scripts/
│   ├── download_wd_tagger.py         # wd-tagger ONNX 权重与标签 CSV 离线友好下载校验器
│   └── inbox_gdoc_exporter.gs        # Google Apps Script 云端 .gdoc -> .txt 定时转换脚本
├── src/gemininest/
│   ├── contracts.py                  # 全模块不可变 DTO 与 18 项统一 ErrorCode
│   ├── config.py                     # 配置合并校验（黑名单并集追加 + Secrets repr 脱敏）
│   ├── meta_filter.py                # NFKC + casefold 前置黑名单硬拦截防火墙
│   ├── memory_store.py               # SQLite WAL + FTS5 trigram (<3 字回退) + 艾宾浩斯 Top-8 + 账本
│   ├── fetcher.py                    # 只读 B 站 WBI API / yt-dlp / SponsorBlock / -412 熔断 / 快照
│   ├── media_processor.py            # FFmpeg v1 音频裁剪 / v2 抽帧与严格帧一致净版 MP4 重封 / 用完即焚
│   ├── safety_gate.py                # v1 Passthrough 桩 / v2 wd-tagger ONNX (DirectML/CUDA/CPU + Early-Exit)
│   ├── agent_bridge.py               # 静态前缀缓存拼装 / Packet 打包 / JSON Schema 校验 / 账本估算
│   ├── budget_ui.py                  # 60s 非抢占 tkinter 预算小窗（含 TclError 兜底）+ Windows Toast
│   ├── sync_manager.py               # 纯文本扩展名防火墙 + exports 独立隐藏 Git + 轮转 + inbox 补丁吸收
│   └── runner.py                     # .cat.lock PID 锁、宿主驱动 CLI 子命令与 --dry-run 离线回放
└── tests/                            # 149 条全离线单元测试（零网络、零真实数据触碰）
```

### 2. `.gitignore` 严格屏蔽的本地私有文件（严禁提交至 Git）

| 本地路径（不会出现在公开仓库） | 用途说明 |
| :--- | :--- |
| `.env` | B 站只读小号 `BILI_SESSDATA` / `BILI_JCT` / `BILI_BUVID3` 本地凭据 |
| `config.local.json` | 本机 `ffmpeg_binary`、`cloud_nest_dir`、`extra_blacklist` 与 v1/v2 开关覆盖 |
| `.agent/rules/cat-soul.local.md` | 可选的个人私有性格微调叠层（自动追加至静态前缀 Block 1，不污染公版规则） |
| `data/cat_memory.db`（及 `-wal`/`-shm`） | 本地 SQLite 长期记忆、世界书、观看史与 5 小时算力账本 |
| `data/models/`（`model.onnx`、`selected_tags.csv`） | v2 `wd-tagger` 本地 ONNX 权重（约 361 MB） |
| `data/exports/`（含内部独立 `.git/`） | 导出的纯文本手账与独立隐藏 Git 历史仓库（物理隔绝主代码库） |
| `data/temp/`、`data/snapshots/`、`data/.cat.lock` | 两阶段用完即焚临时媒体目录、离线黑匣子快照与 PID 防撞车锁 |

---

## User / AI Agent 通用快速部署清单（Quickstart Checklist）

本项目**不附带任何隐式一键安装脚本**，所有初始化动作均透明可控。无论是人工按清单执行，还是直接交由 AI Agent 自动接管部署，请严格依次完成以下 5 步：

### 交给 AI Agent 自动接管安装的引导提示词

如果你正在使用 Google Antigravity、Cursor、Claude Code 等 Coding Agent，克隆仓库后可直接把下面这段话发给它：

> 请阅读当前仓库的 `README.md` 和 `docs/SETUP_AND_RUNTIME.md`，严格按照「User / AI Agent 通用快速部署清单」帮我完成部署：先检查本机 `conda` / `python`（3.11）与 `ffmpeg` 可用性，创建 `gemininest` 环境并安装我指定的依赖（默认 v1，若我要启用 v2 视觉模式请安装 `requirements-v2.txt` + `onnxruntime-directml` 并下载 `wd-tagger` 权重），拷贝缺失的 `.env` 与 `config.local.json`（不覆盖已有文件），然后运行 `--dry-run` 离线验货和 `pytest` 确认 149/149 通过；遇到需要填写 B 站小号 Cookie、本机路径或 Google Drive 目录时请暂停并列出待填项交给我手动填写，严禁将任何凭据写入 Git。

---

### Step 1：克隆仓库并创建 Python 3.11 独立环境

- [ ] 确认本机已安装 **FFmpeg**（支持 `aac`、`m4a`、`libx264`、`mp4`，且建议在 `PATH` 中可见或记录完整路径 `<YOUR_FFMPEG_PATH>`）。
- [ ] 克隆代码并创建独立 Python 3.11 环境（推荐 Conda，亦可使用标准 `venv`）：

```powershell
git clone https://github.com/e-ter-nal/GeminiNest.git
cd GeminiNest

# 方式 A：Conda（推荐）
conda create -n gemininest python=3.11 -y
conda activate gemininest

# 方式 B：Python 标准 venv（若未使用 Conda）
# python -m venv .venv
# .\.venv\Scripts\Activate.ps1
```

### Step 2：安装依赖（v1 纯音频 或 v2 视觉安检门）

- [ ] **基础必装（v1 纯音频 + 离线测试）**：

```powershell
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

- [ ] ***(可选) 升级 v2 视觉安检门（`wd-tagger` ONNX）***：任选一种推理后端，并下载本地权重至 `data/models/`：

```powershell
# 方案 A（Windows 通用 GPU 推荐：DirectML，支持 NVIDIA/AMD/Intel，免装 CUDA Toolkit）
pip install -r requirements-v2.txt
pip uninstall -y onnxruntime
pip install onnxruntime-directml

# 方案 B（纯 CPU 环境）
# pip install -r requirements-v2.txt

# 方案 C（已配置完整 CUDA/cuDNN 的 NVIDIA 环境）
# pip install -r requirements-v2.txt
# pip uninstall -y onnxruntime
# pip install onnxruntime-gpu

# 下载并校验 wd-tagger 权重（约 361 MB；网络受限可加 --mirror https://hf-mirror.com）
python scripts/download_wd_tagger.py
```

### Step 3：拷贝本地私有配置模板并填写关键项

- [ ] 从模板生成本地私有文件（两者均已被 `.gitignore` 严格忽略，**严禁提交至 Git**）：

```powershell
# PowerShell（Linux/macOS 请改用 cp）
Copy-Item .env.example .env
Copy-Item config.local.example.json config.local.json
```

- [ ] 编辑 `.env`：在浏览器**无痕窗口**登录 B 站**只读小号**，填入 `BILI_SESSDATA`、`BILI_JCT`、`BILI_BUVID3`（无需任何 Gemini API Key）。
- [ ] 编辑 `config.local.json`：
  - 将 `media_preprocess.ffmpeg_binary` 设为你的 `<YOUR_FFMPEG_PATH>`（若已在 `PATH` 中可留 `"ffmpeg"`）；
  - 将 `sync.cloud_nest_dir` 设为你的 Google Drive 本地同步目录 `<YOUR_GOOGLE_DRIVE_NEST>`，并在该目录下创建 `0_active`、`1_yesterday_readonly`、`inbox`、`inbox_archived` 四个空子目录；
  - 若需启用 v2 视觉安检，按上方「热插拔切换方式」补充 `pipeline_mode: "sanitized_video"` 与 `wd_tagger_enabled: true`。
- [ ] 确认代理分流：B 站域名（`bilibili.com`、`bilivideo.com`、`hdslb.com`）走 `DIRECT` 直连，Google / Antigravity 走单一稳定节点。

### Step 4：离线验货（`--dry-run`）与 149 条单元测试

在接入真实小号与定时任务前，先运行全离线演练与单元测试（全程使用隔离临时沙箱，**零网络请求、零额度消耗、绝不触碰真实数据库与云盘**）：

- [ ] 运行离线闭环演练：

```powershell
$env:PYTHONPATH = "src"

# v1 纯音频离线闭环演练
python -m gemininest.runner --dry-run --headless

# v2 视觉安检 + 净版 MP4 + E5001 自动降级离线闭环演练
python -m gemininest.runner --dry-run --mode sanitized_video --headless
```

- [ ] 运行全量离线单元测试（确认 **149/149** 全部通过）：

```powershell
python -m pytest -v
```

### Step 5：配置 Antigravity 定时巡逻与跨端云盘联动

- [ ] 阅读 [`docs/SETUP_AND_RUNTIME.md`](docs/SETUP_AND_RUNTIME.md)，完成以下生产落地配置：
  1. 在 Antigravity 左侧 **`Scheduled Tasks`** 使用 **`Create With Prompt`** 粘贴「v1 / v2 自适应通用版唤醒 Prompt」（默认不带 `--headless`，保留 60 秒非抢占预算弹窗；锁屏或无桌面自动捕获 `TclError` 静默兜底 `0.35`）；
  2. *(可选)* 部署 [`scripts/inbox_gdoc_exporter.gs`](scripts/inbox_gdoc_exporter.gs) 至 Google Apps Script（每 15 分钟将 `inbox/` 中的 `.gdoc` 转存为同名 `.txt`）；
  3. *(可选)* 配置 Gemini Web 端常驻 Gem 与每日 `Scheduled Actions`，实现手机/网页端与本地记忆的纯文本双向闭环。

---

## 合规与免责声明

1. **仅供学习与个人研究**：本项目仅面向个人多模态 Agent 架构学习、本地媒体安检与长期记忆实验研究，默认以**纯只读、低频自律**方式运行，**不包含且严禁添加任何自动点赞、投币、收藏、评论、私信、关注或刷量功能**；内置请求随机间隔、`-412` 连续两次自动熔断与 5 小时窗口算力限额保护。
2. **遵守平台服务条款与法律法规**：使用者须严格遵守 Bilibili、Google Antigravity、Google Drive 及相关第三方平台的服务条款与所在地法律法规，因违规操作、滥用或违反平台协议引发的任何账号风控、封禁或其他后果均由使用者自行承担。
3. **妥善保管个人凭据与隐私**：请仅在浏览器无痕窗口提取只读小号 Cookie，绝勿使用主力账号；严禁将 `.env`、`config.local.json`、`data/cat_memory.db`、模型权重或任何含个人隐私/凭据的文件提交至公开仓库或上传至云盘常规同步目录。

---

## License

本项目基于 [MIT License](LICENSE) 开源。
