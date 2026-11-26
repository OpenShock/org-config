"""Managed files: copied from a template into every matching repo, with a few placeholders filled in."""

from ..config import Config, applies
from ..model import Repo, Result


def _managed(repo: Repo, cfg: Config) -> list[dict]:
    return [r for r in cfg.section('files').get('managed', []) if applies(repo, r)]


def render(template: str, repo: Repo, rule: dict) -> str:
    """Fills in {org}, {repo} and the rule's `vars`, where `repo_vars` holds what one repo does differently."""
    own = next((v for name, v in rule.get('repo_vars', {}).items() if name.lower() == repo.name.lower()), {})
    for key, value in {**rule.get('vars', {}), **own, 'org': repo.org, 'repo': repo.name}.items():
        template = template.replace('{' + key + '}', value)
    return template


def check(repo: Repo, cfg: Config) -> Result:
    result = Result()
    for rule in _managed(repo, cfg):
        wanted = render(cfg.path(rule['template']).read_text(encoding='utf-8'), repo, rule)
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
    return {rule['path'] for rule in _managed(repo, cfg)}
