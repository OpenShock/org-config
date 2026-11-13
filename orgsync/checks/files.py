"""Managed files: copied verbatim from a template into every matching repo."""

from ..config import Config, applies
from ..model import Repo, Result


def check(repo: Repo, cfg: Config) -> Result:
    result = Result()
    for rule in cfg.section('files').get('managed', []):
        if not applies(repo, rule):
            continue
        template = cfg.path(rule['template']).read_text(encoding='utf-8')
        wanted = template.replace('{org}', repo.org).replace('{repo}', repo.name)
        path = rule['path']
        current = repo.read(path)
        if current != wanted:
            result.files[path] = wanted
            result.details.append(f'{"update" if current is not None else "add"} `{path}`')
        # Copies in other locations would shadow or contradict the managed one.
        for other in rule.get('remove', []):
            if other in repo.tree:
                result.files[other] = None
                result.details.append(f'remove `{other}` in favour of `{path}`')
    return result


def managed_paths(repo: Repo, cfg: Config) -> set[str]:
    """Paths the managed-file rules guarantee for this repo, whether or not they exist yet."""
    return {rule['path'] for rule in cfg.section('files').get('managed', []) if applies(repo, rule)}
