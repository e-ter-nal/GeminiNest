"""Module 8: Google Drive pure-text two-way sync, isolated exports Git, and inbox patch absorption.

Enforces:
- Pure-text extension firewall (`E8002_SYNC_EXT_VIOLATION` on any non-whitelisted file)
- Forward export of `persona_snapshot.md`, `memory_summary.md`, `diary_latest.md`, `sync_manifest.json`
- Isolated hidden Git repository commits inside `data/exports/` (`-c core.quotepath=false`, UTF-8)
- Cloud drive mount probing with graceful local-only fallback (`E8001_CLOUD_DRIVE_UNMOUNTED`)
- Windows-safe readonly rotation (`0_active/` -> `1_yesterday_readonly/` with explicit `S_IWRITE` unlock,
  whole-file overwrite, `<!-- READONLY SNAPSHOT: DO NOT EDIT -->` banner, fault-tolerant `S_IREAD` restore)
- Reverse `[MEMORY_PATCH]` absorption from `inbox/` (`.txt`/`.md`, silently ignoring `.gdoc` pointers)
  with strict whitelist & safety redline enforcement (`E8003_PATCH_PARSE_FAILED`,
  `E8004_PATCH_POLICY_REJECTED`) and archiving to `inbox_archived/`
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import subprocess
import time
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from gemininest.contracts import (
    AppConfig,
    ErrorCode,
    GeminiNestError,
    MemoryPatch,
    SyncReport,
    VideoMeta,
)
from gemininest.memory_store import MemoryStore
from gemininest.meta_filter import MetaFilter

__all__ = [
    "ALLOWED_PATCH_OPS",
    "DEFAULT_PERSONA_PATH",
    "IGNORED_CLOUD_POINTER_EXTENSIONS",
    "PURE_TEXT_EXTENSIONS",
    "READONLY_BANNER",
    "SyncManager",
    "compute_canonical_patch_hash",
    "compute_canonical_patch_id",
    "parse_memory_patch_blocks",
    "sync_report_to_dict",
    "validate_memory_patch",
    "validate_sync_extension",
]

logger = logging.getLogger(__name__)

DEFAULT_PERSONA_PATH = Path(".agent/rules/cat-soul.md")
PURE_TEXT_EXTENSIONS: frozenset[str] = frozenset({".md", ".json", ".txt"})
INBOX_PATCH_EXTENSIONS: frozenset[str] = frozenset({".txt", ".md"})
IGNORED_CLOUD_POINTER_EXTENSIONS: frozenset[str] = frozenset(
    {".gdoc", ".gsheet", ".gslides", ".gdraw", ".gmap"}
)
_IGNORED_OS_FILENAMES: frozenset[str] = frozenset({"desktop.ini", "thumbs.db", ".ds_store"})

ALLOWED_PATCH_OPS: frozenset[str] = frozenset(
    {"add_memory", "boost_memory", "add_curiosity", "suggest_persona_tweak"}
)
READONLY_BANNER = "<!-- READONLY SNAPSHOT: DO NOT EDIT -->"
_READONLY_MARKER_PREFIX = "<!-- READONLY SNAPSHOT"

_ISOLATED_EXPORTS_GITIGNORE = (
    "# Isolated exports repo: pure-text whitelist only\n"
    "*\n"
    "!.gitignore\n"
    "!*.md\n"
    "!*.json\n"
    "!*.txt\n"
)

_MAX_MEMORY_TITLE_CHARS = 120
_MAX_MEMORY_SUMMARY_CHARS = 600
_MAX_MEMORY_TAGS = 10
_MAX_TAG_CHARS = 30
_MAX_CURIOSITY_KEYWORDS_PER_PATCH = 3
_MAX_CURIOSITY_KEYWORD_CHARS = 40
_MAX_CURIOSITY_REASON_CHARS = 200
_MAX_PERSONA_TWEAK_CHARS = 300
_MAX_BOOST_DELTA = 3.0

_PATCH_START_RE = re.compile(r"^\s*\[MEMORY_PATCH\]\s*$", re.IGNORECASE)
_PATCH_END_RE = re.compile(r"^\s*\[/MEMORY_PATCH\]\s*$", re.IGNORECASE)
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json|yaml|yml|txt|text)?\s*\n(?P<body>.*?)\n\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)
_YAML_KV_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(?P<val>.*)$")
_YAML_LIST_ITEM_RE = re.compile(r"^\s*-\s+(?P<item>.+?)\s*$")
_HR_LINE_RE = re.compile(r"^\s*(?:---+|\*\*\*+|===+)\s*$")

_SAFETY_REDLINE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "attempt to modify or bypass safety redline / blacklist / perception honesty",
        re.compile(
            r"(?:修改|改写|删除|取消|忽略|关闭|解除|绕过|放宽|废除|禁用|移除|突破|无视)"
            r".{0,10}"
            r"(?:安全红线|红线|黑名单|安检|视觉安检|前置过滤|感知诚实|安全边界|安全策略|审核|过滤规则)",
            re.IGNORECASE,
        ),
    ),
    (
        "attempt to allow blacklisted categories",
        re.compile(
            r"(?:允许|放行|解禁|解除拦截|不再拦截|取消拦截|取消过滤)"
            r".{0,10}"
            r"(?:二游|宅舞|擦边|敏感|违规|黑名单|时尚|美妆|mmd)",
            re.IGNORECASE,
        ),
    ),
    (
        "attempt to violate perception honesty rule",
        re.compile(
            r"(?:假装看到|捏造画面|编造视觉|伪造视觉|虚构画面|假装看过视频画面)",
            re.IGNORECASE,
        ),
    ),
    (
        "attempt to bypass pure-text cloud isolation",
        re.compile(
            r"(?:关闭纯文本|取消纯文本|绕过纯文本|允许上传|同步二进制|上传原片|上传数据库|上传\s*\.?(?:mp4|db|m4a))",
            re.IGNORECASE,
        ),
    ),
    (
        "attempt to exfiltrate credentials or secrets",
        re.compile(
            r"(?:bili_sessdata|bili_jct|bili_buvid3|sessdata\s*=|"
            r"(?:泄露|导出|写入|记录|打印).{0,10}(?:cookie|凭据|\.env|密钥|密码))",
            re.IGNORECASE,
        ),
    ),
    (
        "English safety/policy bypass directive",
        re.compile(
            r"(?:(?:ignore|bypass|disable|override|remove|delete|weaken|modify)\s+"
            r"(?:all\s+)?(?:the\s+)?(?:previous\s+)?"
            r"(?:safety|security|blacklist|meta_filter|safety_gate|redline|guardrail|policy|pure_text_only)"
            r"|(?:pure_text_only|skip_on_safety_block|wd_tagger_enabled)\s*[:=]\s*false)",
            re.IGNORECASE,
        ),
    ),
)

_FALLBACK_PERSONA_TEXT = (
    "# GeminiNest Core Persona & Rules (cat-soul)\n\n"
    "（本地 `.agent/rules/cat-soul.md` 未找到，已回退默认安全摘要。）\n"
)


# ---------------------------------------------------------------------------
# Pure-Text Firewall & Utilities
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_policy_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def compute_canonical_patch_hash(op: str, payload: Mapping[str, Any]) -> str:
    """Compute a deterministic SHA-256 content hash from normalized `(op, payload)`."""

    def _canon(value: Any, *, sort_str_list: bool = False) -> Any:
        if isinstance(value, str):
            return " ".join(unicodedata.normalize("NFKC", value).strip().split())
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for k in sorted(value.keys(), key=lambda key: str(key)):
                ks = str(k)
                out[ks] = _canon(value[k], sort_str_list=(ks in ("tags", "keywords")))
            return out
        if isinstance(value, (list, tuple)):
            items = [_canon(item) for item in value]
            if sort_str_list and all(isinstance(item, str) for item in items):
                return sorted(dict.fromkeys(items))
            return items
        if isinstance(value, float):
            return round(value, 6)
        return value

    canonical_obj = {
        "op": _canon(op).lower(),
        "payload": _canon(payload),
    }
    encoded = json.dumps(
        canonical_obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_canonical_patch_id(op: str, payload: Mapping[str, Any]) -> tuple[str, str]:
    """Return `(stable_patch_id, content_hash)` independent of filename or block formatting."""
    content_hash = compute_canonical_patch_hash(op, payload)
    safe_op = re.sub(r"[^a-z0-9_]+", "_", op.strip().lower()).strip("_") or "patch"
    return f"patch_{safe_op}_{content_hash[:16]}", content_hash


def _utf8_subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Prevent any inherited git env vars from hijacking isolated data/exports git commands.
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(key, None)
    return env


def validate_sync_extension(
    path: Path | str,
    *,
    allowed_extensions: Sequence[str] = (".md", ".json", ".txt"),
    pure_text_only: bool = True,
) -> Path:
    """Strictly enforce the pure-text extension firewall before any cloud sync or export commit.

    Raises `GeminiNestError(ErrorCode.E8002_SYNC_EXT_VIOLATION)` immediately for any disallowed
    extension (e.g., `.mp4`, `.m4a`, `.db`, `.png`, `.7z`, `.exe`, or extensionless binaries).
    """
    target = Path(path)
    normalized_allowed = {ext.strip().lower() for ext in allowed_extensions if ext.strip()}
    if pure_text_only:
        effective_allowed = normalized_allowed & PURE_TEXT_EXTENSIONS
    else:
        effective_allowed = normalized_allowed

    ext = target.suffix.lower()
    if not ext or ext not in effective_allowed:
        raise GeminiNestError(
            ErrorCode.E8002_SYNC_EXT_VIOLATION,
            f"Pure-text sync firewall blocked {target.name!r} (extension={ext or '<none>'!r}); "
            f"allowed extensions: {sorted(effective_allowed)}",
        )
    return target


def _unlock_readonly_path(path: Path) -> None:
    """Clear Windows/POSIX readonly bit so overwrite or removal never fails with WinError 5."""
    try:
        if path.exists():
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError as exc:
        logger.warning("Failed to clear readonly bit on %s: %s", path, exc)


def _unlock_readonly_tree(directory: Path) -> None:
    if not directory.exists():
        return
    for root, dirs, files in os.walk(directory):
        root_path = Path(root)
        for fname in files:
            _unlock_readonly_path(root_path / fname)
        for dname in dirs:
            _unlock_readonly_path(root_path / dname)


def _set_readonly_best_effort(path: Path) -> bool:
    try:
        os.chmod(path, stat.S_IREAD)
        return True
    except OSError as exc:
        logger.warning("Best-effort readonly chmod failed on %s: %s", path, exc)
        return False


def _apply_readonly_snapshot_content(path: Path, raw_text: str) -> str:
    """Ensure readonly snapshot banner is preserved/added without breaking JSON validity."""
    ext = path.suffix.lower()
    if ext == ".json":
        try:
            loaded = json.loads(raw_text)
            if isinstance(loaded, dict):
                marked = {"_readonly_snapshot": "READONLY SNAPSHOT: DO NOT EDIT", **loaded}
                return json.dumps(marked, ensure_ascii=False, indent=2) + "\n"
        except ValueError:
            pass
        return raw_text

    stripped_bom = raw_text.lstrip("\ufeff")
    first_line = stripped_bom.splitlines()[0].strip() if stripped_bom.splitlines() else ""
    if first_line.startswith(_READONLY_MARKER_PREFIX):
        return stripped_bom
    return f"{READONLY_BANNER}\n\n{stripped_bom}"


def _is_ignored_cloud_junk(path: Path) -> bool:
    name_lower = path.name.lower()
    if name_lower in _IGNORED_OS_FILENAMES:
        return True
    if path.name.startswith("."):
        return True
    return False


# ---------------------------------------------------------------------------
# Reverse [MEMORY_PATCH] Parser & Policy Validator
# ---------------------------------------------------------------------------


def _parse_scalar_value(raw: str) -> Any:
    val = raw.strip()
    if not val:
        return ""
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        if len(val) >= 2:
            if val.startswith('"'):
                try:
                    return json.loads(val)
                except ValueError:
                    return val[1:-1]
            return val[1:-1]
    if val.startswith("[") and val.endswith("]"):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return parsed
        except ValueError:
            inner = val[1:-1].strip()
            if not inner:
                return []
            return [p.strip().strip("\"'") for p in re.split(r"[,，]", inner) if p.strip()]
    if val.startswith("{") and val.endswith("}"):
        try:
            return json.loads(val)
        except ValueError:
            pass
    lower = val.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in ("null", "none"):
        return None
    if re.fullmatch(r"[+-]?\d+", val):
        return int(val)
    if re.fullmatch(r"[+-]?\d+\.\d+", val):
        return float(val)
    return val


def _parse_single_patch_body(body: str) -> dict[str, Any]:
    cleaned = body.strip()
    if not cleaned:
        raise GeminiNestError(
            ErrorCode.E8003_PATCH_PARSE_FAILED,
            "Empty [MEMORY_PATCH] block.",
        )

    fence_match = _CODE_FENCE_RE.match(cleaned)
    if fence_match:
        cleaned = fence_match.group("body").strip()
        if not cleaned:
            raise GeminiNestError(
                ErrorCode.E8003_PATCH_PARSE_FAILED,
                "Empty fenced [MEMORY_PATCH] block.",
            )

    if cleaned.startswith("{"):
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise GeminiNestError(
                ErrorCode.E8003_PATCH_PARSE_FAILED,
                f"Invalid JSON inside [MEMORY_PATCH] block: {exc}",
            ) from exc
        if not isinstance(parsed, dict):
            raise GeminiNestError(
                ErrorCode.E8003_PATCH_PARSE_FAILED,
                "[MEMORY_PATCH] JSON payload must be an object.",
            )
        return parsed

    # Simple pure-Python YAML key-value & list parser (zero PyYAML dependency).
    result: dict[str, Any] = {}
    current_list_key: str | None = None
    for lineno, raw_line in enumerate(cleaned.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        list_match = _YAML_LIST_ITEM_RE.match(raw_line)
        if list_match and current_list_key is not None:
            item_val = _parse_scalar_value(list_match.group("item"))
            result[current_list_key].append(item_val)
            continue

        kv_match = _YAML_KV_RE.match(stripped)
        if not kv_match:
            raise GeminiNestError(
                ErrorCode.E8003_PATCH_PARSE_FAILED,
                f"Malformed line {lineno} in [MEMORY_PATCH] block: {stripped[:80]!r}",
            )

        key = kv_match.group("key").strip()
        val_raw = kv_match.group("val").strip()
        if not val_raw:
            result[key] = []
            current_list_key = key
        else:
            result[key] = _parse_scalar_value(val_raw)
            current_list_key = None

    if not result:
        raise GeminiNestError(
            ErrorCode.E8003_PATCH_PARSE_FAILED,
            "No key-value fields found in [MEMORY_PATCH] block.",
        )
    return result


def parse_memory_patch_blocks(
    raw_text: str,
    *,
    source_file: Path | str,
) -> list[tuple[str, dict[str, Any] | None, str | None]]:
    """Extract and parse all `[MEMORY_PATCH]` blocks from a `.txt`/`.md` inbox file.

    Returns a list of `(patch_id, parsed_dict_or_None, parse_error_or_None)`.
    """
    src = Path(source_file)
    text = raw_text.lstrip("\ufeff")
    lines = text.splitlines()

    raw_blocks: list[str] = []
    idx = 0
    while idx < len(lines):
        if _PATCH_START_RE.match(lines[idx]):
            idx += 1
            buf: list[str] = []
            while idx < len(lines):
                line = lines[idx]
                if _PATCH_END_RE.match(line):
                    idx += 1
                    break
                if _PATCH_START_RE.match(line):
                    break
                if _HR_LINE_RE.match(line) and buf:
                    idx += 1
                    break
                buf.append(line)
                idx += 1
            raw_blocks.append("\n".join(buf))
        else:
            idx += 1

    if not raw_blocks:
        return [
            (
                f"{src.stem}_block0",
                None,
                f"[{ErrorCode.E8003_PATCH_PARSE_FAILED.value}] No [MEMORY_PATCH] block found in {src.name}",
            )
        ]

    parsed_results: list[tuple[str, dict[str, Any] | None, str | None]] = []
    for block_idx, block_body in enumerate(raw_blocks, start=1):
        digest = hashlib.sha256(f"{src.name}:{block_idx}:{block_body}".encode("utf-8")).hexdigest()[:10]
        patch_id = f"{src.stem}_{block_idx}_{digest}"
        try:
            payload = _parse_single_patch_body(block_body)
            parsed_results.append((patch_id, payload, None))
        except GeminiNestError as exc:
            parsed_results.append((patch_id, None, str(exc)))
    return parsed_results


def _check_safety_redline_violation(text: str) -> str | None:
    if not text or not text.strip():
        return None
    norm = _normalize_policy_text(text)
    for reason, pattern in _SAFETY_REDLINE_PATTERNS:
        if pattern.search(norm):
            return reason
    return None


def _coerce_tags(raw: Any) -> tuple[str, ...]:
    if raw is None or raw == "":
        return ()
    if isinstance(raw, str):
        parts = [p.strip() for p in re.split(r"[,，、;；]+", raw) if p.strip()]
        return tuple(parts)
    if isinstance(raw, (list, tuple)):
        return tuple(str(x).strip() for x in raw if str(x).strip())
    raise ValueError(f"Unsupported tags format: {type(raw).__name__}")


def _check_blacklist_text(text: str, meta_filter: MetaFilter | None) -> str | None:
    if meta_filter is None or not text.strip():
        return None
    probe = VideoMeta(
        bv_id="PATCH_PROBE",
        aid=None,
        title=text,
        desc="",
        tname=text,
        tags=(text,),
        author="",
        duration_sec=0,
        view_count=0,
        like_count=0,
        pubdate_iso="1970-01-01T00:00:00Z",
        source_channel="curiosity",
        curiosity_keyword=text,
    )
    res = meta_filter.check(probe)
    if not res.allowed:
        return res.reason
    return None


def validate_memory_patch(
    *,
    patch_id: str,
    source_file: Path | str,
    raw_payload: Mapping[str, Any],
    meta_filter: MetaFilter | None = None,
) -> tuple[MemoryPatch | None, str | None]:
    """Validate a parsed `[MEMORY_PATCH]` dict against op whitelist, bounds, and safety redlines.

    Returns `(MemoryPatch, None)` when valid, or `(MemoryPatch | None, error_string)` when rejected
    with `ErrorCode.E8003_PATCH_PARSE_FAILED` or `ErrorCode.E8004_PATCH_POLICY_REJECTED`.
    """
    src = Path(source_file)
    op_raw = raw_payload.get("op")
    if not isinstance(op_raw, str) or not op_raw.strip():
        return (
            None,
            f"[{ErrorCode.E8003_PATCH_PARSE_FAILED.value}] Missing required 'op' field in patch {patch_id}.",
        )
    op = op_raw.strip()

    if op not in ALLOWED_PATCH_OPS:
        return (
            None,
            f"[{ErrorCode.E8004_PATCH_POLICY_REJECTED.value}] Disallowed patch operation {op!r}; "
            f"allowed ops: {sorted(ALLOWED_PATCH_OPS)}",
        )

    typed_op: Literal["add_memory", "boost_memory", "add_curiosity", "suggest_persona_tweak"] = op  # type: ignore[assignment]

    def _reject(code: ErrorCode, reason: str) -> tuple[MemoryPatch, str]:
        full = f"[{code.value}] {reason}"
        return (
            MemoryPatch(
                patch_id=patch_id,
                source_file=src,
                op=typed_op,
                payload=dict(raw_payload),
                validated=False,
                reject_reason=full,
            ),
            full,
        )

    # Global safety redline scan across all string fields in payload
    all_text_parts: list[str] = []
    for k, v in raw_payload.items():
        if k == "op":
            continue
        if isinstance(v, str):
            all_text_parts.append(v)
        elif isinstance(v, list):
            all_text_parts.extend(str(item) for item in v)
    combined_text = "\n".join(all_text_parts)
    redline_hit = _check_safety_redline_violation(combined_text)
    if redline_hit is not None:
        return _reject(
            ErrorCode.E8004_PATCH_POLICY_REJECTED,
            f"Safety redline violation detected ({redline_hit}).",
        )

    if typed_op == "add_memory":
        title = raw_payload.get("title")
        summary = raw_payload.get("summary") or raw_payload.get("content")
        if not isinstance(title, str) or not title.strip():
            return _reject(ErrorCode.E8003_PATCH_PARSE_FAILED, "add_memory requires non-empty 'title'.")
        if not isinstance(summary, str) or not summary.strip():
            return _reject(ErrorCode.E8003_PATCH_PARSE_FAILED, "add_memory requires non-empty 'summary'.")
        title_s = title.strip()
        summary_s = summary.strip()
        if len(title_s) > _MAX_MEMORY_TITLE_CHARS:
            return _reject(
                ErrorCode.E8004_PATCH_POLICY_REJECTED,
                f"add_memory 'title' exceeds {_MAX_MEMORY_TITLE_CHARS} chars ({len(title_s)}).",
            )
        if len(summary_s) > _MAX_MEMORY_SUMMARY_CHARS:
            return _reject(
                ErrorCode.E8004_PATCH_POLICY_REJECTED,
                f"add_memory 'summary' exceeds {_MAX_MEMORY_SUMMARY_CHARS} chars ({len(summary_s)}).",
            )
        raw_imp = raw_payload.get("importance", 6.0)
        if isinstance(raw_imp, bool) or not isinstance(raw_imp, (int, float)):
            return _reject(
                ErrorCode.E8003_PATCH_PARSE_FAILED,
                f"add_memory 'importance' must be a number, got {raw_imp!r}.",
            )
        importance = float(raw_imp)
        if not (1.0 <= importance <= 10.0):
            return _reject(
                ErrorCode.E8004_PATCH_POLICY_REJECTED,
                f"add_memory 'importance' must be within [1.0, 10.0], got {importance}.",
            )
        try:
            tags = _coerce_tags(raw_payload.get("tags"))
        except ValueError as exc:
            return _reject(ErrorCode.E8003_PATCH_PARSE_FAILED, str(exc))
        if len(tags) > _MAX_MEMORY_TAGS or any(len(t) > _MAX_TAG_CHARS for t in tags):
            return _reject(
                ErrorCode.E8004_PATCH_POLICY_REJECTED,
                f"add_memory 'tags' exceeds limit (max {_MAX_MEMORY_TAGS} tags, each <= {_MAX_TAG_CHARS} chars).",
            )
        bv_id_raw = raw_payload.get("bv_id")
        bv_id = str(bv_id_raw).strip() if isinstance(bv_id_raw, str) and bv_id_raw.strip() else None

        normalized_payload: dict[str, Any] = {
            "title": title_s,
            "summary": summary_s,
            "importance": importance,
            "tags": list(tags),
            "bv_id": bv_id,
        }
        stable_id, _ = compute_canonical_patch_id(typed_op, normalized_payload)
        return (
            MemoryPatch(
                patch_id=stable_id,
                source_file=src,
                op=typed_op,
                payload=normalized_payload,
                validated=True,
                reject_reason=None,
            ),
            None,
        )

    if typed_op == "boost_memory":
        raw_id = raw_payload.get("memory_id")
        raw_query = raw_payload.get("query") or raw_payload.get("title")
        memory_id: int | None = None
        query_str: str | None = None

        if raw_id is not None and raw_id != "":
            if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id <= 0:
                return _reject(
                    ErrorCode.E8003_PATCH_PARSE_FAILED,
                    f"boost_memory 'memory_id' must be a positive integer, got {raw_id!r}.",
                )
            memory_id = int(raw_id)
        elif isinstance(raw_query, str) and raw_query.strip():
            query_str = raw_query.strip()
        else:
            return _reject(
                ErrorCode.E8003_PATCH_PARSE_FAILED,
                "boost_memory requires either 'memory_id' or non-empty 'query'/'title'.",
            )

        raw_delta = raw_payload.get("importance_delta", 1.0)
        if isinstance(raw_delta, bool) or not isinstance(raw_delta, (int, float)):
            return _reject(
                ErrorCode.E8003_PATCH_PARSE_FAILED,
                f"boost_memory 'importance_delta' must be a number, got {raw_delta!r}.",
            )
        delta = float(raw_delta)
        if not (0.0 < delta <= _MAX_BOOST_DELTA):
            return _reject(
                ErrorCode.E8004_PATCH_POLICY_REJECTED,
                f"boost_memory 'importance_delta' must be in (0.0, {_MAX_BOOST_DELTA}], got {delta}.",
            )

        boost_payload = {
            "memory_id": memory_id,
            "query": query_str,
            "importance_delta": delta,
        }
        stable_id, _ = compute_canonical_patch_id(typed_op, boost_payload)
        return (
            MemoryPatch(
                patch_id=stable_id,
                source_file=src,
                op=typed_op,
                payload=boost_payload,
                validated=True,
                reject_reason=None,
            ),
            None,
        )

    if typed_op == "add_curiosity":
        raw_keywords = raw_payload.get("keywords")
        raw_single = raw_payload.get("keyword")
        keywords: list[str] = []
        if raw_single is not None and str(raw_single).strip():
            keywords.append(str(raw_single).strip())
        if raw_keywords is not None and raw_keywords != "":
            try:
                keywords.extend(_coerce_tags(raw_keywords))
            except ValueError as exc:
                return _reject(ErrorCode.E8003_PATCH_PARSE_FAILED, str(exc))

        deduped_kws = list(dict.fromkeys(k for k in keywords if k))
        if not deduped_kws:
            return _reject(
                ErrorCode.E8003_PATCH_PARSE_FAILED,
                "add_curiosity requires non-empty 'keyword' or 'keywords'.",
            )
        if len(deduped_kws) > _MAX_CURIOSITY_KEYWORDS_PER_PATCH:
            return _reject(
                ErrorCode.E8004_PATCH_POLICY_REJECTED,
                f"add_curiosity allows at most {_MAX_CURIOSITY_KEYWORDS_PER_PATCH} keywords per patch.",
            )
        for kw in deduped_kws:
            if len(kw) > _MAX_CURIOSITY_KEYWORD_CHARS:
                return _reject(
                    ErrorCode.E8004_PATCH_POLICY_REJECTED,
                    f"Curiosity keyword {kw!r} exceeds {_MAX_CURIOSITY_KEYWORD_CHARS} chars.",
                )
            bl_reason = _check_blacklist_text(kw, meta_filter)
            if bl_reason is not None:
                return _reject(
                    ErrorCode.E8004_PATCH_POLICY_REJECTED,
                    f"Curiosity keyword {kw!r} violates blacklist firewall: {bl_reason}",
                )

        reason = str(raw_payload.get("reason") or raw_payload.get("content") or "Added via Web inbox patch").strip()
        if len(reason) > _MAX_CURIOSITY_REASON_CHARS:
            return _reject(
                ErrorCode.E8004_PATCH_POLICY_REJECTED,
                f"add_curiosity 'reason' exceeds {_MAX_CURIOSITY_REASON_CHARS} chars.",
            )

        curiosity_payload = {"keywords": deduped_kws, "reason": reason}
        stable_id, _ = compute_canonical_patch_id(typed_op, curiosity_payload)
        return (
            MemoryPatch(
                patch_id=stable_id,
                source_file=src,
                op=typed_op,
                payload=curiosity_payload,
                validated=True,
                reject_reason=None,
            ),
            None,
        )

    # suggest_persona_tweak
    suggestion = raw_payload.get("suggestion") or raw_payload.get("content") or raw_payload.get("tweak")
    if not isinstance(suggestion, str) or not suggestion.strip():
        return _reject(
            ErrorCode.E8003_PATCH_PARSE_FAILED,
            "suggest_persona_tweak requires non-empty 'suggestion' (or 'content'/'tweak').",
        )
    suggestion_s = suggestion.strip()
    if len(suggestion_s) > _MAX_PERSONA_TWEAK_CHARS:
        return _reject(
            ErrorCode.E8004_PATCH_POLICY_REJECTED,
            f"suggest_persona_tweak exceeds max length {_MAX_PERSONA_TWEAK_CHARS} chars ({len(suggestion_s)}).",
        )
    bl_hit = _check_blacklist_text(suggestion_s, meta_filter)
    if bl_hit is not None:
        return _reject(
            ErrorCode.E8004_PATCH_POLICY_REJECTED,
            f"Persona tweak mentions blacklisted topic: {bl_hit}",
        )
    section = str(raw_payload.get("section") or "style_preference").strip()[:60]
    reason = str(raw_payload.get("reason") or "Web chat persona tweak suggestion").strip()[:160]

    tweak_payload = {
        "suggestion": suggestion_s,
        "section": section,
        "reason": reason,
    }
    stable_id, _ = compute_canonical_patch_id(typed_op, tweak_payload)
    return (
        MemoryPatch(
            patch_id=stable_id,
            source_file=src,
            op=typed_op,
            payload=tweak_payload,
            validated=True,
            reject_reason=None,
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Serialization Helper
# ---------------------------------------------------------------------------


def sync_report_to_dict(report: SyncReport) -> dict[str, Any]:
    """Convert frozen `SyncReport` into a JSON-serializable dict."""
    return {
        "exported_files": [str(p) for p in report.exported_files],
        "export_git_commit_hash": report.export_git_commit_hash,
        "yesterday_rotated": report.yesterday_rotated,
        "active_synced_count": report.active_synced_count,
        "patches_applied": report.patches_applied,
        "patches_rejected": report.patches_rejected,
        "patches_skipped_duplicate": report.patches_skipped_duplicate,
        "archived_files": [str(p) for p in report.archived_files],
        "errors": list(report.errors),
    }


# ---------------------------------------------------------------------------
# SyncManager
# ---------------------------------------------------------------------------


class SyncManager:
    """Manages pure-text local exports, isolated exports Git, cloud rotation, and inbox patches."""

    def __init__(
        self,
        config: AppConfig,
        *,
        persona_path: Path | str = DEFAULT_PERSONA_PATH,
        local_export_dir: Path | str | None = None,
        cloud_nest_dir: Path | str | None = None,
        persona_tweaks_path: Path | str | None = None,
        meta_filter: MetaFilter | None = None,
        probe_retries: int = 2,
        probe_retry_delay_sec: float = 0.05,
    ) -> None:
        self._config = config
        self._sync_cfg = config.sync
        self._persona_path = Path(persona_path)
        self._local_export_dir = Path(
            local_export_dir if local_export_dir is not None else self._sync_cfg.local_export_dir
        )
        self._cloud_nest_dir = Path(
            cloud_nest_dir if cloud_nest_dir is not None else self._sync_cfg.cloud_nest_dir
        )
        self._persona_tweaks_path = (
            Path(persona_tweaks_path)
            if persona_tweaks_path is not None
            else self._local_export_dir / "persona_tweaks_review.md"
        )
        self._meta_filter = meta_filter if meta_filter is not None else MetaFilter(config)
        self._probe_retries = max(1, int(probe_retries))
        self._probe_retry_delay_sec = max(0.0, float(probe_retry_delay_sec))

    @property
    def local_export_dir(self) -> Path:
        return self._local_export_dir

    @property
    def cloud_nest_dir(self) -> Path:
        return self._cloud_nest_dir

    @property
    def active_dir(self) -> Path:
        return self._cloud_nest_dir / self._sync_cfg.active_subdir

    @property
    def yesterday_dir(self) -> Path:
        return self._cloud_nest_dir / self._sync_cfg.yesterday_subdir

    @property
    def inbox_dir(self) -> Path:
        return self._cloud_nest_dir / self._sync_cfg.inbox_subdir

    @property
    def inbox_archived_dir(self) -> Path:
        return self._cloud_nest_dir / self._sync_cfg.inbox_archived_subdir

    @property
    def persona_tweaks_path(self) -> Path:
        return self._persona_tweaks_path

    # ------------------------------------------------------------------
    # Firewall & Mount Probe
    # ------------------------------------------------------------------

    def validate_file(self, path: Path | str) -> Path:
        """Validate a single file path against the pure-text extension firewall."""
        return validate_sync_extension(
            path,
            allowed_extensions=self._sync_cfg.allowed_sync_extensions,
            pure_text_only=self._sync_cfg.pure_text_only,
        )

    def _collect_and_validate_export_files(self) -> tuple[Path, ...]:
        """Collect all files under `local_export_dir` (excluding `.git/` and `.gitignore`) and validate."""
        if not self._local_export_dir.exists():
            return ()
        valid_files: list[Path] = []
        for entry in sorted(self._local_export_dir.iterdir(), key=lambda p: p.name.lower()):
            if entry.name == ".git" or entry.name == ".gitignore":
                continue
            if entry.is_dir():
                for nested in sorted(entry.rglob("*")):
                    if nested.is_file():
                        self.validate_file(nested)
                        valid_files.append(nested)
                continue
            if entry.is_file():
                self.validate_file(entry)
                valid_files.append(entry)
        return tuple(valid_files)

    def probe_cloud_nest(self) -> bool:
        """Probe whether `cloud_nest_dir` is mounted and accessible with short retries."""
        for attempt in range(self._probe_retries):
            try:
                if self._cloud_nest_dir.exists() and self._cloud_nest_dir.is_dir():
                    with os.scandir(self._cloud_nest_dir) as it:
                        next(it, None)
                    return True
            except OSError as exc:
                logger.warning(
                    "Cloud nest probe attempt %d/%d failed for %s: %s",
                    attempt + 1,
                    self._probe_retries,
                    self._cloud_nest_dir,
                    exc,
                )
            if attempt + 1 < self._probe_retries and self._probe_retry_delay_sec > 0.0:
                time.sleep(self._probe_retry_delay_sec)
        return False

    # ------------------------------------------------------------------
    # Forward Export (SQLite + cat-soul.md -> data/exports/)
    # ------------------------------------------------------------------

    def _resolve_effective_diary_entries(
        self,
        store: MemoryStore,
        diary_entries: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Prefer explicit `diary_entries` if provided; otherwise load persisted entries from SQLite."""
        if diary_entries:
            return [dict(entry) for entry in diary_entries]
        persisted = store.list_recent_diary_entries(limit=10)
        return [
            {
                "id": item.id,
                "memory_id": item.memory_id,
                "run_id": item.run_id,
                "bv_id": item.bv_id,
                "title": item.title,
                "impression_score": item.impression_score,
                "public_diary": item.public_diary,
                "private_thought": item.private_thought,
                "pipeline_used": item.pipeline_used,
                "created_at": item.created_at,
            }
            for item in persisted
        ]

    def _compute_data_stamp(
        self,
        store: MemoryStore,
        diary_entries: Sequence[Mapping[str, Any]],
    ) -> str:
        timestamps: list[str] = []
        for rec in store.list_recent_memories(limit=5):
            timestamps.append(rec.created_at)
            timestamps.append(rec.last_recalled_at)
        for diary in store.list_recent_diary_entries(limit=5):
            timestamps.append(diary.created_at)
        for wb in store.list_worldbook(status=None):
            timestamps.append(wb.updated_at)
        for hist in store.list_recent_watch_history(limit=5):
            timestamps.append(hist.updated_at)
        for entry in diary_entries:
            ts = entry.get("created_at") or entry.get("timestamp")
            if isinstance(ts, str) and ts.strip():
                timestamps.append(ts.strip())
        return max(timestamps) if timestamps else "initial"

    def _render_persona_snapshot(self, store: MemoryStore, data_stamp: str) -> str:
        if self._persona_path.is_file():
            core_soul = self._persona_path.read_text(encoding="utf-8").strip()
        else:
            core_soul = _FALLBACK_PERSONA_TEXT.strip()

        prefs = store.list_worldbook(category="preference", status="active")
        lines = [
            f"<!-- GeminiNest Persona Snapshot | revision: {data_stamp} -->",
            core_soul,
            "",
            "---",
            "",
            "## 受控人设微调建议区（来自 Web 端合法补丁，不覆盖上述核心安全红线）",
        ]
        if prefs:
            for item in prefs:
                lines.append(f"- `{item.keyword}`: {item.content}（更新于 {item.updated_at}）")
        else:
            lines.append("- （当前无待审或生效中的 Web 端人设微调建议）")
        return "\n".join(lines) + "\n"

    def _render_memory_summary(self, store: MemoryStore, data_stamp: str) -> str:
        total_memories = store.count_memories()
        top_memories = store.recall_memories(
            "",
            top_k_total=15,
            top_k_global=15,
            top_k_contextual=0,
            mark_recalled=False,
        )
        core_lore = store.list_worldbook(category="core_lore", status="active")
        curiosity = store.list_worldbook(category="curiosity_queue", status="active")
        prefs = store.list_worldbook(category="preference", status="active")

        lines = [
            "# GeminiNest 记忆摘要视图（Memory Summary）",
            "",
            f"- 数据修订标记：`{data_stamp}`",
            f"- 记忆主库总条数：{total_memories}",
            f"- 活跃好奇心词条数：{len(curiosity)}",
            "",
            "## 1. 核心激活记忆（按艾宾浩斯保留度排序）",
        ]
        if top_memories:
            for item in top_memories:
                rec = item.record
                bv_part = f" (`{rec.bv_id}`)" if rec.bv_id else ""
                tags_part = f" | 标签: {', '.join(rec.tags)}" if rec.tags else ""
                lines.append(
                    f"- {item.display_label} **{rec.title}**{bv_part} "
                    f"(印象分 {rec.importance:.1f}, 回忆 {rec.recall_count} 次): {rec.summary}{tags_part}"
                )
        else:
            lines.append("- （当前暂无动态记忆，待下一次 B 站巡逻后写入）")

        lines.extend(["", "## 2. 明日好奇心兔洞队列（Curiosity Queue）"])
        if curiosity:
            for entry in curiosity:
                lines.append(f"- **{entry.keyword}**：{entry.content}")
        else:
            lines.append("- （当前好奇心队列为空）")

        lines.extend(["", "## 3. 稳定世界书与偏好（Core Lore & Preferences）"])
        if core_lore or prefs:
            for entry in (*core_lore, *prefs):
                lines.append(f"- [{entry.category}] **{entry.keyword}**：{entry.content}")
        else:
            lines.append("- （暂无扩展世界书词条）")

        return "\n".join(lines) + "\n"

    def _render_diary_latest(
        self,
        store: MemoryStore,
        diary_entries: Sequence[Mapping[str, Any]],
        data_stamp: str,
    ) -> str:
        effective_entries = self._resolve_effective_diary_entries(store, diary_entries)
        recent_memories = store.list_recent_memories(limit=10)
        recent_history = store.list_recent_watch_history(limit=10)
        curiosity = store.list_worldbook(category="curiosity_queue", status="active")

        lines = [
            "# GeminiNest 最新冲浪手账（Latest Diary）",
            "",
            f"- 数据修订标记：`{data_stamp}`",
            f"- 管线模式：`{self._config.project.pipeline_mode}`",
            "",
        ]

        if effective_entries:
            lines.append("## 本轮冲浪日记")
            for idx, entry in enumerate(effective_entries, start=1):
                bv_id = str(entry.get("bv_id") or "")
                title = str(entry.get("title") or bv_id or f"作品 #{idx}")
                score = entry.get("impression_score", "-")
                pipeline_used = str(entry.get("pipeline_used") or "").strip()
                public_diary = str(entry.get("public_diary") or "").strip()
                private_thought = str(entry.get("private_thought") or "").strip()
                mode_suffix = f" | {pipeline_used}" if pipeline_used else ""
                lines.append(f"### {idx}. {title} (`{bv_id}` | 印象分: {score}{mode_suffix})")
                if public_diary:
                    lines.append(f"**公开手账**：{public_diary}")
                if self._sync_cfg.export_private_thought and private_thought:
                    lines.append(f"**猫猫碎碎念**：{private_thought}")
                lines.append("")

        lines.append("## 近期记忆回放")
        if recent_memories:
            for rec in recent_memories:
                bv_part = f" (`{rec.bv_id}`)" if rec.bv_id else ""
                tags_part = f" [{', '.join(rec.tags)}]" if rec.tags else ""
                lines.append(
                    f"- [{rec.source}] **{rec.title}**{bv_part} "
                    f"(印象分 {rec.importance:.1f}): {rec.summary}{tags_part}"
                )
        else:
            lines.append("- （暂无近期记忆记录）")

        lines.extend(["", "## 近期浏览与过滤状态"])
        if recent_history:
            for h in recent_history:
                recap = f" — {h.prev_recap}" if h.prev_recap else ""
                lines.append(f"- `{h.bv_id}`《{h.title}》[{h.status} | {h.pipeline_used}]{recap}")
        else:
            lines.append("- （暂无近期浏览记录）")

        lines.extend(["", "## 待探索好奇心词条"])
        if curiosity:
            lines.append("- " + "、".join(e.keyword for e in curiosity[:6]))
        else:
            lines.append("- （无）")

        return "\n".join(lines) + "\n"

    def _compute_export_content_hash(
        self,
        non_manifest_paths: Sequence[Path],
        data_stamp: str,
    ) -> str:
        hasher = hashlib.sha256()
        hasher.update(f"revision:{data_stamp}\n".encode("utf-8"))
        for path in sorted(non_manifest_paths, key=lambda p: p.name.lower()):
            file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            hasher.update(f"{path.name}:{file_digest}\n".encode("utf-8"))
        return hasher.hexdigest()

    def _render_sync_manifest(
        self,
        store: MemoryStore,
        exported_names: Sequence[str],
        data_stamp: str,
        *,
        content_hash: str,
    ) -> str:
        curiosity = store.list_worldbook(category="curiosity_queue", status="active")
        payload = {
            "project": self._config.project.name,
            "version": self._config.project.version,
            "pipeline_mode": self._config.project.pipeline_mode,
            "pure_text_only": self._sync_cfg.pure_text_only,
            "export_private_thought": self._sync_cfg.export_private_thought,
            "allowed_sync_extensions": list(self._sync_cfg.allowed_sync_extensions),
            "data_revision": data_stamp,
            "content_hash": content_hash,
            "memory_count": store.count_memories(),
            "diary_count": len(store.list_recent_diary_entries(limit=1000)),
            "active_curiosity_keywords": [e.keyword for e in curiosity[:10]],
            "exported_files": list(exported_names),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    def export_local_views(
        self,
        store: MemoryStore,
        *,
        diary_entries: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[Path, ...]:
        """Export pure-text Markdown & JSON views from SQLite + `cat-soul.md` into `local_export_dir`."""
        self._local_export_dir.mkdir(parents=True, exist_ok=True)

        data_stamp = self._compute_data_stamp(store, diary_entries)
        persona_path = self.validate_file(self._local_export_dir / "persona_snapshot.md")
        memory_path = self.validate_file(self._local_export_dir / "memory_summary.md")
        diary_path = self.validate_file(self._local_export_dir / "diary_latest.md")
        manifest_path = self.validate_file(self._local_export_dir / "sync_manifest.json")

        persona_path.write_text(self._render_persona_snapshot(store, data_stamp), encoding="utf-8")
        memory_path.write_text(self._render_memory_summary(store, data_stamp), encoding="utf-8")
        diary_path.write_text(
            self._render_diary_latest(store, diary_entries, data_stamp),
            encoding="utf-8",
        )

        content_paths = [persona_path, memory_path, diary_path]
        exported_paths = [persona_path, memory_path, diary_path, manifest_path]
        if self._persona_tweaks_path.is_file() and self._persona_tweaks_path.parent == self._local_export_dir:
            self.validate_file(self._persona_tweaks_path)
            content_paths.append(self._persona_tweaks_path)
            exported_paths.append(self._persona_tweaks_path)

        content_hash = self._compute_export_content_hash(content_paths, data_stamp)
        manifest_path.write_text(
            self._render_sync_manifest(
                store,
                [p.name for p in exported_paths],
                data_stamp,
                content_hash=content_hash,
            ),
            encoding="utf-8",
        )
        return tuple(exported_paths)

    # ------------------------------------------------------------------
    # Isolated Hidden Git Repository inside `data/exports/`
    # ------------------------------------------------------------------

    def _run_export_git(
        self,
        git_args: Sequence[str],
        *,
        use_git_dir: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        export_dir = self._local_export_dir.resolve()
        git_dir = export_dir / ".git"
        if use_git_dir:
            cmd = [
                "git",
                f"--git-dir={git_dir}",
                f"--work-tree={export_dir}",
                "-c",
                "core.quotepath=false",
                "-c",
                "i18n.commitEncoding=utf-8",
                *git_args,
            ]
        else:
            cmd = [
                "git",
                "-c",
                "core.quotepath=false",
                "-c",
                "i18n.commitEncoding=utf-8",
                *git_args,
            ]
        return subprocess.run(
            cmd,
            cwd=str(export_dir),
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_utf8_subprocess_env(),
            timeout=30.0,
            check=False,
        )

    def _ensure_isolated_export_git_repo(self) -> None:
        export_dir = self._local_export_dir.resolve()
        export_dir.mkdir(parents=True, exist_ok=True)

        gitignore_path = export_dir / ".gitignore"
        if not gitignore_path.exists():
            gitignore_path.write_text(_ISOLATED_EXPORTS_GITIGNORE, encoding="utf-8")

        git_dir = export_dir / ".git"
        if not git_dir.exists():
            init_proc = self._run_export_git(["init"], use_git_dir=False)
            if init_proc.returncode != 0:
                raise RuntimeError(
                    f"Failed to initialize isolated export Git repo at {export_dir}: "
                    f"{(init_proc.stderr or init_proc.stdout).strip()}"
                )

        name_proc = self._run_export_git(["config", "--local", "--get", "user.name"])
        if name_proc.returncode != 0 or not name_proc.stdout.strip():
            self._run_export_git(["config", "--local", "user.name", "GeminiNest Sync"])

        email_proc = self._run_export_git(["config", "--local", "--get", "user.email"])
        if email_proc.returncode != 0 or not email_proc.stdout.strip():
            self._run_export_git(["config", "--local", "user.email", "gemininest-sync@local"])

        self._run_export_git(["config", "--local", "core.quotepath", "false"])

    def commit_local_exports(self, *, commit_message: str | None = None) -> str | None:
        """Auto-commit UTF-8 pure-text views into the isolated `data/exports/` Git repo.

        Gracefully skips committing and returns `None` when there are no file changes.
        Raises `E8002_SYNC_EXT_VIOLATION` if any non-whitelisted file exists in `local_export_dir`.
        """
        if not self._sync_cfg.enable_local_export_git_commit:
            return None

        # Enforce pure-text firewall before staging anything
        valid_files = self._collect_and_validate_export_files()
        self._ensure_isolated_export_git_repo()

        add_proc = self._run_export_git(["add", "--all"])
        if add_proc.returncode != 0:
            raise RuntimeError(
                f"Isolated export git add failed: {(add_proc.stderr or add_proc.stdout).strip()}"
            )

        status_proc = self._run_export_git(["status", "--porcelain"])
        if status_proc.returncode != 0:
            raise RuntimeError(
                f"Isolated export git status failed: {(status_proc.stderr or status_proc.stdout).strip()}"
            )
        if not status_proc.stdout.strip():
            logger.debug("No changes in %s; skipping isolated export Git commit.", self._local_export_dir)
            return None

        msg = commit_message or f"sync(exports): 更新纯文本记忆与日记快照 ({len(valid_files)} files)"
        commit_proc = self._run_export_git(["commit", "-m", msg])
        if commit_proc.returncode != 0:
            raise RuntimeError(
                f"Isolated export git commit failed: {(commit_proc.stderr or commit_proc.stdout).strip()}"
            )

        rev_proc = self._run_export_git(["rev-parse", "HEAD"])
        if rev_proc.returncode != 0:
            return None
        return rev_proc.stdout.strip() or None

    # ------------------------------------------------------------------
    # Windows-Safe Cloud Drive Rotation & Forward Sync
    # ------------------------------------------------------------------

    @staticmethod
    def _read_manifest_dict(path: Path | None) -> dict[str, Any] | None:
        if path is None or not path.is_file():
            return None
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            return parsed if isinstance(parsed, dict) else None
        except (OSError, ValueError):
            return None

    def _is_same_published_revision(
        self,
        active_existing: Sequence[Path],
        outbound: Sequence[Path],
    ) -> bool:
        """Compare existing `0_active/` manifest/content against incoming `outbound` exports."""
        if not active_existing or not outbound:
            return False

        active_by_name = {p.name: p for p in active_existing if p.is_file()}
        outbound_by_name = {p.name: p for p in outbound if p.is_file()}

        existing_manifest = self._read_manifest_dict(active_by_name.get("sync_manifest.json"))
        incoming_manifest = self._read_manifest_dict(outbound_by_name.get("sync_manifest.json"))

        if existing_manifest is not None and incoming_manifest is not None:
            ex_rev = existing_manifest.get("data_revision")
            in_rev = incoming_manifest.get("data_revision")
            ex_hash = existing_manifest.get("content_hash")
            in_hash = incoming_manifest.get("content_hash")
            if (
                isinstance(ex_hash, str)
                and ex_hash
                and isinstance(in_hash, str)
                and in_hash
            ):
                return ex_hash == in_hash and ex_rev == in_rev
            if ex_rev is not None and ex_rev == in_rev:
                non_manifest_names = [n for n in outbound_by_name if n != "sync_manifest.json"]
                if non_manifest_names and all(
                    n in active_by_name
                    and active_by_name[n].read_bytes() == outbound_by_name[n].read_bytes()
                    for n in non_manifest_names
                ):
                    return True
                return False
            return False

        if set(active_by_name.keys()) != set(outbound_by_name.keys()):
            return False
        return all(
            active_by_name[name].read_bytes() == outbound_by_name[name].read_bytes()
            for name in outbound_by_name
        )

    def sync_to_cloud(
        self,
        exported_files: Sequence[Path] | None = None,
    ) -> tuple[bool, int, tuple[str, ...]]:
        """Rotate `0_active/` -> `1_yesterday_readonly/` on new revisions and sync pure-text exports.

        - Always enforces `E8002_SYNC_EXT_VIOLATION` before touching the cloud directory.
        - If cloud drive is unmounted, logs warning and returns `E8001_CLOUD_DRIVE_UNMOUNTED`
          without raising, preserving local exports and isolated Git history.
        - Skips `0_active/` -> `1_yesterday_readonly/` rotation when publishing the exact same
          revision/content_hash so `1_yesterday_readonly/` always preserves the previous distinct snapshot.
        """
        # 1. Validate all outbound files strictly against pure-text firewall first
        if exported_files is not None:
            outbound = tuple(self.validate_file(p) for p in exported_files)
            # Also audit the export directory itself so stray non-text files never slip through
            if self._local_export_dir.exists():
                self._collect_and_validate_export_files()
        else:
            outbound = self._collect_and_validate_export_files()

        # 2. Probe cloud drive mount
        if not self.probe_cloud_nest():
            err_msg = (
                f"[{ErrorCode.E8001_CLOUD_DRIVE_UNMOUNTED.value}] "
                f"Cloud nest directory is unavailable or unmounted: {self._cloud_nest_dir}; "
                "local exports and isolated Git commit were preserved."
            )
            logger.warning(err_msg)
            return False, 0, (err_msg,)

        errors: list[str] = []
        self.active_dir.mkdir(parents=True, exist_ok=True)
        self.yesterday_dir.mkdir(parents=True, exist_ok=True)

        # 3. Inspect existing `0_active/` pure-text files
        active_existing: list[Path] = []
        for entry in sorted(self.active_dir.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_file() or _is_ignored_cloud_junk(entry):
                continue
            if entry.suffix.lower() in IGNORED_CLOUD_POINTER_EXTENSIONS:
                logger.info("Ignoring cloud pointer shortcut in 0_active/: %s", entry.name)
                continue
            # Validate active file before rotating
            self.validate_file(entry)
            active_existing.append(entry)

        same_revision = self._is_same_published_revision(active_existing, outbound)
        yesterday_rotated = False
        if active_existing and not same_revision:
            # Windows-safe: unlock readonly attributes on ALL existing files in 1_yesterday_readonly/
            _unlock_readonly_tree(self.yesterday_dir)

            incoming_names = {p.name for p in active_existing}
            for old_entry in list(self.yesterday_dir.iterdir()):
                if not old_entry.is_file() or _is_ignored_cloud_junk(old_entry):
                    continue
                if old_entry.name not in incoming_names:
                    _unlock_readonly_path(old_entry)
                    try:
                        old_entry.unlink(missing_ok=True)
                    except OSError as exc:
                        errors.append(f"Failed removing stale yesterday file {old_entry.name}: {exc}")

            for src_active in active_existing:
                dest_yesterday = self.yesterday_dir / src_active.name
                raw_text = src_active.read_text(encoding="utf-8")
                marked_text = _apply_readonly_snapshot_content(src_active, raw_text)
                if dest_yesterday.exists():
                    _unlock_readonly_path(dest_yesterday)
                # Whole-file overwrite (avoids Google Drive `.tmp` rename conflict copies)
                dest_yesterday.write_text(marked_text, encoding="utf-8")
                _set_readonly_best_effort(dest_yesterday)
            yesterday_rotated = True
        elif same_revision:
            logger.info(
                "Skipping 0_active/ -> 1_yesterday_readonly/ rotation because export revision is unchanged."
            )

        # 4. Write today's validated pure-text exports into `0_active/` (skipping identical files on same revision)
        active_synced_count = 0
        for src_export in outbound:
            if not src_export.is_file():
                continue
            self.validate_file(src_export)
            dest_active = self.active_dir / src_export.name
            new_text = src_export.read_text(encoding="utf-8")
            if same_revision and dest_active.is_file():
                try:
                    if dest_active.read_text(encoding="utf-8") == new_text:
                        continue
                except OSError:
                    pass
            if dest_active.exists():
                _unlock_readonly_path(dest_active)
            dest_active.write_text(new_text, encoding="utf-8")
            active_synced_count += 1

        return yesterday_rotated, active_synced_count, tuple(errors)

    # ------------------------------------------------------------------
    # Reverse Patch Absorption (`inbox/` -> SQLite/review -> `inbox_archived/`)
    # ------------------------------------------------------------------

    def _append_persona_tweak_review(self, patch: MemoryPatch) -> Path:
        """Append validated persona tweak suggestion to the controlled review file (never editing core safety rules)."""
        self.validate_file(self._persona_tweaks_path)
        self._persona_tweaks_path.parent.mkdir(parents=True, exist_ok=True)

        header = (
            "# GeminiNest 受控人设微调建议区（Web Patch Controlled Review）\n\n"
            "> 本文件仅记录通过安全红线与长度校验的 Web 端微调建议，绝不直接改写 `.agent/rules/cat-soul.md` 核心安全红线。\n\n"
        )
        if not self._persona_tweaks_path.exists():
            self._persona_tweaks_path.write_text(header, encoding="utf-8")

        entry_line = (
            f"- [{_utc_now_iso()}] (`{patch.patch_id}` | section=`{patch.payload['section']}`) "
            f"{patch.payload['suggestion']}  <!-- reason: {patch.payload['reason']} -->\n"
        )
        existing = self._persona_tweaks_path.read_text(encoding="utf-8")
        if patch.patch_id not in existing:
            with self._persona_tweaks_path.open("a", encoding="utf-8") as fh:
                fh.write(entry_line)
        return self._persona_tweaks_path

    def _apply_validated_patch(
        self,
        store: MemoryStore,
        patch: MemoryPatch,
    ) -> tuple[Literal["applied", "skipped_duplicate", "rejected"], str | None]:
        """Atomically check idempotency, apply patch op, and record `applied_patches` in one transaction."""
        op = patch.op
        p = patch.payload
        content_hash = compute_canonical_patch_hash(op, p)
        needs_persona_review = False

        with store.transaction():
            if store.is_patch_applied(patch.patch_id):
                logger.info(
                    "Skipping already-applied inbox patch %s (op=%s, file=%s).",
                    patch.patch_id,
                    op,
                    patch.source_file.name,
                )
                return "skipped_duplicate", None

            if op == "add_memory":
                store.add_memory(
                    source="web_patch",
                    bv_id=p.get("bv_id"),
                    title=p["title"],
                    summary=p["summary"],
                    tags=tuple(p.get("tags", ())),
                    importance=float(p["importance"]),
                )
            elif op == "boost_memory":
                memory_id = p.get("memory_id")
                delta = float(p["importance_delta"])
                if memory_id is not None:
                    boosted = store.boost_memory(int(memory_id), importance_delta=delta)
                    if boosted is None:
                        return (
                            "rejected",
                            f"[{ErrorCode.E8004_PATCH_POLICY_REJECTED.value}] "
                            f"boost_memory target memory_id={memory_id} does not exist.",
                        )
                else:
                    query = str(p.get("query") or "")
                    matches = store.recall_memories(
                        query,
                        top_k_total=1,
                        top_k_global=0,
                        top_k_contextual=1,
                        mark_recalled=False,
                    )
                    if not matches:
                        return (
                            "rejected",
                            f"[{ErrorCode.E8004_PATCH_POLICY_REJECTED.value}] "
                            f"boost_memory query {query!r} matched no existing memory.",
                        )
                    store.boost_memory(matches[0].record.id, importance_delta=delta)
            elif op == "add_curiosity":
                for kw in p["keywords"]:
                    store.upsert_worldbook(
                        keyword=kw,
                        category="curiosity_queue",
                        content=p["reason"],
                        status="active",
                    )
            elif op == "suggest_persona_tweak":
                store.upsert_worldbook(
                    keyword=f"persona_tweak:{patch.patch_id}",
                    category="preference",
                    content=p["suggestion"],
                    status="active",
                )
                needs_persona_review = True
            else:
                return (
                    "rejected",
                    f"[{ErrorCode.E8004_PATCH_POLICY_REJECTED.value}] Unsupported op {op!r}.",
                )

            store.record_applied_patch(
                patch_id=patch.patch_id,
                op=op,
                content_hash=content_hash,
                source_file=patch.source_file.name,
            )

        if needs_persona_review:
            self._append_persona_tweak_review(patch)
        return "applied", None

    def _archive_inbox_file(
        self,
        src_file: Path,
        *,
        rejection_reasons: Sequence[str] = (),
    ) -> tuple[Path, ...]:
        """Move a processed inbox patch file into `inbox_archived/` and attach rejection log if needed."""
        self.inbox_archived_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = self.inbox_archived_dir / src_file.name
        if dest.exists():
            dest = self.inbox_archived_dir / f"{src_file.stem}_{stamp}{src_file.suffix}"

        content = src_file.read_text(encoding="utf-8", errors="replace")
        dest.write_text(content, encoding="utf-8")
        _unlock_readonly_path(src_file)
        src_file.unlink(missing_ok=True)

        archived: list[Path] = [dest]
        if rejection_reasons:
            log_path = self.inbox_archived_dir / f"{dest.stem}_rejected.txt"
            log_lines = [
                f"# Rejected Inbox Patch Audit Log ({_utc_now_iso()})",
                f"source_file: {src_file.name}",
                f"archived_as: {dest.name}",
                "reasons:",
                *(f"- {reason}" for reason in rejection_reasons),
                "",
            ]
            log_path.write_text("\n".join(log_lines), encoding="utf-8")
            archived.append(log_path)
        return tuple(archived)

    def absorb_inbox_patches(self, store: MemoryStore) -> SyncReport:
        """Step 1: Scan `inbox/` `.txt`/`.md` files, validate & apply `[MEMORY_PATCH]`, archive originals.

        Silently ignores `.gdoc` shortcut files (waiting for Apps Script `.txt` export).
        """
        if not self.probe_cloud_nest():
            err_msg = (
                f"[{ErrorCode.E8001_CLOUD_DRIVE_UNMOUNTED.value}] "
                f"Cloud nest directory is unavailable for inbox scan: {self._cloud_nest_dir}"
            )
            logger.warning(err_msg)
            return SyncReport(
                exported_files=(),
                export_git_commit_hash=None,
                yesterday_rotated=False,
                active_synced_count=0,
                patches_applied=0,
                patches_rejected=0,
                archived_files=(),
                errors=(err_msg,),
            )

        if not self.inbox_dir.is_dir():
            return SyncReport(
                exported_files=(),
                export_git_commit_hash=None,
                yesterday_rotated=False,
                active_synced_count=0,
                patches_applied=0,
                patches_rejected=0,
                archived_files=(),
                errors=(),
            )

        patches_applied = 0
        patches_rejected = 0
        patches_skipped_duplicate = 0
        archived_files: list[Path] = []
        errors: list[str] = []

        for entry in sorted(self.inbox_dir.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_file() or _is_ignored_cloud_junk(entry):
                continue

            ext = entry.suffix.lower()
            if ext in IGNORED_CLOUD_POINTER_EXTENSIONS:
                logger.info(
                    "Ignored Google Drive pointer file %s in inbox/; waiting for Apps Script .txt export.",
                    entry.name,
                )
                continue

            if ext not in INBOX_PATCH_EXTENSIONS:
                logger.info("Skipping non-patch file %s in inbox/ (ext=%s).", entry.name, ext)
                continue

            raw_text = entry.read_text(encoding="utf-8", errors="replace")
            parsed_blocks = parse_memory_patch_blocks(raw_text, source_file=entry)
            file_rejections: list[str] = []

            for patch_id, raw_payload, parse_err in parsed_blocks:
                if parse_err is not None or raw_payload is None:
                    patches_rejected += 1
                    err_txt = parse_err or f"[{ErrorCode.E8003_PATCH_PARSE_FAILED.value}] Unknown parse error."
                    file_rejections.append(err_txt)
                    errors.append(f"{entry.name}: {err_txt}")
                    continue

                patch_obj, policy_err = validate_memory_patch(
                    patch_id=patch_id,
                    source_file=entry,
                    raw_payload=raw_payload,
                    meta_filter=self._meta_filter,
                )
                if policy_err is not None or patch_obj is None or not patch_obj.validated:
                    patches_rejected += 1
                    err_txt = policy_err or f"[{ErrorCode.E8004_PATCH_POLICY_REJECTED.value}] Rejected by policy."
                    file_rejections.append(err_txt)
                    errors.append(f"{entry.name}: {err_txt}")
                    continue

                apply_status, apply_err = self._apply_validated_patch(store, patch_obj)
                if apply_status == "applied":
                    patches_applied += 1
                elif apply_status == "skipped_duplicate":
                    patches_skipped_duplicate += 1
                else:
                    patches_rejected += 1
                    err_txt = apply_err or f"[{ErrorCode.E8004_PATCH_POLICY_REJECTED.value}] Failed to apply patch."
                    file_rejections.append(err_txt)
                    errors.append(f"{entry.name}: {err_txt}")

            archived = self._archive_inbox_file(entry, rejection_reasons=file_rejections)
            archived_files.extend(archived)

        return SyncReport(
            exported_files=(),
            export_git_commit_hash=None,
            yesterday_rotated=False,
            active_synced_count=0,
            patches_applied=patches_applied,
            patches_rejected=patches_rejected,
            archived_files=tuple(archived_files),
            errors=tuple(errors),
            patches_skipped_duplicate=patches_skipped_duplicate,
        )

    # ------------------------------------------------------------------
    # Orchestrated Step 6 & Full Sync Entrypoints
    # ------------------------------------------------------------------

    def export_and_sync_to_cloud(
        self,
        store: MemoryStore,
        *,
        diary_entries: Sequence[Mapping[str, Any]] = (),
        enable_git_commit: bool | None = None,
        commit_message: str | None = None,
    ) -> SyncReport:
        """Step 6: Export pure-text views -> isolated Git commit -> rotate & sync to cloud drive."""
        exported_files = self.export_local_views(store, diary_entries=diary_entries)

        do_git = (
            self._sync_cfg.enable_local_export_git_commit
            if enable_git_commit is None
            else bool(enable_git_commit)
        )
        commit_hash: str | None = None
        git_errors: list[str] = []
        if do_git:
            commit_hash = self.commit_local_exports(commit_message=commit_message)

        yesterday_rotated, active_synced_count, cloud_errors = self.sync_to_cloud(exported_files)
        return SyncReport(
            exported_files=exported_files,
            export_git_commit_hash=commit_hash,
            yesterday_rotated=yesterday_rotated,
            active_synced_count=active_synced_count,
            patches_applied=0,
            patches_rejected=0,
            archived_files=(),
            errors=tuple([*git_errors, *cloud_errors]),
        )

    def run_full_sync(
        self,
        store: MemoryStore,
        *,
        diary_entries: Sequence[Mapping[str, Any]] = (),
        enable_git_commit: bool | None = None,
        commit_message: str | None = None,
    ) -> SyncReport:
        """Run both Step 1 (`inbox/` absorption) and Step 6 (local export + Git + cloud sync)."""
        inbox_report = self.absorb_inbox_patches(store)
        export_report = self.export_and_sync_to_cloud(
            store,
            diary_entries=diary_entries,
            enable_git_commit=enable_git_commit,
            commit_message=commit_message,
        )
        # Deduplicate repeated E8001 mount error if cloud was unmounted in both phases
        merged_errors = list(dict.fromkeys([*inbox_report.errors, *export_report.errors]))
        return SyncReport(
            exported_files=export_report.exported_files,
            export_git_commit_hash=export_report.export_git_commit_hash,
            yesterday_rotated=export_report.yesterday_rotated,
            active_synced_count=export_report.active_synced_count,
            patches_applied=inbox_report.patches_applied,
            patches_rejected=inbox_report.patches_rejected,
            archived_files=inbox_report.archived_files,
            errors=tuple(merged_errors),
            patches_skipped_duplicate=inbox_report.patches_skipped_duplicate,
        )
