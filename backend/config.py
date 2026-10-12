"""Central configuration, loaded from environment variables / .env (never hard-code secrets)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # python-dotenv is optional
    pass

ROOT = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    graph_backend: str = field(default_factory=lambda: os.getenv("GRAPH_BACKEND", "in_memory"))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", ROOT / "data")))
    clone_root: Path = field(default_factory=lambda: Path(os.getenv("CLONE_ROOT", ROOT / "data" / "clones")))
    clone_timeout: int = field(default_factory=lambda: _int("CLONE_TIMEOUT_SECONDS", 180))
    max_file_bytes: int = field(default_factory=lambda: _int("MAX_FILE_BYTES", 1_000_000))
    max_files: int = field(default_factory=lambda: _int("MAX_FILES", 25_000))
    max_repo_mb: int = field(default_factory=lambda: _int("MAX_REPO_MB", 1500))
    allowed_git_hosts: list[str] = field(default_factory=lambda: [
        h.strip().lower() for h in os.getenv("ALLOWED_GIT_HOSTS", "github.com,gitlab.com,bitbucket.org").split(",") if h.strip()])
    allow_local_paths: bool = field(default_factory=lambda: _bool("ALLOW_LOCAL_PATHS", False))
    keep_clones: bool = field(default_factory=lambda: _bool("KEEP_CLONES", False))
    max_view_nodes: int = field(default_factory=lambda: _int("MAX_VIEW_NODES", 150))
    hard_max_nodes: int = field(default_factory=lambda: _int("HARD_MAX_NODES", 600))
    # Neo4j
    neo4j_uri: str = field(default_factory=lambda: os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    neo4j_user: str = field(default_factory=lambda: os.getenv("NEO4J_USER", "neo4j"))
    neo4j_password: str = field(default_factory=lambda: os.getenv("NEO4J_PASSWORD", ""))
    neo4j_database: str = field(default_factory=lambda: os.getenv("NEO4J_DATABASE", "neo4j"))
    # NLP / Gemini
    nlp_enabled: bool = field(default_factory=lambda: _bool("NLP_ENABLED", True))
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    nlp_timeout: int = field(default_factory=lambda: _int("NLP_REQUEST_TIMEOUT_SECONDS", 30))
    nlp_max_rows: int = field(default_factory=lambda: _int("NLP_MAX_RESULT_ROWS", 50))
    nlp_max_hops: int = field(default_factory=lambda: _int("NLP_MAX_HOPS", 6))
    nlp_explain_with_llm: bool = field(default_factory=lambda: _bool("NLP_EXPLAIN_WITH_LLM", True))
    # API
    api_key: str = field(default_factory=lambda: os.getenv("API_KEY", ""))
    cors_origins: list[str] = field(default_factory=lambda: [
        o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:8000,http://localhost:8000").split(",") if o.strip()])
    rate_limit_analyze_per_min: int = field(default_factory=lambda: _int("RATE_LIMIT_ANALYZE_PER_MIN", 6))
    rate_limit_ask_per_min: int = field(default_factory=lambda: _int("RATE_LIMIT_ASK_PER_MIN", 40))
    analysis_workers: int = field(default_factory=lambda: _int("ANALYSIS_WORKERS", 2))

    @property
    def gemini_enabled(self) -> bool:
        return self.nlp_enabled and bool(self.gemini_api_key)


settings = Settings()
