import mimetypes
import re
from pathlib import Path

from ..models import CreatedFile


def strip_ansi(text: str) -> str:
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_escape.sub("", text)


def sanitize_code(code: str) -> str:
    """
    Sanitizes code for a headless server environment:
      - If 'matplotlib' or 'plt' is present, silence verbose matplotlib font manager logging.
      - If 'xarray' is present, switch its default display style to plain text
        to avoid HTML/CSS-heavy outputs.
    """
    out = code

    # Matplotlib backend selection and log silencing
    if ("matplotlib" in out) or ("plt" in out):
        to_add = (
            "import matplotlib\n"
            "import logging\n"
            "logging.getLogger('matplotlib.font_manager').disabled = True\n"
            "import matplotlib.pyplot as plt\n"
        )
        out = f"{to_add}{out}"

    # xarray text display (prepend so it runs before user code)
    if "xarray" in out:
        out = f"import xarray as xr\nxr.set_options(display_style='text')\n{out}"

    return out


# ── exit() / quit() handling ────────────────────────────────────────────────

EXIT_RE = re.compile(r"(?m)^\s*(exit|quit)\s*\(\s*\)\s*(#.*)?$")


def should_restart_after(code: str) -> bool:
    return bool(EXIT_RE.search(code))


# ── detect new or modified files ────────────────────────────────────────────

IGNORED_FILE_NAMES = {
    ".ipynb_checkpoints",
    "__pycache__",
}

IGNORED_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".tmp",
    ".part",
    ".swp",
}


def _safe_relative_path(root: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _fingerprint_file(path: Path) -> dict | None:
    try:
        stat = path.stat()
        if not path.is_file():
            return None

        return {
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
        }
    except OSError:
        return None


def snapshot_files(root: Path) -> dict[str, dict]:
    files: dict[str, dict] = {}

    if not root.exists():
        return files

    for path in root.rglob("*"):
        if any(part in IGNORED_FILE_NAMES for part in path.parts):
            continue
        if path.suffix in IGNORED_SUFFIXES:
            continue

        rel = _safe_relative_path(root, path)
        if rel is None:
            continue

        fp = _fingerprint_file(path)
        if fp is None:
            continue

        files[rel] = fp

    return files


def detect_created_or_modified_files(
    root: Path,
    before: dict[str, dict],
    after: dict[str, dict],
) -> list[CreatedFile]:
    created_files = []

    for rel_path, after_fp in after.items():
        before_fp = before.get(rel_path)

        is_new = before_fp is None
        is_modified = before_fp is not None and before_fp != after_fp

        if not (is_new or is_modified):
            continue

        abs_path = root / rel_path
        mime_type, _ = mimetypes.guess_type(abs_path.name)

        created_files.append(
            CreatedFile(
                path=rel_path,
                mime_type=mime_type or "application/octet-stream",
            )
        )

    return created_files
