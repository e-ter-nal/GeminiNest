"""Offline unit tests for Module 5: gemininest.safety_gate (v1 passthrough + v2 wd-tagger)."""

from __future__ import annotations

import logging
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from gemininest.config import load_config
from gemininest.contracts import ErrorCode, FrameScore, SafetyGateResult
from gemininest.safety_gate import (
    BaseSafetyGate,
    PassthroughSafetyGate,
    RatingIndices,
    WdTaggerSafetyGate,
    create_safety_gate,
    load_rating_indices,
    preprocess_wd_tagger_image,
)

REPO_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"


def _load_v1_config():
    return load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)


def _write_color_png(path: Path, size: tuple[int, int] = (160, 90), color: tuple[int, int, int] = (200, 80, 40)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color)
    img.save(path, format="PNG")
    return path


def _write_sample_tags_csv(path: Path) -> Path:
    """Create a shuffled-column selected_tags.csv with non-zero rating row indices."""
    lines = [
        "count,category,name,tag_id",
        "10000,0,1girl,100",
        "9000,0,blue_sky,101",
        "5000,9,general,0",
        "4000,0,scenery,102",
        "3000,9,sensitive,1",
        "2000,9,questionable,2",
        "1000,9,explicit,3",
        "500,0,cat,103",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class _MockOnnxSession:
    """Mock ONNX InferenceSession returning caller-supplied probability vectors per call."""

    def __init__(
        self,
        outputs_per_frame: list[list[float]],
        *,
        input_size: int = 448,
        providers: list[str] | None = None,
    ) -> None:
        self._outputs = outputs_per_frame
        self._input_size = input_size
        self._providers = providers or ["CPUExecutionProvider"]
        self.received_shapes: list[tuple[int, ...]] = []
        self.received_dtypes: list[Any] = []
        self._idx = 0

    def get_providers(self) -> list[str]:
        return list(self._providers)

    def get_inputs(self) -> list[Any]:
        return [
            type(
                "InputMeta",
                (),
                {"name": "input_1:0", "shape": [None, self._input_size, self._input_size, 3]},
            )()
        ]

    def get_outputs(self) -> list[Any]:
        return [type("OutputMeta", (), {"name": "predictions_sigmoid"})()]

    def run(self, output_names: Any, feed_dict: dict[str, Any]) -> list[np.ndarray]:
        del output_names
        tensor = next(iter(feed_dict.values()))
        self.received_shapes.append(tuple(tensor.shape))
        self.received_dtypes.append(tensor.dtype)
        probs = self._outputs[self._idx]
        self._idx += 1
        return [np.asarray([probs], dtype=np.float32)]


def test_base_safety_gate_is_abstract() -> None:
    with pytest.raises(TypeError):
        BaseSafetyGate()  # type: ignore[abstract]


def test_passthrough_safety_gate_empty_frames() -> None:
    cfg = _load_v1_config()
    gate = PassthroughSafetyGate()

    result = gate.inspect_frames([], cfg.safety_alignment)

    assert isinstance(result, SafetyGateResult)
    assert result.passed is True
    assert result.downgrade_to_audio is False
    assert result.engine == "v1_passthrough"
    assert result.total_frames == 0
    assert result.blackout_ratio == 0.0
    assert result.blackout_indices == ()
    assert result.safe_frame_paths == ()
    assert result.frame_scores == ()

    with pytest.raises(FrozenInstanceError):
        result.passed = False  # type: ignore[misc]


def test_passthrough_safety_gate_approves_all_frames(tmp_path: Path) -> None:
    cfg = _load_v1_config()
    gate = PassthroughSafetyGate()
    frames = [tmp_path / f"frame_{i:04d}.jpg" for i in range(3)]

    result = gate.inspect_frames(frames, cfg.safety_alignment)

    assert result.passed is True
    assert result.downgrade_to_audio is False
    assert result.engine == "v1_passthrough"
    assert result.total_frames == 3
    assert result.blackout_ratio == 0.0
    assert result.blackout_indices == ()
    assert result.safe_frame_paths == tuple(frames)
    assert result.frame_scores == (
        FrameScore(frame_index=0, general_score=1.0, unsafe_score=0.0, blacked_out=False),
        FrameScore(frame_index=1, general_score=1.0, unsafe_score=0.0, blacked_out=False),
        FrameScore(frame_index=2, general_score=1.0, unsafe_score=0.0, blacked_out=False),
    )


def test_create_safety_gate_returns_v1_passthrough_for_app_and_safety_config() -> None:
    cfg = _load_v1_config()
    assert cfg.safety_alignment.wd_tagger_enabled is False

    gate_from_app = create_safety_gate(cfg)
    gate_from_safety = create_safety_gate(cfg.safety_alignment)

    assert isinstance(gate_from_app, PassthroughSafetyGate)
    assert isinstance(gate_from_safety, PassthroughSafetyGate)


def test_create_safety_gate_returns_wd_tagger_for_v2_mode_or_flag() -> None:
    cfg = _load_v1_config()

    v2_safety_cfg = replace(cfg.safety_alignment, wd_tagger_enabled=True)
    gate_from_flag = create_safety_gate(v2_safety_cfg)
    assert isinstance(gate_from_flag, WdTaggerSafetyGate)

    v2_app_cfg = replace(cfg, project=replace(cfg.project, pipeline_mode="sanitized_video"))
    gate_from_pipeline_mode = create_safety_gate(v2_app_cfg)
    assert isinstance(gate_from_pipeline_mode, WdTaggerSafetyGate)


def test_load_rating_indices_dynamic_csv_parsing(tmp_path: Path) -> None:
    csv_path = _write_sample_tags_csv(tmp_path / "selected_tags.csv")
    indices = load_rating_indices(csv_path)

    # Shuffled columns, non-zero row indices -> resolved purely by header + category/name
    assert indices == RatingIndices(general=2, sensitive=4, questionable=5, explicit=6)


def test_preprocess_wd_tagger_image_white_padding_and_bgr_float32_nhwc(tmp_path: Path) -> None:
    # Wide red image (200x100) -> scaled to 448x224, centered on 448x448 white canvas
    img_path = _write_color_png(tmp_path / "wide_red.png", size=(200, 100), color=(255, 0, 0))
    tensor = preprocess_wd_tagger_image(img_path, target_size=448)

    assert tensor.shape == (1, 448, 448, 3)
    assert tensor.dtype == np.float32

    # Top-left padding region must be pure white (255, 255, 255)
    assert np.allclose(tensor[0, 5, 5], [255.0, 255.0, 255.0])
    # Center pixel of red RGB (255, 0, 0) must be BGR (0, 0, 255)
    assert np.allclose(tensor[0, 224, 224], [0.0, 0.0, 255.0], atol=2.0)


def test_wd_tagger_all_green_pass(tmp_path: Path) -> None:
    cfg = _load_v1_config()
    v2_safety = replace(cfg.safety_alignment, wd_tagger_enabled=True)
    csv_path = _write_sample_tags_csv(tmp_path / "selected_tags.csv")

    frames = [_write_color_png(tmp_path / f"frame_{i:06d}.png", color=(20, 180, 90)) for i in range(4)]
    # Output length 8 matching sample CSV indices (general=2, sensitive=4, questionable=5, explicit=6)
    safe_vec = [0.1, 0.2, 0.98, 0.1, 0.01, 0.005, 0.001, 0.1]
    mock_sess = _MockOnnxSession([safe_vec] * 4)

    gate = create_safety_gate(v2_safety, session=mock_sess, tags_csv_path=csv_path)
    result = gate.inspect_frames(frames, v2_safety)

    assert result.passed is True
    assert result.downgrade_to_audio is False
    assert result.engine == "v2_wd_tagger"
    assert result.total_frames == 4
    assert result.blackout_indices == ()
    assert result.blackout_ratio == 0.0
    assert result.safe_frame_paths == tuple(frames)
    assert mock_sess.received_shapes == [(1, 448, 448, 3)] * 4
    assert all(dt == np.float32 for dt in mock_sess.received_dtypes)

    # Pixels remain original green color
    with Image.open(frames[0]) as img:
        assert img.getpixel((10, 10)) == (20, 180, 90)


def test_wd_tagger_single_frame_surgical_blackout_exact_zero_pixels(tmp_path: Path) -> None:
    cfg = _load_v1_config()
    v2_safety = replace(cfg.safety_alignment, wd_tagger_enabled=True, max_blackout_ratio=0.15)
    csv_path = _write_sample_tags_csv(tmp_path / "selected_tags.csv")

    # 8 frames total: 1 unsafe frame -> 1/8 = 12.5% <= 15% (passes with surgical blackout)
    frames = [_write_color_png(tmp_path / f"frame_{i:06d}.png", size=(120, 80), color=(40, 120, 220)) for i in range(8)]
    safe_vec = [0.0, 0.0, 0.97, 0.0, 0.02, 0.005, 0.001, 0.0]
    unsafe_vec = [0.0, 0.0, 0.35, 0.0, 0.20, 0.40, 0.05, 0.0]
    vectors = [safe_vec] * 8
    vectors[3] = unsafe_vec

    gate = WdTaggerSafetyGate(v2_safety, session=_MockOnnxSession(vectors), tags_csv_path=csv_path)
    result = gate.inspect_frames(frames, v2_safety)

    assert result.passed is True
    assert result.downgrade_to_audio is False
    assert result.engine == "v2_wd_tagger"
    assert result.total_frames == 8
    assert result.blackout_indices == (3,)
    assert result.blackout_ratio == pytest.approx(0.125)
    assert result.frame_scores[3].blacked_out is True
    assert result.frame_scores[0].blacked_out is False

    # Frame 3 must be replaced in-place with identical dimensions and pure (0, 0, 0) pixels
    with Image.open(frames[3]) as black_img:
        assert black_img.size == (120, 80)
        arr = np.asarray(black_img)
        assert arr.shape == (80, 120, 3)
        assert int(arr.max()) == 0
        assert int(arr.min()) == 0

    # Neighboring safe frame 2 must remain completely untouched
    with Image.open(frames[2]) as safe_img:
        assert safe_img.size == (120, 80)
        assert safe_img.getpixel((10, 10)) == (40, 120, 220)


def test_wd_tagger_unsafe_policy_max_vs_sum(tmp_path: Path) -> None:
    cfg = _load_v1_config()
    v2_safety = replace(cfg.safety_alignment, wd_tagger_enabled=True, min_general_score=0.90, max_unsafe_score=0.05)
    # general=0.93 (>=0.90), questionable=0.03, explicit=0.03 -> max=0.03 (pass), sum=0.06 (blackout)
    borderline = [0.93, 0.01, 0.03, 0.03]
    indices = RatingIndices(general=0, sensitive=1, questionable=2, explicit=3)

    f_max = _write_color_png(tmp_path / "max.png", color=(100, 100, 100))
    gate_max = WdTaggerSafetyGate(
        v2_safety,
        session=_MockOnnxSession([borderline]),
        rating_indices=indices,
        unsafe_policy="max",
    )
    res_max = gate_max.inspect_frames([f_max], v2_safety)
    assert res_max.blackout_indices == ()
    assert res_max.frame_scores[0].unsafe_score == pytest.approx(0.03)

    f_sum = _write_color_png(tmp_path / "sum.png", color=(100, 100, 100))
    gate_sum = WdTaggerSafetyGate(
        v2_safety,
        session=_MockOnnxSession([borderline]),
        rating_indices=indices,
        unsafe_policy="sum",
    )
    res_sum = gate_sum.inspect_frames([f_sum], v2_safety)
    assert res_sum.blackout_indices == (0,)
    assert res_sum.frame_scores[0].unsafe_score == pytest.approx(0.06)
    assert res_sum.downgrade_to_audio is True


def test_wd_tagger_blackout_ratio_over_15_percent_triggers_downgrade(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _load_v1_config()
    v2_safety = replace(
        cfg.safety_alignment,
        wd_tagger_enabled=True,
        max_blackout_ratio=0.15,
        downgrade_to_audio_on_exceed=True,
    )
    indices = RatingIndices(general=0, sensitive=1, questionable=2, explicit=3)
    frames = [_write_color_png(tmp_path / f"f_{i}.png") for i in range(5)]

    # 1/5 = 20% > 15% -> triggers E5001 downgrade_to_audio=True
    safe_vec = [0.98, 0.01, 0.005, 0.001]
    unsafe_vec = [0.20, 0.10, 0.50, 0.20]
    gate = WdTaggerSafetyGate(
        v2_safety,
        session=_MockOnnxSession([safe_vec, unsafe_vec, safe_vec, safe_vec, safe_vec]),
        rating_indices=indices,
    )

    with caplog.at_level(logging.WARNING):
        result = gate.inspect_frames(frames, v2_safety)

    assert result.passed is False
    assert result.downgrade_to_audio is True
    assert result.safe_frame_paths == ()
    assert result.blackout_indices == (1,)
    assert result.blackout_ratio == pytest.approx(0.20)
    assert ErrorCode.E5001_SAFETY_GATE_DOWNGRADED.value in caplog.text


def test_wd_tagger_cuda_unavailable_gracefully_falls_back_to_cpu(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _load_v1_config()
    cuda_safety = replace(
        cfg.safety_alignment,
        wd_tagger_enabled=True,
        wd_tagger_device="cuda",
        wd_tagger_model_path=str(tmp_path / "model.onnx"),
    )
    (tmp_path / "model.onnx").write_bytes(b"mock-onnx-weights")
    _write_sample_tags_csv(tmp_path / "selected_tags.csv")
    frame = _write_color_png(tmp_path / "frame.png")
    safe_vec = [0.0, 0.0, 0.99, 0.0, 0.005, 0.002, 0.001, 0.0]

    # Case 1: CUDAExecutionProvider is not listed in get_available_providers()
    created_providers_1: list[list[str]] = []

    class _CpuOnlyOrt:
        @staticmethod
        def get_available_providers() -> list[str]:
            return ["CPUExecutionProvider"]

        @staticmethod
        def InferenceSession(model_path: str, providers: list[str]) -> _MockOnnxSession:  # noqa: N802
            del model_path
            created_providers_1.append(list(providers))
            return _MockOnnxSession([safe_vec], providers=providers)

    gate1 = WdTaggerSafetyGate(cuda_safety, ort_module=_CpuOnlyOrt())
    with caplog.at_level(logging.WARNING):
        res1 = gate1.inspect_frames([frame], cuda_safety)

    assert res1.passed is True
    assert gate1.active_provider == "CPUExecutionProvider"
    assert gate1.fallback_used is True
    assert created_providers_1 == [["CPUExecutionProvider"]]
    assert "CPUExecutionProvider" in caplog.text

    # Case 2: CUDA listed in providers, but InferenceSession raises missing cuDNN DLL error
    caplog.clear()
    created_providers_2: list[list[str]] = []

    class _BrokenCudaDllOrt:
        @staticmethod
        def get_available_providers() -> list[str]:
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]

        @staticmethod
        def InferenceSession(model_path: str, providers: list[str]) -> _MockOnnxSession:  # noqa: N802
            del model_path
            created_providers_2.append(list(providers))
            if "CUDAExecutionProvider" in providers:
                raise RuntimeError("LoadLibrary failed: cudnn64_9.dll not found")
            return _MockOnnxSession([safe_vec], providers=providers)

    gate2 = WdTaggerSafetyGate(cuda_safety, ort_module=_BrokenCudaDllOrt())
    with caplog.at_level(logging.WARNING):
        res2 = gate2.inspect_frames([frame], cuda_safety)

    assert res2.passed is True
    assert gate2.active_provider == "CPUExecutionProvider"
    assert gate2.fallback_used is True
    assert created_providers_2 == [
        ["CUDAExecutionProvider", "CPUExecutionProvider"],
        ["CPUExecutionProvider"],
    ]
    assert "cudnn64_9.dll" in caplog.text


def test_wd_tagger_max_sensitive_score_guardrail(tmp_path: Path) -> None:
    cfg = _load_v1_config()
    v2_safety = replace(
        cfg.safety_alignment,
        wd_tagger_enabled=True,
        min_general_score=0.20,
        max_sensitive_score=0.75,
        max_unsafe_score=0.05,
        max_blackout_ratio=0.50,
    )
    indices = RatingIndices(general=0, sensitive=1, questionable=2, explicit=3)
    frames = [_write_color_png(tmp_path / f"sens_{i}.png", color=(80, 140, 200)) for i in range(2)]

    # Frame 0: real-world safe frame (general=0.28, sensitive=0.70 <= 0.75, questionable/explicit tiny) -> PASS
    # Frame 1: high-sensitive frame (general=0.21, sensitive=0.78 > 0.75, questionable/explicit tiny) -> BLACKOUT
    vectors = [
        [0.28, 0.70, 0.01, 0.005],
        [0.21, 0.78, 0.01, 0.005],
    ]
    gate = WdTaggerSafetyGate(
        v2_safety,
        session=_MockOnnxSession(vectors),
        rating_indices=indices,
    )
    result = gate.inspect_frames(frames, v2_safety)

    assert result.passed is True
    assert result.downgrade_to_audio is False
    assert result.blackout_indices == (1,)
    assert result.frame_scores[0].blacked_out is False
    assert result.frame_scores[0].sensitive_score == pytest.approx(0.70)
    assert result.frame_scores[1].blacked_out is True
    assert result.frame_scores[1].sensitive_score == pytest.approx(0.78)


def test_wd_tagger_early_exit_stops_remaining_inference(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _load_v1_config()
    v2_safety = replace(
        cfg.safety_alignment,
        wd_tagger_enabled=True,
        max_blackout_ratio=0.15,
        downgrade_to_audio_on_exceed=True,
    )
    indices = RatingIndices(general=0, sensitive=1, questionable=2, explicit=3)
    # 10 frames -> budget is 1.5 frames; 2nd unsafe frame (2/10 = 20% > 15%) must trigger immediate early-exit
    frames = [_write_color_png(tmp_path / f"early_{i:02d}.png", color=(30, 160, 90)) for i in range(10)]
    safe_vec = [0.95, 0.03, 0.005, 0.001]
    unsafe_vec = [0.10, 0.15, 0.60, 0.15]
    vectors = [safe_vec, unsafe_vec, unsafe_vec] + [safe_vec] * 7
    mock_sess = _MockOnnxSession(vectors)

    gate = WdTaggerSafetyGate(
        v2_safety,
        session=mock_sess,
        rating_indices=indices,
    )
    with caplog.at_level(logging.WARNING):
        result = gate.inspect_frames(frames, v2_safety)

    assert result.passed is False
    assert result.downgrade_to_audio is True
    assert result.safe_frame_paths == ()
    assert result.total_frames == 10
    assert result.blackout_indices == (1, 2)
    assert result.blackout_ratio == pytest.approx(0.20)
    # Only frames 0, 1, 2 were inferred; frames 3..9 were completely skipped!
    assert len(mock_sess.received_shapes) == 3
    assert len(result.frame_scores) == 3
    assert ErrorCode.E5001_SAFETY_GATE_DOWNGRADED.value in caplog.text
    # Triggering frame 2 skipped in-place disk blackout because whole video is immediately downgraded
    with Image.open(frames[2]) as img:
        assert img.getpixel((10, 10)) == (30, 160, 90)


def test_wd_tagger_dml_and_auto_provider_routing_and_fallback(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _load_v1_config()
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"mock-onnx-weights")
    _write_sample_tags_csv(tmp_path / "selected_tags.csv")
    frame = _write_color_png(tmp_path / "frame.png")
    safe_vec = [0.0, 0.0, 0.96, 0.0, 0.02, 0.005, 0.001, 0.0]

    # Case 1: device="auto" selects DmlExecutionProvider first when available
    auto_cfg = replace(
        cfg.safety_alignment,
        wd_tagger_enabled=True,
        wd_tagger_device="auto",
        wd_tagger_model_path=str(model_path),
    )
    calls_1: list[list[str]] = []

    class _DmlReadyOrt:
        @staticmethod
        def get_available_providers() -> list[str]:
            return ["DmlExecutionProvider", "CPUExecutionProvider"]

        @staticmethod
        def InferenceSession(model_path: str, providers: list[str]) -> _MockOnnxSession:  # noqa: N802
            del model_path
            calls_1.append(list(providers))
            return _MockOnnxSession([safe_vec], providers=providers)

    gate1 = WdTaggerSafetyGate(auto_cfg, ort_module=_DmlReadyOrt())
    res1 = gate1.inspect_frames([frame], auto_cfg)
    assert res1.passed is True
    assert gate1.active_provider == "DmlExecutionProvider"
    assert gate1.fallback_used is False
    assert calls_1 == [["DmlExecutionProvider", "CPUExecutionProvider"]]

    # Case 2: device="dml" falls back to CUDAExecutionProvider when DML init fails
    dml_cfg = replace(auto_cfg, wd_tagger_device="dml")
    calls_2: list[list[str]] = []

    class _DmlFailsCudaOkOrt:
        @staticmethod
        def get_available_providers() -> list[str]:
            return ["DmlExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]

        @staticmethod
        def InferenceSession(model_path: str, providers: list[str]) -> _MockOnnxSession:  # noqa: N802
            del model_path
            calls_2.append(list(providers))
            if "DmlExecutionProvider" in providers:
                raise RuntimeError("DirectML device creation failed")
            return _MockOnnxSession([safe_vec], providers=providers)

    caplog.clear()
    gate2 = WdTaggerSafetyGate(dml_cfg, ort_module=_DmlFailsCudaOkOrt())
    with caplog.at_level(logging.WARNING):
        res2 = gate2.inspect_frames([frame], dml_cfg)
    assert res2.passed is True
    assert gate2.active_provider == "CUDAExecutionProvider"
    assert gate2.fallback_used is True
    assert calls_2 == [
        ["DmlExecutionProvider", "CPUExecutionProvider"],
        ["CUDAExecutionProvider", "CPUExecutionProvider"],
    ]

    # Case 3: device="dml" falls back all the way to CPUExecutionProvider when neither DML nor CUDA works
    calls_3: list[list[str]] = []

    class _NoGpuOrt:
        @staticmethod
        def get_available_providers() -> list[str]:
            return ["CPUExecutionProvider"]

        @staticmethod
        def InferenceSession(model_path: str, providers: list[str]) -> _MockOnnxSession:  # noqa: N802
            del model_path
            calls_3.append(list(providers))
            return _MockOnnxSession([safe_vec], providers=providers)

    caplog.clear()
    gate3 = WdTaggerSafetyGate(dml_cfg, ort_module=_NoGpuOrt())
    with caplog.at_level(logging.WARNING):
        res3 = gate3.inspect_frames([frame], dml_cfg)
    assert res3.passed is True
    assert gate3.active_provider == "CPUExecutionProvider"
    assert gate3.fallback_used is True
    assert calls_3 == [["CPUExecutionProvider"]]
    assert "CPUExecutionProvider" in caplog.text
