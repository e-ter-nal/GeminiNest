# GeminiNest Architecture Specification (v2.0 Refactored)

## 1. 项目定位与核心哲学
**GeminiNest** 是一个基于 Google Antigravity 原生多模态能力、本地控制层与 Google Drive 跨端云存储构建的**自主 B 站冲浪与持续记忆数字生命系统**。系统严格遵循五大工程哲学：

1. **安全对齐前置（Safety-by-Design）**：元数据黑名单前置硬拦截 + 本地全帧视觉安检门 + 临时媒体用完即焚 + 云端纯文本物理隔离。
2. **分阶段演进与热插拔（v1 Audio MVP → v2 Sanitized Video）**：v1 以纯音频+文本极速跑通完整生命闭环；v2 仅替换 `safety_gate` 与 `media_processor` 即可无痛升级视觉能力，零架构返工。
3. **控制权反转（Inversion of Control）**：本地程序批量完成数据拉取、黑名单过滤、记忆预检索与媒体封装，将 Agent 交互压缩为“选片 + 鉴赏”极简轮次，根除多轮工具乒乓带来的 Token 爆炸。
4. **跨端有界动态记忆（Bounded Cross-Platform Memory）**：以本地 SQLite 为唯一真理源（Single Source of Truth），经 Google Drive 实现 Antigravity 与 Gemini Web 端正反向同步；以艾宾浩斯衰减 + Top-8 硬封顶保证上下文永不膨胀。
5. **配置驱动与离线可测（Config-Driven & Dry-Run First）**：所有运行参数收口至 `config.json`；全流程支持黑匣子快照离线回放（`--dry-run`），开发调试零风控、零额度消耗。

---

## 2. 双阶段演进路线

### 2.1 v1 阶段（当前 MVP 目标）：纯音频 + 文本模式
* **感知输入**：视频元数据（标题、简介、分区、标签）+ CC/AI 字幕 + 精选评论（热评 Top10 + 最新评 Top5）+ 本地提取的 **48kbps 单声道 `m4a` 独立音频流**（约 25 Tokens/秒）。
* **范围控制**：暂不启用视觉输入与本地 GIF 赛博相册；完整交付人设规则（`cat-soul.md`）、每日双日记、SQLite 艾宾浩斯记忆引擎、好奇心兔洞状态机、本地算力账本与 Google Drive 纯文本跨端同步。
* **核心优势**：原生听懂 BGM、人声语气与鬼畜踩点；零视觉合规风险；极低带宽与算力开销。

### 2.2 v2 阶段（可插拔视觉升级）：本地全帧安检 → 净版 MP4 → Inline 直传
* **放弃散装多图与原片直传的原因**：
  * Gemini 4 视频通道在 `low`/`medium` 档位仅耗 **70 Tokens/帧**，较独立多图输入（280–1,120 Tokens/张）节省 **4–16 倍 Token**，且天然保持音画时间轴对齐。
  * 官方 Inline 负载上限已提升至 **100 MB**，净版 MP4 可直接 Inline 投喂，彻底消除 `File API` 上传、状态轮询与 48 小时云端留存链路。
* **本地安检与重封铁律（Frame Parity Invariant）**：**本地安检门检过哪几帧，最终净版 MP4 里就严格只包含哪几帧**。
  1. `media_processor` 调用 FFmpeg 按配置分辨率（`480p` 匹配 `low`/`medium`；识字读代码切 `high` 档时匹配 `720p`/`1080p`）与配置帧率（`1.0` 或 `0.5 FPS`）抽取全部画面帧。
  2. `safety_gate` 调用本地开源二次元图像分级模型 **`wd-tagger`（ONNX，支持 CUDA 毫秒级推理或纯 CPU 零显存运行）** 对抽取帧逐一打分。
  3. **外科手术式抹黑**：单帧不满足安全阈值（`general < min_general_score` 或 `questionable`/`explicit > max_unsafe_score`）时，原地替换为纯黑帧。
  4. **整片熔断降级**：若单条视频抹黑帧占比超过 `max_blackout_ratio`（默认 `15%`），直接整条降级为 v1 纯音频模式，不上传任何画面。
  5. **无损重封 Inline 直传**：将 100% 过检画面帧与 `48kbps` 单声道音轨快速封装为极小体积净版 MP4，通过 Inline 直传 Gemini 4。
* **视频处理模式自适应（Static vs Agentic）**：
  * **短片（`<= agentic_threshold_sec`，默认 300 秒）**：采用 `static` 模式（支持自定义 `extract_fps` 与 300 秒基准分段，低延迟、无额外导航推理开销）。
  * **长片（`> 300 秒`）**：按配置切换为 Gemini 4 原生 `agentic` 模式（对已安检净版 MP4 动态按需读字幕与画面，最高省 88% 输入 Token 且 100% 安全），或按 300 秒分段无状态接力 / 降级纯音频。

---

## 3. 开源组件参考与八大核心模块拓扑

系统拒绝盲目克隆臃肿仓库，在独立 Anaconda 环境（`gemininest`, Python 3.11）中采用以下高内聚、低耦合的 8 模块架构（辅以顶层调度入口 `runner`）：

| 模块名 | 核心职责 | 依赖 / 参考开源方案 |
| :--- | :--- | :--- |
| **`config`** | 加载并校验 `config.json` 全局参数；隔离读取 `.env` / `config.local.json` 敏感凭证；导出只读配置对象。 | Python 标准库 `json` / `dataclasses` |
| **`fetcher`** | 管理 B 站小号登录态心跳检测；拉取推荐流、分区排行、好奇心搜索、字幕、热评/新评及下载音视频流；内置 `-412` 指数退避与空降助手时间戳查询。 | v1 采用轻量只读 **`httpx`**（WBI 签名）+ **`yt-dlp`** + **`Bilibili SponsorBlock`**（暂不挂载 MCP，省去工具定义 Token）；借鉴 `dsh-bili-whale` 退避策略与 `agent-bilibili` 扁平文本清洗；预留 `bilibili-mcp-agent` 按需扩展 |
| **`meta_filter`** | **前置硬过滤防火墙**：在下载任何媒体流前拦截元数据命中黑名单的视频（主流二游全杀 + 宅舞/时尚/美妆/MMD/政治敏感）。 | 纯本地规则引擎（正则/集合匹配 `tname`/`tags`/`title`） |
| **`media_processor`** | 封装本地原生 `FFmpeg`：v1 提取 `48kbps` 单声道 `m4a` 与空降去广告裁剪；v2 负责抽帧、净版 MP4 重封、分段对齐及临时目录用完即焚。 | 本地原生 **`FFmpeg`** (`subprocess`) |
| **`safety_gate`** | 本地视觉安检门接口。**v1 为空壳直通桩（Passthrough Stub）**；**v2 热插拔 `wd-tagger` ONNX 引擎**，执行逐帧评分、黑帧替换与整片降级判定。 | v1 零依赖；v2 接入 `onnxruntime` + `wd-tagger` |
| **`agent_bridge`** | **Antigravity 宿主驱动桥接层**（零额外 API Key，复用 Antigravity Pro 订阅额度）：负责静态前缀拼装（4,500–5,200 Tokens 稳过隐式缓存阈值）、本地媒体路径打包（供宿主原生 `read_file` 加载 `m4a`/`mp4`）、宿主 JSON 输出校验、算力账本估算，以及 `SAFETY`/`PROHIBITED_CONTENT` 异常优雅跳过。 | Python 标准库（v1 零 LLM SDK 依赖）+ Antigravity 原生多模态环境 |
| **`memory_store`** | 管理本地 SQLite 四表（动态记忆 + FTS5 `trigram` 虚拟表、世界书表、已看与长视频断点追更表、算力账本表）；实现 `bm25()` 四列加权（含 `<3` 字中文短词回退）与艾宾浩斯 Top-8 显式权重输出。 | Python 标准库 **`sqlite3`**（内置 FTS5） |
| **`sync_manager`** | 管理纯文本云盘正反向同步：本地纯文本导出 + `data/exports/` 独立隐藏 Git 自动提交 + 滚动备份 `1_yesterday_readonly/` + 刷新 `0_active/` + 扫描合并 `inbox/` 纯文本反向补丁（Apps Script 导出 `.txt` 及手动 `.md`/`.txt`）。v2 预留 `py7zr` 加密冷备接口。 | **Google Drive for Desktop**（流式挂载）+ 本地 **Git**；v2 预留 `py7zr` |

---

## 4. 关键创新机制设计规范

### 4.1 控制权反转与隐式前缀缓存
* **两轮极简闭环**：本地先拉取 30–50 条候选视频 → `meta_filter` 前置拦截 → `memory_store` 毫秒级预匹配 → 清洗为“今日菜单简报” → **第 1 轮（小脑）**一次性选片 → 本地完成媒体准备与安检 → **第 2 轮（大脑）**沉浸鉴赏并输出结构化日记、心动分、新记忆与明日好奇心词条。
* **隐式前缀缓存规范**：Gemini 4 隐式缓存要求请求头部静态前缀稳定超过 4,096 Tokens。`agent_bridge` 拼装 Prompt 时必须将 **`cat-soul.md` + 黄金 Few-Shot 范例 + 评分与感知诚实标尺 + 完整 JSON Schema + 月度稳定世界书**（总计 4,500–5,200 Tokens）固定在最顶部且逐字不变；所有动态字段（日期、菜单、Top-8 激活记忆、本地媒体路径）统一置于最底部。

### 4.2 SQLite FTS5 + 艾宾浩斯 Top-8 显式注意力记忆引擎
* **四列原生 BM25 加权**：采用 FTS5 内置 `trigram` 分词器，零外部 NLP 依赖，对关联文本按权重检索：**标题 (3.0) ＞ 简介 (1.5) ＞ 热评 Top10 (1.0) ＞ 最新评 Top5 (0.5)**，天然抑制评论区复读刷屏权重膨胀。
* **单分制稳定性映射**：大模型仅输出 1–10 的主观印象分 $I$，本地代码自动映射初始稳定性 $S_0$（1–3 分 → 2 天；4–7 分 → 12 天；8–10 分 → 60 天）。记忆保留度公式：
  $$R = I \times e^{-\frac{\Delta t}{S}}, \quad S = S_0 \times (1 + 0.5N)$$
* **永不删库 + Top-8 硬封顶**：记忆永不物理删除，仅按得分沉底。每次唤醒严格注入 **Top-8**（全局保留度最高 3 条 + 当前场景关联最高 5 条），按得分降序排列并附显式语义权重标签（如 `[激活权重: 0.94 | 核心记忆]`），直接利用模型 Self-Attention 实现无向量矩阵加权。

### 4.3 三端记忆正反向同步闭环（Google Drive 纯文本物理隔离）
云盘映射路径：`<YOUR_GOOGLE_DRIVE_NEST>\`（**铁律：仅存纯文本，绝不写入明文视频、图片或二进制 `.db` 文件**）。

```text
[本地 SSD 主目录 (唯一真理源)]                    [Google Drive: Gemini_Nest (纯文本枢纽)]
  ├── data/cat_memory.db                           ├── 0_active/                (当日只读视图，供 Web 端读)
  ├── data/exports/*.md, *.json ──(正向每日同步)──▶ ├── 1_yesterday_readonly/    (昨日只读镜像副本，防写坏)
  ├── .agent/rules/cat-soul.md                     ├── inbox/                   ◀──(Web 端 Export to Docs 反向写入)
  └── data/temp/ (用完即焚，绝不入云)                └── inbox_archived/          (已被本地吸收的历史补丁)
```

* **正向传播（Antigravity 巡逻 → 云盘 → Gemini Web 端）**：巡逻结束导出最新人设、核心记忆摘要、当日公开/私密日记至 `data/exports/` → 在 `data/exports/` 独立隐藏 Git 仓库静默提交留痕（与主代码库彻底隔离） → 旧 `0_active/` 镜像覆盖 `1_yesterday_readonly/`（先解除只读再覆写） → 新内容写入 `0_active/`。Gemini Web 端通过 **Google Workspace 扩展 + 常驻 Skill + 每日 `Scheduled Actions`** 读取，实现手机/网页端随时接续昨日冲浪记忆。
* **反向传播（Gemini Web 端聊天 → `inbox/` → 本地主库 → 次日巡逻生效）**：Web 端聊天产生新人设微调、新梗记忆或好奇心指令时，输出标准 `[MEMORY_PATCH]` 块并点击 **导出到 Google 文档（Export to Docs）** 放入 `inbox/`，由云端 Google Apps Script 定时自动导出为同名 `.txt` 纯文本（同时直接兼容手动放入的 `.md`/`.txt`）。次日 Antigravity 启动 Step 1 自动解析并安全校验合并至本地 SQLite 与 `cat-soul.md`，归档原文件至 `inbox_archived/`。
* **v2 冷备预留**：若 v2 开启本地赛博相册并冷备至 5 TB 云盘，必须由独立归档任务每周调用 `py7zr`（AES-256 + `header_encryption=True` 加密文件名）生成加密卷上传，并在验证通过后执行错周延迟清理。

### 4.4 好奇心兔洞与黑匣子 Dry-Run
* **好奇心兔洞**：大脑每日日记末尾可许愿 1–2 个好奇关键词；次日调度器将约 30% 候选名额用于主动搜索该词（同样必须经过 `meta_filter` 硬黑名单安检）。
* **黑匣子离线调试**：真实巡逻时将抓取的元数据与拼装菜单存入本地快照目录（已被 `.gitignore` 屏蔽）。执行 `--dry-run` 时直接回放快照离线验证黑名单、FTS5 排序、算力估算与同步导出，全程零网络、零风控、零额度消耗。

---

## 5. 安全、运维与鲁棒性红线
1. **前置黑名单硬拦截**：严禁先下后筛。命中主流二游（碧蓝档案/蔚蓝档案/碧蓝航线/原神/星铁/鸣潮/绝区零/明日方舟/NIKKE/二游标签等）及宅舞、时尚、美妆、MMD、政治敏感标签时，在元数据阶段直接丢弃。
2. **用完即焚与异常兜底**：临时音频/帧图/净版 MP4 在 API 返回（无论成功或异常）的 `finally` 块中立即物理删除；遇到 `SAFETY` / `PROHIBITED_CONTENT` 拦截记录结构化日志并优雅跳过，严禁死循环重试。
3. **防撞车与启动预算确认**：使用 `.cat.lock` PID 文件锁防止电脑合盖休眠后补发定时任务撞车；启动时先测小号 Cookie 心跳，再弹出 60 秒非抢占 `tkinter` 预算小窗（超时按默认预算出发，支持临时调高或当日休眠）。
4. **网络与账号物理隔离**：Clash 将 B 站域名全部设为 `DIRECT` 直连，将 Google/Antigravity 流量锁死单一稳定节点；小号 Cookie 仅通过无痕窗口提取，严禁登录日常主力浏览器。
5. **纯本地 Git 与脱敏规范**：项目初始化即执行 `git init`，严格通过 `.gitignore` 屏蔽凭据、Cookie、本地配置、SQLite 数据库、临时媒体、快照及 ONNX 权重，采用 `main` + `feat/v1-mvp` + `feat/v2-visual` 分支小步提交。