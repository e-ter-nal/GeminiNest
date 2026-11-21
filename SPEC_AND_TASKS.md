# GeminiNest Technical Spec & Phased Task Checklist

## 1. 标准项目目录树

### 1.1 本地主工程目录（`<YOUR_PROJECT_DIR>`）
```text
GeminiNest/
├── .agent/
│   └── rules/
│       └── cat-soul.md               # 核心人设与审美规范（静态前缀缓存核心）
├── config.json                       # 全局非敏感配置（纳入 Git，驱动一切行为）
├── config.local.example.json         # 本地路径/敏感项示例模板（纳入 Git）
├── config.local.json                 # [Git忽略] 真实本地路径与私有覆盖项
├── .env.example                      # 凭证环境变量模板（纳入 Git）
├── .env                              # [Git忽略] B 站小号 Cookie（零 Gemini API Key）
├── .gitignore                        # 严格脱敏与忽略规则
├── ARCHITECTURE.md                   # 全局架构设计白皮书
├── SPEC_AND_TASKS.md                 # 本文件：详细规范与施工任务书
├── HANDOFF_LOG.md                    # 跨对话交接黑板
├── assets/
│   └── gemininest.ico                # 桌面小窗与 Win32 系统托盘自定义图标
├── scripts/
│   ├── create_companion_shortcut.py  # 一键生成桌面无黑框小窗快捷方式
│   ├── download_wd_tagger.py         # wd-tagger ONNX 权重与标签 CSV 下载校验器
│   └── inbox_gdoc_exporter.gs        # Google Apps Script 云端 .gdoc -> .txt 定时转换脚本
├── requirements.txt                  # v1 极简依赖清单（v2 依赖独立写于 requirements-v2.txt）
├── requirements-dev.txt              # 离线单元测试依赖（pytest）
├── src/
│   └── gemininest/
│       ├── __init__.py
│       ├── config.py                 # [模块1] 配置加载、合并与校验
│       ├── fetcher.py                # [模块2] B 站只读 Web API (httpx+WBI) / yt-dlp / 空降助手
│       ├── meta_filter.py            # [模块3] 前置黑名单硬过滤防火墙
│       ├── media_processor.py        # [模块4] 本地 FFmpeg 音频提取 / v2 抽帧与净版 MP4 重封
│       ├── safety_gate.py            # [模块5] 本地视觉安检门（v1 直通桩 / v2 wd-tagger ONNX）
│       ├── agent_bridge.py           # [模块6] Antigravity 宿主桥接层（前缀拼装/媒体清单/JSON校验/账本估算）
│       ├── memory_store.py           # [模块7] SQLite FTS5 + 艾宾浩斯 Top-8 引擎 + 账本
│       ├── sync_manager.py           # [模块8] Google Drive 纯文本正反向同步 + exports 独立隐藏 Git
│       ├── budget_ui.py              # 4 条件联合预算面板（Tk/Toplevel）+ 原子文件 IPC + 120s Toast 转发
│       ├── companion.py              # WinSta0\Default 桌面常驻轻量小窗 + Win32 系统托盘 + 状态看板
│       └── runner.py                 # 顶层调度入口（进程锁、三分支启动门禁、只读 status、--dry-run 离线回放）
├── data/                             # [全目录受控，均由主仓库 Git 忽略]
│   ├── cat_memory.db                 # [Git忽略] SQLite 主库
│   ├── exports/                      # [主仓库忽略，内部维护独立隐藏 .git 仓库留痕] 纯文本导出区
│   ├── snapshots/                    # [Git忽略] 黑匣子 Dry-Run 离线快照
│   ├── run_states/                   # [Git忽略] 跨命令巡逻活跃运行租约与 token 预估缓存
│   ├── budget_ipc/                   # [Git忽略] 跨桌面预算请求/确认/Toast 原子 IPC 文件区
│   ├── temp/                         # [Git忽略] 媒体临时区（两阶段用完即焚 + 租约安全兜底清扫）
│   └── models/                       # [Git忽略] v2 wd-tagger ONNX 权重目录
└── tests/                            # 离线单元测试（全离线、零网络请求）
```

### 1.2 云端映射目录（`<YOUR_GOOGLE_DRIVE_NEST>`，纯文本物理隔离）
```text
Gemini_Nest/
├── 0_active/                         # 当日最新纯文本视图（persona_snapshot.md / memory_summary.md / diary_latest.md）
├── 1_yesterday_readonly/             # 昨日只读镜像副本（先解除只读再覆写，后更新 0_active）
├── inbox/                            # Gemini Web 端反向补丁收件箱（Apps Script 导出 .txt + 手动 .md/.txt）
├── inbox_archived/                   # 已吸收合并的历史补丁归档
└── cold_backup/                      # [仅 v2 启用] py7zr AES-256 加密文件名冷备卷
```

---

## 2. 核心数据契约、错误码与工程规范

### 2.1 SQLite 核心表结构（`data/cat_memory.db`）
1. **`memories`（动态记忆主表）**
   * `id` (PK), `bv_id` (TEXT, 可空), `source` (`bili_watch` | `web_patch` | `system`), `title` (TEXT), `summary` (TEXT), `tags` (TEXT), `importance` (REAL, 1.0–10.0), `stability_days` (REAL, 由 I 映射初值 2/12/60), `recall_count` (INT, 默认 0), `created_at` (ISO8601), `last_recalled_at` (ISO8601).
2. **`memory_fts`（FTS5 虚拟检索表，`tokenize='trigram'`）**
   * 四列独立权重存储：`memory_id` (UNINDEXED), `title_col`, `desc_col`, `hot_comments_col`, `latest_comments_col`。
   * 检索公式：`bm25(memory_fts, 3.0, 1.5, 1.0, 0.5)`，结果结合艾宾浩斯保留度 $R = I \cdot e^{-\Delta t / S}$ 归一化融合，严格截断 **Top-8**（全局高分 3 条 + 场景关联 5 条）。
   * **中文 `< 3` 字短词回退规范（强制）**：SQLite FTS5 `trigram` 分词器要求查询词长度 ≥ 3 个字符，直接检索“鬼畜”“整活”“科普”等 1–2 字中文词会静默返回 0 条。`memory_store.py` 必须对 `< 3` 字符的查询词自动路由至 `LIKE` 子串 + `tags` 精确匹配回退检索，按相同列权重（`title 3.0 > desc 1.5 > summary/tags 1.0`）折算归一化相关度，与 ≥ 3 字符的 FTS5 `bm25()` 结果去重合并后再计算艾宾浩斯 Top-8。
3. **`worldbook`（稳定概念与好奇心词条表）**
   * `id` (PK), `keyword` (TEXT UNIQUE), `category` (`core_lore` | `curiosity_queue` | `preference`), `content` (TEXT), `status` (`active` | `consumed` | `archived`), `updated_at` (ISO8601).
4. **`watch_history`（已看去重与长视频断点追更表）**
   * `bv_id` (PK), `title`, `duration_sec`, `status` (`completed` | `in_progress` | `dropped` | `filtered_blacklist` | `filtered_safety`), `last_position_sec` (INT), `prev_recap` (TEXT, <= 50字前情提要), `pipeline_used` (`audio_only` | `sanitized_video` | `downgraded_audio`), `updated_at`.
5. **`budget_ledger`（本地算力账本表）**
   * `id` (PK), `run_id`, `window_bucket` (5小时窗口标识), `est_audio_tokens`, `est_video_tokens`, `est_text_tokens`, `est_cached_tokens`, `calibration_k` (REAL, 默认 1.0), `total_est_tokens`, `budget_ratio_used` (REAL), `status` (`ok` | `rate_limited_429` | `safety_blocked`), `created_at`.

### 2.2 八大核心模块 DTO 字段清单（纯标准库 `@dataclass(frozen=True)`）

各模块仅通过显式强类型 DTO 交互，严禁传递未校验的松散 `dict` 跨越模块边界：

1. **`config` 模块**
   * `AppConfig`: 聚合只读强类型子配置 `project`, `model_and_api`, `media_preprocess`（含 `ffmpeg_binary: str`）, `safety_alignment`（合并 `config.local.json` 的 `extra_blacklist` 并集）, `fetcher`, `memory`, `budget_and_runtime`, `sync`（含 `local_export_git_isolated: bool`）。
   * `SecretsConfig(bili_sessdata: str, bili_jct: str, bili_buvid3: str)`：全部敏感字段设 `field(repr=False)`，严禁写入日志、异常堆栈或序列化导出。
2. **`fetcher` 模块**
   * `CommentItem(comment_id: str, author: str, content: str, like_count: int, is_hot: bool, created_at: str)`
   * `SponsorSegment(start_sec: float, end_sec: float, category: str)`
   * `VideoMeta(bv_id: str, aid: int | None, title: str, desc: str, tname: str, tags: tuple[str, ...], author: str, duration_sec: int, view_count: int, like_count: int, pubdate_iso: str, source_channel: Literal["feed", "curiosity", "resume"], curiosity_keyword: str | None)`
   * `VideoDetail(meta: VideoMeta, subtitles_text: str, hot_comments: tuple[CommentItem, ...], latest_comments: tuple[CommentItem, ...], sponsor_segments: tuple[SponsorSegment, ...])`
   * `FetchBatchResult(candidates: tuple[VideoMeta, ...], snapshot_path: Path | None, consecutive_412: int, aborted: bool, error_code: ErrorCode | None)`
3. **`meta_filter` 模块**
   * `FilterResult(bv_id: str, allowed: bool, reason: str, matched_field: Literal["tname", "tags", "title", "none"], matched_rule: str)`
   * 规则校验前统一执行 `unicodedata.normalize("NFKC", s).casefold()`，支持 `config.local.json` 中 `extra_blacklist` 并集追加。
4. **`media_processor` 模块**
   * `PreparedMedia(bv_id: str, run_id: str, mode: Literal["audio_only", "sanitized_video", "downgraded_audio"], temp_dir: Path, payload_path: Path, mime_type: str, payload_bytes: int, effective_duration_sec: float, segment_index: int, segment_start_sec: float, segment_end_sec: float, trimmed_sponsor_sec: float, frame_count: int)`
5. **`safety_gate` 模块（v1/v2 热插拔抽象契约）**
   * 抽象基类 `BaseSafetyGate.inspect_frames(frame_paths: list[Path], cfg: SafetyConfig) -> SafetyGateResult`
   * `FrameScore(frame_index: int, general_score: float, unsafe_score: float, blacked_out: bool)`
   * `SafetyGateResult(passed: bool, safe_frame_paths: tuple[Path, ...], blackout_indices: tuple[int, ...], total_frames: int, blackout_ratio: float, downgrade_to_audio: bool, frame_scores: tuple[FrameScore, ...], engine: Literal["v1_passthrough", "v2_wd_tagger"])`
   * v1 `PassthroughSafetyGate`：直接返回 `passed=True, downgrade_to_audio=False, engine="v1_passthrough"`（且 v1 模式不触发抽帧）。
6. **`agent_bridge` 模块（Antigravity 宿主桥接层）**
   * `TokenEstimate(est_static_prefix_tokens: int, est_dynamic_text_tokens: int, est_audio_tokens: int, est_video_tokens: int, est_total_tokens: int, meets_implicit_cache_threshold: bool)`
   * `SelectionPacket(run_id: str, static_prefix_prompt: str, dynamic_menu_prompt: str, candidate_bv_ids: tuple[str, ...], token_estimate: TokenEstimate)`
   * `AppreciationPacket(run_id: str, bv_id: str, static_prefix_prompt: str, dynamic_context_prompt: str, media_abs_path: Path, media_mime_type: str, media_mode: Literal["audio_only", "sanitized_video", "downgraded_audio"], token_estimate: TokenEstimate)`
   * `SelectedVideo(bv_id: str, reason: str, target_mode: Literal["audio_only", "sanitized_video"])`
   * `SelectionResponse(selected_videos: tuple[SelectedVideo, ...])`
   * `AppreciationResponse(bv_id: str, public_diary: str, private_thought: str, impression_score: int, memory_summary: str, continue_watching: bool, recap_50chars: str, next_curiosity_keywords: tuple[str, ...], safety_blocked: bool, block_reason: str | None)`
7. **`memory_store` 模块**
   * `MemoryRecord(id: int, bv_id: str | None, source: Literal["bili_watch", "web_patch", "system"], title: str, summary: str, tags: tuple[str, ...], importance: float, stability_days: float, recall_count: int, created_at: str, last_recalled_at: str)`
   * `RecalledMemory(record: MemoryRecord, retention_score: float, relevance_score: float, final_weight: float, bucket: Literal["global", "contextual"], display_label: str)`
   * `BudgetStatus(window_bucket: str, used_tokens: int, capacity_tokens: int, used_ratio: float, allowed_ratio: float, remaining_tokens: int, in_cooldown: bool)`
8. **`sync_manager` 模块**
   * `MemoryPatch(patch_id: str, source_file: Path, op: Literal["add_memory", "boost_memory", "add_curiosity", "suggest_persona_tweak"], payload: dict[str, Any], validated: bool, reject_reason: str | None)`
   * `SyncReport(exported_files: tuple[Path, ...], export_git_commit_hash: str | None, yesterday_rotated: bool, active_synced_count: int, patches_applied: int, patches_rejected: int, archived_files: tuple[Path, ...], errors: tuple[str, ...])`

### 2.3 统一错误码表（`ErrorCode(StrEnum)`）

| 错误码 | 触发场景 | 强制处理策略 |
| :--- | :--- | :--- |
| `E1001_CONFIG_INVALID` | `config.json` / `config.local.json` 校验失败 | 启动即中止，不触网 |
| `E1002_SECRETS_MISSING` | `.env` 缺失必要小号凭证 | 启动即中止，不触网 |
| `E2001_BILI_AUTH_EXPIRED` | 小号 Cookie 心跳失效 | 本地熔断 + Toast 提醒，零 Token 消耗 |
| `E2002_BILI_RATE_LIMIT_412` | 连续 2 次 `-412` 触发风控 | 本地立即中止，零 Token 消耗 |
| `E2003_FETCH_NETWORK_ERROR` | B 站/SponsorBlock 瞬时网络异常 | 按退避策略重试后跳过单条或中止 |
| `E3001_BLACKLIST_REJECTED` | 前置黑名单命中 | 写 `watch_history(status='filtered_blacklist')`，严禁下载 |
| `E4001_FFMPEG_FAILED` | FFmpeg 抽取/裁剪/重封失败 | 立即清理该 BV 临时目录并跳过 |
| `E4002_PAYLOAD_TOO_LARGE` | 净版媒体超过 `inline_max_payload_mb` (95 MB) | 触发分段或降级纯音频，超限则跳过 |
| `E5001_SAFETY_GATE_DOWNGRADED` | v2 抹黑率 `> 15%` | 整条自动降级为 `downgraded_audio` 纯音频 |
| `E6001_HOST_SAFETY_BLOCKED` | 宿主鉴赏遇 `SAFETY`/`PROHIBITED_CONTENT` | 写 `filtered_safety`，调用 `burn-temp` 焚毁临时媒体，优雅跳过 |
| `E6002_HOST_RATE_LIMIT_429` | 宿主触发 5 小时额度限流 | 记录账本并开启 5 小时冷却 |
| `E6003_SCHEMA_INVALID` | 宿主输出 JSON 校验不通过 | 有限重试（`max_api_retries_non_safety`）后跳过该 BV |
| `E7001_DB_LOCKED` | SQLite 写入锁冲突 | 短退避重试（`timeout=10.0` + WAL 模式） |
| `E7002_LOCKFILE_CONFLICT` | `.cat.lock` 检测到存活 PID | 静默退出，防止休眠补发撞车 |
| `E8001_CLOUD_DRIVE_UNMOUNTED` | `<YOUR_GOOGLE_DRIVE_NEST>` 未挂载 | 保留本地 `data/exports/` 落盘与独立 Git 提交，记录告警 |
| `E8002_SYNC_EXT_VIOLATION` | 非白名单扩展名（`.md`/`.json`/`.txt` 以外）试图入云 | 立即阻断并抛安全异常 |
| `E8003_PATCH_PARSE_FAILED` | `inbox/` 补丁语法非法 | 拒绝合并并归档留痕 |
| `E8004_PATCH_POLICY_REJECTED` | 反向补丁试图改写安全红线或超长人设 | 拒绝合并并归档留痕 |

### 2.4 Antigravity 宿主驱动协议、媒体加载核验与隐式前缀缓存规范
1. **宿主媒体读取实测结论（Phase 0 已实测通过）**：
   * 实测确认 Antigravity 宿主原生 `read_file(Path="<绝对路径>")` 直接支持读取本地 `48kbps` 单声道 `.m4a`（识别为 `audio/mp4`）与 `480p 1fps` 净版 `.mp4`（识别为 `video/mp4`），零外部 LLM SDK、零额外 API Key，完整复用 Antigravity Pro 订阅额度。
2. **宿主驱动两阶段“用完即焚”生命周期（防临时媒体泄漏铁律）**：
   * **Step A（准备）**：宿主调用 CLI `prepare-media --run-id <id> --bv <bv>` → `media_processor` 生成 `data/temp/<run_id>/<bv>/audio.m4a`（纯英文路径），`agent_bridge` 输出包含绝对路径与静态/动态 Prompt 的 `AppreciationPacket` JSON。
   * **Step B（鉴赏）**：Antigravity 宿主调用 `read_file` 加载该绝对路径媒体并生成严格 JSON 鉴赏结果。
   * **Step C（提交与焚毁）**：无论 Step B 成功、校验失败还是触发 `SAFETY`/`PROHIBITED_CONTENT`，宿主**必须**立即调用 `commit-appreciation` 或 `burn-temp --run-id <id> --bv <bv>`，Python 在 `try...finally` 中物理删除 `data/temp/<run_id>/<bv>/`。
   * **兜底清扫**：`runner.py` 每次启动及退出时自动执行 `sweep_stale_temp()`，清除任意上次异常中断遗留的孤立临时目录，确保磁盘零残留。
3. **隐式前缀缓存组织规范（稳定 `4,500–5,200 Tokens` > `4,096` 阈值）**：
   * `agent_bridge` 组装的静态前缀严格由 5 块逐字固定内容构成：
     1. `.agent/rules/cat-soul.md`（~850–1,000 Tokens）；
     2. **黄金 Few-Shot 范例库**（3 组不同分档 2 分/6 分/9 分大脑鉴赏完整 JSON 范例 + 1 组小脑选片反营销号正反例，~1,500–1,800 Tokens）；
     3. **审美评分标尺与感知诚实细则**（1–10 分锚点、各分区听觉鉴赏维度、v2 黑帧忽略规范，~700–900 Tokens）；
     4. **完整标准 JSON Schema + 边界校验规范**（~700–900 Tokens）；
     5. **月度静态核心世界书**（固定世界观与术语表，~500–800 Tokens）。
   * 所有动态变量（日期、`run_id`、当日好奇心词条、Top-8 激活记忆、视频元数据/字幕/评论）一律置于最底部动态区，保护连续多条鉴赏的隐式缓存命中。
4. **Web 端反向补丁协议（`[MEMORY_PATCH]`）**：
   * 纯文本块，采用标准 YAML/JSON 键值对，仅允许白名单操作：`add_memory`、`boost_memory`、`add_curiosity`、`suggest_persona_tweak`（人设变更需经格式与长度校验，拒绝修改安全红线）。
   * 云盘 `inbox/` 由云端 Google Apps Script 定时将新增 Google Docs（`.gdoc` 指针）导出为同名 `.txt`；`sync_manager` 同时原生兼容手动放入的 `.md`/`.txt`（忽略裸 `.gdoc` 指针文件不报错），吸收后移入 `inbox_archived/`。

### 2.5 Windows 工程避坑规范（强制落地）
1. **UTF-8 与中文路径编码**：
   * 所有文件读写显式 `encoding="utf-8"`；所有 `subprocess.run()` 强制 `shell=False`、`encoding="utf-8"`、`errors="replace"` 并注入 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`。
   * Git 命令统一带 `-c core.quotepath=false`；所有临时媒体固定落在纯英文路径 `data/temp/`，避免 FFmpeg 遇到中文路径报错。
2. **Google Drive 流式挂载与只读覆写**：
   * `sync_manager` 同步前先探活 `cloud_nest_dir` 并短重试；若未挂载则降级为仅完成本地 `data/exports/` 落盘与独立 Git 提交（记录 `E8001_CLOUD_DRIVE_UNMOUNTED`），不丢数据。
   * 轮转 `1_yesterday_readonly/` 前，先对目标旧文件递归 `os.chmod(p, stat.S_IWRITE)` 清除只读属性，再以整文件覆写（避免虚拟盘 `.tmp` 原子重命名引发 `file (1).md` 冲突副本），写入后容错恢复 `S_IREAD` 并在文本顶部附 `<!-- READONLY SNAPSHOT: DO NOT EDIT -->` 标记。
3. **`data/exports/` 独立隐藏 Git 仓库**：
   * 主工程 `.gitignore` 严格忽略 `data/exports/`；`data/exports/` 内部初始化独立本地 Git 仓库（`git -C data/exports commit`），彻底隔离私人冲浪日记历史与未来可开源的工程代码库。
4. **`tkinter` 无头 / 隔离桌面与跨桌面前端**：
   * `budget_ui.py` 与 `companion.py` 支持 4 条件联合交集上限（公版默认 `0.4` / `100` / `0.30` / `30`）及 `--headless` / CLI 参数覆盖；当隔离桌面（`WinSta0\exebox-*`）直接弹窗不可见时，通过 `data/budget_ipc/` 原子文件 IPC 交由真实桌面 `WinSta0\Default` 常驻小窗以 `tk.Toplevel` 弹窗并代发 120s TTL Toast，锁屏 `tk.TclError` 或 60s `time.monotonic()` 超时自动静默按当前有效交集默认值继续。

---

## 3. 强制运维与安全红线核对表
* [ ] **二游与敏感分区全杀**：`meta_filter` 必须覆盖所有主流二游、宅舞、时尚、美妆、MMD 及政治敏感词，且同时扫描 `tname`、`tags` 与 `title`（支持 `config.local.json` 并集扩展）。
* [ ] **零硬编码**：所有阈值、模式开关、路径策略均从 `config.json` + `config.local.json` 读取。
* [ ] **两阶段用完即焚 + 启停兜底清扫**：`data/temp/` 单条鉴赏后立即清理，且 `runner` 启停自动清扫孤立残留。
* [ ] **云盘纯文本铁律**：`sync_manager` 内置扩展名白名单（仅允许 `.md` / `.json` / `.txt` 写入 `0_active/`），物理阻断任何音视频或 `.db` 文件进入常规同步目录。
* [ ] **安全截断不死循环**：捕获 `SAFETY` / `PROHIBITED_CONTENT` 后记录日志、焚毁临时媒体并跳过该 BV，继续下一条。
* [ ] **B 站风控零 Token 熔断**：连续 2 次 `-412` 或 Cookie 心跳失效直接本地中止，绝不唤醒宿主大模型。

---

## 4. 分阶段施工任务清单（模块化小步提交）

### Phase 0：环境初始化与 Git 基建（架构规划对话完成验收）
- [x] 在 Anaconda 创建独立虚拟环境 `gemininest`（Python 3.11）并安装 v1 最小依赖（`yt-dlp`, `httpx`, `python-dotenv`, `winotify`, `pytest`）。
- [x] 验证原生 `FFmpeg`（`<YOUR_FFMPEG_PATH>`，6.1.1-full_build，支持 `aac`/`m4a`/`libx264`）并加入用户 `PATH` 及 `config.local.json`。
- [x] 验证 `<YOUR_GOOGLE_DRIVE_NEST>` 读写权限，并补建 `0_active`、`1_yesterday_readonly`、`inbox`、`inbox_archived` 四个子目录。
- [x] 实测核验 Antigravity 宿主 `read_file` 原生读取本地 `48kbps` 单声道 `.m4a`（`audio/mp4`）与 `.mp4`（`video/mp4`）通道畅通。
- [x] 落盘 `.gitignore`、`config.json`、`.env.example`、`config.local.example.json`、`requirements.txt`、`requirements-dev.txt`、空模块目录骨架及修订后的核心文档。
- [x] 初始化主仓库 Git（`main` 分支初始提交 `chore: init project docs, templates, skeleton and strict gitignore`），切出开发分支 `feat/v1-mvp`，并在 `data/exports/` 初始化独立隐藏 Git 仓库。

### Phase 1：配置、黑名单防火墙与记忆引擎（纯离线、零外部依赖）
- [x] 实现 `config.py`：加载合并 `config.json` + `config.local.json`（黑名单并集追加）+ `.env`（`SecretsConfig` 脱敏 `repr=False`），启动时做完整参数合法性校验与 `ErrorCode` 导出。
- [x] 实现 `meta_filter.py`：实现 NFKC+casefold 规范化的二游/敏感分区/标签/标题前置硬拦截及全覆盖单元测试。
- [x] 实现 `memory_store.py`：SQLite 四表建表（WAL 模式）、FTS5 `trigram` + `bm25()` 四列加权、**`< 3` 字中文短词 `LIKE`+`tags` 回退检索**、艾宾浩斯 Top-8 显式标签渲染及本地算力账本。
- [x] 提交阶段代码：`feat(core): add config, meta_filter and sqlite memory_store with unit tests`。

### Phase 2：B 站只读拉取、v1 音频管线与安检门桩接口
- [x] 实现 `fetcher.py`：基于轻量 `httpx`（WBI 签名）+ `yt-dlp` 实现小号 Cookie 心跳检测、推荐流、好奇心搜索、字幕、热评/新评、空降助手时间戳拉取；内置 `-412` 退避熔断与黑匣子快照落盘（v1 暂不挂载 MCP）。
- [x] 实现 `media_processor.py`（v1 部分）：调用本地 `FFmpeg` 提取 `48kbps` 单声道 `m4a`、空降裁剪、`burn_temp_dir()` 与 `sweep_stale_temp()` 兜底清理。
- [x] 实现 `safety_gate.py`（v1 `BaseSafetyGate` 抽象基类与 `PassthroughSafetyGate` 直通桩，严格对齐 v2 契约）。
- [x] 提交阶段代码：`feat(media): add fetcher, v1 audio pipeline and safety_gate stub`。

### Phase 3：Antigravity 宿主桥接层、预算弹窗与离线 Dry-Run 闭环
- [x] 实现 `agent_bridge.py`：4,500–5,200 Tokens 静态前缀缓存拼装（含 Few-Shot 范例/评分标尺/完整 Schema/核心世界书）、`SelectionPacket`/`AppreciationPacket` 打包、宿主 JSON 严格校验、`SAFETY`/`PROHIBITED_CONTENT` 优雅跳过、本地算力账本估算。
- [x] 实现 `budget_ui.py`：60 秒非抢占 `tkinter` 预算弹窗（含 Session 0 / `--headless` 兜底与 `time.monotonic()` 倒计时）+ 高分宝藏视频 `winotify` 提醒。
- [x] 实现 `runner.py`：`.cat.lock` PID 存活校验锁、两阶段宿主驱动子命令状态机（`prepare-feed` / `prepare-media` / `commit-appreciation` / `burn-temp` / `sync`）与 `--dry-run` 黑匣子离线回放模式。
- [x] 使用离线快照跑通全链路 `--dry-run` 测试，零报错后提交：`feat(runtime): add agent_bridge, budget_ui and dry-run runner`。

### Phase 4：Google Drive 纯文本正反向同步、Web 联动与定时任务上线
- [x] 实现 `sync_manager.py`（并接入 `runner.py` Step 1/`sync`/Step 6 及 `--dry-run`）：纯文本扩展名白名单校验（`.md`/`.json`/`.txt`，阻断抛 `E8002`）、导出至 `data/exports/` + 独立隐藏 Git 自动提交、`1_yesterday_readonly/` 先解只读再覆写滚动备份、`0_active/` 刷新、扫描解析 `inbox/`（Apps Script 导出 `.txt` + 手动 `.md`/`.txt`，忽略裸 `.gdoc`）中 `[MEMORY_PATCH]` 并归档至 `inbox_archived/`。
- [x] 交付脱敏运维与跨端联动模板：云端 Google Apps Script 定时转换脚本（`scripts/inbox_gdoc_exporter.gs`）、Antigravity `Scheduled Tasks` 唤醒 Prompt 与 Gemini Web 端常驻 Skill + `Scheduled Actions` 配置文案（`docs/SETUP_AND_RUNTIME.md`）。
- [x] 真实小号低频试运行 1 次，确认云盘双端闭环与两阶段用完即焚全部生效，合并 `feat/v1-mvp` 至 `main` 并打标签 `v1.0.0`。

### Phase 5：[v2 分支 `feat/v2-visual`] 可插拔视觉升级与加密冷备（独立演进）
- [x] 从 `main` 切出分支 `feat/v2-visual`，新建 `requirements-v2.txt`（`onnxruntime`、`pillow`、`numpy`、`py7zr`）。
- [x] 升级 `safety_gate.py`：接入本地 `wd-tagger` ONNX（支持 `dml`/`cuda`/`cpu`/`auto` 多级路由与优雅回退）、`min_general_score`/`max_sensitive_score`/`max_unsafe_score` 三重护栏、逐帧打分、纯黑帧替换、超 15% 抹黑率提前熔断（Early-Exit）与自动降级纯音频。
- [x] 升级 `media_processor.py`：实现按配置分辨率/帧率抽帧 → 安检门过检 → 严格帧一致重封净版 MP4（`<= 95 MB` 供宿主 `read_file` 直接加载），并输出四段耗时诊断。
- [x] 升级 `agent_bridge.py`：支持 `media_resolution`（`low`/`medium`/`high`）与 `video_processing_mode`（`static`/`agentic`/`auto` 按 300 秒阈值切换）。
- [x] Phase 5-B 真实权重部署与首次“睁眼”试飞验收（`SmilingWolf/wd-vit-tagger-v3` + Windows DirectML GPU 加速 + 净版 `sanitized.mp4` 宿主 `read_file` 真实观影、用完即焚与云盘同步闭环），合并 `feat/v2-visual` 至 `main` 并打附注标签 `v2.0.0`。
- [ ] 可选扩展：实现本地赛博相册与每周 `py7zr`（AES-256 + `header_encryption=True`）加密打包上传 + 错周延迟清理。

### Phase 6：[v3 分支 `feat/v3-companion`] 4 条件联合预算、桌面常驻托盘前端与三分支启动门禁
- [x] 升级 `budget_ui.py` / `config.py` / `contracts.py` / `runner.py`：实现 4 条件联合交集上限（`max_use_ratio` / `max_videos` / `reserve_ratio` / `max_minutes`）、CLI「仅传 1 项其余不限」与 `MAX_SELECT_PER_BATCH_GUARDRAIL = 30` 防滥用护栏。
- [x] 实现 `companion.py`、`assets/gemininest.ico` 与 `scripts/create_companion_shortcut.py`：真实桌面 `WinSta0\Default` 常驻轻量 `tkinter` 小窗（单实例互斥锁 + `.companion.lock` 心跳）、纯 Win32 `Shell_NotifyIconW` 系统托盘收纳/还原、高 DPI 自适应布局、5h 窗口起点与日记/记忆计数展示。
- [x] 实现三分支启动门禁与只读接口：扩展 `runner.py status` 只读看板接口；在 `prepare-feed` 接入小窗 IPC 握手（`desktop_companion_offline` / `desktop_companion_aborted`）、云盘挂载门禁（`cloud_drive_unmounted` / `E8001`）与 120s TTL Toast 代发，全量离线单测 `163/163` 通过后合并回 `main` 并发布 `v3.0.0`。
