"""Repository scanner: enumerates files safely (no symlink following, path-traversal safe,
size limits, standard exclusions, basic .gitignore support) and classifies them."""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", "build", "dist",
    "target", "out", "coverage", ".idea", ".vscode", ".gradle", ".mvn", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "bower_components", ".next", ".nuxt", "vendor", ".eggs",
    "htmlcov", ".terraform", "bin", "obj",
}

LANG_BY_EXT = {
    ".java": "java", ".py": "python", ".sql": "sql",
    ".json": "openapi?", ".yaml": "openapi?", ".yml": "openapi?", ".xml": "xml?",
}

TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__|src/test|testing)(/|$)|(^|/)test_[^/]*\.py$|_test\.py$|Tests?\.java$|IT\.java$", re.I)


@dataclass
class ScannedFile:
    path: str          # relative posix path
    abs_path: Path
    ext: str
    kind: str          # java | python | sql | openapi? | xml? | other
    size: int
    is_test: bool


@dataclass
class ScanResult:
    files: list[ScannedFile] = field(default_factory=list)
    directories: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (path, reason)
    total_files_seen: int = 0


def _load_gitignore(root: Path) -> list[str]:
    gi = root / ".gitignore"
    pats: list[str] = []
    try:
        if gi.is_file() and gi.stat().st_size < 200_000:
            for line in gi.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("!"):
                    pats.append(line)
    except OSError:
        pass
    return pats


def _ignored(rel: str, name: str, patterns: list[str]) -> bool:
    for p in patterns:
        pp = p.rstrip("/")
        if pp.startswith("/"):
            if fnmatch.fnmatch(rel, pp[1:]) or rel.startswith(pp[1:] + "/"):
                return True
        elif "/" in pp:
            if fnmatch.fnmatch(rel, pp) or fnmatch.fnmatch(rel, "*/" + pp):
                return True
        elif fnmatch.fnmatch(name, pp):
            return True
    return False


def scan(root: Path, max_file_bytes: int = 1_000_000, max_files: int = 25_000) -> ScanResult:
    root = root.resolve()
    res = ScanResult()
    patterns = _load_gitignore(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dpath = Path(dirpath)
        rel_dir = dpath.relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for d in sorted(dirnames):
            full = dpath / d
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if d in EXCLUDED_DIRS or d.startswith(".") or full.is_symlink() or _ignored(rel, d, patterns):
                continue
            keep.append(d)
        dirnames[:] = keep
        if rel_dir:
            res.directories.append(rel_dir)
        for fn in sorted(filenames):
            res.total_files_seen += 1
            full = dpath / fn
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            if full.is_symlink():
                res.skipped.append((rel, "symlink"))
                continue
            try:
                resolved = full.resolve()
                resolved.relative_to(root)            # path traversal guard
            except (ValueError, OSError):
                res.skipped.append((rel, "outside repository"))
                continue
            ext = full.suffix.lower()
            kind = LANG_BY_EXT.get(ext, "other")
            if kind == "other":
                continue                               # not an analysable artifact; not an error
            if _ignored(rel, fn, patterns):
                res.skipped.append((rel, "gitignored"))
                continue
            try:
                size = full.stat().st_size
            except OSError:
                res.skipped.append((rel, "unreadable"))
                continue
            if size > max_file_bytes:
                res.skipped.append((rel, f"larger than {max_file_bytes} bytes"))
                continue
            if fn.endswith((".min.js", ".lock")) or fn in {"package-lock.json", "package.json", "tsconfig.json"}:
                continue
            if len(res.files) >= max_files:
                res.skipped.append((rel, "MAX_FILES limit reached"))
                continue
            res.files.append(ScannedFile(rel, full, ext, kind, size, bool(TEST_PATH_RE.search(rel))))
    return res


def read_text(path: Path) -> str:
    data = path.read_bytes()
    if b"\x00" in data[:4096]:
        raise ValueError("binary file")
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "ignore")
