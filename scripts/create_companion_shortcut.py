"""Create a zero-console Windows Desktop shortcut (.lnk) for GeminiNest desktop companion.

Dynamically discovers:
- Repository root directory (parent of `scripts/`)
- `pythonw.exe` inside the active or `gemininest` Python/Conda environment
- Current user's Windows Desktop directory (unless `--output` is specified)

Uses Windows `WScript.Shell` COM automation via PowerShell (`shell=False`) with
environment-variable parameter passing so paths with spaces or Unicode never break
quoting. Supports `--dry-run` for completely offline verification.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_SHORTCUT_NAME = "GeminiNest 桌面小窝助手.lnk"
_CSIDL_DESKTOPDIRECTORY = 0x0010


@dataclass(frozen=True)
class ShortcutSpec:
    repo_root: Path
    pythonw_path: Path
    shortcut_path: Path
    target_path: Path
    arguments: str
    working_directory: Path
    description: str


def detect_repo_root(start_file: Path | str | None = None) -> Path:
    """Resolve the repository root dynamically from this script's location."""
    anchor = Path(start_file) if start_file is not None else Path(__file__)
    candidate = anchor.resolve().parents[1]
    if (candidate / "src" / "gemininest").is_dir() and (candidate / "config.json").is_file():
        return candidate
    cwd = Path.cwd().resolve()
    if (cwd / "src" / "gemininest").is_dir() and (cwd / "config.json").is_file():
        return cwd
    return candidate


def _find_conda_env_pythonw(env_name: str = "gemininest") -> Path | None:
    conda_exe = shutil.which("conda")
    if not conda_exe:
        return None
    try:
        proc = subprocess.run(
            [conda_exe, "info", "--envs", "--json"],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10.0,
            check=False,
        )
        if proc.returncode != 0:
            return None
        payload = json.loads(proc.stdout or "{}")
        envs = payload.get("envs") or []
        for raw_env in envs:
            env_dir = Path(str(raw_env))
            if env_dir.name.casefold() == env_name.casefold():
                for cand in (env_dir / "pythonw.exe", env_dir / "Scripts" / "pythonw.exe"):
                    if cand.is_file():
                        return cand.resolve()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return None


def detect_pythonw_executable(
    *,
    explicit_pythonw: Path | str | None = None,
    env_name: str = "gemininest",
) -> Path:
    """Locate `pythonw.exe` for the `gemininest` environment without hardcoded paths."""
    if explicit_pythonw is not None and str(explicit_pythonw).strip():
        return Path(explicit_pythonw).expanduser().resolve()

    cur_exe = Path(sys.executable).resolve()
    cur_prefix = Path(sys.prefix).resolve()
    active_env_name = os.environ.get("CONDA_DEFAULT_ENV", "").strip()

    # 1. Already running inside the target gemininest environment or a dedicated venv
    if (
        cur_prefix.name.casefold() == env_name.casefold()
        or active_env_name.casefold() == env_name.casefold()
        or cur_prefix.name in (".venv", "venv")
    ):
        for cand in (cur_exe.with_name("pythonw.exe"), cur_prefix / "pythonw.exe"):
            if cand.is_file():
                return cand.resolve()

    # 2. Check CONDA_PREFIX if it points to target env
    conda_prefix = os.environ.get("CONDA_PREFIX", "").strip()
    if conda_prefix:
        cp_path = Path(conda_prefix)
        if cp_path.name.casefold() == env_name.casefold():
            cand = cp_path / "pythonw.exe"
            if cand.is_file():
                return cand.resolve()

    # 3. Probe conda environments for `gemininest`
    conda_found = _find_conda_env_pythonw(env_name=env_name)
    if conda_found is not None:
        return conda_found

    # 4. Sibling `pythonw.exe` next to current sys.executable
    sibling = cur_exe.with_name("pythonw.exe")
    if sibling.is_file():
        return sibling

    # 5. PATH lookup or best-effort expected path next to sys.executable
    which_pw = shutil.which("pythonw") or shutil.which("pythonw.exe")
    if which_pw:
        return Path(which_pw).resolve()
    return sibling


def detect_desktop_dir() -> Path:
    """Resolve the user's Windows Desktop directory dynamically."""
    if sys.platform == "win32":
        try:
            buf = ctypes.create_unicode_buffer(32768)
            hr = ctypes.windll.shell32.SHGetFolderPathW(
                None,
                _CSIDL_DESKTOPDIRECTORY,
                None,
                0,
                buf,
            )
            if hr == 0 and buf.value.strip():
                return Path(buf.value.strip())
        except Exception:  # noqa: BLE001
            pass
    return Path.home() / "Desktop"


def resolve_shortcut_output_path(
    output: Path | str | None = None,
    *,
    shortcut_name: str = DEFAULT_SHORTCUT_NAME,
) -> Path:
    if output is not None and str(output).strip():
        out_path = Path(output).expanduser()
        if out_path.suffix.lower() == ".lnk":
            return out_path.resolve()
        return (out_path / shortcut_name).resolve()
    return (detect_desktop_dir() / shortcut_name).resolve()


def build_companion_launch_arguments() -> str:
    """Build `pythonw.exe` arguments that set `PYTHONPATH=src` and run `gemininest.companion`."""
    inline_bootstrap = (
        "import os,runpy,sys;"
        "s=os.path.abspath('src');"
        "old=os.environ.get('PYTHONPATH','');"
        "os.environ['PYTHONPATH']=s+(os.pathsep+old if old else '');"
        "sys.path.insert(0,s);"
        "sys.argv=['gemininest.companion'];"
        "runpy.run_module('gemininest.companion',run_name='__main__')"
    )
    return f'-c "{inline_bootstrap}"'


def build_shortcut_spec(
    *,
    repo_root: Path | str | None = None,
    explicit_pythonw: Path | str | None = None,
    output: Path | str | None = None,
    shortcut_name: str = DEFAULT_SHORTCUT_NAME,
) -> ShortcutSpec:
    eff_root = Path(repo_root).resolve() if repo_root is not None else detect_repo_root()
    pythonw = detect_pythonw_executable(explicit_pythonw=explicit_pythonw)
    shortcut_path = resolve_shortcut_output_path(output, shortcut_name=shortcut_name)
    return ShortcutSpec(
        repo_root=eff_root,
        pythonw_path=pythonw,
        shortcut_path=shortcut_path,
        target_path=pythonw,
        arguments=build_companion_launch_arguments(),
        working_directory=eff_root,
        description="GeminiNest 桌面常驻小窝助手（无黑框启动）",
    )


def create_windows_shortcut(spec: ShortcutSpec) -> Path:
    """Create a `.lnk` shortcut via Windows `WScript.Shell` COM object through PowerShell."""
    if sys.platform != "win32":
        raise OSError("Windows .lnk shortcut creation is only supported on Windows (win32).")

    spec.shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    ps_script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$sc = $ws.CreateShortcut($env:GN_LNK_PATH); "
        "$sc.TargetPath = $env:GN_LNK_TARGET; "
        "$sc.Arguments = $env:GN_LNK_ARGS; "
        "$sc.WorkingDirectory = $env:GN_LNK_WORKDIR; "
        "$sc.Description = $env:GN_LNK_DESC; "
        "$sc.WindowStyle = 1; "
        "$sc.Save()"
    )

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["GN_LNK_PATH"] = str(spec.shortcut_path)
    env["GN_LNK_TARGET"] = str(spec.target_path)
    env["GN_LNK_ARGS"] = spec.arguments
    env["GN_LNK_WORKDIR"] = str(spec.working_directory)
    env["GN_LNK_DESC"] = spec.description

    ps_exe = shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"
    proc = subprocess.run(
        [
            ps_exe,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps_script,
        ],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=20.0,
        check=False,
    )
    if proc.returncode != 0 or not spec.shortcut_path.is_file():
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Failed creating desktop shortcut via WScript.Shell (rc={proc.returncode}): {err}")
    return spec.shortcut_path


def _spec_to_payload(spec: ShortcutSpec, *, dry_run: bool) -> dict[str, Any]:
    raw = asdict(spec)
    return {
        "ok": True,
        "dry_run": dry_run,
        **{k: (str(v) if isinstance(v, Path) else v) for k, v in raw.items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="create_companion_shortcut",
        description="Create a Windows Desktop .lnk shortcut to launch GeminiNest Companion without a console window.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Custom output .lnk file path or target directory (defaults to user Desktop)",
    )
    parser.add_argument(
        "--name",
        default=DEFAULT_SHORTCUT_NAME,
        help="Shortcut filename when --output is a directory or omitted",
    )
    parser.add_argument(
        "--pythonw",
        default=None,
        help="Explicit path to gemininest environment pythonw.exe",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Override repository root directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and print shortcut metadata as JSON without modifying disk",
    )
    args = parser.parse_args(argv)

    spec = build_shortcut_spec(
        repo_root=args.repo_root,
        explicit_pythonw=args.pythonw,
        output=args.output,
        shortcut_name=args.name,
    )
    if not args.dry_run:
        create_windows_shortcut(spec)

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    print(json.dumps(_spec_to_payload(spec, dry_run=bool(args.dry_run)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
