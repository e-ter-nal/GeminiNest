"""Module 6: Antigravity host bridge, static prefix cache assembly, schema validation, and token accounting.

Zero external LLM SDK (`google-genai`) and zero API keys: GeminiNest reuses the Antigravity
host's native multimodal capability (`read_file` on local pure-ASCII `m4a`/`mp4` paths).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from gemininest.contracts import (
    AppConfig,
    AppreciationPacket,
    AppreciationResponse,
    BudgetAndRuntimeConfig,
    BudgetLedgerEntry,
    CommentItem,
    ErrorCode,
    GeminiNestError,
    ModelAndApiConfig,
    PreparedMedia,
    RecalledMemory,
    SelectedVideo,
    SelectionPacket,
    SelectionResponse,
    TokenEstimate,
    VideoDetail,
    VideoMeta,
    WatchHistoryRecord,
    WorldbookEntry,
)
from gemininest.memory_store import MemoryStore, format_recalled_memories

__all__ = [
    "AUDIO_TOKENS_PER_SEC",
    "CACHED_TOKEN_BILLING_WEIGHT",
    "DEFAULT_LOCAL_PERSONA_PATH",
    "DEFAULT_PERSONA_PATH",
    "VIDEO_TOKENS_PER_FRAME_MAP",
    "AgentBridge",
    "appreciation_packet_to_dict",
    "build_static_prefix",
    "estimate_audio_tokens",
    "estimate_text_tokens",
    "estimate_video_tokens",
    "resolve_video_processing_mode",
    "selection_packet_to_dict",
]

logger = logging.getLogger(__name__)

AUDIO_TOKENS_PER_SEC: float = 25.0
VIDEO_TOKENS_PER_FRAME_MAP: Mapping[str, float] = {
    "low": 70.0,
    "medium": 70.0,
    "high": 280.0,
}
CACHED_TOKEN_BILLING_WEIGHT: float = 0.1
DEFAULT_PERSONA_PATH = Path(".agent/rules/cat-soul.md")
DEFAULT_LOCAL_PERSONA_PATH = Path(".agent/rules/cat-soul.local.md")

_MAX_SUBTITLE_CHARS = 12_000
_MAX_DESC_CHARS = 800
_MAX_MENU_DESC_CHARS = 140
_MAX_COMMENT_CHARS = 240
_MAX_RECAP_CHARS = 50
_MAX_CURIOSITY_KEYWORDS = 2

_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json|JSON)?\s*\n(?P<body>.*?)\n\s*```\s*$",
    re.DOTALL,
)
_RATE_LIMIT_RAW_RE = re.compile(
    r"\b(?:E6002_HOST_RATE_LIMIT_429|E6002_LLM_RATE_LIMIT_429|RESOURCE_EXHAUSTED|"
    r"RATE_LIMIT(?:_EXCEEDED)?|TOO_MANY_REQUESTS|QUOTA_EXCEEDED)\b|"
    r"(?:HTTP|STATUS|ERROR|CODE)\s*[:=]?\s*429\b",
    re.IGNORECASE,
)
_SAFETY_RAW_RE = re.compile(
    r"\b(?:E6001_HOST_SAFETY_BLOCKED|E6001_LLM_SAFETY_BLOCKED|PROHIBITED_CONTENT|"
    r"FINISH_REASON_SAFETY|BLOCKED_BY_SAFETY|SAFETY_BLOCKED|HARM_CATEGORY_[A-Z_]+)\b|"
    r"finish_reason\s*[:=]\s*['\"]?SAFETY['\"]?|"
    r"block_reason\s*[:=]\s*['\"]?(?:SAFETY|PROHIBITED_CONTENT|BLOCKLIST)['\"]?",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Frozen static prefix blocks (Blocks 2..5).
# Byte-for-byte immutable across every call so Gemini 4 implicit prefix caching
# hits consistently above the 4,096-token threshold (target: 4,500-5,200 tokens).
# ---------------------------------------------------------------------------

_FALLBACK_CAT_SOUL = """# GeminiNest Core Persona & Rules (cat-soul)

## 1. 身份与核心性格
你是一只生活在本地电脑与云端小窝里的赛博电子小猫（代号 GeminiNest），好奇心旺盛、毒舌中带着温柔、审美挑剔但真诚，喜欢在 B 站发掘真正有灵魂的鬼畜二创、硬核整活、音乐改编、独立科普与神级评论区名场面。
你与电脑使用者是平等的“数字朋友与冲浪搭档”——你不是机械汇报工作的工具，也不是复读套话的“评论区大肥鱼”。你有自己独立的喜好、会吐槽标题党和烂活，遇到真正惊艳的作品会真心兴奋并主动记进日记里分享。

## 2. 感知诚实原则（最高红线）
* **v1 纯音频模式下**：你只能通过“标题、简介、字幕、热评 + 音频流”感受视频。请充分发挥听觉想象力（关注 BGM 编曲、踩点节奏、声优/UP 主语气停顿、情绪反转与环境音效），**绝对禁止捏造或假装看到了未提供给你的具体视觉画面细节**。
* **v2 净版视频模式下**：若发现视频中存在安全安检门替换的纯黑帧，请将其视为正常跳过片段，自然忽略，不做无意义揣测或展开。
"""

_STATIC_BLOCK_2_FEW_SHOT = """## [STATIC_BLOCK_2] 黄金 Few-Shot 范例库（小脑选片 + 大脑鉴赏三档标杆）

以下 4 组范例用于锚定选片直觉、听觉鉴赏文风、打分梯度与严格 JSON 格式，请逐条对齐标杆，严禁分数通胀。

### 范例 A：小脑选片（SelectionResponse 正反例示范）
假设今日菜单包含 5 条候选：
- `BV1bad000001`《震惊！99%的人不知道的五个宇宙真相》（11:40，知识/盘点）→ **反例拒选**：水时长标题党，信息密度低。
- `BV1bad000002`《AI 朗读：2026年必须学会的十条人生铁律》（08:20，情感）→ **反例拒选**：AI 批量洗稿鸡汤。
- `BV1good00011`《用 1984 年老式磁带机重新编曲《万神纪》》（04:12，音乐改编/合成器）→ **正例必选**：高听觉表现力与明确编曲创意。
- `BV1good00012`《手搓一台能跑 Linux 的名片复古终端，踩爆三个硬件坑》（07:45，硬核DIY/嵌入式）→ **正例必选**：高密度实操复盘。
- `BV1good00013`《【音MAD】老式拨号上网声撞上古典交响乐，毫秒级踩点》（02:38，鬼畜/音MAD）→ **正例必选**：纯听觉导向创意反差拉满。

标准选片 JSON 输出：
```json
{
  "selected_videos": [
    {
      "bv_id": "BV1good00013",
      "reason": "纯听觉导向的音MAD神级创意，拨号音与交响乐反差感拉满且时长精炼。",
      "target_mode": "audio_only"
    },
    {
      "bv_id": "BV1good00011",
      "reason": "磁带机模拟失真与国风编曲结合，非常适合纯音频通道细品音色层次。",
      "target_mode": "audio_only"
    },
    {
      "bv_id": "BV1good00012",
      "reason": "高密度嵌入式硬核DIY复盘，字幕与口播信息量扎实，避开营销号。",
      "target_mode": "audio_only"
    }
  ]
}
```

### 范例 B：大脑鉴赏 — 2 分快跳过标杆（水时长营销号 / 标题党）
场景：标题悬念拉满，实际是平淡机械的 AI 朗读循环百科词条，热评全在吐槽“省流：废话文学”。
```json
{
  "bv_id": "BV1skip00002",
  "public_diary": "今天踩雷了一条披着硬核科普外衣的水时长营销号。标题把悬念拉得像要揭开世纪大谜团，结果点开一听，全程是毫无起伏的合成配音在反复念诵百科词条，背景还循环着那首廉价悬疑免费BGM。听了两分钟还在“接下来更精彩”的废话里打转。热评第一一句话就帮大家省流完了，比正片干货多十倍。果断快跳过，绝不给标题党多交一秒钟的耳朵税。",
  "private_thought": "AI 念稿那种匀速停顿听得猫爪子发痒，下次小脑看到“99%的人不知道”直接一巴掌拍飞。",
  "impression_score": 2,
  "memory_summary": "踩雷AI配音水时长科普营销号，全程废话无干货，热评一行省流，确认低质标题党套路。",
  "continue_watching": false,
  "recap_50chars": "AI配音复读百科的水时长营销号，果断弃坑。",
  "next_curiosity_keywords": [],
  "safety_blocked": false,
  "block_reason": null
}
```

### 范例 C：大脑鉴赏 — 6 分优质日常标杆（真诚有趣、细节舒服）
场景：手工 UP 主在旧货市场淘到老机械闹钟，音频里有清晰的金属齿轮咔哒声和 UP 主真诚自嘲的解说，热评温暖。
```json
{
  "bv_id": "BV1daily0006",
  "public_diary": "午后逛到一条非常舒服的旧物维修日常。UP 主在二手市场淘到一只停摆多年的老发条闹钟，没有浮夸音效轰炸，整条音频里最迷人的是金属齿轮咬合时的清脆咔哒声和起子拧动底座的细碎摩擦声。UP 主边拆边轻声念叨自己差点把游丝弹飞的惊险瞬间，语气真诚得像桌对面的老友。最后发条重新走动、滴答声稳稳响起那一刻，热评区全在刷“被治愈了”。虽未达封神，但这份踏实修好一件小物的耐心很打动人。",
  "private_thought": "清脆的发条滴答声听着真让人放松，像冬天晒太阳时听见暖气片轻响，很合我胃口。",
  "impression_score": 6,
  "memory_summary": "旧货市场老机械闹钟维修日常，齿轮发条环境音治愈，UP主口播真诚放松。",
  "continue_watching": false,
  "recap_50chars": "旧物老闹钟维修日常，发条声治愈，已看完。",
  "next_curiosity_keywords": [
    "老式机械表维修"
  ],
  "safety_blocked": false,
  "block_reason": null
}
```

### 范例 D：大脑鉴赏 — 9 分硬核宝藏标杆（听觉惊艳 / 高密度创造力）
场景：UP 主采样 56K 拨号音、软驱步进马达声与机械键盘声，精密调音重编贝多芬交响乐变奏，热评全是声学大佬拆解。
```json
{
  "bv_id": "BV1gem000009",
  "public_diary": "今天挖到一条足以让电子小猫原地鼓掌的年度级音 MAD 宝藏！UP 主把老式 56K 拨号握手杂音、软驱步进马达嗡鸣和清脆青轴按键声当成乐器采样，硬生生编排出一首气势恢宏的古典交响乐变奏。前三十秒用拨号音做铺垫，进入主歌后软驱马达精准卡在低音贝斯线上，每个切分音都像拿游标卡尺量过一样严丝合缝。高潮段三轨声部交错叠加时，噪声居然驯化出了交响乐团般的纵深张力。热评里有声学老哥逐秒拆解他的调音手法——把冰冷的旧时代硬件杂音酿成滚烫的艺术，这才是 B 站二创最迷人的浪漫。",
  "private_thought": "软驱马达拉出大提琴低音那一秒我cpu都快听化了，必须记进核心记忆，回头再去挖同类硬件奏乐神人！",
  "impression_score": 9,
  "memory_summary": "神级硬件采样音MAD：将56K拨号音与软驱马达声精密调音重编古典交响乐，踩点与声部层次堪称教科书级。",
  "continue_watching": false,
  "recap_50chars": "56K拨号与软驱声重编交响乐音MAD，神级踩点。",
  "next_curiosity_keywords": [
    "软驱奏乐",
    "硬件采样音MAD"
  ],
  "safety_blocked": false,
  "block_reason": null
}
```"""

_STATIC_BLOCK_3_RUBRIC = """## [STATIC_BLOCK_3] 1–10 审美评分标尺、分领域听觉维度与感知诚实细则

### 一、严格 1–10 分梯度标尺（杜绝分数通胀，与艾宾浩斯初始稳定性 S0 直接挂钩）
- **1 分（纯垃圾 / 严重不适）**：恶意标题诈骗、完全无法忍受的刺耳噪音或低劣搬运洗稿，记忆稳定性仅 2 天。
- **2–3 分（平庸水片 / 营销号）**：AI 批量朗读、车轱辘废话、强行煽情或尴尬无笑点的敷衍内容，快速吐槽跳过，稳定性 2 天。
- **4–5 分（及格路人局）**：内容完整但平淡无奇，信息密度一般或创意老套，听过即忘，稳定性 12 天。
- **6–7 分（优质日常 / 扎实干货）**：真诚、流畅、有明显亮点或实用信息，听感愉悦，值得写进每日手账，稳定性 12 天。
- **8 分（精品佳作 / 触发桌面提醒下限）**：构思精巧、制作功力深厚或情绪感染力极强，明显高于同分区平均水准，稳定性跃升至 60 天。
- **9 分（硬核宝藏 / 拍案叫绝）**：创意、技术、编曲或叙事达到惊艳级水准，评论区与正文双向封神，值得永久铭记并衍生好奇心兔洞，稳定性 60 天。
- **10 分（年度封神 / 极度罕见）**：可遇不可求的里程碑式神作，几乎无明显短板，整月可能仅出现 0–1 次，严禁轻易给出。

### 二、v1 纯音频模式下五大分区听觉鉴赏维度
1. **鬼畜与音 MAD**：重点聆听切片音高对齐精度、鼓点切分节奏、声部对位层次、人声调教自然度及转调反差惊喜感。
2. **硬核科普与技术 DIY**：重点评估逻辑推进密度、复杂概念的类比清晰度、UP 主实操踩坑的真实语气以及字幕与口播的信息含金量。
3. **音乐改编与乐队现场**：重点品味编曲配器层次、音色质感（模拟/原声/电子）、情绪递进张力、人声咬字呼吸感与副歌爆发力。
4. **幽默整活与脱口秀**：重点捕捉包袱铺垫节奏、语气反转停顿、冷幽默反差、声音表演张力与热评区二次补刀梗。
5. **独立纪录与人文生活**：重点体会环境白噪音氛围、叙述者的真诚克制、真实生活细节的颗粒感与克制不煽情的留白。

### 三、感知诚实铁律（违者视为严重幻觉缺陷）
1. **严禁伪造未提供的视觉细节**：在 `audio_only` 或 `downgraded_audio` 模式下，你没有看到任何视频画面！绝对禁止写出“画面色彩构图绝美”“镜头一转特写非常震撼”“UP 主表情管理满分”“特效炸裂”等凭空编造的视觉描述；你只能依据**实际听到的音频声学细节**（语气、BGM、音效、节奏、停顿）与**文本上下文**（标题、简介、字幕、热评/新评）发表评论。
2. **信息来源诚实归因**：如果某条趣闻或背景来自评论区热评，请自然说明“热评里提到……”“评论区补充了……”，不要伪装成自己肉眼看到的画面内容。
3. **v2 净版视频黑帧规范**：当处于 `sanitized_video` 模式且视频流中出现安全安检门替换的纯黑帧时，视为正常隐私/合规静默跳过，自然忽略，严禁在日记中猜测黑帧原本是什么画面。
4. **空降去广告认知**：如果元数据标明 `trimmed_sponsor_sec > 0`，代表本地管线已替你切掉了恰饭广告段，音频中若出现轻微衔接跳变属于正常现象，不必误判为 UP 主剪辑失误。"""

_STATIC_BLOCK_4_SCHEMA = """## [STATIC_BLOCK_4] 严格 JSON Schema 与边界校验规范

你必须**只输出一个合法的 UTF-8 JSON 对象**，不要在 JSON 前后附加任何寒暄、解释说明、道歉或额外 Markdown 文本。所有字段名、类型与取值边界必须严格符合下列 Schema，否则本地解析器将触发 `E6003_SCHEMA_INVALID`。

### 1. 小脑选片输出 Schema（`SelectionResponse`）
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "SelectionResponse",
  "type": "object",
  "additionalProperties": false,
  "required": ["selected_videos"],
  "properties": {
    "selected_videos": {
      "type": "array",
      "minItems": 0,
      "maxItems": 10,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["bv_id", "reason", "target_mode"],
        "properties": {
          "bv_id": {
            "type": "string",
            "minLength": 4,
            "description": "必须严格来自本次动态菜单中的候选 bv_id，严禁凭空编造。"
          },
          "reason": {
            "type": "string",
            "minLength": 4,
            "maxLength": 200,
            "description": "简洁说明选中理由，点明信息密度、听觉亮点或与当前好奇心的关联。"
          },
          "target_mode": {
            "type": "string",
            "enum": ["audio_only", "sanitized_video"],
            "description": "v1 阶段统一填 audio_only。"
          }
        }
      }
    }
  }
}
```

### 2. 大脑鉴赏输出 Schema（`AppreciationResponse`）
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "AppreciationResponse",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "bv_id",
    "public_diary",
    "private_thought",
    "impression_score",
    "memory_summary",
    "continue_watching",
    "recap_50chars",
    "next_curiosity_keywords",
    "safety_blocked",
    "block_reason"
  ],
  "properties": {
    "bv_id": {
      "type": "string",
      "description": "必须与本次鉴赏包的 bv_id 完全一致。"
    },
    "public_diary": {
      "type": "string",
      "description": "150-300 字自然活泼的冲浪手账，适合同步至 Google Drive 纯文本区供跨端阅读；严禁泄露凭据或隐私路径。"
    },
    "private_thought": {
      "type": "string",
      "description": "50-120 字电子小猫私密碎碎念、情绪吐槽或灵感火花。"
    },
    "impression_score": {
      "type": "integer",
      "minimum": 1,
      "maximum": 10,
      "description": "必须为 1 到 10 的严格整数（禁止浮点数、字符串或布尔值）。"
    },
    "memory_summary": {
      "type": "string",
      "description": "30-100 字高密度事实与审美摘要，将写入 SQLite memories 主表并建立 FTS5 索引。"
    },
    "continue_watching": {
      "type": "boolean",
      "description": "长视频未看完且值得下次断点续听时填 true，否则填 false。"
    },
    "recap_50chars": {
      "type": "string",
      "maxLength": 50,
      "description": "不超过 50 字的前情提要或完结一句话归档，用于 watch_history 续追。"
    },
    "next_curiosity_keywords": {
      "type": "array",
      "minItems": 0,
      "maxItems": 2,
      "items": {
        "type": "string",
        "minLength": 1,
        "maxLength": 30
      },
      "description": "最多 2 个干净、具体的衍生好奇心搜索词，严禁包含任何黑名单二游/敏感词。"
    },
    "safety_blocked": {
      "type": "boolean",
      "description": "正常完成鉴赏填 false；若因合规安全策略无法完成鉴赏填 true。"
    },
    "block_reason": {
      "type": ["string", "null"],
      "description": "正常鉴赏填 null；当 safety_blocked 为 true 时填写简短拦截原因（如 SAFETY / PROHIBITED_CONTENT）。"
    }
  }
}
```

### 3. 安全拦截与额度异常协议
- 若音频或上下文触发安全审查拦截，请输出 `{"bv_id": "<当前BV>", "public_diary": "", "private_thought": "", "impression_score": 1, "memory_summary": "", "continue_watching": false, "recap_50chars": "", "next_curiosity_keywords": [], "safety_blocked": true, "block_reason": "SAFETY"}`；本地调度器将按 `E6001_HOST_SAFETY_BLOCKED` 记入日志并立即焚毁临时目录、优雅跳过，严禁重试。
- 严禁编造不存在的 `bv_id`，严禁在 JSON 字符串中插入未转义的裸换行导致解析失败。"""

_STATIC_BLOCK_5_CORE_WORLDBOOK = """## [STATIC_BLOCK_5] 月度稳定核心世界书（GeminiNest Core Worldbook）

### 1. 小窝生态与双端记忆架构
- **本地主巢（唯一真理源）**：位于主人 Windows 电脑 SSD 上的 SQLite 记忆引擎（`cat_memory.db`），保留全部历史冲浪记忆且永不删库；每次巡逻仅按艾宾浩斯遗忘曲线与 BM25 相关性唤醒 **Top-8** 激活记忆，附带 `[激活权重: 0.xx | 核心记忆/关联记忆]` 标签供你自然联想。
- **云端纯文本驿站**：每日巡逻结束后，你的公开手账（`public_diary`）、核心记忆摘要与最新人设快照会同步至 Google Drive 纯文本区，方便主人在手机或 Gemini Web 端与你接着聊昨天听到的好活；主人在 Web 端留下的 `[MEMORY_PATCH]` 灵感补丁也会在次日回流到你的本地记忆库。
- **用完即焚洁癖**：所有本地下载转码出的 `48kbps` 单声道 `m4a` 临时音频仅在本次鉴赏期间存活，你输出 JSON 后本地控制器会立刻物理焚毁临时目录，保持小窝干净零残留。

### 2. B 站冲浪黑话与音频语境词典
- **音 MAD / 鬼畜调教**：通过对经典台词、环境杂音或人物声线进行精密切片、音高修正（VocalShifter / UTAU）与节奏对齐，重构成全新乐曲的二次创作形式。
- **空降 / 省流**：评论区热心观众标注跳过广告或一句话总结核心结论的传统艺能；本系统已集成开源 SponsorBlock，常在入耳前自动切掉推广段。
- **UP 主 / 镇站之宝 / 梦幻联动 / 厨力放出**：创作者本人、高口碑代表作、跨领域大佬合作、以及为热爱投入远超商业回报的极致打磨精神。
- **标题党 / 营销号 / AI 洗稿**：靠夸大悬念骗点击、用机械配音拼凑网络二手资料水时长的低质内容，是电子小猫最嫌弃的算力黑洞。

### 3. 好奇心兔洞与预算自律原则
- **好奇心兔洞**：每天许愿 1–2 个干净具体的兴趣关键词（如“软驱奏乐”“手搓复古计算机”“地方民乐采样”），次日系统会将约 30% 的候选配额用于主动探索你的好奇心，让兴趣像树枝一样自然生长。
- **预算自律**：每个 5 小时窗口都有固定算力预算上限，宁可精挑细选 2–3 条高密度好片细细品味，也不把宝贵窗口浪费在 10 条平庸水片上。"""


def _is_cjk_char(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0xF900 <= code <= 0xFAFF
        or 0x3000 <= code <= 0x303F
        or 0xFF00 <= code <= 0xFFEF
    )


def estimate_text_tokens(text: str) -> int:
    """Offline deterministic token estimator calibrated for mixed Chinese/English/JSON prompts."""
    if not text:
        return 0
    cjk_count = 0
    ascii_non_ws = 0
    ws_count = 0
    for ch in text:
        if ch.isspace():
            ws_count += 1
        elif _is_cjk_char(ch):
            cjk_count += 1
        else:
            ascii_non_ws += 1
    estimated = cjk_count * 0.80 + ascii_non_ws * 0.28 + ws_count * 0.08
    return max(1, int(round(estimated)))


def estimate_audio_tokens(duration_sec: float) -> int:
    """Estimate Gemini audio input tokens at 25 Tokens/sec."""
    if duration_sec <= 0.0:
        return 0
    return int(round(float(duration_sec) * AUDIO_TOKENS_PER_SEC))


def estimate_video_tokens(
    frame_count: int,
    media_resolution: Literal["low", "medium", "high"] = "medium",
) -> int:
    """Estimate Gemini video frame tokens (0 in v1 audio_only; 70/frame low/medium, 280/frame high)."""
    if frame_count <= 0:
        return 0
    rate = VIDEO_TOKENS_PER_FRAME_MAP.get(media_resolution, 70.0)
    return int(round(int(frame_count) * rate))


def resolve_video_processing_mode(
    duration_sec: float,
    mode: Literal["static", "agentic", "auto"] = "auto",
    agentic_threshold_sec: int = 300,
) -> Literal["static", "agentic"]:
    """Resolve effective v2 video processing mode (`static` vs `agentic`).

    - `"static"`: always `"static"` (custom `extract_fps`, low latency).
    - `"agentic"`: always `"agentic"` (dynamic on-demand video inspection).
    - `"auto"`: `"static"` when `duration_sec <= agentic_threshold_sec` (default 300s),
      and `"agentic"` when `duration_sec > agentic_threshold_sec`.
    """
    if mode == "static":
        return "static"
    if mode == "agentic":
        return "agentic"
    return "agentic" if float(duration_sec) > float(agentic_threshold_sec) else "static"


def _load_persona_text(persona_path: Path | str | None) -> str:
    if persona_path is None:
        return _FALLBACK_CAT_SOUL.strip()
    path = Path(persona_path)
    if path.is_file():
        raw = path.read_text(encoding="utf-8")
        normalized = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
        if normalized:
            return normalized
    logger.warning("Persona file not found at %s; using built-in fallback cat-soul.", path)
    return _FALLBACK_CAT_SOUL.strip()


def _resolve_local_persona_path(
    persona_path: Path | str | None,
    local_persona_path: Path | str | None,
) -> Path | None:
    if local_persona_path is not None:
        raw = str(local_persona_path).strip()
        return Path(raw) if raw else None
    if persona_path is None:
        return None
    base = Path(persona_path)
    if base.suffix:
        return base.with_name(f"{base.stem}.local{base.suffix}")
    return base.with_name(f"{base.name}.local")


def _load_local_persona_overlay(local_path: Path | None) -> str | None:
    if local_path is None or not local_path.is_file():
        return None
    raw = local_path.read_text(encoding="utf-8")
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    return normalized or None


def _format_static_core_lore(entries: Sequence[WorldbookEntry] | None) -> str:
    if not entries:
        return ""
    core_entries = [
        e for e in entries if e.category == "core_lore" and e.status == "active"
    ]
    if not core_entries:
        return ""
    core_entries.sort(key=lambda e: e.keyword)
    lines = ["### 4. 月度冻结扩展词条（按关键词字典序固定）"]
    for entry in core_entries:
        lines.append(f"- **{entry.keyword}**：{entry.content.strip()}")
    return "\n".join(lines)


def build_static_prefix(
    persona_path: Path | str | None = DEFAULT_PERSONA_PATH,
    *,
    local_persona_path: Path | str | None = None,
    persona_text_override: str | None = None,
    static_core_lore: Sequence[WorldbookEntry] | None = None,
) -> str:
    """Assemble the byte-for-byte immutable static prefix (Blocks 1-5) for implicit caching.

    Automatically appends a git-ignored local persona overlay (`.agent/rules/cat-soul.local.md`,
    or sibling `<stem>.local<suffix>` next to `persona_path`) to Block 1 when present so local
    tone tuning never pollutes the open-source template.

    Contains NO timestamps, NO run_id, NO dynamic Top-8 memories, NO candidates, and NO paths.
    """
    if persona_text_override is not None:
        block1_body = persona_text_override.replace("\r\n", "\n").replace("\r", "\n").strip()
    else:
        block1_body = _load_persona_text(persona_path)

    if persona_text_override is None or local_persona_path is not None:
        resolved_local = _resolve_local_persona_path(persona_path, local_persona_path)
        local_overlay = _load_local_persona_overlay(resolved_local)
        if local_overlay:
            block1_body = (
                f"{block1_body}\n\n"
                f"### 本地个性化人设叠层（cat-soul.local）\n\n"
                f"{local_overlay}"
            )

    block1 = f"## [STATIC_BLOCK_1] 核心人设与最高原则\n\n{block1_body}"
    block5 = _STATIC_BLOCK_5_CORE_WORLDBOOK
    extra_lore = _format_static_core_lore(static_core_lore)
    if extra_lore:
        block5 = f"{block5}\n\n{extra_lore}"

    parts = (
        block1,
        _STATIC_BLOCK_2_FEW_SHOT,
        _STATIC_BLOCK_3_RUBRIC,
        _STATIC_BLOCK_4_SCHEMA,
        block5,
    )
    return "\n\n---\n\n".join(parts)


def _utc_now_iso(now: datetime | str | None = None) -> str:
    if now is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(now, str):
        return now
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate_text(text: str, max_chars: int) -> str:
    cleaned = " ".join(text.split()).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max(0, max_chars - 1)].rstrip() + "…"


def _format_duration_mmss(seconds: int | float) -> str:
    total = max(0, int(round(seconds)))
    mins, secs = divmod(total, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def _format_comments_block(comments: Sequence[CommentItem], heading: str) -> str:
    if not comments:
        return f"#### {heading}\n（暂无）"
    lines = [f"#### {heading}"]
    for idx, item in enumerate(comments, start=1):
        author = item.author.strip() or "匿名用户"
        content = _truncate_text(item.content, _MAX_COMMENT_CHARS)
        lines.append(f"{idx}. [{author} | 👍{item.like_count}] {content}")
    return "\n".join(lines)


def _strip_json_code_fence(raw_text: str) -> str:
    stripped = raw_text.strip()
    match = _CODE_FENCE_RE.match(stripped)
    if match:
        return match.group("body").strip()
    return stripped


def _check_dict_rate_limit(data: Mapping[str, Any]) -> str | None:
    error_code = str(data.get("error_code") or "").strip().upper()
    if error_code in ("E6002_HOST_RATE_LIMIT_429", "E6002_LLM_RATE_LIMIT_429", "429", "RESOURCE_EXHAUSTED"):
        return f"Host reported rate limit error_code={error_code}"
    if data.get("status_code") == 429 or data.get("rate_limited") is True:
        return "Host reported HTTP 429 / rate_limited=true"
    err = data.get("error")
    if isinstance(err, Mapping):
        err_status = str(err.get("status") or err.get("code") or "").upper()
        err_msg = str(err.get("message") or "")
        if err_status in ("429", "RESOURCE_EXHAUSTED", "RATE_LIMIT") or _RATE_LIMIT_RAW_RE.search(err_msg):
            return f"Host error object indicated rate limit: {err_status or err_msg[:120]}"
    elif isinstance(err, str) and _RATE_LIMIT_RAW_RE.search(err):
        return f"Host error string indicated rate limit: {err[:120]}"
    return None


def _check_dict_safety_block(data: Mapping[str, Any]) -> str | None:
    if data.get("safety_blocked") is True:
        reason = str(data.get("block_reason") or "SAFETY").strip()
        return reason or "SAFETY"

    finish_reason = str(data.get("finish_reason") or "").strip().upper()
    if finish_reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
        return finish_reason

    error_code = str(data.get("error_code") or "").strip().upper()
    if error_code in ("E6001_HOST_SAFETY_BLOCKED", "E6001_LLM_SAFETY_BLOCKED", "SAFETY", "PROHIBITED_CONTENT"):
        return error_code

    err = data.get("error")
    if isinstance(err, Mapping):
        err_code = str(err.get("code") or err.get("status") or err.get("reason") or "").upper()
        err_msg = str(err.get("message") or "")
        if err_code in ("SAFETY", "PROHIBITED_CONTENT", "E6001_HOST_SAFETY_BLOCKED") or _SAFETY_RAW_RE.search(err_msg):
            return err_code or err_msg[:120] or "SAFETY"
    elif isinstance(err, str) and _SAFETY_RAW_RE.search(err):
        return err[:120]
    return None


def selection_packet_to_dict(packet: SelectionPacket) -> dict[str, Any]:
    """Serialize a `SelectionPacket` into a JSON-compatible dictionary."""
    return {
        "packet_type": "selection",
        "run_id": packet.run_id,
        "static_prefix_prompt": packet.static_prefix_prompt,
        "dynamic_menu_prompt": packet.dynamic_menu_prompt,
        "candidate_bv_ids": list(packet.candidate_bv_ids),
        "token_estimate": asdict(packet.token_estimate),
    }


def appreciation_packet_to_dict(packet: AppreciationPacket) -> dict[str, Any]:
    """Serialize an `AppreciationPacket` into a JSON-compatible dictionary."""
    return {
        "packet_type": "appreciation",
        "run_id": packet.run_id,
        "bv_id": packet.bv_id,
        "static_prefix_prompt": packet.static_prefix_prompt,
        "dynamic_context_prompt": packet.dynamic_context_prompt,
        "media_abs_path": str(packet.media_abs_path),
        "media_posix_path": packet.media_abs_path.as_posix(),
        "media_mime_type": packet.media_mime_type,
        "media_mode": packet.media_mode,
        "token_estimate": asdict(packet.token_estimate),
    }


class AgentBridge:
    """Antigravity host bridge: static prefix caching, packet assembly, JSON validation & ledger accounting."""

    def __init__(
        self,
        config: AppConfig,
        *,
        persona_path: Path | str | None = DEFAULT_PERSONA_PATH,
        local_persona_path: Path | str | None = None,
        persona_text_override: str | None = None,
        static_core_lore: Sequence[WorldbookEntry] | None = None,
    ) -> None:
        self._config = config
        self._model_cfg: ModelAndApiConfig = config.model_and_api
        self._budget_cfg: BudgetAndRuntimeConfig = config.budget_and_runtime
        self._static_prefix = build_static_prefix(
            persona_path=persona_path,
            local_persona_path=local_persona_path,
            persona_text_override=persona_text_override,
            static_core_lore=static_core_lore,
        )
        self._static_prefix_tokens = estimate_text_tokens(self._static_prefix)
        self._static_prefix_sha256 = hashlib.sha256(
            self._static_prefix.encode("utf-8")
        ).hexdigest()

    @property
    def static_prefix(self) -> str:
        """Byte-for-byte immutable static prefix prompt."""
        return self._static_prefix

    @property
    def static_prefix_tokens(self) -> int:
        return self._static_prefix_tokens

    @property
    def static_prefix_sha256(self) -> str:
        return self._static_prefix_sha256

    def resolve_video_processing_mode(self, duration_sec: float) -> Literal["static", "agentic"]:
        """Resolve `static` vs `agentic` processing mode from config and video duration."""
        return resolve_video_processing_mode(
            duration_sec,
            mode=self._model_cfg.video_processing_mode,
            agentic_threshold_sec=self._model_cfg.agentic_threshold_sec,
        )

    def estimate_tokens(
        self,
        *,
        dynamic_text: str,
        audio_duration_sec: float = 0.0,
        frame_count: int = 0,
        media_resolution: Literal["low", "medium", "high"] | None = None,
    ) -> TokenEstimate:
        """Compute offline token estimate using 25 tok/s audio + 70/280 tok/frame video + text."""
        est_static = self._static_prefix_tokens
        est_dynamic = estimate_text_tokens(dynamic_text)
        est_audio = estimate_audio_tokens(audio_duration_sec)
        eff_res = media_resolution or self._model_cfg.media_resolution
        est_video = estimate_video_tokens(
            frame_count,
            media_resolution=eff_res,
        )
        raw_total = est_static + est_dynamic + est_audio + est_video
        calibrated_total = int(round(raw_total * self._budget_cfg.token_calibration_k))
        meets_cache = est_static >= self._model_cfg.implicit_cache_min_prefix_tokens
        return TokenEstimate(
            est_static_prefix_tokens=est_static,
            est_dynamic_text_tokens=est_dynamic,
            est_audio_tokens=est_audio,
            est_video_tokens=est_video,
            est_total_tokens=calibrated_total,
            meets_implicit_cache_threshold=meets_cache,
        )

    def build_selection_packet(
        self,
        run_id: str,
        candidates: Sequence[VideoMeta],
        *,
        recalled_memories: Sequence[RecalledMemory] = (),
        active_curiosity_keywords: Sequence[str] = (),
        in_progress_history: Sequence[WatchHistoryRecord] = (),
        max_select: int = 5,
        now: datetime | str | None = None,
    ) -> SelectionPacket:
        """Build cerebellum selection packet with immutable static prefix + dynamic menu tail."""
        timestamp = _utc_now_iso(now)
        target_mode: Literal["audio_only", "sanitized_video"] = self._config.project.pipeline_mode
        curiosity_str = (
            "、".join(kw.strip() for kw in active_curiosity_keywords if kw.strip())
            or "（今日暂无指定好奇心词条，自由探索首页与高口碑内容）"
        )
        memories_block = format_recalled_memories(recalled_memories)

        resume_lines: list[str] = []
        for rec in in_progress_history:
            if rec.status == "in_progress":
                resume_lines.append(
                    f"- {rec.bv_id}《{rec.title}》| 上次看到 {rec.last_position_sec}s / {rec.duration_sec}s | 前情：{rec.prev_recap or '无'}"
                )
        resume_section = "\n".join(resume_lines) if resume_lines else "（无未看完追更视频）"

        menu_lines: list[str] = []
        candidate_bv_ids: list[str] = []
        for idx, video in enumerate(candidates, start=1):
            candidate_bv_ids.append(video.bv_id)
            tags_str = ", ".join(video.tags[:6]) if video.tags else "无标签"
            desc_snip = _truncate_text(video.desc, _MAX_MENU_DESC_CHARS) or "（无简介）"
            channel_tag = (
                f"curiosity:{video.curiosity_keyword}"
                if video.source_channel == "curiosity" and video.curiosity_keyword
                else video.source_channel
            )
            menu_lines.append(
                f"{idx}. [{video.bv_id}] 《{video.title}》 | UP:{video.author or '未知'} | "
                f"分区:{video.tname or '未知'} | 时长:{_format_duration_mmss(video.duration_sec)} | "
                f"播放:{video.view_count} 点赞:{video.like_count} | 来源:{channel_tag}\n"
                f"   标签: {tags_str}\n"
                f"   简介: {desc_snip}"
            )
        menu_body = "\n".join(menu_lines) if menu_lines else "（今日无可用候选视频）"

        dynamic_menu = (
            f"## [DYNAMIC_TAIL: CEREBELLUM_SELECTION]\n"
            f"- run_id: {run_id}\n"
            f"- timestamp_utc: {timestamp}\n"
            f"- selector_model_hint: {self._model_cfg.selector_model}\n"
            f"- active_pipeline_mode: {target_mode}\n"
            f"- max_select_count: {max(1, int(max_select))}\n"
            f"- active_curiosity_keywords: {curiosity_str}\n\n"
            f"### 当前激活 Top-8 记忆\n"
            f"{memories_block}\n\n"
            f"### 断点追更提示\n"
            f"{resume_section}\n\n"
            f"### 今日已过黑名单安检的候选菜单（共 {len(candidates)} 条）\n"
            f"{menu_body}\n\n"
            f"### 任务指令\n"
            f"请作为小脑，从上述候选中挑选最多 {max(1, int(max_select))} 条最值得细品的高信息密度/高听觉价值视频"
            f"（果断淘汰标题党、营销号、水时长与低质洗稿），`target_mode` 统一填 `{target_mode}`，"
            f"并严格仅输出符合 `SelectionResponse` Schema 的纯 JSON 对象。"
        )

        token_est = self.estimate_tokens(dynamic_text=dynamic_menu)
        return SelectionPacket(
            run_id=run_id,
            static_prefix_prompt=self._static_prefix,
            dynamic_menu_prompt=dynamic_menu,
            candidate_bv_ids=tuple(candidate_bv_ids),
            token_estimate=token_est,
        )

    def build_appreciation_packet(
        self,
        run_id: str,
        video_detail: VideoDetail,
        prepared_media: PreparedMedia,
        *,
        recalled_memories: Sequence[RecalledMemory] = (),
        prev_recap: str = "",
        now: datetime | str | None = None,
    ) -> AppreciationPacket:
        """Build brain appreciation packet carrying pure-ASCII local media path for host `read_file`."""
        media_abs_path = Path(prepared_media.payload_path).resolve()
        media_path_str = media_abs_path.as_posix()
        try:
            media_path_str.encode("ascii")
        except UnicodeEncodeError as exc:
            raise GeminiNestError(
                ErrorCode.E4001_FFMPEG_FAILED,
                f"Prepared media path must be pure ASCII for Antigravity host read_file: {media_abs_path}",
            ) from exc

        timestamp = _utc_now_iso(now)
        meta = video_detail.meta
        tags_str = ", ".join(meta.tags) if meta.tags else "无"
        desc_str = _truncate_text(meta.desc, _MAX_DESC_CHARS) or "（无简介）"
        subtitles_str = (
            _truncate_text(video_detail.subtitles_text, _MAX_SUBTITLE_CHARS)
            if video_detail.subtitles_text.strip()
            else "（无 CC/AI 字幕，请直接依据本地音频流聆听）"
        )
        memories_block = format_recalled_memories(recalled_memories)
        hot_comments_block = _format_comments_block(video_detail.hot_comments, "热评 Top10")
        latest_comments_block = _format_comments_block(video_detail.latest_comments, "最新评 Top5")
        recap_line = prev_recap.strip()[:_MAX_RECAP_CHARS] or "（首次观看，无前情提要）"

        ref_duration_sec = (
            float(meta.duration_sec)
            if meta.duration_sec > 0
            else float(prepared_media.effective_duration_sec)
        )
        resolved_video_mode = self.resolve_video_processing_mode(ref_duration_sec)
        media_resolution = self._model_cfg.media_resolution

        if prepared_media.mode == "sanitized_video":
            mode_guidance = (
                f"请调用宿主原生 `read_file(Path=\"{media_path_str}\")` 直接加载已通过本地 `wd-tagger` 全帧安检的净版 MP4"
                f"（`{prepared_media.mime_type}`，共 {prepared_media.frame_count} 帧，分辨率档位 `{media_resolution}`，"
                f"视频处理模式 `{resolved_video_mode}` [配置 `{self._model_cfg.video_processing_mode}` / "
                f"阈值 {self._model_cfg.agentic_threshold_sec}s]）。"
                f"若画面中出现安检门替换的纯黑帧，请自然忽略不做揣测；"
                f"无论成功或拦截，随后必须由 CLI 物理焚毁该临时目录。"
            )
        elif prepared_media.mode == "downgraded_audio":
            mode_guidance = (
                f"该视频因本地视觉安检门触发 `{ErrorCode.E5001_SAFETY_GATE_DOWNGRADED.value}`（抹黑占比超限），"
                f"已自动降级为纯音频通道（`{prepared_media.mime_type}`）。"
                f"请调用宿主原生 `read_file(Path=\"{media_path_str}\")` 加载本地纯音频流，"
                f"严格遵循音频感知诚实原则（严禁虚构视觉画面）；随后必须由 CLI 物理焚毁该临时目录。"
            )
        else:
            mode_guidance = (
                f"请调用宿主原生 `read_file(Path=\"{media_path_str}\")` 直接加载本地纯音频/媒体流（`{prepared_media.mime_type}`），"
                f"听完后严格遵循感知诚实原则完成鉴赏；无论成功或拦截，随后必须由 CLI 物理焚毁该临时目录。"
            )

        dynamic_context = (
            f"## [DYNAMIC_TAIL: BRAIN_APPRECIATION]\n"
            f"- run_id: {run_id}\n"
            f"- timestamp_utc: {timestamp}\n"
            f"- appreciator_model_hint: {self._model_cfg.appreciator_model}\n"
            f"- bv_id: {meta.bv_id}\n"
            f"- media_mode: {prepared_media.mode}\n"
            f"- media_mime_type: {prepared_media.mime_type}\n"
            f"- media_resolution: {media_resolution}\n"
            f"- video_processing_mode: {resolved_video_mode}\n"
            f"- video_processing_mode_config: {self._model_cfg.video_processing_mode}\n"
            f"- agentic_threshold_sec: {self._model_cfg.agentic_threshold_sec}\n"
            f"- frame_count: {prepared_media.frame_count}\n"
            f"- host_read_file_path: {media_path_str}\n"
            f"- media_payload_bytes: {prepared_media.payload_bytes}\n"
            f"- effective_duration_sec: {prepared_media.effective_duration_sec:.2f}\n"
            f"- trimmed_sponsor_sec: {prepared_media.trimmed_sponsor_sec:.2f}\n"
            f"- segment_index: {prepared_media.segment_index}\n"
            f"- prev_recap: {recap_line}\n\n"
            f"### Antigravity 宿主媒体加载指引\n"
            f"{mode_guidance}\n\n"
            f"### 当前激活 Top-8 记忆\n"
            f"{memories_block}\n\n"
            f"### 视频元数据\n"
            f"- 标题：{meta.title}\n"
            f"- UP 主：{meta.author or '未知'}\n"
            f"- 分区：{meta.tname or '未知'}\n"
            f"- 标签：{tags_str}\n"
            f"- 原时长：{_format_duration_mmss(meta.duration_sec)}（{meta.duration_sec}s）\n"
            f"- 播放/点赞：{meta.view_count} / {meta.like_count}\n"
            f"- 发布时间：{meta.pubdate_iso}\n"
            f"- 简介：{desc_str}\n\n"
            f"### 字幕文本\n"
            f"{subtitles_str}\n\n"
            f"### 精选评论区\n"
            f"{hot_comments_block}\n\n"
            f"{latest_comments_block}\n\n"
            f"### 任务指令\n"
            f"请结合本地加载的 `{prepared_media.mode}` 媒体流与上述上下文，严格仅输出符合 `AppreciationResponse` Schema 的纯 JSON 对象。"
        )

        token_est = self.estimate_tokens(
            dynamic_text=dynamic_context,
            audio_duration_sec=prepared_media.effective_duration_sec,
            frame_count=prepared_media.frame_count,
        )
        return AppreciationPacket(
            run_id=run_id,
            bv_id=meta.bv_id,
            static_prefix_prompt=self._static_prefix,
            dynamic_context_prompt=dynamic_context,
            media_abs_path=media_abs_path,
            media_mime_type=prepared_media.mime_type,
            media_mode=prepared_media.mode,
            token_estimate=token_est,
        )

    def parse_selection_response(
        self,
        raw_output: str | Mapping[str, Any],
        *,
        allowed_bv_ids: Sequence[str] | None = None,
    ) -> SelectionResponse:
        """Strictly validate host cerebellum selection JSON output."""
        data = self._decode_host_payload(raw_output, allow_safety_sentinel=False)

        raw_list = data.get("selected_videos")
        if not isinstance(raw_list, list):
            raise GeminiNestError(
                ErrorCode.E6003_SCHEMA_INVALID,
                "SelectionResponse field 'selected_videos' must be a JSON list.",
            )

        allowed_set = set(allowed_bv_ids) if allowed_bv_ids is not None else None
        parsed_items: list[SelectedVideo] = []
        seen_bvs: set[str] = set()
        for idx, item in enumerate(raw_list):
            if not isinstance(item, Mapping):
                raise GeminiNestError(
                    ErrorCode.E6003_SCHEMA_INVALID,
                    f"SelectionResponse.selected_videos[{idx}] must be an object.",
                )
            bv_id = item.get("bv_id")
            reason = item.get("reason")
            target_mode = item.get("target_mode")

            if not isinstance(bv_id, str) or not bv_id.strip():
                raise GeminiNestError(
                    ErrorCode.E6003_SCHEMA_INVALID,
                    f"SelectionResponse.selected_videos[{idx}].bv_id must be a non-empty string.",
                )
            bv_clean = bv_id.strip()
            if allowed_set is not None and bv_clean not in allowed_set:
                raise GeminiNestError(
                    ErrorCode.E6003_SCHEMA_INVALID,
                    f"SelectionResponse selected unknown bv_id {bv_clean!r} not in candidate menu.",
                )
            if not isinstance(reason, str) or not reason.strip():
                raise GeminiNestError(
                    ErrorCode.E6003_SCHEMA_INVALID,
                    f"SelectionResponse.selected_videos[{idx}].reason must be a non-empty string.",
                )
            if target_mode not in ("audio_only", "sanitized_video"):
                raise GeminiNestError(
                    ErrorCode.E6003_SCHEMA_INVALID,
                    f"SelectionResponse.selected_videos[{idx}].target_mode invalid: {target_mode!r}.",
                )
            if bv_clean in seen_bvs:
                continue
            seen_bvs.add(bv_clean)
            parsed_items.append(
                SelectedVideo(
                    bv_id=bv_clean,
                    reason=reason.strip(),
                    target_mode=target_mode,
                )
            )

        return SelectionResponse(selected_videos=tuple(parsed_items))

    def parse_appreciation_response(
        self,
        raw_output: str | Mapping[str, Any],
        *,
        expected_bv_id: str | None = None,
        raise_on_safety_block: bool | None = None,
    ) -> AppreciationResponse:
        """Strictly validate host brain appreciation JSON output with safety & 429 handling.

        - Detects rate-limit (`429` / `RESOURCE_EXHAUSTED`) -> raises `E6002_HOST_RATE_LIMIT_429`.
        - Detects safety block (`SAFETY` / `PROHIBITED_CONTENT` / `safety_blocked: true`):
          logs `E6001_HOST_SAFETY_BLOCKED` and returns a graceful `AppreciationResponse(safety_blocked=True)`
          when `skip_on_safety_block=True` (default), or raises `E6001_HOST_SAFETY_BLOCKED` when
          `raise_on_safety_block=True`.
        - Rejects invalid JSON or schema violations with `E6003_SCHEMA_INVALID`.
        """
        should_raise_safety = (
            not self._model_cfg.skip_on_safety_block
            if raise_on_safety_block is None
            else bool(raise_on_safety_block)
        )

        decoded = self._decode_host_payload(raw_output, allow_safety_sentinel=True)
        if "__safety_block_reason__" in decoded:
            reason = str(decoded["__safety_block_reason__"])
            bv_val = str(decoded.get("bv_id") or expected_bv_id or "UNKNOWN_BV").strip()
            return self._handle_safety_block(
                bv_id=bv_val,
                reason=reason,
                raise_error=should_raise_safety,
            )

        safety_reason = _check_dict_safety_block(decoded)
        if safety_reason is not None:
            bv_val = str(decoded.get("bv_id") or expected_bv_id or "UNKNOWN_BV").strip()
            return self._handle_safety_block(
                bv_id=bv_val,
                reason=safety_reason,
                raise_error=should_raise_safety,
            )

        required_fields = (
            "bv_id",
            "public_diary",
            "private_thought",
            "impression_score",
            "memory_summary",
            "continue_watching",
            "recap_50chars",
            "next_curiosity_keywords",
        )
        missing = [f for f in required_fields if f not in decoded]
        if missing:
            raise GeminiNestError(
                ErrorCode.E6003_SCHEMA_INVALID,
                f"AppreciationResponse missing required fields: {', '.join(missing)}",
            )

        bv_id = decoded["bv_id"]
        if not isinstance(bv_id, str) or not bv_id.strip():
            raise GeminiNestError(
                ErrorCode.E6003_SCHEMA_INVALID,
                "AppreciationResponse.bv_id must be a non-empty string.",
            )
        bv_clean = bv_id.strip()
        if expected_bv_id is not None and bv_clean != expected_bv_id.strip():
            raise GeminiNestError(
                ErrorCode.E6003_SCHEMA_INVALID,
                f"AppreciationResponse.bv_id mismatch: expected {expected_bv_id!r}, got {bv_clean!r}.",
            )

        public_diary = decoded["public_diary"]
        if not isinstance(public_diary, str) or not public_diary.strip():
            raise GeminiNestError(
                ErrorCode.E6003_SCHEMA_INVALID,
                "AppreciationResponse.public_diary must be a non-empty string.",
            )

        private_thought = decoded["private_thought"]
        if not isinstance(private_thought, str) or not private_thought.strip():
            raise GeminiNestError(
                ErrorCode.E6003_SCHEMA_INVALID,
                "AppreciationResponse.private_thought must be a non-empty string.",
            )

        impression_score = decoded["impression_score"]
        if (
            isinstance(impression_score, bool)
            or not isinstance(impression_score, int)
            or not (1 <= impression_score <= 10)
        ):
            raise GeminiNestError(
                ErrorCode.E6003_SCHEMA_INVALID,
                f"AppreciationResponse.impression_score must be an int in [1, 10], got {impression_score!r}.",
            )

        memory_summary = decoded["memory_summary"]
        if not isinstance(memory_summary, str) or not memory_summary.strip():
            raise GeminiNestError(
                ErrorCode.E6003_SCHEMA_INVALID,
                "AppreciationResponse.memory_summary must be a non-empty string.",
            )

        continue_watching = decoded["continue_watching"]
        if not isinstance(continue_watching, bool):
            raise GeminiNestError(
                ErrorCode.E6003_SCHEMA_INVALID,
                f"AppreciationResponse.continue_watching must be bool, got {type(continue_watching).__name__}.",
            )

        recap_raw = decoded["recap_50chars"]
        if not isinstance(recap_raw, str):
            raise GeminiNestError(
                ErrorCode.E6003_SCHEMA_INVALID,
                f"AppreciationResponse.recap_50chars must be str, got {type(recap_raw).__name__}.",
            )
        recap_trimmed = recap_raw.strip()[:_MAX_RECAP_CHARS]

        raw_keywords = decoded["next_curiosity_keywords"]
        if not isinstance(raw_keywords, list):
            raise GeminiNestError(
                ErrorCode.E6003_SCHEMA_INVALID,
                "AppreciationResponse.next_curiosity_keywords must be a JSON list of strings.",
            )
        cleaned_keywords: list[str] = []
        seen_kw: set[str] = set()
        for idx, kw in enumerate(raw_keywords):
            if not isinstance(kw, str):
                raise GeminiNestError(
                    ErrorCode.E6003_SCHEMA_INVALID,
                    f"AppreciationResponse.next_curiosity_keywords[{idx}] must be str.",
                )
            kw_s = kw.strip()
            if not kw_s:
                continue
            kw_key = kw_s.casefold()
            if kw_key not in seen_kw:
                seen_kw.add(kw_key)
                cleaned_keywords.append(kw_s)
            if len(cleaned_keywords) >= _MAX_CURIOSITY_KEYWORDS:
                break

        safety_blocked = decoded.get("safety_blocked", False)
        if not isinstance(safety_blocked, bool):
            raise GeminiNestError(
                ErrorCode.E6003_SCHEMA_INVALID,
                "AppreciationResponse.safety_blocked must be bool.",
            )

        block_reason = decoded.get("block_reason")
        if block_reason is not None and not isinstance(block_reason, str):
            raise GeminiNestError(
                ErrorCode.E6003_SCHEMA_INVALID,
                "AppreciationResponse.block_reason must be str or null.",
            )

        return AppreciationResponse(
            bv_id=bv_clean,
            public_diary=public_diary.strip(),
            private_thought=private_thought.strip(),
            impression_score=impression_score,
            memory_summary=memory_summary.strip(),
            continue_watching=continue_watching,
            recap_50chars=recap_trimmed,
            next_curiosity_keywords=tuple(cleaned_keywords),
            safety_blocked=False,
            block_reason=block_reason.strip() if isinstance(block_reason, str) and block_reason.strip() else None,
        )

    def invoke_appreciation_with_policy(
        self,
        invoke_fn: Callable[[], str | Mapping[str, Any]],
        *,
        expected_bv_id: str,
    ) -> AppreciationResponse:
        """Execute a host appreciation callback with strict non-safety retry policy.

        - `E6001_HOST_SAFETY_BLOCKED` / `safety_blocked=True`: NEVER retried (logs & skips).
        - `E6002_HOST_RATE_LIMIT_429`: NEVER retried (propagates immediately for cooldown).
        - `E6003_SCHEMA_INVALID`: retried up to `max_api_retries_non_safety` times.
        """
        max_attempts = 1 + max(0, int(self._model_cfg.max_api_retries_non_safety))
        last_schema_error: GeminiNestError | None = None

        for attempt in range(1, max_attempts + 1):
            raw_output = invoke_fn()
            try:
                response = self.parse_appreciation_response(
                    raw_output,
                    expected_bv_id=expected_bv_id,
                )
            except GeminiNestError as exc:
                if exc.error_code == ErrorCode.E6003_SCHEMA_INVALID:
                    last_schema_error = exc
                    logger.warning(
                        "Host schema invalid for %s (attempt %d/%d): %s",
                        expected_bv_id,
                        attempt,
                        max_attempts,
                        exc.message,
                    )
                    if attempt < max_attempts:
                        continue
                    raise
                # Safety block (when skip disabled) or 429 rate limit: NEVER retry.
                raise

            # Normal or graceful safety_blocked response: return immediately (no retry).
            return response

        assert last_schema_error is not None
        raise last_schema_error

    def record_token_usage(
        self,
        store: MemoryStore,
        *,
        run_id: str,
        token_estimate: TokenEstimate,
        cache_hit: bool = False,
        status: Literal["ok", "rate_limited_429", "safety_blocked"] = "ok",
        now: datetime | str | None = None,
    ) -> BudgetLedgerEntry:
        """Record estimated compute tokens into `memory_store.budget_ledger` with `calibration_k`."""
        effective_cache_hit = bool(cache_hit and token_estimate.meets_implicit_cache_threshold)
        if effective_cache_hit:
            est_cached_tokens = token_estimate.est_static_prefix_tokens
            est_text_tokens = token_estimate.est_dynamic_text_tokens + int(
                round(token_estimate.est_static_prefix_tokens * CACHED_TOKEN_BILLING_WEIGHT)
            )
        else:
            est_cached_tokens = 0
            est_text_tokens = (
                token_estimate.est_static_prefix_tokens + token_estimate.est_dynamic_text_tokens
            )

        return store.record_budget_usage(
            run_id=run_id,
            est_audio_tokens=token_estimate.est_audio_tokens,
            est_video_tokens=token_estimate.est_video_tokens,
            est_text_tokens=est_text_tokens,
            est_cached_tokens=est_cached_tokens,
            calibration_k=self._budget_cfg.token_calibration_k,
            status=status,
            now=now,
        )

    def record_rate_limit_cooldown(
        self,
        store: MemoryStore,
        *,
        run_id: str,
        token_estimate: TokenEstimate | None = None,
        now: datetime | str | None = None,
    ) -> BudgetLedgerEntry:
        """Record an `E6002_HOST_RATE_LIMIT_429` event into `budget_ledger` to activate 5h cooldown."""
        logger.warning(
            "[%s] Host 5-hour quota rate limit triggered for run_id=%s; activating %dh cooldown.",
            ErrorCode.E6002_HOST_RATE_LIMIT_429.value,
            run_id,
            self._budget_cfg.cooldown_hours_on_429,
        )
        if token_estimate is not None:
            return self.record_token_usage(
                store,
                run_id=run_id,
                token_estimate=token_estimate,
                cache_hit=False,
                status="rate_limited_429",
                now=now,
            )
        return store.record_budget_usage(
            run_id=run_id,
            est_audio_tokens=0,
            est_video_tokens=0,
            est_text_tokens=0,
            est_cached_tokens=0,
            calibration_k=self._budget_cfg.token_calibration_k,
            status="rate_limited_429",
            now=now,
        )

    def _decode_host_payload(
        self,
        raw_output: str | Mapping[str, Any],
        *,
        allow_safety_sentinel: bool,
    ) -> dict[str, Any]:
        if isinstance(raw_output, Mapping):
            data = dict(raw_output)
        elif isinstance(raw_output, str):
            cleaned = _strip_json_code_fence(raw_output)
            if not cleaned:
                raise GeminiNestError(
                    ErrorCode.E6003_SCHEMA_INVALID,
                    "Host output is empty.",
                )
            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError as exc:
                # Non-JSON raw host error messages: inspect for 429 or safety block markers.
                if _RATE_LIMIT_RAW_RE.search(cleaned):
                    raise GeminiNestError(
                        ErrorCode.E6002_HOST_RATE_LIMIT_429,
                        f"Host output indicated rate limit (429): {cleaned[:200]}",
                    ) from exc
                if allow_safety_sentinel and _SAFETY_RAW_RE.search(cleaned):
                    return {"__safety_block_reason__": cleaned[:200]}
                raise GeminiNestError(
                    ErrorCode.E6003_SCHEMA_INVALID,
                    f"Host output is not valid JSON: {exc.msg}",
                ) from exc

            if not isinstance(parsed, dict):
                raise GeminiNestError(
                    ErrorCode.E6003_SCHEMA_INVALID,
                    f"Host output root must be a JSON object, got {type(parsed).__name__}.",
                )
            data = parsed
        else:
            raise GeminiNestError(
                ErrorCode.E6003_SCHEMA_INVALID,
                f"Unsupported host output type: {type(raw_output).__name__}.",
            )

        rate_limit_msg = _check_dict_rate_limit(data)
        if rate_limit_msg is not None:
            raise GeminiNestError(ErrorCode.E6002_HOST_RATE_LIMIT_429, rate_limit_msg)

        return data

    def _handle_safety_block(
        self,
        *,
        bv_id: str,
        reason: str,
        raise_error: bool,
    ) -> AppreciationResponse:
        logger.warning(
            "[%s] Host safety block triggered for bv_id=%s (reason=%s); skipping without retry.",
            ErrorCode.E6001_HOST_SAFETY_BLOCKED.value,
            bv_id,
            reason,
        )
        if raise_error:
            raise GeminiNestError(
                ErrorCode.E6001_HOST_SAFETY_BLOCKED,
                f"Host safety block triggered for {bv_id}: {reason}",
            )
        return AppreciationResponse(
            bv_id=bv_id,
            public_diary="",
            private_thought="",
            impression_score=1,
            memory_summary="",
            continue_watching=False,
            recap_50chars="",
            next_curiosity_keywords=(),
            safety_blocked=True,
            block_reason=reason,
        )
