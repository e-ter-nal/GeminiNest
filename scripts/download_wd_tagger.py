"""Standalone offline-friendly downloader for wd-tagger ONNX weights & tags CSV.

Downloads `model.onnx` and `selected_tags.csv` from HuggingFace (default repo:
`SmilingWolf/wd-vit-tagger-v3`) or a custom mirror (`--mirror https://hf-mirror.com`)
into `data/models/` and performs structural/integrity validation.

Usage:
    python scripts/download_wd_tagger.py
    python scripts/download_wd_tagger.py --mirror https://hf-mirror.com
    python scripts/download_wd_tagger.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_REPO_ID = "SmilingWolf/wd-vit-tagger-v3"
DEFAULT_HF_BASE_URL = "https://huggingface.co"
DEFAULT_OUTPUT_DIR = Path("data/models")
DEFAULT_MODEL_FILENAME = "model.onnx"
DEFAULT_TAGS_FILENAME = "selected_tags.csv"
DEFAULT_MIN_MODEL_BYTES = 1024 * 1024  # 1 MB sanity threshold for real ONNX weights
REQUIRED_RATING_TAGS = ("general", "sensitive", "questionable", "explicit")

# Signature prefixes indicating an HTML error/redirect page instead of binary ONNX weights
_HTML_PREFIXES = (b"<!doctype", b"<html", b"<head", b"<?xml")

StreamDownloader = Callable[[str, Path], int]


@dataclass(frozen=True)
class DownloadSpec:
    """Resolved URLs and local targets for wd-tagger assets."""

    repo_id: str
    base_url: str
    revision: str
    model_url: str
    tags_url: str
    model_path: Path
    tags_path: Path


@dataclass(frozen=True)
class ValidationResult:
    """Basic file integrity validation result for downloaded wd-tagger assets."""

    ok: bool
    model_path: str
    model_bytes: int
    model_sha256: str
    tags_path: str
    tags_count: int
    rating_indices: dict[str, int]
    errors: tuple[str, ...] = ()


def normalize_base_url(mirror: str | None = None) -> str:
    """Resolve base HuggingFace endpoint from `--mirror`, `HF_ENDPOINT`, or default."""
    raw = (mirror or os.environ.get("HF_ENDPOINT") or DEFAULT_HF_BASE_URL).strip()
    if not raw:
        raw = DEFAULT_HF_BASE_URL
    if not raw.startswith(("http://", "https://")):
        raise ValueError(f"Mirror URL must start with http:// or https://, got: {raw!r}")
    return raw.rstrip("/")


def build_download_spec(
    *,
    repo_id: str = DEFAULT_REPO_ID,
    mirror: str | None = None,
    revision: str = "main",
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    model_filename: str = DEFAULT_MODEL_FILENAME,
    tags_filename: str = DEFAULT_TAGS_FILENAME,
) -> DownloadSpec:
    """Construct resolved HuggingFace download URLs and local destination paths."""
    clean_repo = repo_id.strip().strip("/")
    if "/" not in clean_repo:
        raise ValueError(f"HuggingFace repo_id must be '<org>/<name>', got: {repo_id!r}")
    base_url = normalize_base_url(mirror)
    clean_rev = revision.strip() or "main"
    out_dir = Path(output_dir)

    model_url = f"{base_url}/{clean_repo}/resolve/{clean_rev}/model.onnx"
    tags_url = f"{base_url}/{clean_repo}/resolve/{clean_rev}/selected_tags.csv"
    return DownloadSpec(
        repo_id=clean_repo,
        base_url=base_url,
        revision=clean_rev,
        model_url=model_url,
        tags_url=tags_url,
        model_path=out_dir / model_filename,
        tags_path=out_dir / tags_filename,
    )


def compute_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_tags_csv(tags_path: Path) -> tuple[int, dict[str, int]]:
    """Validate `selected_tags.csv` structure and dynamically locate rating tag indices."""
    if not tags_path.is_file():
        raise FileNotFoundError(f"Tags CSV not found: {tags_path}")

    raw_bytes = tags_path.read_bytes()
    if not raw_bytes.strip():
        raise ValueError(f"Tags CSV is empty: {tags_path}")
    head_lower = raw_bytes[:64].lstrip().lower()
    if any(head_lower.startswith(prefix) for prefix in _HTML_PREFIXES):
        raise ValueError(f"Tags CSV appears to be an HTML page instead of CSV: {tags_path}")

    rating_indices: dict[str, int] = {}
    row_count = 0
    with tags_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"Tags CSV missing header row: {tags_path}")
        normalized_fields = {f.strip().lower(): f for f in reader.fieldnames if f}
        name_col = normalized_fields.get("name")
        if name_col is None:
            raise ValueError(
                f"Tags CSV missing required 'name' column (found: {reader.fieldnames})"
            )
        for idx, row in enumerate(reader):
            row_count += 1
            tag_name = (row.get(name_col) or "").strip().lower()
            if tag_name in REQUIRED_RATING_TAGS and tag_name not in rating_indices:
                rating_indices[tag_name] = idx

    missing = [tag for tag in REQUIRED_RATING_TAGS if tag not in rating_indices]
    if missing:
        raise ValueError(
            f"Tags CSV missing required rating tags {missing} (found: {sorted(rating_indices)})"
        )
    return row_count, rating_indices


def validate_downloaded_files(
    model_path: Path,
    tags_path: Path,
    *,
    min_model_bytes: int = DEFAULT_MIN_MODEL_BYTES,
    expected_model_sha256: str | None = None,
    expected_tags_sha256: str | None = None,
) -> ValidationResult:
    """Perform basic integrity & format checks on downloaded `model.onnx` and `selected_tags.csv`."""
    errors: list[str] = []
    model_bytes = 0
    model_sha = ""
    tags_count = 0
    rating_indices: dict[str, int] = {}

    if not model_path.is_file():
        errors.append(f"Model file not found: {model_path}")
    else:
        model_bytes = model_path.stat().st_size
        if model_bytes < min_model_bytes:
            errors.append(
                f"Model file too small ({model_bytes} bytes < minimum {min_model_bytes} bytes): {model_path}"
            )
        with model_path.open("rb") as fh:
            head = fh.read(64).lstrip().lower()
        if any(head.startswith(prefix) for prefix in _HTML_PREFIXES):
            errors.append(f"Model file appears to be HTML/XML error page, not ONNX binary: {model_path}")
        model_sha = compute_file_sha256(model_path)
        if expected_model_sha256 and model_sha.lower() != expected_model_sha256.strip().lower():
            errors.append(
                f"Model SHA-256 mismatch: expected {expected_model_sha256}, got {model_sha}"
            )

    try:
        tags_count, rating_indices = validate_tags_csv(tags_path)
        if expected_tags_sha256:
            tags_sha = compute_file_sha256(tags_path)
            if tags_sha.lower() != expected_tags_sha256.strip().lower():
                errors.append(
                    f"Tags CSV SHA-256 mismatch: expected {expected_tags_sha256}, got {tags_sha}"
                )
    except (OSError, ValueError) as exc:
        errors.append(str(exc))

    return ValidationResult(
        ok=len(errors) == 0,
        model_path=str(model_path),
        model_bytes=model_bytes,
        model_sha256=model_sha,
        tags_path=str(tags_path),
        tags_count=tags_count,
        rating_indices=rating_indices,
        errors=tuple(errors),
    )


def default_stream_download(
    url: str,
    dest_path: Path,
    *,
    timeout_sec: float = 120.0,
) -> int:
    """Download `url` to `dest_path` atomically using `httpx` (with urllib fallback)."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest_path.with_suffix(dest_path.suffix + ".part")
    total_bytes = 0

    try:
        import httpx

        with httpx.Client(follow_redirects=True, timeout=timeout_sec) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with part_path.open("wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                        if chunk:
                            fh.write(chunk)
                            total_bytes += len(chunk)
    except ImportError:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "GeminiNest-Downloader/2.0"})
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:  # noqa: S310
            with part_path.open("wb") as fh:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    fh.write(chunk)
                    total_bytes += len(chunk)
    except Exception:
        part_path.unlink(missing_ok=True)
        raise

    part_path.replace(dest_path)
    return total_bytes


def download_wd_tagger(
    spec: DownloadSpec,
    *,
    force: bool = False,
    dry_run: bool = False,
    min_model_bytes: int = DEFAULT_MIN_MODEL_BYTES,
    expected_model_sha256: str | None = None,
    expected_tags_sha256: str | None = None,
    downloader: StreamDownloader | None = None,
) -> dict[str, Any]:
    """Download `model.onnx` and `selected_tags.csv` per `spec` and validate them."""
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "repo_id": spec.repo_id,
            "base_url": spec.base_url,
            "model_url": spec.model_url,
            "tags_url": spec.tags_url,
            "model_path": str(spec.model_path),
            "tags_path": str(spec.tags_path),
        }

    dl_fn = downloader or default_stream_download
    spec.model_path.parent.mkdir(parents=True, exist_ok=True)
    spec.tags_path.parent.mkdir(parents=True, exist_ok=True)

    downloaded: list[str] = []
    skipped: list[str] = []

    if force or not spec.model_path.is_file():
        dl_fn(spec.model_url, spec.model_path)
        downloaded.append(str(spec.model_path))
    else:
        skipped.append(str(spec.model_path))

    if force or not spec.tags_path.is_file():
        dl_fn(spec.tags_url, spec.tags_path)
        downloaded.append(str(spec.tags_path))
    else:
        skipped.append(str(spec.tags_path))

    validation = validate_downloaded_files(
        spec.model_path,
        spec.tags_path,
        min_model_bytes=min_model_bytes,
        expected_model_sha256=expected_model_sha256,
        expected_tags_sha256=expected_tags_sha256,
    )
    return {
        "ok": validation.ok,
        "dry_run": False,
        "repo_id": spec.repo_id,
        "base_url": spec.base_url,
        "downloaded": downloaded,
        "skipped": skipped,
        "validation": asdict(validation),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="download_wd_tagger",
        description="Download SmilingWolf wd-tagger ONNX model and selected_tags.csv to data/models/.",
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO_ID,
        help=f"HuggingFace model repository (default: {DEFAULT_REPO_ID})",
    )
    parser.add_argument(
        "--mirror",
        default=None,
        help="Optional HuggingFace mirror base URL (e.g. https://hf-mirror.com; also respects HF_ENDPOINT)",
    )
    parser.add_argument(
        "--revision",
        default="main",
        help="Repository revision/branch (default: main)",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Target local directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--model-filename",
        default=DEFAULT_MODEL_FILENAME,
        help=f"Local filename for ONNX weights (default: {DEFAULT_MODEL_FILENAME})",
    )
    parser.add_argument(
        "--tags-filename",
        default=DEFAULT_TAGS_FILENAME,
        help=f"Local filename for tags CSV (default: {DEFAULT_TAGS_FILENAME})",
    )
    parser.add_argument(
        "--min-model-bytes",
        type=int,
        default=DEFAULT_MIN_MODEL_BYTES,
        help="Minimum valid byte size for model.onnx validation",
    )
    parser.add_argument("--sha256-model", default=None, help="Optional expected SHA-256 of model.onnx")
    parser.add_argument("--sha256-tags", default=None, help="Optional expected SHA-256 of selected_tags.csv")
    parser.add_argument("--force", action="store_true", help="Re-download even if local files exist")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print resolved URLs and target paths without performing any network request",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        spec = build_download_spec(
            repo_id=args.repo,
            mirror=args.mirror,
            revision=args.revision,
            output_dir=args.output_dir,
            model_filename=args.model_filename,
            tags_filename=args.tags_filename,
        )
        result = download_wd_tagger(
            spec,
            force=args.force,
            dry_run=args.dry_run,
            min_model_bytes=args.min_model_bytes,
            expected_model_sha256=args.sha256_model,
            expected_tags_sha256=args.sha256_tags,
        )
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
