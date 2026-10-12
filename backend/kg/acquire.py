"""Safe repository acquisition: URL validation, shallow clone with timeout, no hooks,
no symlinks, no credential prompts. Repository code is NEVER executed."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ..config import settings

_SEG = re.compile(r"^[A-Za-z0-9_.\-]+$")
_REF = re.compile(r"^[A-Za-z0-9_.\-/]{1,200}$")


class AcquisitionError(Exception):
    pass


@dataclass
class RepoSource:
    url: str                 # normalised url or local path
    key: str                 # owner/repo
    ref: str | None
    path: Path               # where the files are
    commit: str | None
    is_temp: bool


def normalize_repo_url(url: str) -> tuple[str, str]:
    """Return (normalised https url, 'owner/repo'). Raises AcquisitionError if unsafe."""
    raw = (url or "").strip()
    if raw.startswith("git@"):
        # git@github.com:owner/repo.git -> https form
        m = re.match(r"^git@([^:]+):(.+)$", raw)
        if not m:
            raise AcquisitionError("Unsupported SSH URL format")
        raw = f"https://{m.group(1)}/{m.group(2)}"
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    if p.scheme not in ("https",):
        raise AcquisitionError("Only https:// repository URLs are allowed")
    if p.username or p.password:
        raise AcquisitionError("Credentials in the URL are not allowed")
    host = (p.hostname or "").lower()
    if host not in settings.allowed_git_hosts:
        raise AcquisitionError(f"Host '{host}' is not in ALLOWED_GIT_HOSTS ({', '.join(settings.allowed_git_hosts)})")
    parts = [s for s in p.path.split("/") if s]
    # allow github tree URLs: /owner/repo/tree/branch
    if len(parts) < 2:
        raise AcquisitionError("URL must look like https://github.com/<owner>/<repo>")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not (_SEG.match(owner) and _SEG.match(repo)) or owner.startswith("-") or repo.startswith("-"):
        raise AcquisitionError("Invalid owner/repository name")
    return f"https://{host}/{owner}/{repo}.git", f"{owner}/{repo}"


def ref_from_url(url: str) -> str | None:
    p = urlparse(url if "://" in url else "https://" + url)
    parts = [s for s in p.path.split("/") if s]
    if len(parts) >= 4 and parts[2] == "tree":
        return "/".join(parts[3:])
    return None


def validate_ref(ref: str | None) -> str | None:
    if ref is None or ref == "":
        return None
    if not _REF.match(ref) or ref.startswith("-") or ".." in ref:
        raise AcquisitionError("Invalid git ref/branch name")
    return ref


def _git_env() -> dict:
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",          # never prompt for credentials
        "GIT_ASKPASS": "echo",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_LFS_SKIP_SMUDGE": "1",
    })
    return env


def _no_hooks_dir() -> str:
    d = settings.clone_root / "_no_hooks"        # empty dir works on Windows and Linux (unlike /dev/null)
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _git(args: list[str], cwd: Path | None = None, timeout: int | None = None) -> str:
    cmd = ["git", "-c", f"core.hooksPath={_no_hooks_dir()}", "-c", "core.symlinks=false",
           "-c", "protocol.file.allow=never", "-c", "submodule.recurse=false"] + args
    try:
        res = subprocess.run(cmd, cwd=cwd, env=_git_env(), capture_output=True, text=True,
                             timeout=timeout or settings.clone_timeout)
    except subprocess.TimeoutExpired:
        raise AcquisitionError(f"git timed out after {timeout or settings.clone_timeout}s (repository too large or network slow)")
    except FileNotFoundError:
        raise AcquisitionError("git is not installed or not on PATH")
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip().splitlines()
        tail = msg[-1] if msg else "unknown git error"
        if "not found" in tail.lower() or "could not read" in tail.lower() or "repository" in tail.lower():
            tail += " (is the repository public and the URL correct?)"
        raise AcquisitionError(f"git failed: {tail}")
    return res.stdout.strip()


def acquire(url: str, ref: str | None = None) -> RepoSource:
    """Clone a public repository (or resolve a local path when ALLOW_LOCAL_PATHS=true)."""
    local = Path(url).expanduser()
    if settings.allow_local_paths and local.exists() and local.is_dir():
        commit = None
        if (local / ".git").exists():
            try:
                commit = _git(["rev-parse", "HEAD"], cwd=local, timeout=20)
            except AcquisitionError:
                commit = None
        return RepoSource(url=str(local.resolve()), key=local.resolve().name, ref=ref,
                          path=local.resolve(), commit=commit, is_temp=False)

    ref = validate_ref(ref or ref_from_url(url))
    norm, key = normalize_repo_url(url)
    settings.clone_root.mkdir(parents=True, exist_ok=True)
    dest = Path(tempfile.mkdtemp(prefix=key.replace("/", "__") + "__", dir=settings.clone_root))
    args = ["clone", "--depth", "1", "--no-tags", "--single-branch", "--filter=blob:limit=2m"]
    if ref:
        args += ["--branch", ref]
    args += ["--", norm, str(dest / "src")]
    try:
        _git(args)
    except AcquisitionError:
        shutil.rmtree(dest, ignore_errors=True)
        if ref:  # maybe a commit sha rather than a branch: fetch it explicitly
            return _acquire_commit(norm, key, ref)
        raise
    path = dest / "src"
    commit = _git(["rev-parse", "HEAD"], cwd=path, timeout=20)
    _check_size(path, dest)
    return RepoSource(url=norm, key=key, ref=ref, path=path, commit=commit, is_temp=True)


def _acquire_commit(norm: str, key: str, sha: str) -> RepoSource:
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        raise AcquisitionError(f"Branch/tag '{sha}' not found in repository")
    dest = Path(tempfile.mkdtemp(prefix=key.replace("/", "__") + "__", dir=settings.clone_root))
    path = dest / "src"
    path.mkdir()
    try:
        _git(["init", "-q"], cwd=path, timeout=20)
        _git(["remote", "add", "origin", norm], cwd=path, timeout=20)
        _git(["fetch", "--depth", "1", "origin", sha], cwd=path)
        _git(["checkout", "-q", "FETCH_HEAD"], cwd=path, timeout=60)
        commit = _git(["rev-parse", "HEAD"], cwd=path, timeout=20)
    except AcquisitionError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    _check_size(path, dest)
    return RepoSource(url=norm, key=key, ref=sha, path=path, commit=commit, is_temp=True)


def _check_size(path: Path, dest: Path) -> None:
    total = 0
    limit = settings.max_repo_mb * 1024 * 1024
    for root, dirs, files in os.walk(path):
        if ".git" in dirs:
            dirs.remove(".git")
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
        if total > limit:
            shutil.rmtree(dest, ignore_errors=True)
            raise AcquisitionError(f"Repository exceeds MAX_REPO_MB={settings.max_repo_mb}")


def cleanup(src: RepoSource) -> None:
    if src.is_temp and not settings.keep_clones:
        shutil.rmtree(src.path.parent, ignore_errors=True)
