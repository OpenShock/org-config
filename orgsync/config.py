"""Loads org.toml and answers which repos a section applies to."""

import pathlib
import tomllib

from .model import Repo


class Config:

    def __init__(self, path: pathlib.Path):
        self.root = path.resolve().parent
        with open(path, 'rb') as f:
            self.data = tomllib.load(f)
        self.org: str = self.data['org']
        self.bot_account: str | None = self.data.get('bot_account')
        self.exclude = {r.lower() for r in self.data.get('exclude', [])}

    def section(self, name: str) -> dict:
        return self.data.get(name, {})

    def path(self, relative: str) -> pathlib.Path:
        return self.root / relative


def applies(repo: Repo, rule: dict) -> bool:
    """Evaluates the include / exclude / visibility filters any section may carry."""
    name = repo.name.lower()
    include = rule.get('include')
    if include is not None and name not in {r.lower() for r in include}:
        return False
    if name in {r.lower() for r in rule.get('exclude', [])}:
        return False
    visibility = rule.get('visibility', 'all')
    if visibility == 'public' and repo.private:
        return False
    if visibility == 'private' and not repo.private:
        return False
    return True
