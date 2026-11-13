"""Repository rulesets: the free-plan substitute for org-level rulesets."""

import json
import re

from .. import gh
from ..config import Config, applies
from ..model import ApiChange, Repo, Result

FIELDS = ('name', 'target', 'enforcement', 'conditions', 'rules', 'bypass_actors')


def normalize(ruleset: dict, like: dict | None = None) -> dict:
    """Reduces a ruleset to the fields that are compared, in a stable order.

    GitHub fills in parameters that were never asked for, so only the ones `like` sets are compared.
    """
    wanted = {r['type']: set(r.get('parameters') or {}) for r in (like or {}).get('rules', [])}
    rules = []
    for rule in ruleset.get('rules') or []:
        entry = {'type': rule['type']}
        parameters = rule.get('parameters') or {}
        if like is not None:
            parameters = {k: v for k, v in parameters.items() if k in wanted.get(rule['type'], set())}
        if parameters:
            entry['parameters'] = parameters
        rules.append(entry)
    conditions = ruleset.get('conditions') or {}
    ref_name = conditions.get('ref_name') or {}
    actors = [
        {'actor_id': a.get('actor_id'), 'actor_type': a['actor_type'], 'bypass_mode': a.get('bypass_mode', 'always')}
        for a in ruleset.get('bypass_actors') or []
    ]
    return {
        'target': ruleset.get('target', 'branch'),
        'enforcement': ruleset.get('enforcement', 'active'),
        'conditions': {'ref_name': {'include': sorted(ref_name.get('include', [])), 'exclude': sorted(ref_name.get('exclude', []))}},
        'rules': sorted(rules, key=lambda r: r['type']),
        'bypass_actors': sorted(actors, key=lambda a: (a['actor_type'], str(a['actor_id']))),
    }


def desired_rulesets(repo: Repo, cfg: Config) -> dict[str, dict]:
    """Every ruleset this repo should have, by name: the shared ones assigned to it plus its own folder."""
    wanted: dict[str, dict] = {}
    for rule in cfg.section('rulesets').get('managed', []):
        if not applies(repo, rule):
            continue
        ruleset = json.loads(cfg.path(rule['file']).read_text(encoding='utf-8'))
        # Some repos have automation that must be able to write to protected branches.
        extra = {k.lower(): v for k, v in rule.get('extra_bypass', {}).items()}.get(repo.name.lower(), [])
        if extra:
            ruleset['bypass_actors'] = [*ruleset.get('bypass_actors', []), *extra]
        wanted[ruleset['name']] = ruleset
    for folder in cfg.path('rulesets/repos').glob('*'):
        if folder.is_dir() and folder.name.lower() == repo.name.lower():
            for path in sorted(folder.glob('*.json')):
                ruleset = json.loads(path.read_text(encoding='utf-8'))
                wanted[ruleset['name']] = ruleset
    return wanted


def live_rulesets(repo: Repo) -> dict[str, dict] | None:
    """Rulesets defined on the repo itself, by name. None when the plan does not allow rulesets."""
    try:
        live = gh.api(f'repos/{repo.slug}/rulesets?includes_parents=false', paginate=True)
    except gh.GhError as e:
        if e.status == 403:
            return None
        raise
    return {r['name']: r for r in live if r.get('source_type') == 'Repository'}


def slug(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def import_missing(repo: Repo, cfg: Config) -> list[str]:
    """Writes every ruleset that exists on GitHub but not here into rulesets/repos/<repo>/."""
    live = live_rulesets(repo)
    if not live:
        return []
    known = desired_rulesets(repo, cfg)
    written = []
    for name, summary in sorted(live.items()):
        if name in known:
            continue
        detail = gh.api(f'repos/{repo.slug}/rulesets/{summary["id"]}')
        actors = [{k: a[k] for k in ('actor_id', 'actor_type', 'bypass_mode') if k in a} for a in detail.get('bypass_actors') or []]
        content = {**{k: detail[k] for k in FIELDS if k in detail}, 'bypass_actors': actors}
        path = cfg.path(f'rulesets/repos/{repo.name}/{slug(name)}.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content, indent=2) + '\n', encoding='utf-8')
        written.append(str(path.relative_to(cfg.root)))
    return written


def check(repo: Repo, cfg: Config) -> Result:
    result = Result()
    wanted = desired_rulesets(repo, cfg)
    by_name = live_rulesets(repo)
    if by_name is None:
        if wanted:
            result.notes.append('rulesets are unavailable, private repo on the free plan')
        return result

    for name, desired in wanted.items():
        body = {k: desired[k] for k in FIELDS if k in desired}
        existing = by_name.get(name)
        if existing is None:
            result.api.append(ApiChange(f'create ruleset `{name}`', 'POST', f'repos/{repo.slug}/rulesets', body))
            continue
        detail = gh.api(f'repos/{repo.slug}/rulesets/{existing["id"]}')
        if normalize(detail, like=desired) != normalize(desired):
            result.api.append(ApiChange(f'update ruleset `{name}`', 'PUT', f'repos/{repo.slug}/rulesets/{existing["id"]}', body))

    prune = cfg.section('rulesets').get('prune', False)
    for name in sorted(set(by_name) - set(wanted)):
        if prune:
            result.api.append(ApiChange(f'delete ruleset `{name}`, it has no file here', 'DELETE', f'repos/{repo.slug}/rulesets/{by_name[name]["id"]}'))
        else:
            result.notes.append(f'unmanaged ruleset `{name}` ({by_name[name]["enforcement"]})')
    return result
