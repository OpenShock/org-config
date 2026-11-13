"""Loads org.toml and answers which repos a section applies to."""

import pathlib
import tomllib

from .model import Repo


class ConfigError(Exception):
    pass


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

    def unknown_repos(self, names: list[str]) -> list[str]:
        """Per-repo folders and settings that name no active repo.

        A misspelled or renamed repo would otherwise lose its own files without anyone noticing.
        """
        known = {n.lower() for n in names}
        folders = ['rulesets/repos', *(r['overrides'] for r in self.section('files').get('managed', []) if 'overrides' in r)]
        problems = [
            f'{folder}/{child.name}'
            for folder in folders
            for child in sorted(self.path(folder).glob('*'))
            if child.is_dir() and child.name.lower() not in known
        ]
        for rule in self.section('rulesets').get('managed', []):
            problems += [f'extra_bypass.{name} for {rule["file"]}' for name in rule.get('extra_bypass', {}) if name.lower() not in known]
        return problems


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
