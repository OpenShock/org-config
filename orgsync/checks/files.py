"""Managed files: copied verbatim from a template into every matching repo."""

import pathlib

from ..config import Config, applies
from ..model import Repo, Result


def template_for(repo: Repo, rule: dict, cfg: Config) -> pathlib.Path:
    """The rule's template, or the repo's own file from the `overrides` folder when it has one."""
    if 'overrides' in rule:
        for folder in cfg.path(rule['overrides']).glob('*'):
            own = folder / pathlib.PurePosixPath(rule['path']).name
            if folder.name.lower() == repo.name.lower() and own.is_file():
                return own
    return cfg.path(rule['template'])


def _managed(repo: Repo, cfg: Config) -> list[dict]:
    rules = [r for r in cfg.section('files').get('managed', []) if applies(repo, r)]
    # Some files are only kept in step where a repo already opted in by having one.
    return [r for r in rules if not r.get('only_existing', False) or r['path'] in repo.tree]


def check(repo: Repo, cfg: Config) -> Result:
    result = Result()
    for rule in _managed(repo, cfg):
        template = template_for(repo, rule, cfg).read_text(encoding='utf-8')
        wanted = template.replace('{org}', repo.org).replace('{repo}', repo.name)
        path = rule['path']
        current = repo.read(path)
        if current != wanted:
            result.files[path] = wanted
            detail = f'{"update" if current is not None else "add"} `{path}`'
            if rule.get('direct', False):
                result.direct.add(path)
                result.direct_details.append(detail)
            else:
                result.details.append(detail)
        # Copies in other locations would shadow or contradict the managed one.
        for other in rule.get('remove', []):
            if other in repo.tree:
                result.files[other] = None
                result.details.append(f'remove `{other}` in favour of `{path}`')
    return result


def managed_paths(repo: Repo, cfg: Config) -> set[str]:
    """Paths the managed-file rules guarantee for this repo, whether or not they exist yet."""
    return {rule['path'] for rule in _managed(repo, cfg)}
