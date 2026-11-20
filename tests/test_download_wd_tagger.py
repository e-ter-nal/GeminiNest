"""Offline unit tests for scripts/download_wd_tagger.py (zero network requests)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "download_wd_tagger.py"


def _load_downloader_module():
    spec = importlib.util.spec_from_file_location("download_wd_tagger", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_build_download_spec_default_and_mirror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod = _load_downloader_module()
    monkeypatch.delenv("HF_ENDPOINT", raising=False)

    spec_default = mod.build_download_spec(output_dir=tmp_path)
    assert spec_default.repo_id == "SmilingWolf/wd-vit-tagger-v3"
    assert spec_default.model_url == "https://huggingface.co/SmilingWolf/wd-vit-tagger-v3/resolve/main/model.onnx"
    assert spec_default.tags_url == "https://huggingface.co/SmilingWolf/wd-vit-tagger-v3/resolve/main/selected_tags.csv"
    assert spec_default.model_path == tmp_path / "model.onnx"
    assert spec_default.tags_path == tmp_path / "selected_tags.csv"

    spec_mirror = mod.build_download_spec(mirror="https://hf-mirror.com/", output_dir=tmp_path)
    assert spec_mirror.model_url.startswith("https://hf-mirror.com/SmilingWolf/wd-vit-tagger-v3/")

    monkeypatch.setenv("HF_ENDPOINT", "https://mirror.example.org")
    spec_env = mod.build_download_spec(output_dir=tmp_path)
    assert spec_env.base_url == "https://mirror.example.org"


def test_download_wd_tagger_mocked_success_and_validation(tmp_path: Path) -> None:
    mod = _load_downloader_module()
    spec = mod.build_download_spec(mirror="https://hf-mirror.com", output_dir=tmp_path / "models")
    requested_urls: list[str] = []

    def fake_downloader(url: str, dest: Path) -> int:
        requested_urls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if url.endswith("model.onnx"):
            payload = b"\x08\x07\x12\x0bonnx-binary" + b"\x00" * 512
        else:
            payload = (
                "tag_id,name,category,count\n"
                "0,general,9,1000\n"
                "1,sensitive,9,500\n"
                "2,questionable,9,200\n"
                "3,explicit,9,100\n"
            ).encode("utf-8")
        dest.write_bytes(payload)
        return len(payload)

    res = mod.download_wd_tagger(spec, min_model_bytes=128, downloader=fake_downloader)
    assert res["ok"] is True
    assert len(requested_urls) == 2
    assert res["validation"]["ok"] is True
    assert res["validation"]["rating_indices"] == {
        "general": 0,
        "sensitive": 1,
        "questionable": 2,
        "explicit": 3,
    }


def test_validate_downloaded_files_rejects_html_and_corrupt_csv(tmp_path: Path) -> None:
    mod = _load_downloader_module()
    bad_model = tmp_path / "model.onnx"
    bad_model.write_bytes(b"<!DOCTYPE html><html><body>404 Not Found</body></html>" + b" " * 200)
    bad_csv = tmp_path / "selected_tags.csv"
    bad_csv.write_text("tag_id,name,category\n0,cat,0\n", encoding="utf-8")

    val = mod.validate_downloaded_files(bad_model, bad_csv, min_model_bytes=64)
    assert val.ok is False
    assert any("HTML" in err for err in val.errors)
    assert any("missing required rating tags" in err for err in val.errors)


def test_cli_dry_run_emits_json_without_network(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_downloader_module()
    rc = mod.main(
        [
            "--dry-run",
            "--mirror",
            "https://hf-mirror.com",
            "--output-dir",
            str(tmp_path / "models"),
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["model_url"] == "https://hf-mirror.com/SmilingWolf/wd-vit-tagger-v3/resolve/main/model.onnx"
    assert not (tmp_path / "models" / "model.onnx").exists()
