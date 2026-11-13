"""Repository rulesets: the free-plan substitute for org-level rulesets."""

import json

from .. import gh
from ..config import Config, applies
from ..model import ApiChange, Repo, Result

FIELDS = ('name', 'target', 'enforcement', 'conditions', 'rules', 'bypass_actors')


def normalize(ruleset: dict, like: dict | None = None) -> dict:
    """Reduces a ruleset to the fields that are compared, in a stable order.

    GitHub fills in default parameters, so parameters are dropped for rules where `like` has none.
    """
    bare = {r['type'] for r in (like or {}).get('rules', []) if not r.get('parameters')}
    rules = []
    for rule in ruleset.get('rules') or []:
        entry = {'type': rule['type']}
        if rule.get('parameters') and rule['type'] not in bare:
            entry['parameters'] = rule['parameters']
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


def check(repo: Repo, cfg: Config) -> Result:
    result = Result()
    rules = [r for r in cfg.section('rulesets').get('managed', []) if applies(repo, r)]
    if not rules:
        return result
    try:
        live = gh.api(f'repos/{repo.slug}/rulesets?includes_parents=false', paginate=True)
    except gh.GhError as e:
        if e.status == 403:
            result.notes.append('rulesets are unavailable, private repo on the free plan')
            return result
        raise
    by_name = {r['name']: r for r in live if r.get('source_type') == 'Repository'}

    managed = set()
    for rule in rules:
        desired = json.loads(cfg.path(rule['file']).read_text(encoding='utf-8'))
        body = {k: desired[k] for k in FIELDS if k in desired}
        managed.add(desired['name'])
        existing = by_name.get(desired['name'])
        if existing is None:
            result.api.append(ApiChange(f'create ruleset `{desired["name"]}`', 'POST', f'repos/{repo.slug}/rulesets', body))
            continue
        detail = gh.api(f'repos/{repo.slug}/rulesets/{existing["id"]}')
        if normalize(detail, like=desired) != normalize(desired):
            result.api.append(ApiChange(f'update ruleset `{desired["name"]}`', 'PUT', f'repos/{repo.slug}/rulesets/{existing["id"]}', body))

    # Never deleted automatically: an unmanaged ruleset may be protecting something on purpose.
    for name in sorted(set(by_name) - managed):
        result.notes.append(f'unmanaged ruleset `{name}` ({by_name[name]["enforcement"]})')
    return result
