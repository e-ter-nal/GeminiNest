"""Module 5: Local visual safety gate abstraction (v1 passthrough + v2 wd-tagger ONNX).

v1 (`audio_only`) uses `PassthroughSafetyGate` with zero external ML dependencies.
v2 (`sanitized_video` / `wd_tagger_enabled=True`) plugs in `WdTaggerSafetyGate`
behind the exact same `BaseSafetyGate` contract with lazy ML imports (`onnxruntime`,
`pillow`, `numpy`), dynamic `selected_tags.csv` rating column resolution, automatic
CUDA -> CPU graceful fallback, in-place surgical frame blackout, and >15%整片熔断降级
(`ErrorCode.E5001_SAFETY_GATE_DOWNGRADED`).
"""

from __future__ import annotations

import csv
import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from gemininest.contracts import (
    AppConfig,
    ErrorCode,
    FrameScore,
    GeminiNestError,
    SafetyConfig,
    SafetyGateResult,
)

__all__ = [
    "DEFAULT_WD_TAGGER_INPUT_SIZE",
    "BaseSafetyGate",
    "PassthroughSafetyGate",
    "RatingIndices",
    "WdTaggerSafetyGate",
    "blackout_frame_in_place",
    "create_safety_gate",
    "load_rating_indices",
    "preprocess_wd_tagger_image",
]

logger = logging.getLogger(__name__)

DEFAULT_WD_TAGGER_INPUT_SIZE = 448
UnsafeScorePolicy = Literal["max", "sum"]
WdTaggerDevice = Literal["cpu", "cuda", "dml", "auto"]


@dataclass(frozen=True)
class RatingIndices:
    """Dynamically resolved output-vector indices for wd-tagger rating labels."""

    general: int
    questionable: int
    explicit: int
    sensitive: int | None = None


def _lazy_import_pil_image() -> Any:
    try:
        from PIL import Image

        return Image
    except ImportError as exc:
        raise GeminiNestError(
            ErrorCode.E1001_CONFIG_INVALID,
            "Pillow (`pillow`) is required for v2 visual safety_gate. "
            "Install v2 dependencies via `pip install -r requirements-v2.txt`.",
        ) from exc


def _lazy_import_numpy() -> Any:
    try:
        import numpy as np

        return np
    except ImportError as exc:
        raise GeminiNestError(
            ErrorCode.E1001_CONFIG_INVALID,
            "NumPy (`numpy`) is required for v2 visual safety_gate. "
            "Install v2 dependencies via `pip install -r requirements-v2.txt`.",
        ) from exc


def _lazy_import_onnxruntime() -> Any:
    try:
        import onnxruntime as ort

        return ort
    except ImportError as exc:
        raise GeminiNestError(
            ErrorCode.E1001_CONFIG_INVALID,
            "ONNX Runtime (`onnxruntime` or `onnxruntime-gpu`) is required for v2 wd-tagger inference. "
            "Install v2 dependencies via `pip install -r requirements-v2.txt`.",
        ) from exc


def load_rating_indices(tags_csv_path: Path | str) -> RatingIndices:
    """Dynamically parse `selected_tags.csv` to locate rating indices without hardcoding columns.

    Reads the CSV header via `csv.DictReader` to resolve `name` (and optional `category`).
    When `category` is present, rows with `category == "9"` (wd-tagger rating category)
    take priority so non-rating tags can never shadow rating indices.
    """
    path = Path(tags_csv_path)
    if not path.is_file():
        raise GeminiNestError(
            ErrorCode.E1001_CONFIG_INVALID,
            f"wd-tagger selected_tags.csv not found at {path}. "
            "Run `python scripts/download_wd_tagger.py` to download model weights and tags CSV.",
        )

    rating_map: dict[str, int] = {}
    fallback_map: dict[str, int] = {}
    target_names = {"general", "sensitive", "questionable", "explicit"}

    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise GeminiNestError(
                ErrorCode.E1001_CONFIG_INVALID,
                f"wd-tagger tags CSV has no header row: {path}",
            )
        field_lookup = {name.strip().lower(): name for name in reader.fieldnames if name}
        name_col = field_lookup.get("name")
        category_col = field_lookup.get("category")
        if name_col is None:
            raise GeminiNestError(
                ErrorCode.E1001_CONFIG_INVALID,
                f"wd-tagger tags CSV missing required 'name' column: {path}",
            )

        for idx, row in enumerate(reader):
            tag_name = (row.get(name_col) or "").strip().lower()
            if tag_name not in target_names:
                continue
            category_val = (row.get(category_col) or "").strip() if category_col else ""
            if category_val == "9" and tag_name not in rating_map:
                rating_map[tag_name] = idx
            if tag_name not in fallback_map:
                fallback_map[tag_name] = idx

    resolved = {k: rating_map.get(k, fallback_map.get(k)) for k in target_names}
    missing = [k for k in ("general", "questionable", "explicit") if resolved[k] is None]
    if missing:
        raise GeminiNestError(
            ErrorCode.E1001_CONFIG_INVALID,
            f"wd-tagger tags CSV {path} missing required rating labels: {', '.join(missing)}",
        )

    return RatingIndices(
        general=int(resolved["general"]),  # type: ignore[arg-type]
        questionable=int(resolved["questionable"]),  # type: ignore[arg-type]
        explicit=int(resolved["explicit"]),  # type: ignore[arg-type]
        sensitive=int(resolved["sensitive"]) if resolved["sensitive"] is not None else None,
    )


def preprocess_wd_tagger_image(
    image_path: Path | str,
    *,
    target_size: int = DEFAULT_WD_TAGGER_INPUT_SIZE,
) -> Any:
    """Apply standard SmilingWolf `wd-tagger` preprocessing to a frame image.

    Steps:
    1. Open image via Pillow and composite onto a pure white `(255, 255, 255)` RGB canvas
       so alpha/palette channels never produce dark artifacts.
    2. Aspect-ratio-preserving resize so `max(width, height) == target_size` and center-pad
       onto a `(target_size, target_size)` white RGB square.
    3. Convert to `numpy.float32` in **BGR** channel order (`[0.0, 255.0]` range) with NHWC
       batch layout `(1, target_size, target_size, 3)`.
    """
    Image = _lazy_import_pil_image()
    np = _lazy_import_numpy()

    path = Path(image_path)
    if not path.is_file():
        raise GeminiNestError(
            ErrorCode.E4001_FFMPEG_FAILED,
            f"Frame image not found for wd-tagger preprocessing: {path}",
        )

    size = max(1, int(target_size))
    with Image.open(path) as img:
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            rgb_bg = Image.new("RGB", rgba.size, (255, 255, 255))
            rgb_bg.paste(rgba, mask=rgba.split()[3])
            rgb_img = rgb_bg
        else:
            rgb_img = img.convert("RGB")

        width, height = rgb_img.size
        max_dim = max(width, height, 1)
        scale = size / float(max_dim)
        new_w = max(1, min(size, int(round(width * scale))))
        new_h = max(1, min(size, int(round(height * scale))))

        resample = getattr(getattr(Image, "Resampling", Image), "BICUBIC")
        resized = rgb_img.resize((new_w, new_h), resample=resample)

        canvas = Image.new("RGB", (size, size), (255, 255, 255))
        offset_x = (size - new_w) // 2
        offset_y = (size - new_h) // 2
        canvas.paste(resized, (offset_x, offset_y))

        rgb_arr = np.asarray(canvas, dtype=np.float32)

    # RGB -> BGR float32 NHWC (1, H, W, 3)
    bgr_arr = np.ascontiguousarray(rgb_arr[:, :, ::-1], dtype=np.float32)
    return np.expand_dims(bgr_arr, axis=0)


def blackout_frame_in_place(image_path: Path | str) -> tuple[int, int]:
    """Replace an unsafe frame image in-place with an identical-dimension pure black `(0,0,0)` frame."""
    Image = _lazy_import_pil_image()
    path = Path(image_path)
    if not path.is_file():
        raise GeminiNestError(
            ErrorCode.E4001_FFMPEG_FAILED,
            f"Frame image not found for surgical blackout: {path}",
        )

    with Image.open(path) as img:
        width, height = img.size
        img_format = img.format

    black_img = Image.new("RGB", (width, height), (0, 0, 0))
    save_kwargs: dict[str, Any] = {}
    if img_format:
        save_kwargs["format"] = img_format
    if (img_format or "").upper() in ("JPEG", "JPG") or path.suffix.lower() in (".jpg", ".jpeg"):
        save_kwargs["quality"] = 95
        save_kwargs["subsampling"] = 0
    black_img.save(path, **save_kwargs)
    return width, height


class BaseSafetyGate(ABC):
    """Hot-swappable contract for local visual frame safety inspection."""

    @abstractmethod
    def inspect_frames(
        self,
        frame_paths: list[Path],
        cfg: SafetyConfig,
    ) -> SafetyGateResult:
        """Inspect extracted video frames and return sanitized frame metadata.

        Args:
            frame_paths: Ordered list of extracted frame image paths.
            cfg: Safety alignment thresholds and policy configuration.

        Returns:
            Immutable `SafetyGateResult` describing pass/blackout/downgrade decisions.
        """


class PassthroughSafetyGate(BaseSafetyGate):
    """v1 no-op passthrough stub that approves all frames without visual inference.

    In v1 (`audio_only`) mode, frame extraction is not triggered; if called directly
    (for example by contract tests or dry-run scaffolding), this stub unconditionally
    returns `passed=True, downgrade_to_audio=False, engine="v1_passthrough"`.
    """

    def inspect_frames(
        self,
        frame_paths: list[Path],
        cfg: SafetyConfig,
    ) -> SafetyGateResult:
        del cfg  # Unused in v1 passthrough mode.
        safe_paths = tuple(Path(frame_path) for frame_path in frame_paths)
        frame_scores = tuple(
            FrameScore(
                frame_index=idx,
                general_score=1.0,
                unsafe_score=0.0,
                blacked_out=False,
            )
            for idx in range(len(safe_paths))
        )
        return SafetyGateResult(
            passed=True,
            safe_frame_paths=safe_paths,
            blackout_indices=(),
            total_frames=len(safe_paths),
            blackout_ratio=0.0,
            downgrade_to_audio=False,
            frame_scores=frame_scores,
            engine="v1_passthrough",
        )


def _coerce_rating_indices(value: RatingIndices | Mapping[str, int]) -> RatingIndices:
    if isinstance(value, RatingIndices):
        return value
    return RatingIndices(
        general=int(value["general"]),
        questionable=int(value["questionable"]),
        explicit=int(value["explicit"]),
        sensitive=int(value["sensitive"]) if value.get("sensitive") is not None else None,
    )


class WdTaggerSafetyGate(BaseSafetyGate):
    """v2 local visual safety gate powered by ONNX `wd-tagger`.

    Unsafe-score policy (`unsafe_policy`):
    - `"max"` (default): `unsafe_score = max(questionable, explicit)`.
      A frame is surgically replaced in-place with a pure black `(0, 0, 0)` image when
      `general_score < cfg.min_general_score` OR `max(questionable, explicit) > cfg.max_unsafe_score`.
    - `"sum"`: `unsafe_score = min(1.0, questionable + explicit)`.
      Stricter combined probability budget when `general_score < cfg.min_general_score` OR
      `(questionable + explicit) > cfg.max_unsafe_score`.

    Whole-video downgrade circuit breaker:
    - Computes `blackout_ratio = len(blackout_indices) / total_frames`.
    - If `blackout_ratio > cfg.max_blackout_ratio` (default `0.15`) and
      `cfg.downgrade_to_audio_on_exceed=True`, emits `ErrorCode.E5001_SAFETY_GATE_DOWNGRADED`
      semantics and returns `downgrade_to_audio=True` so `media_processor` burns all frames
      and falls back to pure `48kbps` mono audio.

    Device fallback:
    - When `wd_tagger_device` is `"auto"` or `"dml"`, tries `DmlExecutionProvider` first,
      then falls back gracefully to `CUDAExecutionProvider` and finally `CPUExecutionProvider`.
    - When `wd_tagger_device == "cuda"` and CUDA/cuDNN DLLs are unavailable on the host,
      logs a warning and gracefully falls back to `CPUExecutionProvider`.
    """

    def __init__(
        self,
        safety_cfg: SafetyConfig | None = None,
        *,
        model_path: Path | str | None = None,
        tags_csv_path: Path | str | None = None,
        device: WdTaggerDevice | None = None,
        session: Any = None,
        ort_module: Any = None,
        rating_indices: RatingIndices | Mapping[str, int] | None = None,
        input_size: int | None = None,
        unsafe_policy: UnsafeScorePolicy = "max",
    ) -> None:
        if unsafe_policy not in ("max", "sum"):
            raise ValueError(f"Invalid unsafe_policy {unsafe_policy!r}; expected 'max' or 'sum'.")
        self._default_cfg = safety_cfg
        self._explicit_model_path = Path(model_path) if model_path is not None else None
        self._explicit_tags_csv_path = Path(tags_csv_path) if tags_csv_path is not None else None
        self._explicit_device = device
        self._session = session
        self._ort_module = ort_module
        self._rating_indices: RatingIndices | None = (
            _coerce_rating_indices(rating_indices) if rating_indices is not None else None
        )
        self._input_size = input_size
        self._unsafe_policy: UnsafeScorePolicy = unsafe_policy
        self._active_provider: str | None = None
        self._fallback_used: bool = False

    @property
    def unsafe_policy(self) -> UnsafeScorePolicy:
        return self._unsafe_policy

    @property
    def active_provider(self) -> str | None:
        return self._active_provider

    @property
    def fallback_used(self) -> bool:
        return self._fallback_used

    def _resolve_model_path(self, cfg: SafetyConfig) -> Path:
        candidate = self._explicit_model_path or Path(cfg.wd_tagger_model_path)
        if candidate.is_file():
            return candidate
        # Support default HuggingFace asset filename `model.onnx` in the same directory
        alt_model = candidate.with_name("model.onnx")
        if alt_model.is_file():
            return alt_model
        return candidate

    def _resolve_tags_csv_path(self, resolved_model_path: Path) -> Path:
        if self._explicit_tags_csv_path is not None:
            return self._explicit_tags_csv_path
        return resolved_model_path.with_name("selected_tags.csv")

    def _create_onnx_session(self, model_path: Path, requested_device: WdTaggerDevice) -> Any:
        if not model_path.is_file():
            raise GeminiNestError(
                ErrorCode.E1001_CONFIG_INVALID,
                f"wd-tagger ONNX model file not found at {model_path}. "
                "Run `python scripts/download_wd_tagger.py` to download model weights.",
            )

        ort = self._ort_module if self._ort_module is not None else _lazy_import_onnxruntime()
        available_providers = list(ort.get_available_providers()) if hasattr(ort, "get_available_providers") else []

        if requested_device == "cpu":
            sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
            self._active_provider = "CPUExecutionProvider"
            self._fallback_used = False
            return sess

        if requested_device in ("auto", "dml"):
            candidate_providers = ("DmlExecutionProvider", "CUDAExecutionProvider")
        else:
            candidate_providers = ("CUDAExecutionProvider",)

        primary_provider = candidate_providers[0]
        last_cpu_sess: Any = None

        for provider in candidate_providers:
            if provider not in available_providers:
                logger.warning(
                    "wd-tagger device configured as %r, but %s is unavailable (available=%s); "
                    "checking next provider fallback.",
                    requested_device,
                    provider,
                    available_providers,
                )
                continue

            try:
                sess = ort.InferenceSession(
                    str(model_path),
                    providers=[provider, "CPUExecutionProvider"],
                )
                sess_providers = (
                    list(sess.get_providers()) if hasattr(sess, "get_providers") else [provider]
                )
                if provider in sess_providers:
                    self._active_provider = provider
                    self._fallback_used = provider != primary_provider
                    return sess
                logger.warning(
                    "wd-tagger requested %s, but active session providers are %s; "
                    "checking next provider fallback.",
                    provider,
                    sess_providers,
                )
                last_cpu_sess = sess
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "wd-tagger failed to initialize %s (%s: %s); falling back gracefully.",
                    provider,
                    exc.__class__.__name__,
                    exc,
                )

        if last_cpu_sess is not None:
            self._active_provider = "CPUExecutionProvider"
            self._fallback_used = True
            return last_cpu_sess

        logger.warning(
            "wd-tagger device=%r fell back gracefully to CPUExecutionProvider.",
            requested_device,
        )
        sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._active_provider = "CPUExecutionProvider"
        self._fallback_used = True
        return sess

    def ensure_loaded(self, cfg: SafetyConfig | None = None) -> tuple[Any, RatingIndices, int]:
        """Lazily initialize ONNX session, rating indices, and input spatial dimension."""
        eff_cfg = cfg or self._default_cfg
        if eff_cfg is None:
            raise GeminiNestError(
                ErrorCode.E1001_CONFIG_INVALID,
                "SafetyConfig is required to initialize WdTaggerSafetyGate.",
            )

        resolved_model = self._resolve_model_path(eff_cfg)

        if self._session is None:
            req_device = self._explicit_device or eff_cfg.wd_tagger_device
            self._session = self._create_onnx_session(resolved_model, req_device)
        elif self._active_provider is None and hasattr(self._session, "get_providers"):
            providers = list(self._session.get_providers())
            self._active_provider = providers[0] if providers else "InjectedSession"

        if self._rating_indices is None:
            tags_path = self._resolve_tags_csv_path(resolved_model)
            self._rating_indices = load_rating_indices(tags_path)

        target_size = self._resolve_input_size(self._session)
        return self._session, self._rating_indices, target_size

    def _resolve_input_size(self, session: Any) -> int:
        if self._input_size is not None and self._input_size > 0:
            return int(self._input_size)
        try:
            inputs = session.get_inputs()
            if inputs:
                shape = getattr(inputs[0], "shape", None)
                if shape and len(shape) >= 3:
                    h_dim = shape[1]
                    if isinstance(h_dim, int) and h_dim > 0:
                        return h_dim
        except Exception:  # noqa: BLE001
            pass
        return DEFAULT_WD_TAGGER_INPUT_SIZE

    def _compute_unsafe_score(self, questionable: float, explicit: float) -> float:
        if self._unsafe_policy == "sum":
            return min(1.0, max(0.0, questionable + explicit))
        return max(questionable, explicit)

    def _infer_frame_probs(self, session: Any, tensor: Any) -> Any:
        input_name = "input_1:0"
        output_names: list[str] | None = None
        if hasattr(session, "get_inputs"):
            inputs = session.get_inputs()
            if inputs and hasattr(inputs[0], "name"):
                input_name = str(inputs[0].name)
        if hasattr(session, "get_outputs"):
            outputs = session.get_outputs()
            if outputs and hasattr(outputs[0], "name"):
                output_names = [str(outputs[0].name)]

        raw_out = session.run(output_names, {input_name: tensor})
        probs = raw_out[0]
        # Flatten batch dimension (1, N) -> (N,)
        if hasattr(probs, "ndim") and probs.ndim > 1:
            return probs[0]
        if isinstance(probs, (list, tuple)) and probs and isinstance(probs[0], (list, tuple)):
            return probs[0]
        return probs

    def inspect_frames(
        self,
        frame_paths: list[Path],
        cfg: SafetyConfig,
    ) -> SafetyGateResult:
        """Inspect each extracted frame with wd-tagger, black out unsafe frames, or downgrade video."""
        paths = [Path(p) for p in frame_paths]
        total_frames = len(paths)
        if total_frames == 0:
            return SafetyGateResult(
                passed=True,
                safe_frame_paths=(),
                blackout_indices=(),
                total_frames=0,
                blackout_ratio=0.0,
                downgrade_to_audio=False,
                frame_scores=(),
                engine="v2_wd_tagger",
            )

        session, rating_indices, target_size = self.ensure_loaded(cfg)

        frame_scores: list[FrameScore] = []
        blackout_indices: list[int] = []
        required_indices = [
            rating_indices.general,
            rating_indices.questionable,
            rating_indices.explicit,
        ]
        if rating_indices.sensitive is not None:
            required_indices.append(rating_indices.sensitive)
        max_required_idx = max(required_indices)

        for idx, frame_path in enumerate(paths):
            tensor = preprocess_wd_tagger_image(frame_path, target_size=target_size)
            probs = self._infer_frame_probs(session, tensor)
            if len(probs) <= max_required_idx:
                raise GeminiNestError(
                    ErrorCode.E1001_CONFIG_INVALID,
                    f"wd-tagger output length ({len(probs)}) is smaller than required rating index "
                    f"{max_required_idx}; verify model.onnx matches selected_tags.csv.",
                )

            general_score = float(probs[rating_indices.general])
            sensitive_score = (
                float(probs[rating_indices.sensitive])
                if rating_indices.sensitive is not None
                else 0.0
            )
            questionable_score = float(probs[rating_indices.questionable])
            explicit_score = float(probs[rating_indices.explicit])
            unsafe_score = self._compute_unsafe_score(questionable_score, explicit_score)

            is_unsafe = (
                general_score < cfg.min_general_score
                or sensitive_score > cfg.max_sensitive_score
                or unsafe_score > cfg.max_unsafe_score
            )
            frame_scores.append(
                FrameScore(
                    frame_index=idx,
                    general_score=round(general_score, 6),
                    unsafe_score=round(unsafe_score, 6),
                    blacked_out=is_unsafe,
                    sensitive_score=round(sensitive_score, 6),
                )
            )
            if is_unsafe:
                blackout_indices.append(idx)
                current_ratio = len(blackout_indices) / float(total_frames)
                if cfg.downgrade_to_audio_on_exceed and current_ratio > cfg.max_blackout_ratio:
                    logger.warning(
                        "[%s] Early-exit triggered at frame %d/%d: blackout ratio %.3f "
                        "(%d/%d frames) exceeded max_blackout_ratio=%.3f; skipping remaining "
                        "%d frame(s) and downgrading entire video to pure audio.",
                        ErrorCode.E5001_SAFETY_GATE_DOWNGRADED.value,
                        idx + 1,
                        total_frames,
                        current_ratio,
                        len(blackout_indices),
                        total_frames,
                        cfg.max_blackout_ratio,
                        total_frames - (idx + 1),
                    )
                    return SafetyGateResult(
                        passed=False,
                        safe_frame_paths=(),
                        blackout_indices=tuple(blackout_indices),
                        total_frames=total_frames,
                        blackout_ratio=round(current_ratio, 6),
                        downgrade_to_audio=True,
                        frame_scores=tuple(frame_scores),
                        engine="v2_wd_tagger",
                    )
                blackout_frame_in_place(frame_path)

        blackout_ratio = len(blackout_indices) / float(total_frames)
        exceeded = blackout_ratio > cfg.max_blackout_ratio
        downgrade = bool(exceeded and cfg.downgrade_to_audio_on_exceed)

        if downgrade:
            logger.warning(
                "[%s] Video blackout ratio %.3f (%d/%d frames) exceeded max_blackout_ratio=%.3f; "
                "downgrading entire video to pure audio.",
                ErrorCode.E5001_SAFETY_GATE_DOWNGRADED.value,
                blackout_ratio,
                len(blackout_indices),
                total_frames,
                cfg.max_blackout_ratio,
            )
            return SafetyGateResult(
                passed=False,
                safe_frame_paths=(),
                blackout_indices=tuple(blackout_indices),
                total_frames=total_frames,
                blackout_ratio=round(blackout_ratio, 6),
                downgrade_to_audio=True,
                frame_scores=tuple(frame_scores),
                engine="v2_wd_tagger",
            )

        return SafetyGateResult(
            passed=not exceeded,
            safe_frame_paths=tuple(paths),
            blackout_indices=tuple(blackout_indices),
            total_frames=total_frames,
            blackout_ratio=round(blackout_ratio, 6),
            downgrade_to_audio=False,
            frame_scores=tuple(frame_scores),
            engine="v2_wd_tagger",
        )


def create_safety_gate(
    cfg: SafetyConfig | AppConfig,
    *,
    session: Any = None,
    ort_module: Any = None,
    tags_csv_path: Path | str | None = None,
    rating_indices: RatingIndices | Mapping[str, int] | None = None,
    unsafe_policy: UnsafeScorePolicy = "max",
) -> BaseSafetyGate:
    """Create the configured safety gate implementation (v1 passthrough / v2 wd-tagger ONNX).

    Returns `WdTaggerSafetyGate` when `pipeline_mode == "sanitized_video"` (for `AppConfig`)
    or `wd_tagger_enabled == True`, and `PassthroughSafetyGate` otherwise.
    Supports injecting a custom inference `session` or mock `ort_module` for offline tests.
    """
    if isinstance(cfg, AppConfig):
        safety_cfg = cfg.safety_alignment
        use_v2 = (
            cfg.project.pipeline_mode == "sanitized_video"
            or safety_cfg.wd_tagger_enabled
        )
    else:
        safety_cfg = cfg
        use_v2 = safety_cfg.wd_tagger_enabled

    if use_v2:
        return WdTaggerSafetyGate(
            safety_cfg,
            tags_csv_path=tags_csv_path,
            session=session,
            ort_module=ort_module,
            rating_indices=rating_indices,
            unsafe_policy=unsafe_policy,
        )
    return PassthroughSafetyGate()
