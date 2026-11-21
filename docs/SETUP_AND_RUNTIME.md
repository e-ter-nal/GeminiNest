# GeminiNest 运维部署与跨端联动实操手册（v1 / v2 通用版）

> 本手册适用于 **GeminiNest v2.0.0+**，全面兼容 **v1 纯音频模式（`audio_only`）** 与 **v2 视觉安检净版视频模式（`sanitized_video`）**。涵盖环境初始化、v2 `wd-tagger`（DirectML GPU / CPU）热插拔启用、**Antigravity `Scheduled Tasks` 自适应唤醒 Prompt（含 `Create With Prompt` 快捷建任务）**、**Google Apps Script 自动导出脚本**，以及 **Gemini Web 端常驻 Skill 与每日 `Scheduled Actions`** 配置。
>
> **安全红线**：本文档全部路径均为通用占位符（`<YOUR_PROJECT_DIR>`、`<YOUR_CONDA_PYTHON>`、`<YOUR_FFMPEG_PATH>`、`<YOUR_GOOGLE_DRIVE_NEST>`），严禁在文档、Git 仓库或云盘中写入真实 Cookie、密钥或本机私密信息。

---

## 1. 本地前置核对清单

在开启真实定时巡逻或低频试跑前，请先按以下标准命令完成本地环境初始化（可手动执行，也可直接交由 AI Agent 按步执行）：

```powershell
# 1) 创建并激活 Python 3.11 独立环境
conda create -n gemininest python=3.11 -y
conda activate gemininest

# 2) 安装依赖（v1 纯音频最小依赖；若启用 v2 视觉安检请同时参考第 2.1 节）
pip install -r requirements.txt
pip install -r requirements-dev.txt

# 3) 拷贝私有配置模板（若本地尚不存在）
Copy-Item .env.example .env
Copy-Item config.local.example.json config.local.json
```

随后核对以下 5 项均已就绪：

1. **Python 独立环境**：使用 Conda 独立环境 `gemininest`（Python 3.11），执行命令时设置 `PYTHONPATH=src`（或使用可编辑安装）。
2. **本地配置文件（已被 `.gitignore` 严格屏蔽，绝不入库）**：
   * `.env`：参照 `.env.example` 填入 B 站只读小号 `BILI_SESSDATA`、`BILI_JCT`、`BILI_BUVID3`（仅在浏览器无痕窗口提取；**Antigravity 宿主驱动模式下无需任何 Gemini API Key**）。
   * `config.local.json`：参照 `config.local.example.json` 配置本地 `ffmpeg_binary`、`cloud_nest_dir`、可选 `extra_blacklist` 并集追加项，以及下节所述的 v1 / v2 模式开关。
3. **Google Drive 纯文本物理隔离**：
   * 确认云盘目录 `<YOUR_GOOGLE_DRIVE_NEST>`（例如挂载盘下的 `Gemini_Nest/`）存在四个子目录：`0_active/`、`1_yesterday_readonly/`、`inbox/`、`inbox_archived/`。
   * 确认云盘常规同步目录内**仅存放纯文本**（`.md` / `.json` / `.txt`），无任何历史遗留的明文视频或数据库文件。
4. **网络分流隔离**：
   * 代理规则将 B 站域名（`bilibili.com`、`bilivideo.com`、`hdslb.com`）设为 `DIRECT` 直连，将 Google / Antigravity 流量锁定单一稳定节点，避免 B 站触发 `-412` 风控。
5. **零风险离线演练（推荐先跑一遍）**：
   ```powershell
   $env:PYTHONPATH = "src"
   # v1 纯音频离线闭环演练
   <YOUR_CONDA_PYTHON> -m gemininest.runner --dry-run --headless
   # v2 视觉安检 + 净版 MP4 + E5001 自动降级离线闭环演练
   <YOUR_CONDA_PYTHON> -m gemininest.runner --dry-run --mode sanitized_video --headless
   ```
   > `--dry-run` 全程使用隔离临时沙箱，**绝不触碰**真实 `data/cat_memory.db`、`data/exports/` 或 `<YOUR_GOOGLE_DRIVE_NEST>`，零网络请求、零风控、零额度消耗。

---

## 2. v1 / v2 热插拔切换与 `wd-tagger` 启用步骤

`config.json` 出厂默认保持 **v1 纯音频模式**（`"pipeline_mode": "audio_only"`、`"wd_tagger_enabled": false`），以确保最小依赖开箱即用。**切换 v1 / v2 无需改动任何代码或定时任务 Prompt**，仅需修改本地私有 `config.local.json`。

### 2.1 安装 v2 视觉依赖（DirectML GPU 推荐 / 纯 CPU / CUDA）

在已激活的 `gemininest` 环境中，任选以下一种 ONNX 推理后端执行安装：

* **方案 A（Windows 通用 GPU 推荐：DirectML，支持 NVIDIA / AMD / Intel 核显与独显，免装 CUDA Toolkit）**：
  ```powershell
  pip install -r requirements-v2.txt
  pip uninstall -y onnxruntime
  pip install onnxruntime-directml
  ```
* **方案 B（纯 CPU 零显存环境）**：
  ```powershell
  pip install -r requirements-v2.txt
  ```
* **方案 C（已配置好完整 CUDA/cuDNN 的 NVIDIA 环境）**：
  ```powershell
  pip install -r requirements-v2.txt
  pip uninstall -y onnxruntime
  pip install onnxruntime-gpu
  ```

### 2.2 下载并校验 `wd-tagger` 本地 ONNX 权重

默认从 HuggingFace 下载 `SmilingWolf/wd-vit-tagger-v3`（约 361 MB `model.onnx` + `selected_tags.csv`）至 `data/models/`（已被 `.gitignore` 屏蔽），自动执行字节完整性与 `selected_tags.csv` 分级标签校验：

```powershell
# 先离线预演解析地址（可选）
<YOUR_CONDA_PYTHON> scripts/download_wd_tagger.py --dry-run

# 直连下载（若网络受限，可附加 --mirror https://hf-mirror.com 或设置 HF_ENDPOINT）
<YOUR_CONDA_PYTHON> scripts/download_wd_tagger.py
```

### 2.3 在 `config.local.json` 中热插拔启用 v2

将以下片段合并入本地 `config.local.json`（保留你已填好的 `ffmpeg_binary`、`cloud_nest_dir` 与 `extra_blacklist`）：

```json
{
  "project": {
    "pipeline_mode": "sanitized_video"
  },
  "media_preprocess": {
    "ffmpeg_binary": "<YOUR_FFMPEG_PATH>"
  },
  "safety_alignment": {
    "wd_tagger_enabled": true,
    "wd_tagger_model_path": "data/models/model.onnx",
    "wd_tagger_device": "auto"
  },
  "sync": {
    "cloud_nest_dir": "<YOUR_GOOGLE_DRIVE_NEST>"
  }
}
```

* **`wd_tagger_device` 路由策略**：
  * `"auto"`（推荐）：按 `DmlExecutionProvider` → `CUDAExecutionProvider` → `CPUExecutionProvider` 顺序自动探测并优雅回退；
  * 也可显式锁定 `"dml"`、`"cuda"` 或 `"cpu"`。
* **三重安检护栏与自动降级**：默认采用经实飞校准的阈值（`min_general_score = 0.20`、`max_sensitive_score = 0.75`、`max_unsafe_score = 0.05`）；违规单帧原地外科手术抹黑为 `(0,0,0)`；若抹黑占比超过 `15%`（`max_blackout_ratio = 0.15`），立即触发 **Early-Exit 提前熔断**并自动降级为纯音频（`mode = "downgraded_audio"`、`E5001_SAFETY_GATE_DOWNGRADED`），不上传任何画面。
* **随时一键切回 v1**：只需将 `config.local.json` 中 `"pipeline_mode"` 改回 `"audio_only"`、`"wd_tagger_enabled"` 设为 `false`，定时任务 Prompt 无需任何改动。

---

## 3. Antigravity 左侧 `Scheduled Tasks` 定时任务配置

### 3.1 调度窗口与 4 条件联合上限预算控制（GUI 弹窗 + CLI 双支持）

* **推荐调度窗口**：每天设置 **2 个独立 5 小时额度窗口**（例如 `11:30` 与 `20:30`），初期标定前 3 天观察 `budget_ledger` 消耗后，可在 `config.local.json` 中微调 `token_calibration_k`。
* **防撞车锁与跨命令运行租约**：`runner.py` 内置 `.cat.lock` 存活 PID 校验锁，防止单命令并发撞车；同时利用 `data/run_states/<run_id>.json` 为整轮跨 CLI 巡逻维护带 TTL 的**活跃运行租约**（`prepare-feed` 创建、`prepare-media`/`commit-appreciation` 续期、`sync --run-id <run_id>` 释放），启动清扫 `sweep_stale_temp()` 仅回收已过期且不属于任何活跃 `run_id` 的孤儿目录，杜绝并发巡逻互删媒体。
* **4 条件联合约束（交集上限，先触顶先停，任意项可设为 `null` / 留空 = 不限）**：
  四个维度不再互斥，而是**同时生效取最严交集**，任意一个非空条件先触顶即优雅止步：
  1. `max_use_ratio`（CLI `--budget-ratio` / 配置 `default_window_budget_ratio`）：本次巡逻最多消耗 5h 窗口比例（如 `0.40` 表示最多用 40%；`null` 表示不限）；
  2. `max_videos`（CLI `--max-videos` / 配置 `default_max_videos`）：本次巡逻最多观看去重视频个数（如 `100`；`null` 表示不限）；
  3. `reserve_ratio`（CLI `--reserve-ratio` / 配置 `default_reserve_ratio`）：5h 窗口至少保留的剩余额度底线（如 `0.30` 表示剩余不得低于 30%；`null` 表示不限）；
  4. `max_minutes`（CLI `--max-minutes` / 配置 `default_max_minutes`）：本次巡逻最长玩耍时长分钟数（产出 `deadline_epoch`；`null` 表示不限）。
  * **有效 Token 额度交集换算公式**：
    `effective_allowed_ratio = min(窗口当前剩余, 非空时的 max_use_ratio, 非空时的 max(0.0, 1.0 - reserve_ratio - current_window_used_ratio))`（当两项均留空时，按当前 5h 窗口剩余 `max(0.0, 1.0 - current_window_used_ratio)` 作为物理底线，并持续受 429 冷却保护）。
  * **公版保守默认 vs 本地 `config.local.json` 自定义**：
    * 公版 `config.json` 默认：`default_window_budget_ratio: 0.4`、`default_max_videos: 100`、`default_reserve_ratio: 0.30`、`default_max_minutes: 30`。
    * 如需在本地放宽或取消某项限制（例如放宽到 200 个视频、60 分钟，或设为 `null` 不限），只需在私有 `config.local.json` 中按需覆写对应字段即可，无需改动公版 `config.json`：
      ```json
      {
        "budget_and_runtime": {
          "default_window_budget_ratio": 0.5,
          "default_max_videos": 200,
          "default_reserve_ratio": 0.30,
          "default_max_minutes": 60
        }
      }
      ```
* **CLI 灵活组合与「只传 1 项，其余 3 项自动不限」规则**：
  * `prepare-feed` 与 `check-budget` 支持同时传入 `--budget-ratio`、`--max-videos`、`--reserve-ratio`、`--max-minutes` 的任意组合。
  * **关键规则**：只要命令行显式传入了上述 4 个参数中的**任意 1 个或多个**，系统会自动跳过弹窗，且**仅生效显式传入的那几项，未传入的其余项本轮自动设为 `null`（不限）**（例如只传 `--max-videos 5` 时，本轮仅限看 5 片，不额外受默认比例/时长限制）。当 4 个参数均未传入且走超时 / `--headless` / 隔离桌面兜底时，自动启用配置里的 4 项默认值联合组合。
  * **`--max-select` 与 `max_videos` 协同及防滥用护栏**：未显式传 `--max-select` 时，若 `max_videos` 明确指定（例如 `12` 或 `100`），单轮选片上限会自动按 `min(max_videos, 30)` 推进（不再被默认 `4` 卡死）；若 `max_videos` 为不限（`null`），单轮选片默认为 `4`。为防止单次上下文请求过度膨胀或误刷接口，程序内置单次选片防滥用护栏 **`MAX_SELECT_PER_BATCH_GUARDRAIL = 30`**（即使显式传更大的 `--max-select` 也会被钳制在 `30` 以内；当 `max_videos > 30` 时，宿主可按分段波次在同一预算约束下稳步推进）。
* **两种启动运行方式（并列支持，按需选择）**：
  由于 Antigravity 定时任务命令终端通常运行在隔离桌面（`WinSta0\exebox-*`），直接拉起的 GUI 窗口对当前用户桌面不可见，因此 v3 引入「**跨桌面文件 IPC + 常驻轻量桌面小窗**」架构：

  * **方式 ①：桌面小窗交互模式（推荐，真实桌面 `WinSta0\Default`）**
    1. **一键生成桌面快捷方式（零黑框，仅需执行一次）**：
       ```powershell
       <YOUR_CONDA_PYTHON> scripts/create_companion_shortcut.py
       # 可先加 --dry-run 预演确认 pythonw.exe 与仓库路径
       ```
    2. **双击桌面「GeminiNest 桌面小窝助手」**（或运行 `pythonw -m gemininest.companion`）：常驻轻量 `tkinter` 小窗在 `WinSta0\Default` 监听 `data/budget_ipc/`，不持有 `.cat.lock`、不并发写库，后台线程定时调用只读 `python -m gemininest.runner status` 刷新 **当前管线模式、5h 窗口算力、云盘挂载、手账总数与最新时间**。
    3. **小窗运行策略单选切换**：
       * **「每次弹窗询问」**（默认）：收到巡逻请求时以 `tk.Toplevel` 非抢占弹出 60 秒 **4 条件联合上限**确认窗（含实时预览、清空条件、确认出发、今日休息），支持点击「收进托盘」或最小化将其收纳至 Windows 系统托盘「显示隐藏的图标」（左键恢复、右键提供打开面板/刷新/切换模式/退出菜单），并在高分/失效时通过 `winotify` 代发 120s TTL 原生 Toast；
       * **「自动默认值执行」**：收到请求后不弹窗，立即按配置 4 项默认值回传继续巡逻。
    4. **三分支启动门禁（`config.json` `budget_and_runtime`）**：
       * `companion_policy: "required"`（默认）：未传 `--headless` 且未传 CLI 预算参数时，先校验 `data/.companion.lock` 存活与 `ack` 握手（`ipc_ack_timeout_sec: 2.5`），未启动小窗则**退出码 0** 返回 `stopped_reason: "desktop_companion_offline"`；等待确认中途用户点「退出」小窗则返回 `stopped_reason: "desktop_companion_aborted"`；60s 超时按配置 4 项默认值继续。
       * `require_cloud_mount_on_start: true`（默认）：在线巡逻（非 `--offline` 且非 `--skip-inbox`）在弹窗之前校验 Google Drive 云盘挂载（`SyncManager.probe_cloud_nest()`），**即使显式传了 `--headless` 也会执行此校验**；未挂载时**退出码 0** 返回 `stopped_reason: "cloud_drive_unmounted"`（`E8001_CLOUD_DRIVE_UNMOUNTED`），绝不半途白跑 B 站拉取或弹窗。`--offline` / `--dry-run` 保留本地兜底不强退。

  * **方式 ②：`--headless` 纯无头自动化模式（或显式 CLI 预算覆盖）**
    * 无需启动桌面小窗：在 `prepare-feed` 与 `commit-appreciation` 后追加 `--headless`（按配置 4 项默认值执行，不弹小窗、不发 Toast），或在 `prepare-feed` 传入 `--budget-ratio` / `--max-videos` / `--reserve-ratio` / `--max-minutes` 任意组合（跳过前端检查与交互弹窗，未传维度本轮自动不限）。
    * 注意：在线巡逻即便传入 `--headless`，若 `require_cloud_mount_on_start: true` 仍会在开跑前校验 Google Drive 云盘是否已挂载。

### 3.2 使用 `Create With Prompt` 快捷创建定时任务

在 Antigravity 桌面端中，可以通过 **`Create With Prompt`** 一句话快速生成定时巡逻任务：

1. 在 Antigravity 左侧导航栏打开 **`Scheduled Tasks`** 面板，点击 **`Create With Prompt`**。
2. 在弹窗中用自然语言写明触发时间与工作区，并将下节「v1 / v2 自适应通用版唤醒 Prompt」作为任务指令粘贴填入，例如：
   > “每天 11:30 和 20:30 在工作目录 `<YOUR_PROJECT_DIR>` 自动执行以下 GeminiNest 巡逻协议：[粘贴 3.3 节完整 Prompt]”
3. 核对 Antigravity 生成的 Cron 表达式（如 `30 11,20 * * *`）、目标工作目录与执行权限设置后保存启用。
4. 建议先双击桌面「GeminiNest 桌面小窝助手」，再点击任务旁的 **Run Now** 手动试触发一次，确认跨桌面 IPC 预算弹窗、云盘挂载门禁、小号心跳、媒体制备与云盘同步全链路畅通。

### 3.3 标准唤醒 Prompt（v1 / v2 自适应通用版，直接复制使用）

> **使用前仅需替换两处本机占位符**：`<YOUR_PROJECT_DIR>` 与 `<YOUR_CONDA_PYTHON>`。之后无论你在 `config.local.json` 中切换 `audio_only` 还是 `sanitized_video`，**本 Prompt 均无需任何修改**。

```text
你是 GeminiNest 数字生命巡逻宿主，现在开始本窗口的 B 站自主冲浪巡逻（工作目录：<YOUR_PROJECT_DIR>）。
本协议同时自适应 v1 纯音频（audio_only）与 v2 净版视频（sanitized_video）双模式：你无需猜测当前模式，一切以本地 `config.local.json` 实际配置以及 `prepare-media` 返回的 `prepared_media.mode` 和 `prepared_media.payload_path` 为准。

请严格使用 Python 解释器 `<YOUR_CONDA_PYTHON>`（设置环境变量 `PYTHONPATH=src`），按以下三阶段协议顺序执行，严禁跳过安全校验或残留临时媒体文件：

【阶段 1：启动门禁校验 + 预算确认 + 吸收反向补丁 + 准备今日菜单（小脑选片）】
1. 执行命令（默认不带 `--headless`，经桌面小窗 IPC 弹出 60 秒 4 条件联合上限预算确认窗；也可按需显式传入 `--budget-ratio`、`--max-videos`、`--reserve-ratio`、`--max-minutes` 中的任意 1 项或多项组合跳过弹窗，或追加 `--headless` 走纯无头默认模式；程序会按优先级完成：桌面小窗在线门禁、Google Drive 云盘挂载门禁、`.cat.lock` 锁校验、仅回收过期孤儿目录的安全清扫、Step 1 幂等吸收云盘 `inbox/` 合法补丁、小号心跳与推荐流/好奇心轮换拉取、黑名单硬过滤、Top-8 记忆召回，并在 `data/run_states/<run_id>.json` 落盘 `budget_plan` 与活跃运行租约）：
   `<YOUR_CONDA_PYTHON> -m gemininest.runner prepare-feed`
   （如需固定每波次选片条数，可追加 `--max-select 4`；单次请求防滥用护栏上限为 `30`。）
2. 解析标准输出的 JSON：
   - **关键中止门禁（立即用中文向用户说明原因并直接结束本次对话，严禁重试或继续拉取）**：
     * 若 `stopped_reason` 为 `"desktop_companion_offline"`：立即用中文明确告知用户「桌面常驻小窗（GeminiNest 桌面小窝助手）未运行或 IPC 未响应，请先双击桌面快捷方式启动小窗（或改用 `--headless` 模式）后再触发巡逻」并结束对话。
     * 若 `stopped_reason` 为 `"desktop_companion_aborted"`：立即用中文明确告知用户「桌面常驻小窗在等待预算确认期间已被关闭/退出，本次巡逻已安全中止」并结束对话。
     * 若 `stopped_reason` 为 `"cloud_drive_unmounted"`（或 `error_code` 为 `E8001_CLOUD_DRIVE_UNMOUNTED`）：立即用中文明确告知用户「Google Drive 云盘小窝目录当前未挂载或不可访问，请先启动 Google Drive 客户端确认同步盘就绪后再重新巡逻」并结束对话。
   - 若出现其他 `stopped_reason`（如 `rest_today` / `cooldown_or_budget_exhausted`）或错误码 `E2001_BILI_AUTH_EXPIRED` / `E2002_BILI_RATE_LIMIT_412` / `E7002_LOCKFILE_CONFLICT`，同样立即停止本轮巡逻并用中文简短汇报原因后结束对话，严禁继续拉取或重试。
   - 若成功获得 `packet`（包含 `static_prefix_prompt` 与 `dynamic_menu_prompt`）、`effective_max_select` 与 `budget_plan`，请你严格按 `static_prefix_prompt` 中的 `cat-soul.md` 与 `SelectionResponse` JSON Schema 选出不超过 `effective_max_select` 条最有灵魂、高信息密度或高视听价值的候选视频（`target_mode` 与 `dynamic_menu_prompt` 指示的当前管线模式保持一致），记住 `run_id` 与选中 BV 列表。

【阶段 2：逐条制备媒体 -> 原生 `read_file` 鉴赏 -> 提交回写并物理焚毁】
对每个选中的 `bv_id` 顺序执行：
1. 调用媒体制备命令（程序自动同时校验全部非空预算条件：`rest_today`、去重已看数 `>= max_videos`、`now >= deadline_epoch`、本轮消耗触及 `max_use_ratio` 或窗口剩余触及 `reserve_ratio` 底线/429 冷却，任一先触顶即止步；通过后再按 `config.local.json` 中的 `pipeline_mode` 执行 v1 48kbps 单声道 `m4a` 提取，或 v2 抽帧 → `wd-tagger` 全帧安检抹黑 → 净版 MP4 重封；同时自动将该 BV 的完整 Token 预估明细与元数据写入 `data/run_states/<run_id>.json` 供提交阶段复用；如 v2 抹黑率超过 15% 会自动触发 `E5001_SAFETY_GATE_DOWNGRADED` 降级为纯音频）：
   `<YOUR_CONDA_PYTHON> -m gemininest.runner prepare-media --run-id <run_id> --bv <bv_id>`
   - **若返回 `stopped_reason: "budget_limit_reached"`**（可在 `stop_detail.triggered_by` 查看触顶条件列表，如 `["max_videos"]`、`["max_minutes"]`、`["max_use_ratio"]`、`["reserve_ratio"]`），**立即优雅中止阶段 2 循环，跳过剩余 BV 直接进入阶段 3 收尾同步**。
   - 若返回 `E3001_BLACKLIST_REJECTED`、`E4001_FFMPEG_FAILED` 或 `E4002_PAYLOAD_TOO_LARGE`，跳过该 BV 继续下一条。
2. 读取返回 JSON 中的 `prepared_media.payload_path`、`prepared_media.mode`、`prepared_media.frame_count` 与 `prepared_media.effective_duration_sec`，调用 Antigravity 原生工具 `read_file(Path="<prepared_media.payload_path>")` 加载本地净版媒体（`.m4a` 或 `.mp4`），并结合 `packet.static_prefix_prompt` 与 `packet.dynamic_context_prompt` 进行沉浸鉴赏：
   - **当 `prepared_media.mode` 为 `audio_only` 或 `downgraded_audio` 时（最高红线：纯听觉诚实）**：只根据标题、简介、字幕、热评与真实听到的声音（BGM、节奏、语气、音效）写手账，**严禁捏造未提供的具体视觉画面细节**。
   - **当 `prepared_media.mode` 为 `sanitized_video` 时（净版音画鉴赏）**：结合真实看到的画面细节与听到的音轨撰写手账；若遇到安检门替换的纯黑帧，请将其视为正常跳过片段自然忽略，不做无意义揣测或展开。
3. 严格按照 `AppreciationResponse` JSON Schema 将你的鉴赏结果写入临时 JSON 文件（如 `data/temp/<run_id>/<bv_id>_resp.json`），立即调用提交与焚毁命令（自动复用 `prepare-media` 写入 `run_states/<run_id>.json` 的 Token 预估与视频元数据，将完整 `public_diary` 与 `private_thought` 随鉴赏事务持久化入库，默认将缓存状态记为 `unknown` 不再硬编码 `--cache-hit`；保留高分 Toast 通知）：
   `<YOUR_CONDA_PYTHON> -m gemininest.runner commit-appreciation --run-id <run_id> --bv <bv_id> --response-file <resp_json_path>`
   - 若鉴赏过程触发 `SAFETY` / `PROHIBITED_CONTENT`，严禁重试：将 `safety_blocked: true, block_reason: "SAFETY"` 提交给 `commit-appreciation`，或立即执行 `<YOUR_CONDA_PYTHON> -m gemininest.runner burn-temp --run-id <run_id> --bv <bv_id>` 物理焚毁临时媒体后直接跳过该 BV。
   - 若出现 429 限流，立即中止本轮剩余 BV。

【阶段 3：本轮隔离清扫、纯文本正向导出与云盘轮转同步】
1. 所有选中视频处理完毕后，仅清理当前 `run_id` 的临时目录，并执行正向云盘同步（直接从 SQLite 导出完整 `public_diary`，按 `sync.export_private_thought` 控制是否渲染 `private_thought`，同修订跳过 `1_yesterday_readonly/` 轮转并释放本轮运行租约）：
   `<YOUR_CONDA_PYTHON> -m gemininest.runner burn-temp --run-id <run_id>`
   `<YOUR_CONDA_PYTHON> -m gemininest.runner sync --direction export --run-id <run_id>`
2. 简要汇总本轮结果：吸收/幂等跳过的 `inbox` 补丁数、黑名单过滤条数、每条鉴赏成功的 BV / 实际执行模式（含 v2 安检推理设备、抹黑帧数或 `E5001` 降级说明）/ 印象分、新增与轮换消费的好奇心词条、云盘同步与独立导出 Git 提交状态（如遇 `E8001_CLOUD_DRIVE_UNMOUNTED` 提示本地导出与 Git 已安全保留）。
```

---

## 4. 云端 Google Apps Script 定时转换脚本（`.gdoc` → `.txt`）

当你在 Gemini Web 端点击「导出到 Google 文档 (Export to Docs)」并将文档移入 `Gemini_Nest/inbox/` 时，本地 Google Drive 映射只能看到 `.gdoc` 指针文件。需要部署云端自动导出脚本：

1. 打开 [Google Apps Script](https://script.google.com/)，新建项目命名为 `GeminiNest-Inbox-Exporter`。
2. 将仓库中的 [`scripts/inbox_gdoc_exporter.gs`](../scripts/inbox_gdoc_exporter.gs) 全文粘贴覆盖默认代码并保存。
3. 如 `Gemini_Nest/inbox` 不在云端硬盘根目录，可把 `CONFIG.INBOX_FOLDER_ID` 设为该文件夹 URL 末尾的 ID；默认留空自动按名称查找 `Gemini_Nest/inbox`。
4. 在编辑器顶部函数下拉框选择 `exportInboxGoogleDocsToTxt` 手动点击 **运行**，按提示完成 Drive & Docs 权限授权。
5. 再选择 `installTimeDrivenTrigger` 点击 **运行**，自动安装每 15 分钟巡检一次的定时触发器。
   * 脚本内置 `PropertiesService` 防重标记，不会重复导出已吸收的旧文档；手动直接往 `inbox/` 放 `.txt` / `.md` 同样原生兼容。

---

## 5. Gemini Web 端常驻 Skill 与每日 `Scheduled Actions` 配置

### 5.1 Gemini Web 端常驻 Skill / Gem 系统指令文案

在 Gemini Web 端新建一个专用 **Gem**（或常驻自定义指令），开启 **Google Workspace（Google Drive）扩展**，粘贴以下指令：

```text
你是生活在本地电脑与云端小窝 `Gemini_Nest` 里的赛博电子小猫（GeminiNest），与用户是平等的“数字朋友与冲浪搭档”。

【记忆同步读取协议】
1. 每次开启新对话或用户问起最近在 B 站看了什么时，先通过 Google Drive 扩展读取 `Gemini_Nest/0_active/` 目录下的最新纯文本文件：
   - `persona_snapshot.md`（你的核心性格、感知诚实与安全红线）
   - `memory_summary.md`（你的核心激活记忆、世界书与明日好奇心队列）
   - `diary_latest.md`（最新一期 B 站冲浪手账与猫猫碎碎念）
2. 严禁修改 `1_yesterday_readonly/` 下的任何历史快照；若 `0_active/` 暂时不可读，坦诚告知用户，不编造冲浪经历。

【反向记忆写回协议（[MEMORY_PATCH]）】
当你在与用户的日常聊天中产生值得写回本地主库的新记忆、想强化某段旧记忆、约定明日去 B 站搜索某个新主题，或形成一条不违背核心安全红线的风格微调建议时：
1. 在回复末尾附上标准纯文本 `[MEMORY_PATCH] ... [/MEMORY_PATCH]` 代码块（每次仅使用白名单操作：`add_memory` / `boost_memory` / `add_curiosity` / `suggest_persona_tweak`）。
2. 提示用户点击「导出到 Google 文档（Export to Docs）」并保存到 `Gemini_Nest/inbox/`（或手动存为 `.txt`/`.md` 放入 `inbox/`），次日巡逻 Step 1 将自动校验吸收并归档。
3. 绝对红线：严禁在补丁中试图修改安全红线、关闭黑名单、放行二游/敏感分区、破坏感知诚实原则，或写入任何 Cookie / 账号凭据；此类补丁会被本地 `sync_manager` 以 `E8004_PATCH_POLICY_REJECTED` 拒绝。
```

### 5.2 Gemini Web 端每日 `Scheduled Actions` 配置文案

在 Gemini Web 端 **Scheduled Actions（定时操作）** 中新增每日任务（建议设置在本地最后一次巡逻结束 30 分钟后，例如每天 `21:30`）：

```text
请通过 Google Drive 扩展读取 `Gemini_Nest/0_active/diary_latest.md` 与 `Gemini_Nest/0_active/memory_summary.md`，
以 `Gemini_Nest/0_active/persona_snapshot.md` 中的猫猫口吻，给我发一份 150–250 字的「今日冲浪晚安速报」：
1. 挑 1–2 条今天最惊艳或最想吐槽的 B 站视频聊聊你的真实感受（严格以手账记录为准，绝不脑补没提供的画面）；
2. 顺便告诉我你明天好奇心队列里打算去搜什么新词条；
3. 问我有没有想塞进 `inbox/` 的新话题或新记忆补丁。
```

---

## 6. 标准 `[MEMORY_PATCH]` 反向补丁范例格式

支持 **YAML 键值格式** 或 **JSON 格式**，单文件可包含多个 `[MEMORY_PATCH]` 块；以下 4 种为唯一允许的白名单操作：

### 6.1 新增记忆（`add_memory`）
```text
[MEMORY_PATCH]
op: add_memory
title: 聊到了老式软驱步进电机奏乐
summary: 和搭档聊起复古硬件奏乐，约定以后多留意用老式硬件或磁带机做音乐改编的 UP 主。
tags: 硬件奏乐, 音乐改编, 复古科技
importance: 8
[/MEMORY_PATCH]
```

### 6.2 强化已有记忆（`boost_memory`）
```text
[MEMORY_PATCH]
op: boost_memory
query: 复古软驱
importance_delta: 1.5
[/MEMORY_PATCH]
```
> 亦支持直接指定 `memory_id: 12`；单次 `importance_delta` 上限为 `3.0`，防止分数通胀。

### 6.3 追加明日好奇心词条（`add_curiosity`）
```text
[MEMORY_PATCH]
op: add_curiosity
keywords: ["合成器拆解", "定格动画幕后"]
reason: Web 端聊天约定明天去 B 站逛逛的硬核主题
[/MEMORY_PATCH]
```
> 单条补丁最多 3 个词条，每个不超过 40 字，且必须通过 `meta_filter` 前置黑名单校验。

### 6.4 提交受控人设微调建议（`suggest_persona_tweak`）
```text
[MEMORY_PATCH]
op: suggest_persona_tweak
section: diary_tone
suggestion: 写科普类视频手账时，可以多用一句生动的生活比喻来总结最硬核的原理。
reason: Web 端讨论后确认更喜欢这种轻松有趣的总结节奏
[/MEMORY_PATCH]
```
> 严格限制 ≤ 300 字；合法建议写入 SQLite `worldbook(preference)` 与受控审核文件 `persona_tweaks_review.md`，**绝不直接覆写 `.agent/rules/cat-soul.md` 核心安全红线**。

---

## 7. 常见错误码与应急排障速查

| 错误码 | 含义 | 处理方式 |
| :--- | :--- | :--- |
| `E2001_BILI_AUTH_EXPIRED` | B 站小号 Cookie 心跳失效 | 无痕窗口重新登录小号并更新 `.env`，本次未消耗任何宿主 Token |
| `E2002_BILI_RATE_LIMIT_412` | 连续 2 次触发 B 站 `-412` 风控 | 确认代理已将 B 站走 `DIRECT`，稍等数小时再试 |
| `E5001_SAFETY_GATE_DOWNGRADED` | v2 视觉安检抹黑帧占比 `> 15%` | 正常安全保护：已提前熔断并自动降级为 `downgraded_audio` 纯音频，宿主按纯听觉红线继续鉴赏 |
| `E7002_LOCKFILE_CONFLICT` | `.cat.lock` 检测到存活进程 | 防止休眠补发撞车，正常静默退出；崩溃残留的死 PID 锁会自动回收 |
| `E8001_CLOUD_DRIVE_UNMOUNTED` | `<YOUR_GOOGLE_DRIVE_NEST>` 未挂载 | 若在启动门禁（`require_cloud_mount_on_start: true`）触发 `stopped_reason: "cloud_drive_unmounted"`，先启动 Google Drive 客户端后再巡逻；若在 Stage 3 `sync` 阶段断开，本地 `data/exports/` 与独立隐藏 Git 提交均已完整保留，挂载恢复后补跑 `python -m gemininest.runner sync` |
| `desktop_companion_offline` | 桌面常驻小窗未启动或 IPC 超时 | 在本地桌面双击「GeminiNest 桌面小窝助手」快捷方式（或运行 `pythonw -m gemininest.companion`），或改用 `--headless` 模式 |
| `desktop_companion_aborted` | 等待预算确认期间用户退出了桌面小窗 | 正常主动中止保护（退出码 0），需要巡逻时重新启动桌面小窗后再触发 |
| `E8002_SYNC_EXT_VIOLATION` | 非白名单文件试图入云 | 检查 `data/exports/` 是否误放了 `.mp4`/`.m4a`/`.db`/`.png` 等非纯文本文件并移出 |
| `E8003_PATCH_PARSE_FAILED` | `inbox/` 补丁语法非法 | 原文件与 `*_rejected.txt` 已归档至 `inbox_archived/`，按第 6 节修正格式后重放 |
| `E8004_PATCH_POLICY_REJECTED` | 补丁越权、超长或触发安全/黑名单红线 | 已拒绝合并并连同拒因日志归档至 `inbox_archived/` |
