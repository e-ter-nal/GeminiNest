# GeminiNest Cross-Conversation Handoff Log

> **交接铁律**：每次 Antigravity 开发/规划对话结束前，必须更新本文件底部状态；新开对话的第一步必须先阅读 `ARCHITECTURE.md`、`SPEC_AND_TASKS.md` 与本文件。

---

## 当前全局状态（Current Snapshot）
* **当前阶段**：✅ **v3 桌面常驻轻量前端（`WinSta0\Default`）+ 三分支启动门禁 + 只读 `status` 接口完成开发与全离线验收**（离线单元测试 **161/161 全绿**，零网络、零真实数据触碰；保留提交于特性分支 `feat/v3-companion`，暂未合并 `main` 且未 `git push`）
* **当前分支**：`feat/v3-companion`（基于 `main` 切出）
* **当前激活模式**：`config.json` 默认保持 v1 `audio_only`、`wd_tagger_enabled: false`、4 条件公版安全默认预算（`0.4` / `100` / `0.30` / `30`），以及桌面前端与启动门禁默认配置（`companion_policy: "required"`、`ipc_dir: "data/budget_ipc"`、`ipc_ack_timeout_sec: 2.5`、`require_cloud_mount_on_start: true`）；未改动本机私有 `config.local.json`
* **已落盘与核验基建**：
  * `README.md`、`LICENSE`（MIT）、`ARCHITECTURE.md`、`SPEC_AND_TASKS.md`、`docs/SETUP_AND_RUNTIME.md`（已并列补充「桌面小窗交互模式」与「`--headless` 纯无头自动化模式」，并在第 3.3 节标准唤醒 Prompt 补充 `desktop_companion_offline` / `desktop_companion_aborted` / `cloud_drive_unmounted` 明确中文汇报与立即中止指令）
  * `.agent/rules/cat-soul.md`、`config.json`、`.gitignore`（新增屏蔽 `data/budget_ipc/`、`data/.companion.lock`、`data/*.lock`）、`.env.example`、`config.local.example.json`、`requirements.txt`、`requirements-v2.txt`、`requirements-dev.txt`、`pyproject.toml`
  * 核心代码模块与脚本：`src/gemininest/{contracts,config,meta_filter,memory_store,fetcher,media_processor,safety_gate,agent_bridge,budget_ui,companion,sync_manager,runner}.py`、`scripts/create_companion_shortcut.py`、`scripts/download_wd_tagger.py`、`scripts/inbox_gdoc_exporter.gs`
  * 全离线单元测试：`tests/test_{config,meta_filter,memory_store,fetcher,media_processor,safety_gate,download_wd_tagger,agent_bridge,budget_ui,companion,runner,sync_manager}.py`（`pytest` **161/161 全绿**，零外部网络请求、真实 `data/` 与真实云盘目录 `<YOUR_GOOGLE_DRIVE_NEST>` 零触碰）
  * 本地私有资产（均被 `.gitignore` 严格忽略）：`config.local.json`、`data/models/model.onnx`（`SmilingWolf/wd-vit-tagger-v3`，361.00 MB）与 `data/models/selected_tags.csv`（10,861 标签）、`data/cat_memory.db`（4 条真实巡逻记忆）、`data/exports/` 内独立隐藏 Git 仓库
  * Anaconda 环境 `gemininest`（Python 3.11.18 + SQLite 3.54.1 FTS5 `trigram` + `yt-dlp`、`httpx`、`python-dotenv`、`winotify`、`pytest`、`Pillow 10.4.0`、`numpy 2.4.6`、`onnxruntime-directml 1.24.4`）
  * Google Drive 云盘目录：`<YOUR_GOOGLE_DRIVE_NEST>\{0_active,1_yesterday_readonly,inbox,inbox_archived}` 均已建齐并通过双端实飞同步验证

## 环境约束备忘（Environment Constraints）
* **操作系统**：Windows（主工程位于 `<YOUR_PROJECT_DIR>`，云盘挂载位于 `<YOUR_GOOGLE_DRIVE_NEST>`）
* **Python 环境**：Anaconda 独立环境 `gemininest`（`<YOUR_CONDA_PYTHON>`，Python 3.11）
* **本地工具**：原生 `FFmpeg 6.1.1-full_build`（`<YOUR_FFMPEG_PATH>`，已加入当前用户 `PATH` 并写入 `config.local.json`，支持 `aac`/`m4a`/`libx264`/`mp4`）；v1 暂不安装 `uv`/`uvx` 与 MCP
* **网络隔离**：Clash 必须将 B 站域名走 `DIRECT`，Google 流量锁定单一稳定节点
* **安全红线**：严禁将 `.env`、B 站 Cookie、`config.local.json`、`cat_memory.db`、临时媒体、ONNX 模型权重提交至 Git 或上传云盘常规同步目录；**注意**：云盘根目录遗留有一个旧 `Videos/` 目录（2026-11-18 创建），提醒用户人工清理以彻底满足纯文本物理隔离

## 已决策的架构变更记录（Decision Log）
1. **[重大变更]** 推翻原片直传与散装多图方案；v1 专注 `48kbps` 单声道 `m4a` 纯音频+文本 MVP，v2 热插拔本地 `wd-tagger` 全帧安检+黑帧抹除+净版 MP4（<=95 MB）。
2. **[重大变更]** 取消云盘 `Videos/` 明文视频目录与 v1 GIF 相册；Google Drive 严格物理隔离为**纯文本同步枢纽**（v2 冷备强制 `py7zr` AES-256 加密文件名）。
3. **[安全红线]** 新增 `meta_filter` 前置硬过滤模块，下载前全量拦截主流二游及敏感分区标签（支持 `config.local.json` 并集扩展）。
4. **[Phase 0 拍板]** 采用 **Antigravity 宿主驱动模式**（零外部 Gemini API Key，复用 Antigravity Pro 订阅额度），将 `llm_client.py` 重构为 `agent_bridge.py`；已实测确认宿主原生 `read_file` 直接支持 `audio/mp4`（`.m4a`）与 `video/mp4`（`.mp4`），并确立“准备 → 宿主鉴赏 → 提交/焚毁 + 启停兜底清扫”两阶段用完即焚协议。
5. **[Phase 0 拍板]** 主仓库 `.gitignore` 维持屏蔽 `data/exports/`，在 `data/exports/` 内维护**独立隐藏 Git 仓库**隔离日记历史与工程代码。
6. **[Phase 0 拍板]** `inbox/` 反向补丁采用云端 Google Apps Script 定时将 `.gdoc` 转存同名 `.txt` + 原生兼容手动 `.md`/`.txt`；`fetcher.py` v1 直接使用 `httpx`（只读 Web API + WBI 签名）+ `yt-dlp`，暂不挂载 `bilibili-mcp-agent` MCP。

## 下一对话启动入口（Next Action）
* **分支与合并建议（`feat/v3-companion` 已就绪，暂未合并 `main` 或 `git push`）**：
  1. **真机桌面验收后合并 `main`**：运行 `<YOUR_CONDA_PYTHON> scripts/create_companion_shortcut.py` 在桌面生成快捷方式并双击启动「GeminiNest 桌面小窝助手」，触发 1 次 `Scheduled Tasks`（或 `prepare-feed`）核验 `WinSta0\Default` 跨桌面 `Toplevel` 弹窗、Toast 代发与云盘挂载门禁；确认无误后执行 `git checkout main && git merge --no-ff feat/v3-companion`（建议可打标签 `v3.0.0`）并按需 `git push`。
  2. 可按 `docs/SETUP_AND_RUNTIME.md` 更新定时任务唤醒 Prompt（新增对 `desktop_companion_offline` / `desktop_companion_aborted` / `cloud_drive_unmounted` 的中文明确终止汇报）。
  3. 如需推进可选扩展（Phase 5 末项）：安装 `py7zr` 实现本地赛博相册与每周 AES-256（`header_encryption=True`）加密冷备卷及错周清理。
  4. 建议人工清理云盘历史遗留目录 `<YOUR_GOOGLE_DRIVE_NEST>\Videos`（2026-11-18 创建），确保云盘 100% 纯文本隔离。

---

## 历史对话交接记录（按时间倒序追加）
### [v3 Companion & 3-Branch Startup Gate - 桌面常驻轻量前端 + 跨桌面 IPC + 只读 `status`] - 2026-11-21
* **分支与提交策略**：基于 `main` 新建并保留提交于 **`feat/v3-companion`**（暂不合并 `main`，暂不 `git push`）。
* **核心实现**：
  * **配置与契约扩展（`.gitignore` / `config.json` / `contracts.py` / `config.py`）**：
    * `.gitignore` 新增忽略 `data/budget_ipc/`、`data/.companion.lock`、`data/*.lock`。
    * `config.json` `budget_and_runtime` 新增公版默认值：`companion_policy: "required"`、`ipc_dir: "data/budget_ipc"`、`ipc_ack_timeout_sec: 2.5`、`require_cloud_mount_on_start: true`；`contracts.py`（`CompanionPolicy`、`BudgetAndRuntimeConfig`）与 `config.py` 完整向后兼容旧 `config.local.json` 并校验合法性；`budget_ui.py` `BudgetDecisionSource` 新增 `"companion_auto_default"`。
  * **只读日记计数与 `runner status`（`memory_store.py` / `runner.py`）**：
    * `MemoryStore.count_diary_entries()` 轻量只读计数；`python -m gemininest.runner status` 绝不持有 `.cat.lock` 且不创建缺失 DB 文件，返回 5h 窗口算力（`used_tokens`/`capacity_tokens`/`used_ratio`/`remaining_tokens`/`in_cooldown`）、`pipeline_mode`、`cloud_mounted`、`cat_lock_active`、`active_run_count`、`companion_online`、`diary_count` 及最近 3 条 `recent_diaries`。
  * **三分支启动门禁、原子 IPC 握手与 Toast 代发（`runner.py` / `budget_ui.py`）**：
    * 在 `prepare-feed`（及共用 `check-budget`）发起任何 B 站请求、吸收 `inbox/` 或写库建租约前执行：CLI 豁免（`--headless` 或显式预算参数）→ 分支 A（校验 `.companion.lock` 存活/工作区/心跳并完成 `req_` → `ack_` 原子 IPC 握手，未通过返回**退出码 `0`** + `desktop_companion_offline`）→ 分支 B（在线巡逻 `require_cloud_mount_on_start=true` 且非 `--offline`/`--skip-inbox` 时，**即便带 `--headless` 也在弹窗前**执行 `SyncManager.probe_cloud_nest()`，未挂载返回**退出码 `0`** + `cloud_drive_unmounted` / `E8001_CLOUD_DRIVE_UNMOUNTED`）→ 分支 C（轮询 `res_` 含中途退出检测 `desktop_companion_aborted` 与 60s 超时默认继续）。
    * 非 `--headless` 且前端在线时，`notify_high_score_video` 与 `notify_auth_expired` 通过原子 `toast_<id>.json`（120s TTL）交由桌面前端 `winotify` 在 `WinSta0\Default` 代发，无前端或失败时安全降级。
  * **轻量 `tkinter` 桌面常驻小窗（`src/gemininest/companion.py` & `budget_ui.py`）**：
    * 纯标准库 + 已有 `winotify`；Windows `CreateMutexW` 命名互斥锁 + 原子 `data/.companion.lock` 心跳防双开；严格单 `tk.Tk()` 实例，通过 `budget_ui.open_budget_toplevel()` 以 `tk.Toplevel(master=root)`（不设 `transient`，最小化到任务栏仍可弹窗）复用 4 条件联合上限面板；`root.after(250, ...)` 非阻塞 IPC 轮询 + daemon 线程定时调用 `runner status` 经 `queue.Queue` 回传渲染；启用 best-effort Win32 DPI 感知与自适应尺寸；支持「每次弹窗询问」/「自动默认值执行」单选切换与「退出」干净清理；严禁持有 `.cat.lock` 或直接写 SQLite。
  * **桌面快捷方式生成器（`scripts/create_companion_shortcut.py`）**：
    * 零硬编码动态探测仓库根、`gemininest` `pythonw.exe` 与本机桌面路径，支持 `--dry-run` 与 `--output`，通过 PowerShell `WScript.Shell` 创建无黑框 `PYTHONPATH=src` 启动快捷方式。
  * **文档与回归**：同步更新 `README.md` 与 `docs/SETUP_AND_RUNTIME.md`（并列说明两种启动方式与 3.3 节唤醒 Prompt 门禁中止汇报）；新增/扩充 `tests/test_{companion,runner,budget_ui,config,memory_store}.py`。
* **回归验证**：
  * `<YOUR_CONDA_PYTHON> -m pytest -v`：**161 passed**（100% 全绿，零网络、真实 `data/` 与 `<YOUR_GOOGLE_DRIVE_NEST>` 零触碰）。
  * `<YOUR_CONDA_PYTHON> scripts/create_companion_shortcut.py --dry-run` 解析验证通过；Git 跟踪文件与新增文件本机路径/用户名扫描 0 残留。
### [Budget System Upgrade - 4 条件联合约束（交集上限，先触顶先停）] - 2026-11-21
* **核心升级（留在 `main` 分支，暂不 `git push`）**：
  * **配置与契约升级（`config.json` / `contracts.py` / `config.py` / `memory_store.py`）**：
    * 在 `config.json` 的 `budget_and_runtime` 显式暴露公版保守默认 4 条件：`default_window_budget_ratio: 0.4`、`default_max_videos: 100`、`default_reserve_ratio: 0.30`、`default_max_minutes: 30`（四项均支持在 `config.local.json` 覆写为数值或 `null` 表示不限；未触碰本地已有 `config.local.json`）。
    * 更新 `BudgetPlan` 与 `BudgetAndRuntimeConfig` 同时持有 `max_use_ratio`、`max_videos`、`reserve_ratio`、`max_minutes`、`deadline_epoch` 及交集换算字段 `effective_allowed_ratio = min(窗口当前剩余, 非空 max_use_ratio, 非空时 max(0.0, 1.0 - reserve_ratio - current_window_used_ratio))`（两项均空时按窗口剩余）；完整兼容旧配置字段与旧 `run_states` JSON；`MemoryStore` 新增 `get_run_used_tokens()` 并兼容 `default_window_budget_ratio=None`。
  * **GUI 弹窗升级（`budget_ui.py`）**：
    * 移除 4 选 1 互斥单选按钮，改为 **4 个独立输入框**（明确标注「留空 = 不限」，默认回填生效配置 4 项默认值），新增 **「清空条件」** 快捷按钮（一键清零 4 框方便只填 1 项），保留 **「确认出发」** 与 **「今日休息」**，顶部实时预览综合换算后的本次可用 Token 比例与 60 秒 `time.monotonic()` 单调时钟倒计时；完整保留隔离桌面（`exebox-*`）快退、锁屏 `TclError` 兜底、`SW_HIDE` 辅助子进程与非抢占置顶显示。
  * **CLI 参数与逐条交集熔断升级（`runner.py`）**：
    * 移除 `prepare-feed` 与 `check-budget` 间 4 个预算参数的互斥校验，支持任意组合同时传入；只要显式传入任意 1 个或多个参数，即自动跳过弹窗且**仅生效传入项、其余未传项本轮自动设为 `None`（不限）**；4 项均未传且走超时/`--headless`/隔离桌面兜底时采用配置 4 项默认值联合组合。
    * 优化 `--max-select` 与 `max_videos` 协同（未传 `--max-select` 时，指定 `max_videos` 后单轮选片随 `min(max_videos, 30)` 推进不再被默认 `4` 卡死；保留单次请求防滥用护栏 `MAX_SELECT_PER_BATCH_GUARDRAIL = 30`）。
    * `prepare-media`（及 `run_dry_run`）每次执行前同时校验全部非空条件（`rest_today`、去重视频数 `>= max_videos`、`now >= deadline_epoch`、本轮消耗触及 `max_use_ratio`、窗口剩余触及 `reserve_ratio` 底线、429 冷却、以及 4 项全留空时的 5h 窗口 100% 物理上限 `window_exhausted`），任一先触顶即返回退出码 0 + `{"ok": true, "stopped_reason": "budget_limit_reached", "stop_detail": {"triggered_by": [...]}}`。
  * **文档与测试同步**：同步更新 `docs/SETUP_AND_RUNTIME.md`、`README.md` 与 `HANDOFF_LOG.md`；扩充 `tests/test_config.py`、`tests/test_budget_ui.py`、`tests/test_runner.py`、`tests/test_memory_store.py`。
* **回归验证**：`<YOUR_CONDA_PYTHON> -m pytest -v` **151 passed**（100% 全绿，零网络、零真实数据触碰）。
* **提交记录**：`main` 分支提交 `feat(budget): upgrade to 4-condition combined intersection budget limits`。
### [Core Reliability Hardening - 外部审计 3×P1 + 2×P2 修复] - 2026-11-20
* **修复内容（留在 `main` 分支，暂不 `git push`）**：
  * **P1-1（持久化完整手账）**：在 `memory_store.py` 新增向后兼容平滑迁移逻辑与 `diary_entries` 表（及可重入原子 `transaction()`），`commit-appreciation` 将完整 `public_diary`、`private_thought`、`pipeline_used`、`run_id` 随鉴赏事务原子入库；`sync_manager.py` 导出 `diary_latest.md` 时直接从 SQLite 读取最新完整手账，并按 `config.json` 新增开关 `sync.export_private_thought`（默认 `true`）控制是否渲染 `private_thought`。
  * **P1-2（跨命令巡逻运行租约与隔离清理）**：复用 `data/run_states/<run_id>.json` 为整轮跨 CLI 巡逻维护带 TTL 的活跃运行租约（`prepare-feed` 创建、`prepare-media`/`commit-appreciation` 续期、`sync --run-id <run_id>` 或 `burn-temp --run-id <run_id> --release-lease` 释放）；`burn-temp --run-id <run_id>` 仅清理当前 `run_id` 目录，启动清扫 `sweep_stale_temp()` 仅回收已过期且不属于任何活跃 `run_id` 的孤儿目录。
  * **P1-3（补丁幂等账本与同修订防覆写）**：在 `memory_store.py` 新增 `applied_patches` 幂等账本表，`sync_manager.py` 基于规范化内容哈希生成与文件名/格式无关的稳定 `patch_id`，在单一 SQLite 事务内原子完成「判重 → 应用补丁 → 写入账本」，重放补丁幂等跳过并干净归档；`sync_manifest.json` 写入确定性 `content_hash`，`sync_to_cloud()` 轮转 `1_yesterday_readonly/` 前比对现有 `0_active/` 修订标识，同修订连续导出跳过轮转以保护上一版真实历史快照。
  * **P2-1（Token 预估复用与去硬编码 `--cache-hit`）**：`prepare-media` 将该 BV 的完整 `token_estimate` 与视频元数据写入 `run_states/<run_id>.json`，`commit-appreciation` 优先复用并默认记录 `budget_ledger.cache_status = "unknown"`（兼容显式 `--cache-hit` / `--cache-status`）；更新 `docs/SETUP_AND_RUNTIME.md` 去掉硬编码 `--cache-hit`、阶段 3 改用 `burn-temp --run-id <run_id>` 与 `sync --direction export --run-id <run_id>`，并在 `README.md` 注明实测 Token 数据为「本地公式估算 / 特定环境观测值」。
  * **P2-2（好奇心词条轮换与自动消费）**：向后兼容迁移 `worldbook` 新增 `last_searched_at` 与 `search_count`，`prepare-feed` 通过 `select_curiosity_keywords()` 优先挑选未搜/最久未搜的活跃词条，拉取成功后调用 `mark_curiosity_searched()` 刷新时间并递增计数，达到 3 次自动标记为 `consumed`。
* **回归验证**：`<YOUR_CONDA_PYTHON> -m pytest -v` **149 passed**（新增 11 条覆盖全部 5 项修复与老库迁移的离线单元测试，零网络、零真实数据污染）。
* **提交记录**：`main` 分支提交 `fix(core): persist full diary, harden run lease, idempotent sync and curiosity rotation`。
### [OSS Release Packaging - 精简开源版整理（移除一键部署脚本 & 升级 Quickstart Checklist）] - 2026-11-20
* **精简与文档升级**：
  * 移除一键部署脚本，保留 `scripts/download_wd_tagger.py` 与 `scripts/inbox_gdoc_exporter.gs`，同步将 `docs/SETUP_AND_RUNTIME.md`、`README.md` 与 `HANDOFF_LOG.md` 中的环境初始化改为标准 Conda / pip 手动或 Agent 可执行命令。
  * 重写项目门面 `README.md`：明确区分「Git 追踪公版文件」与「`.gitignore` 屏蔽本地私有文件」，补全「User / AI Agent 通用快速部署清单（Quickstart Checklist）」及可直接丢给 AI Agent 自动接管安装的引导提示词。
  * 复核 `docs/SETUP_AND_RUNTIME.md` v1/v2 自适应唤醒 Prompt（默认不带 `--headless`）并完成全仓库路径、Cookie 与私有数据 0 残留扫描。
* **回归验证**：`<YOUR_CONDA_PYTHON> -m pytest -v` **134 passed**（零逻辑破坏、零敏感与绝对路径残留）。
### [OSS Release Packaging - v2 定时任务文档升级与开源出厂脱敏包装] - 2026-11-20
* **分支与变更**：从 `main`（`v2.0.0`）切出 `chore/oss-release`，在不破坏现有代码逻辑的前提下完成开源发布前全套包装：
  * 升级 `docs/SETUP_AND_RUNTIME.md` 为 **v1 / v2 自适应通用版**：支持通过 `config.local.json` 热切换 `audio_only` / `sanitized_video` 而无需改 Prompt；宿主按 `prepare-media` 返回的 `payload_path` 与 `mode` 原生 `read_file` 鉴赏并透传 `--frame-count` / `--pipeline-used`；定时任务默认移除 `--headless`（启用 60s 非抢占 `tkinter` 预算弹窗与锁屏 `TclError` 自动兜底 `0.35`）；补齐 `Create With Prompt` 快捷建任务指引与 `wd-tagger`（DirectML / CPU / CUDA）启用步骤。
  * 全局隐私与本机路径脱敏清洗：扫描全部 Git 跟踪文件，将 `ARCHITECTURE.md`、`SPEC_AND_TASKS.md`、`HANDOFF_LOG.md`、`docs/SETUP_AND_RUNTIME.md`、`config.json`、`config.local.example.json`、`src/gemininest/runner.py`（仅注释）及 `tests/test_{config,media_processor,runner,sync_manager}.py` 中所有本机硬编码路径统一替换为 `<YOUR_PROJECT_DIR>`、`<YOUR_CONDA_PYTHON>`、`<YOUR_FFMPEG_PATH>`、`<YOUR_GOOGLE_DRIVE_NEST>` 及环境变量/PATH 动态探测；确认零 Cookie、零 `.env`、零 `config.local.json`、零 `data/` 私有数据入库。
  * 新增标准 MIT `LICENSE` 与中文主版（附英文简介）项目门面 `README.md`（含架构对比、v1/v2 实测数据、Mermaid 架构图、快速部署清单与合规声明）。
* **回归验证**：`<YOUR_CONDA_PYTHON> -m pytest -v` **134 passed**（零逻辑破坏、零盘符路径残留）。
### [v2.0.0 封板验收 - Phase 5-B 真实 ONNX 部署、DirectML 加速与首次“睁眼”实飞] - 2026-11-20
* **权重部署与初飞排障**：
  * 通过 `scripts/download_wd_tagger.py` 直连 HuggingFace 下载并校验 `SmilingWolf/wd-vit-tagger-v3` 至 `data/models/`（`model.onnx` `378,536,310` 字节 / `361.00 MB` + `selected_tags.csv` `10,861` 标签，`rating_indices={general:0, sensitive:1, questionable:2, explicit:3}`）。
  * 首次 CPU 试飞 `BV16q4y1R7ok`（01:50）实测暴露两项真实工程问题：① 原默认 `min_general_score=0.90` 对实拍视频+白边填充误杀率达 `99.08%`（`108/109` 帧，触发 `E5001_SAFETY_GATE_DOWNGRADED` 自动降级纯音频并诚实完成音频鉴赏入库 `memory_id=3`、评分 `8`）；② CPU 全帧推理+写盘总耗时达 `291.15 s`。
* **工程修复与 Windows DirectML GPU 加速（提交 `a82aa32`）**：
  * 校准 `safety_gate.py` / `config.json` / `config.py` / `contracts.py`：引入向后兼容的 `max_sensitive_score=0.75` 护栏、将 `min_general_score` 校准为 `0.20`、严守 `max_unsafe_score=0.05`；新增超阈值**提前熔断（Early-Exit）**跳过剩余帧推理与落盘；在 `media_processor.py` 与 `runner.py` 补充四段耗时（下载/抽帧/安检/重封）与 `active_provider` 诊断。
  * 新增 `DmlExecutionProvider` 路由（`"auto"` / `"dml"` 优先 DirectML，失败依次回退 CUDA → CPU），在 `gemininest` 换装 `onnxruntime-directml 1.24.4`（零 CUDA Toolkit、零 DLL 报错），RTX 4060 Laptop 单帧热态纯推理耗时由 CPU `360.76 ms` 降至 **`51.75 ms`**（**7.0× 加速**，与 CPU 分数 6 位小数一致）。
  * 补充 `max_sensitive_score`、Early-Exit 与 `DmlExecutionProvider` 回退单测，全量 `pytest` **134/134 全部通过**。
* **v2 首次真实“睁眼”实飞验收指标（`run_id = host_20261120T083849Z_43944`）**：
  * **选定视频**：`BV1mp4i67EKW`《译制腔《人民的名义:赵德汉吃面》》（02:13，命中好奇心词条 `经典译制片配音幕后`）。
  * **管线性能与安检结果**：推理设备 **`DmlExecutionProvider`**；总耗时 **`21.769 s`**（下载 `2.508 s`｜抽帧与音轨 `5.605 s`｜132 帧安检推理 `12.434 s`｜重封 `0.673 s`，较 CPU 291.15 s 提速 **13.4×**）；总抽帧 `132` 帧、单帧外科手术抹黑 `1` 帧（索引 `113` / `01:53`，占比 `0.76%`）、未触发降级；最终净版 `sanitized.mp4` 体积 **`2.73 MB`**（`2,858,964` 字节，`video/mp4`，132 帧 @ 1 FPS）。
  * **真实“睁眼”观影与闭环核验**：调用 Antigravity 宿主原生 `read_file` 直接加载 `sanitized.mp4`，完整看清开场老居民楼红福字木门、赵德汉吃炸酱面捏生蒜与左侧“威廉·查德汉｜夏德城堡男爵”花字、水墨山水画客厅、双语字幕与配音错位细节、01:53 单帧纯黑安检帧及片尾“未完待续/二创改编纯属娱乐”字卡，并听清完整译制腔音频；`commit-appreciation` 落库（`memory_id=4`，评分 `8`，`tokens_recorded=13040`）并物理焚毁临时目录（`data/temp/` 残留 **`0`**）；`sync` 成功将 4 个白名单纯文本文件同步至 `<YOUR_GOOGLE_DRIVE_NEST>\0_active\`（独立导出库提交 `cea87fdd`）。
* **封板与发版操作**：勾选 `SPEC_AND_TASKS.md` Phase 5 视觉核心与 v2.0.0 验收项、更新本交接日志，提交至 `feat/v2-visual` 后切换至 `main` 合并并打上附注标签 **`v2.0.0`**。
### [Phase 5-A - 核心开发工程师 / Builder 对话] - 2026-11-20
* **分支与依赖**：从 `main`（`v1.0.0`）切出 `feat/v2-visual`，保持 `config.json` 默认 `"pipeline_mode": "audio_only"` 与 `"wd_tagger_enabled": false` 零改动；新建 `requirements-v2.txt`（`-r requirements.txt` + `onnxruntime`、`pillow`、`numpy`、`py7zr`，注释说明 GPU 可换 `onnxruntime-gpu`）；在 `gemininest` 环境安装 `numpy 2.4.6`（`Pillow 10.4.0` 已就绪）；更新 `.gitignore` 屏蔽 `.agent/rules/cat-soul.local.md`。
* **产出文件**：
  * `scripts/download_wd_tagger.py`（独立离线友好下载工具，默认目标 `SmilingWolf/wd-vit-tagger-v3`，支持 `--mirror` / `HF_ENDPOINT`、`--dry-run`、原子 `.part` 落盘，以及对 `model.onnx` HTML 错误页拦截与 `selected_tags.csv` 必需 rating 标签校验）。
  * `src/gemininest/safety_gate.py`（完整保留 v1 `BaseSafetyGate`/`PassthroughSafetyGate`；新增 `WdTaggerSafetyGate`：惰性导入 `onnxruntime`/`PIL`/`numpy`，动态按表头与 `category=9` 解析 `selected_tags.csv` 索引，严格 448x448 白底等比填充 BGR `float32` NHWC 预处理，`wd_tagger_device="cuda"` 缺失 Provider 或 CUDA/cuDNN DLL 异常时记录告警并自动回退 `CPUExecutionProvider`，支持可配置 `unsafe_policy="max"|"sum"`，违规帧原地等尺寸纯黑 `(0,0,0)` 替换，抹黑率 `> max_blackout_ratio (0.15)` 触发 `downgrade_to_audio=True` 与 `E5001_SAFETY_GATE_DOWNGRADED`；更新 `create_safety_gate(cfg)` 支持 `pipeline_mode=="sanitized_video"` / `wd_tagger_enabled==True` 与自定义推理 session 注入）。
  * `src/gemininest/media_processor.py`（保留 v1 纯音频管线；新增严格帧一致 v2 `prepare_sanitized_video()` 与 `sanitized_video_session()`：按 `video_resolution_map[media_resolution]` 与 `extract_fps`、300s/5s 分段窗口与 SponsorBlock 交集抽取无损 PNG 帧并同步提取 `48kbps` 单声道 AAC，全帧送检 `safety_gate`，遇 `E5001` 降级立即焚帧输出 `downgraded_audio`（`audio/mp4`），过检则按原 `extract_fps` + `libx264`/`yuv420p`/`+faststart` + `48kbps` 单声道 AAC 重封净版 MP4，校验 `<= 95 MB` 与 `ffprobe -count_frames` 严格帧一致并即刻清理散帧）。
  * `src/gemininest/agent_bridge.py` & `src/gemininest/runner.py` & `src/gemininest/__init__.py`（`agent_bridge` 新增 `resolve_video_processing_mode()` 支持 `static`/`agentic`/`auto` 按 300s 阈值切换，同步支持 `low`/`medium` 70 Tokens/帧与 `high` 280 Tokens/帧 + 音频 25 Tokens/秒计费，`build_static_prefix()` 自动将本地 `.agent/rules/cat-soul.local.md` 叠层追加至 Block 1；`runner` 的 `prepare-media` 按 `pipeline_mode` 自动路由 v1/v2 并在 `E5001` 时自动降级写史记账，`--dry-run` 新增 `--mode sanitized_video` 全离线闭环跑通抽帧→抹黑→净版 MP4 重封→E5001 降级→焚毁归零）。
  * `tests/test_safety_gate.py`、`tests/test_download_wd_tagger.py`、`tests/test_media_processor.py`、`tests/test_agent_bridge.py`、`tests/test_runner.py`（全离线单测扩充覆盖全绿，顺带隔离 `test_runner.py` 中 `DEFAULT_SNAPSHOTS_DIR` 避免受真实 Maiden Flight 快照干扰）。
* **测试与验证结果**：
  * `<YOUR_CONDA_PYTHON> -m pytest -v`：**129 passed in 26.08s**（全离线零网络请求、真实 `data/` 与云盘目录 `<YOUR_GOOGLE_DRIVE_NEST>` 零触碰）。
  * CLI 实跑 `python -m gemininest.runner --dry-run --mode sanitized_video --headless`：全离线走通 `BV1drygem001`（7 帧涂黑 1 帧 → 净版 MP4 `sanitized_video`）与 `BV1drydiy002`（4 帧涂黑 2 帧 50% > 15% → `E5001` 自动降级 `downgraded_audio`），`temp_residual_count=0`，`static_prefix_tokens=4849`。
* **提交记录**：`feat/v2-visual` 分支提交 `feat(v2): add pluggable wd-tagger safety_gate, sanitized mp4 pipeline and local persona overlay`。
### [v1.0.0 封板验收 - 首次真实小号低频试飞（Maiden Flight）] - 2026-11-20
* **试飞概况**：严格按照 `docs/SETUP_AND_RUNTIME.md` 与 `.agent/rules/cat-soul.md` 两阶段宿主驱动协议，在 `feat/v1-mvp` 分支使用 `<YOUR_CONDA_PYTHON>` 以无头保守模式（`--headless --max-select 2`）完成首次真实 B 站小号试运行，全程零源码修改。
* **全链路验收结果（`run_id = host_20261120T061813Z_28796`）**：
  * **前置风控与挂载保护实测**：首次启动时因 Google Drive 桌面端未启动触发 `E8001_CLOUD_DRIVE_UNMOUNTED` 告警并立即中止，验证了云盘探活保护；手动启动 Google Drive 挂载后重跑 `prepare-feed`，`inbox/` 扫描零报错（`errors: []`，吸收补丁 0 条、拒绝 0 条）。
  * **锁机制、心跳与推荐流**：`.cat.lock` PID 锁校验与退出释放正常；B 站小号登录态心跳与 WBI 推荐流拉取成功（30 条真实候选，黑名单命中 0 条）；无头 5 小时预算校验通过（`allowed_ratio = 0.35`，`remaining_tokens = 490,000`）。
  * **真实音频制备、收听鉴赏与落库焚毁（精选 2 条）**：
    1. `BV1Fspj66E66`《“这波秒开仙人，直接把六小龄童看呆了！”》（荒草音乐，`110.57s` / `680 KB` `48kbps m4a`）→ 原生 `read_file` 真实收听，印象分 **7/10**（`memory_id = 1`，`3,264 tokens`，新增好奇心词条 `86版西游记配音`），提交后临时目录立即物理焚毁。
    2. `BV1CapR6JEAM`《【花小烙】蚰蜒：不就是腿多了点嘛，我们真没那么恐怖！》（画渣花小烙，`344.04s` / `2.05 MB` `48kbps m4a`）→ 原生 `read_file` 真实收听，印象分 **8/10**（`memory_id = 2`，`9,107 tokens`，新增好奇心词条 `节肢动物硬核科普`），且 `prepare-media` 阶段已实测 Top-8 记忆引擎即时召回第 1 条记忆（`激活权重 0.88`），提交后临时目录立即物理焚毁。
  * **云盘纯文本同步与洁癖核查**：`burn-temp --all` 确认 `data/temp/` 残留数 **0**（`.cat.lock` 为 `False`）；`sync --direction export` 完成本地纯文本导出与独立隐藏 Git 自动提交（`88e43dc2a105194c3c76cab60563a5c5309fea95`），并将 `persona_snapshot.md`、`memory_summary.md`、`diary_latest.md`、`sync_manifest.json` 共 4 个白名单纯文本文件无错同步至 `<YOUR_GOOGLE_DRIVE_NEST>\0_active\`。
* **封板与发版操作**：勾选 `SPEC_AND_TASKS.md` Phase 4 剩余项并更新本交接日志，在 `feat/v1-mvp` 提交收尾文档后切换至 `main` 分支合并 `feat/v1-mvp` 并打上版本标签 `v1.0.0`。
### [Phase 4 - 核心开发工程师 / Builder 对话] - 2026-11-20
* **产出文件**：
  * `src/gemininest/sync_manager.py`（纯文本扩展名防火墙：严格限制 `.md`/`.json`/`.txt`，非白名单扩展名如 `.mp4`/`.db`/`.m4a`/`.png`/`.7z` 立即阻断并抛 `ErrorCode.E8002_SYNC_EXT_VIOLATION`；正向导出 `persona_snapshot.md`、`memory_summary.md`、`diary_latest.md`、`sync_manifest.json` 至 `local_export_dir`，采用数据修订戳确保无实质变更时字节级幂等；在 `data/exports/` 内部独立隐藏 Git 仓库通过 `--git-dir`/`--work-tree` 与 `-c core.quotepath=false` 执行 UTF-8 静默自动提交且无变更优雅跳过，物理隔绝主代码库；同步前探活 `cloud_nest_dir`，未挂载保留本地导出与独立 Git 并记录 `E8001_CLOUD_DRIVE_UNMOUNTED` 告警不崩溃；轮转 `1_yesterday_readonly/` 前先递归 `os.chmod(p, stat.S_IWRITE | stat.S_IREAD)` 解锁，整文件覆写避免云盘 `.tmp` 冲突副本，顶部写入/保留 `<!-- READONLY SNAPSHOT: DO NOT EDIT -->`（JSON 保持合法格式加 `_readonly_snapshot` 标记）并容错重设 `S_IREAD`，再将当日新导出写入 `0_active/`；扫描 `inbox/` `.txt`/`.md` 文件并静默忽略 `.gdoc` 指针，纯标准库解析 YAML/JSON 格式 `[MEMORY_PATCH]` 块，仅放行 `add_memory`/`boost_memory`/`add_curiosity`/`suggest_persona_tweak`，合法 `suggest_persona_tweak` 写入 SQLite `worldbook(preference)` 与受控审核文件 `persona_tweaks_review.md` 绝不改写 `.agent/rules/cat-soul.md` 核心安全红线，语法非法记 `E8003_PATCH_PARSE_FAILED`、越权/篡改安全红线/命中黑名单/超长人设记 `E8004_PATCH_POLICY_REJECTED`，均连同拒因日志归档至 `inbox_archived/`）
  * `src/gemininest/memory_store.py`（非破坏性补充只读查询方法 `list_recent_memories()` 与 `list_recent_watch_history()` 供 `sync_manager` 导出使用；未改动冻结 `contracts.py` 与 SQLite 表结构）
  * `src/gemininest/runner.py` & `src/gemininest/__init__.py`（补全 `sync` 子命令支持 `--direction both|inbox|export`、`--export-dir`、`--cloud-dir`、`--no-git`；将 Step 1 反向补丁吸收接入 `prepare-feed` 与 `run_dry_run`，将 Step 6 导出 + 隐藏 Git + 轮转同步接入 `run_dry_run`；`--dry-run` 与 CLI 缺省路径强制使用隔离临时沙箱，绝不触碰真实云盘目录 `<YOUR_GOOGLE_DRIVE_NEST>` 或真实 `data/`）
  * `scripts/inbox_gdoc_exporter.gs`（脱敏可直接粘贴的 Google Apps Script 云端定时转换脚本，内置 `PropertiesService` + 同名时间戳双重防重复导出校验）
  * `docs/SETUP_AND_RUNTIME.md`（脱敏运维与跨端联动实操手册，含 Antigravity `Scheduled Tasks` 标准两阶段唤醒 Prompt、Apps Script 部署步骤、Gemini Web 常驻 Skill / `Scheduled Actions` 文案与全部 4 类 `[MEMORY_PATCH]` 范例及错误码排障表）
  * `tests/test_sync_manager.py` & `tests/test_runner.py`（全离线单元测试：覆盖 `E8002` 非白名单文件物理阻断、`E8001` 云盘未挂载本地兜底、Windows 只读文件三次连续轮转零 `WinError 5` 报错、`data/exports/` 独立隐藏 Git 自动提交与无变更跳过、`.gdoc` 静默忽略、合法 4 类 `[MEMORY_PATCH]` 入库归档、违规越权/红线篡改/黑名单/超长 `E8004` 与语法错误 `E8003` 拒绝并附拒因日志归档、以及 `runner --dry-run` / `sync` / `prepare-feed` 全链路闭环）
* **测试结果**：
  * `<YOUR_CONDA_PYTHON> -m pytest -v`：**109 passed in 15.85s**（Phase 1–4 全部用例 100% 通过，零网络请求、真实 `data/` 与真实云盘目录 `<YOUR_GOOGLE_DRIVE_NEST>` 零触碰）。
  * 实跑 `python -m gemininest.runner --dry-run --headless`：零警告输出标准 UTF-8 JSON（`filtered_blacklist_count=2`, `appreciated_bvs=2`, `static_prefix_tokens=4849`, `temp_residual_count=0`, `sync_exported_count=4`, `sync_active_count=4`, `sync_git_commit_hash` 非空, `sync_errors=[]`），且核验真实 `data/`、`data/exports/`、`data/temp/` 保持 0 污染。
* **避坑要点与工程备忘**：
  * **Git 物理隔离**：`sync_manager` 所有 export 仓库 Git 命令均显式传入 `--git-dir=<export>/.git --work-tree=<export> -c core.quotepath=false` 并剥离环境变量中的 `GIT_DIR`/`GIT_WORK_TREE`，确保绝不会误操作主工程 Git 仓库。
  * **幂等导出支持无变更跳过**：导出视图的时间修订戳取自数据库最新记录时间戳而非当前挂钟秒数，使连续无变更导出产生完全一致字节流，独立 Git 提交得以真正优雅跳过。
  * **离线测试防误触云盘**：`prepare-feed --offline` 仅在显式指定 `--cloud-dir` 时执行 Step 1 收件箱扫描；CLI `--dry-run` 未传路径参数时自动建独立临时沙箱并在 `finally` 只读安全回收，彻底杜绝触碰真实云盘目录 `<YOUR_GOOGLE_DRIVE_NEST>` 与真实 `data/`。

### [Phase 3 - 核心开发工程师 / Builder 对话] - 2026-11-20
* **产出文件**：
  * `src/gemininest/agent_bridge.py`（Antigravity 宿主桥接层：零 `google-genai` SDK、零 API Key；拼装 5 块字节级逐字不变静态前缀 `.agent/rules/cat-soul.md` + 2/6/9 分及选片 Few-Shot + 1–10 审美标尺与感知诚实细则 + 严格 JSON Schema + 月度核心世界书，估算 **4,849 Tokens** 稳落 4,500–5,200 区间且 `> 4,096` 隐式缓存阈值；实现 `SelectionPacket`/`AppreciationPacket` 打包与纯 ASCII 本地音频路径校验；严格校验宿主输出 JSON，非法结构抛 `E6003_SCHEMA_INVALID`，`SAFETY`/`PROHIBITED_CONTENT` 按 `E6001_HOST_SAFETY_BLOCKED` 落日志优雅跳过且在 `invoke_appreciation_with_policy()` 中严禁重试，429/`RESOURCE_EXHAUSTED` 抛 `E6002_HOST_RATE_LIMIT_429` 并写冷却账本；按音频 25 tok/s + 文本 + `token_calibration_k` 更新 `budget_ledger`）
  * `src/gemininest/budget_ui.py`（启动 60 秒非抢占 `tkinter` 预算弹窗：`time.monotonic()` 单调时钟倒计时防休眠卡死，不调用 `focus_force`/`grab_set`，支持自定义比例输入与一键“今日休息 (0.0)”；完整支持 `--headless` 与 `tk.TclError`/Session 0 静默回退默认预算 `0.35`；封装 `winotify` 高分宝藏视频 `impression_score >= toast_min_impression_score` 与小号心跳失效 `E2001` 原生 Toast 通知及非交互环境安全降级）
  * `src/gemininest/runner.py`（`.cat.lock` PID 文件锁：Windows Kernel32 `OpenProcess`/`GetExitCodeProcess` + POSIX 存活检测，存活 PID 抛 `E7002_LOCKFILE_CONFLICT`，死锁/损坏锁自动清理接管；启停自动 `sweep_stale_temp()` 兜底清扫；实现两阶段宿主 CLI 子命令 `check-budget` / `prepare-feed` / `prepare-media` / `commit-appreciation` / `burn-temp` / `run` / `sync`；实现全链路零网络 `--dry-run` 离线回放闭环，合成离线音频走通真实 FFmpeg `48kbps` 单声道 `m4a` + 空降裁剪 + 黑名单拦截 + 记忆召回 + 模拟宿主回写 + 算力记账 + 临时目录归零焚毁）
  * `src/gemininest/__init__.py`（补充导出 Phase 3 符号，并对 `runner` 采用 PEP 562 `__getattr__` 惰性导入避免 `python -m gemininest.runner` 触发 `runpy` 警告；未改动冻结的 `contracts.py`）
  * `tests/test_agent_bridge.py`、`tests/test_budget_ui.py`、`tests/test_runner.py`（全离线单元测试：静态前缀字节稳定性与 > 4,096 Tokens 验收、`E6001` 安全拦截跳过不重试、`E6002` 429 冷却、`E6003` 非法 JSON 校验与有限重试、`budget_ui` 无头/`TclError`/单调时钟超时/今日休息与 Toast 降级、`.cat.lock` 存活冲突与死锁清理、两阶段 CLI 异常仍焚毁临时目录以及 `--dry-run` 端到端闭环归零）
* **CLI 用法速查（使用 `<YOUR_CONDA_PYTHON>`，设置 `PYTHONPATH=src`）**：
  * 全链路离线 Dry-Run：`python -m gemininest.runner --dry-run --headless [--snapshot <path>] [--db <path>] [--temp-root <dir>]`
  * 检查 5h 窗口预算：`python -m gemininest.runner check-budget [--headless]`
  * 阶段 1 准备菜单：`python -m gemininest.runner prepare-feed --run-id <id> [--offline --headless]`
  * 阶段 2A 制备音频：`python -m gemininest.runner prepare-media --run-id <id> --bv <bv> [--offline]`（输出纯 ASCII `media_abs_path` 供宿主 `read_file` 加载）
  * 阶段 2B 回写并焚毁：`python -m gemininest.runner commit-appreciation --run-id <id> --bv <bv> --response-file <json_path> [--cache-hit --headless]`（无论成功/报错/安全拦截，均在 `finally` 物理焚毁临时目录）
  * 显式焚毁/清扫：`python -m gemininest.runner burn-temp --run-id <id> --bv <bv>` 或 `burn-temp --all`
* **测试结果**：
  * `<YOUR_CONDA_PYTHON> -m pytest -v`：**99 passed in 5.93s**（Phase 1 57 条 + Phase 2 19 条 + Phase 3 23 条全绿，零网络请求、真实 `data/` 零污染）。
  * 独立临时目录实跑 `python -m gemininest.runner --dry-run --headless`：零警告输出标准 UTF-8 JSON 报告（`filtered_blacklist_count=2`, `appreciated_bvs=2`, `static_prefix_tokens=4849`, `temp_residual_count=0`）。
* **避坑要点与工程备忘**：
  * **`memory_store.get_budget_status` 边界**：当用户在弹窗选择“今日休息（0.0）”时，`runner` 在调用 `get_budget_status(allowed_ratio=...)` 前先短路返回 `rest_today`，避免触发 `allowed_ratio > 0` 断言。
  * **好奇心词条二次安检**：大脑许愿的 `next_curiosity_keywords` 在写入 `worldbook` 前均经过 `MetaFilter` 过滤，防止模型意外生成黑名单关键词污染次日搜索配额。
  * **Windows 进程存活判定**：`is_pid_alive()` 在 Windows 上直接调用 Kernel32 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` + `GetExitCodeProcess == STILL_ACTIVE (259)`，避免 `os.kill(pid, 0)` 在 Windows 上的兼容性坑。
  * **消除 `runpy` 警告**：`__init__.py` 对 `runner` 符号做 PEP 562 惰性导出，确保 `python -m gemininest.runner` 标准输出为纯净可解析 JSON。
* **Phase 4 启动入口**：
  * 直接复用冻结的 `contracts.py`（`MemoryPatch`/`SyncReport`/`ErrorCode.E8001..E8004`）、`memory_store.py` 与 `runner.py` 的 `sync` 入口，开始实现 `src/gemininest/sync_manager.py` 与 `tests/test_sync_manager.py`。

### [Phase 2 - 核心开发工程师 / Builder 对话] - 2026-11-19
* **产出文件**：
  * `src/gemininest/fetcher.py`（基于 `httpx` 的只读 B 站 Web API 客户端：标准 WBI 签名 `MIXIN_KEY_ENC_TAB`/`extract_wbi_key`/`get_mixin_key`/`sign_wbi_params`；国内直连请求头与 `request_interval_sec` 随机间隔；小号心跳检测 `check_login_heartbeat()` 失效抛 `E2001_BILI_AUTH_EXPIRED`；推荐流、好奇心搜索、视频详情/标签/字幕/热评 Top10/最新评 Top5 拉取；开源 SponsorBlock 空降查询与异常/超时/404 优雅降级返回空元组；`-412` 指数退避与连续 2 次立即中止熔断 `E2002_BILI_RATE_LIMIT_412`；UTF-8 JSON 黑匣子快照 `save_snapshot()`/`load_snapshot()`）
  * `src/gemininest/media_processor.py`（v1 纯音频管线：`yt-dlp` 下载纯音频流 + `config.ffmpeg_binary` 严格 `shell=False`、纯 ASCII 路径、UTF-8 环境变量转码为 `48kbps` 单声道 `m4a`；SponsorBlock 区间合并求补与 FFmpeg `-af`/`-filter_complex` 精准裁剪并记录 `trimmed_sponsor_sec`；校验 `inline_max_payload_mb` 95 MB 上限超限抛 `E4002_PAYLOAD_TOO_LARGE` 并自动焚毁；提供 `audio_session()`/`managed_temp_workspace()` 上下文管理器、显式 `burn_temp()`/`burn_temp_dir()` 与 `sweep_stale_temp()` 启动兜底清扫）
  * `src/gemininest/safety_gate.py`（`BaseSafetyGate` 抽象基类、v1 直通桩 `PassthroughSafetyGate` 与 `create_safety_gate(cfg)` 工厂函数，误开 `wd_tagger_enabled=True` 时给出清晰 v2 未实现提示）
  * `src/gemininest/__init__.py`（补充导出 Phase 2 公共符号；未改动冻结的 `contracts.py` 与 `config.py`）
  * `tests/test_fetcher.py`、`tests/test_media_processor.py`、`tests/test_safety_gate.py`（全离线单元测试：全部 HTTP 使用 `httpx.MockTransport`，`media_processor` 使用本地 FFmpeg 合成 4–5 秒无网测试音频并 mock `yt-dlp`）
* **测试结果**：
  * `<YOUR_CONDA_PYTHON> -m pytest -v`：**76 passed in 2.87s**（Phase 1 57 条 + Phase 2 19 条全绿，零外部网络请求、真实 `data/` 零污染）。
* **避坑要点与工程备忘**：
  * **第三方请求 Cookie 物理隔离**：`BilibiliFetcher` 拆分为携带小号 Cookie 的 `_bili_client` 与零 Cookie 的 `_public_client`，确保 `bsbsb.top`（SponsorBlock）与字幕 CDN 请求绝不外泄 `SESSDATA`/`bili_jct`/`buvid3`。
  * **SponsorBlock 全覆盖兜底**：当空降片段合并后覆盖 100% 音频时长时，`compute_keep_intervals()` 自动回退保留原音频（`trimmed_sponsor_sec=0.0`），避免 FFmpeg 空流报错；单保留区间走 `-af atrim`，多保留区间走 `-filter_complex concat`。
  * **Windows 只读临时文件删除**：`burn_temp()` 内置 `shutil.rmtree(..., onerror=_handle_remove_readonly)`，遇到 Windows 只读属性临时文件自动 `S_IWRITE` 解锁再删除，并顺带清理空的 `<run_id>` 父目录。
  * **FFprobe 同目录定位**：`_resolve_ffprobe_binary()` 优先根据 `config.media_preprocess.ffmpeg_binary` 定位同级 `ffprobe.exe`（并保留 `ffmpeg -i` stderr 时长解析回退），不依赖全局 PATH 顺序。
* **Phase 3 启动入口**：
  * 直接复用 `contracts.py` 中的 `SelectionPacket`/`AppreciationPacket`/`SelectionResponse`/`AppreciationResponse`/`TokenEstimate` 与 Phase 1/2 已完成的 `config`、`meta_filter`、`memory_store`、`fetcher.load_snapshot()`、`media_processor`、`safety_gate`，开始实现 `agent_bridge.py`、`budget_ui.py` 与 `runner.py`。

### [Phase 1 - 核心开发工程师 / Builder 对话] - 2026-11-19
* **产出文件**：
  * `pyproject.toml`（配置 `pytest` `pythonpath = ["src"]` 与 `testpaths = ["tests"]`）
  * `src/gemininest/contracts.py`（按 `SPEC_AND_TASKS.md` 2.2/2.3 冻结全部 8 模块 `@dataclass(frozen=True)` DTO、18 项 `ErrorCode(StrEnum)` 与 `GeminiNestError`）
  * `src/gemininest/config.py`（强类型配置加载/合并/校验；`config.local.json` 缺失静默跳过、黑名单 `extra_blacklist`/`blacklist` 强制 NFKC+casefold 去重并集追加；`SecretsConfig` 全字段 `repr=False` 脱敏；支持 `require_bili_credentials=False` / `offline=True` 离线初始化）
  * `src/gemininest/meta_filter.py`（`tname` → `tags` → `title` 三维度前置硬拦截；统一 NFKC + `casefold()` + 空白规整防全半角/大小写绕过）
  * `src/gemininest/memory_store.py`（SQLite WAL 四业务表 + `memory_fts` `trigram` 虚拟表；`bm25(memory_fts, 0.0, 3.0, 1.5, 1.0, 0.5)` 四列加权；`< 3` 字中文/ASCII 短词自动回退 `LIKE` + `tags` 精确匹配并按列权重折算归并；印象分 $I \to S_0$（2/12/60 天）映射、艾宾浩斯保留度、Top-8 硬封顶（3 全局 + 5 关联）与显式权重标签渲染；`worldbook`/`watch_history` CRUD 与 5 小时窗口算力账本 + 429 冷却核算）
  * `src/gemininest/__init__.py`（统一导出 Phase 1 公共符号）
  * `tests/test_config.py`、`tests/test_meta_filter.py`、`tests/test_memory_store.py`（离线单元测试）
* **测试结果**：
  * `<YOUR_CONDA_PYTHON> -m pytest -v`：**57 passed in 0.46s**（全离线、零网络请求、仅使用 `:memory:` 与 `tmp_path`，真实 `data/` 目录零污染）。
* **新增依赖与工程注意事项**：
  * **零新增外部依赖**（纯标准库 + Phase 0 已安装的 `python-dotenv`、`pytest`）。
  * **SQLite FTS5 `bm25()` 列偏移避坑**：因 `memory_fts` 第 0 列为 `memory_id UNINDEXED`，`bm25()` 权重参数需前置 `0.0`（即 `bm25(memory_fts, 0.0, 3.0, 1.5, 1.0, 0.5)`），否则权重会整体左移导致 `latest_comments_col` 错位超权；已在 `test_memory_store.py` 中固化严格排序回归测试。
  * **浮点预算边界取整**：`1_400_000 * 0.35` 存在 IEEE-754 精度尾差（`489999.99999999994`），已使用 `int(round(capacity * allowed_ratio))` 精确算出 `490_000`。
* **Phase 2 启动入口**：
  * 直接基于 `src/gemininest/contracts.py` 已冻结的 `VideoMeta`/`VideoDetail`/`FetchBatchResult`/`PreparedMedia`/`FrameScore`/`SafetyGateResult` 开始实现 `fetcher.py`、`media_processor.py`（v1）与 `safety_gate.py`（v1 直通桩）。

### [Phase 0 - 首席技术规划师 / Architect 对话] - 2026-11-19
* **完成内容**：
  * 完成 Windows 全量环境体检，定位原生 `FFmpeg 6.1.1-full_build`（`<YOUR_FFMPEG_PATH>`）并加入用户 `PATH`；验证并补齐 `<YOUR_GOOGLE_DRIVE_NEST>` 4 个子目录读写权限。
  * 实测验证 Antigravity 宿主 `read_file` 原生支持本地 `48kbps` 单声道 `.m4a`（`audio/mp4`）与 `480p 1fps` `.mp4`（`video/mp4`）读取，且测试用合成媒体已立即焚毁。
  * 修订 `ARCHITECTURE.md`、`SPEC_AND_TASKS.md` 与 `config.json`，补全 8 模块 DTO 字段清单、统一错误码表、4,500–5,200 Tokens 静态前缀缓存方案、FTS5 `<3` 字中文短词回退与 Windows 工程避坑方案。
  * 创建 Anaconda `gemininest`（Python 3.11）环境并安装 v1 最小依赖；生成脱敏模板、本地忽略覆盖项与空模块目录骨架；完成主仓库 `git init`（`main` 首笔提交并切换 `feat/v1-mvp`）及 `data/exports/` 独立隐藏 Git 仓库初始化。
* **遗留待人工操作**：
  * 建议手动清理 `<YOUR_GOOGLE_DRIVE_NEST>\Videos` 历史遗留目录，确保云盘 100% 纯文本隔离。
  * 在进入 Phase 2 真机联调前，按 `.env.example` 创建本地 `.env` 并填入 B 站小号只读 Cookie（Phase 1 全离线开发无需填入）。

### [Stage 0 - Web 预架构对话] - 2026-11-19
* **完成内容**：完成全局方案重构、安全对齐评审，输出全套零代码设计文档、配置模板与规划对话启动指令。
* **遗留待确认**：需在本地电脑核验 `uv` 安装状态、Google Drive 映射盘符确切路径，以及第一周 Antigravity Pro 额度实测校准值。
