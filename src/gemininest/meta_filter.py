"""Module 3: Pre-download metadata blacklist firewall (NFKC + casefold)."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable

from gemininest.contracts import (
    AppConfig,
    BlacklistConfig,
    ErrorCode,
    FilterResult,
    SafetyConfig,
    VideoMeta,
)

__all__ = [
    "BlacklistConfig",
    "ErrorCode",
    "FilterResult",
    "MetaFilter",
    "VideoMeta",
    "filter_video",
    "normalize_text",
]


def normalize_text(text: str) -> str:
    """Normalize text via Unicode NFKC + casefold + whitespace collapse.

    Prevents full-width/half-width, case variation, and multi-space bypasses
    (e.g., 'Ｂｌｕｅ　Ａｒｃｈｉｖｅ' -> 'blue archive', '崩坏：星穹铁道' -> '崩坏:星穹铁道').
    """
    if not text:
        return ""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _extract_blacklist(source: AppConfig | SafetyConfig | BlacklistConfig) -> BlacklistConfig:
    if isinstance(source, AppConfig):
        return source.safety_alignment.blacklist
    if isinstance(source, SafetyConfig):
        return source.blacklist
    if isinstance(source, BlacklistConfig):
        return source
    raise TypeError(
        f"Expected AppConfig, SafetyConfig, or BlacklistConfig, got {type(source).__name__}"
    )


def _compile_rules(rules: Iterable[str]) -> tuple[tuple[str, str], ...]:
    compiled: list[tuple[str, str]] = []
    seen: set[str] = set()
    for original in rules:
        norm = normalize_text(original)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        compiled.append((norm, original))
    return tuple(compiled)


class MetaFilter:
    """Three-dimensional pre-download metadata firewall (tname / tags / title)."""

    def __init__(self, config: AppConfig | SafetyConfig | BlacklistConfig) -> None:
        blacklist = _extract_blacklist(config)
        self._tname_rules = _compile_rules(blacklist.tname)
        self._tag_rules = _compile_rules(blacklist.tags)
        self._title_rules = _compile_rules(blacklist.title_keywords)

    def check(self, video: VideoMeta) -> FilterResult:
        """Evaluate a candidate video's tname, tags, and title against blacklist rules."""
        tname_norm = normalize_text(video.tname)
        if tname_norm:
            for rule_norm, rule_raw in self._tname_rules:
                if rule_norm in tname_norm:
                    return FilterResult(
                        bv_id=video.bv_id,
                        allowed=False,
                        reason=(
                            f"{ErrorCode.E3001_BLACKLIST_REJECTED.value}: "
                            f"tname matched rule {rule_raw!r}"
                        ),
                        matched_field="tname",
                        matched_rule=rule_raw,
                    )

        for tag in video.tags:
            tag_norm = normalize_text(tag)
            if not tag_norm:
                continue
            for rule_norm, rule_raw in self._tag_rules:
                if rule_norm in tag_norm:
                    return FilterResult(
                        bv_id=video.bv_id,
                        allowed=False,
                        reason=(
                            f"{ErrorCode.E3001_BLACKLIST_REJECTED.value}: "
                            f"tags matched rule {rule_raw!r}"
                        ),
                        matched_field="tags",
                        matched_rule=rule_raw,
                    )

        title_norm = normalize_text(video.title)
        if title_norm:
            for rule_norm, rule_raw in self._title_rules:
                if rule_norm in title_norm:
                    return FilterResult(
                        bv_id=video.bv_id,
                        allowed=False,
                        reason=(
                            f"{ErrorCode.E3001_BLACKLIST_REJECTED.value}: "
                            f"title matched rule {rule_raw!r}"
                        ),
                        matched_field="title",
                        matched_rule=rule_raw,
                    )

        return FilterResult(
            bv_id=video.bv_id,
            allowed=True,
            reason="",
            matched_field="none",
            matched_rule="",
        )

    evaluate = check

    def check_batch(self, videos: Iterable[VideoMeta]) -> tuple[FilterResult, ...]:
        """Evaluate an iterable of candidate videos and return all FilterResults."""
        return tuple(self.check(video) for video in videos)

    def partition(
        self, videos: Iterable[VideoMeta]
    ) -> tuple[tuple[VideoMeta, ...], tuple[FilterResult, ...]]:
        """Split candidate videos into (allowed_videos, rejected_results)."""
        allowed: list[VideoMeta] = []
        rejected: list[FilterResult] = []
        for video in videos:
            result = self.check(video)
            if result.allowed:
                allowed.append(video)
            else:
                rejected.append(result)
        return tuple(allowed), tuple(rejected)


def filter_video(
    video: VideoMeta,
    config: AppConfig | SafetyConfig | BlacklistConfig,
) -> FilterResult:
    """Functional shortcut to evaluate a single video against a blacklist config."""
    return MetaFilter(config).check(video)
