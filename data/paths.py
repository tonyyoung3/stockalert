"""Repo-root paths. SQLite files and ticker lists stay at the repository root."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def repo_file(*parts: str) -> Path:
    return REPO_ROOT.joinpath(*parts)
