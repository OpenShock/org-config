"""renovate.json: every repo owns its rules, the only thing enforced is that it extends the shared presets.

A file that already extends them is never touched. Renovate is never added to a repo without it.
"""

import json

from ..config import Config, applies
from ..model import Repo, Result

# Keys that are never dropped for being equal to a preset's.
STRUCTURAL = ('$schema', 'description', 'extends', 'packageRules')


def references(cfg: Config) -> list[str]:
    """The presets as a repo's `extends` names them."""
    section = cfg.section('renovate')
    source = section['source'].replace('{org}', cfg.org)
    return [source + preset.removesuffix('.json') for preset in section.get('presets', [])]


def _extends(config: dict) -> list[str]:
    value = config.get('extends', [])
    return [value] if isinstance(value, str) else list(value)


def _rule(rule: dict) -> dict:
    return {k: v for k, v in rule.items() if k != 'description'}


def adopt(config: dict, wanted: list[str], presets: list[dict]) -> dict:
    """Puts the shared presets first in `extends` and drops whatever they already provide.

    What is left is what the repo really decides for itself, and it comes later, so it still wins.
    """
    provided_extends = {e for preset in presets for e in _extends(preset)}
    provided_rules = [_rule(r) for preset in presets for r in preset.get('packageRules', [])]
    extends = [*wanted, *(e for e in _extends(config) if e not in wanted and e not in provided_extends)]

    adopted: dict = {}
    for key, value in config.items():
        if key == 'extends':
            adopted[key] = extends
        elif key == 'packageRules':
            own = [r for r in value if _rule(r) not in provided_rules]
            if own:
                adopted[key] = own
        elif key in STRUCTURAL or not any(key in preset and preset[key] == value for preset in presets):
            adopted[key] = value
    if 'extends' not in adopted:
        # First, or right after $schema.
        items = list(adopted.items())
        at = 1 if items and items[0][0] == '$schema' else 0
        adopted = dict([*items[:at], ('extends', extends), *items[at:]])
    return adopted


def _matchers(rule: dict) -> dict:
    return {k: v for k, v in rule.items() if k.startswith('match')}


def handed_over(repo: Repo, cfg: Config) -> list[dict]:
    """Rules waiting in the handover folder for this repo, see the README."""
    folder = cfg.path(cfg.section('renovate').get('handover', 'renovate/handover'))
    return [
        rule
        for path in sorted(folder.glob('*.json')) if path.stem.lower() == repo.name.lower()
        for rule in json.loads(path.read_text(encoding='utf-8')).get('packageRules', [])
    ]


def hand_over(config: dict, rules: list[dict]) -> dict:
    """Makes sure the file carries each rule, updating the repo's own rule when it matches the same things."""
    if not rules:
        return config
    own = [dict(r) for r in config.get('packageRules', [])]
    for rule in rules:
        existing = next((r for r in own if _matchers(r) == _matchers(rule)), None)
        if existing is None:
            own.append(dict(rule))
        else:
            existing.update(rule)
    return {**config, 'packageRules': own}


def dump(value, depth: int = 0) -> str:
    """JSON with two-space indentation, keeping short lists of plain values on one line."""
    pad = '  ' * depth
    if isinstance(value, dict) and value:
        items = [f'{pad}  {json.dumps(k, ensure_ascii=False)}: {dump(v, depth + 1)}' for k, v in value.items()]
        return '{\n' + ',\n'.join(items) + f'\n{pad}}}'
    if isinstance(value, list) and value:
        inline = json.dumps(value, ensure_ascii=False)
        if len(inline) <= 120 and not any(isinstance(v, (dict, list)) for v in value):
            return inline
        return '[\n' + ',\n'.join(f'{pad}  {dump(v, depth + 1)}' for v in value) + f'\n{pad}]'
    return json.dumps(value, ensure_ascii=False)


def check(repo: Repo, cfg: Config) -> Result:
    section = cfg.section('renovate')
    result = Result()
    path = section.get('path', 'renovate.json')
    if not section.get('presets') or not applies(repo, section) or path not in repo.tree:
        return result
    wanted = references(cfg)
    try:
        config = json.loads(repo.read(path) or '')
    except ValueError:
        config = None
    if not isinstance(config, dict):
        result.notes.append(f'{path} is not plain JSON, cannot tell whether it extends the shared presets')
        return result
    updated = config
    missing = [w for w in wanted if w not in _extends(config)]
    if missing:
        presets = [json.loads(cfg.path(p).read_text(encoding='utf-8')) for p in section['presets']]
        updated = adopt(config, wanted, presets)
        result.details.append(f'`{path}`: extend {", ".join(f"`{m}`" for m in missing)}, drop what the presets already provide')
    rules = handed_over(repo, cfg)
    if hand_over(updated, rules) != updated:
        updated = hand_over(updated, rules)
        subjects = ', '.join(f'`{name}`' for rule in rules for name in rule.get('matchPackageNames', ['...']))
        result.details.append(f'`{path}`: this repo now carries the rule for {subjects} itself, it is leaving the shared presets')
    if updated != config:
        result.files[path] = dump(updated) + '\n'
    return result
