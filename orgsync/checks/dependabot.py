"""dependabot.yml: generated from the manifests actually present in the repo."""

import hashlib
import posixpath
import re

import yaml

from ..config import Config, applies
from ..model import Repo, Result

PATH = '.github/dependabot.yml'
ALT_PATH = '.github/dependabot.yaml'
HEADER = '# Managed by org-sync, do not edit by hand.\n# Change org.toml in {org}/org-sync instead.\n'
DAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
IGNORED_DIRS = re.compile(r'(^|/)(node_modules|vendor|third_party|\.venv|bin|obj)/')

# ecosystem -> (manifest patterns tried in order, keep only the top-most matching directories)
ECOSYSTEMS: dict[str, tuple[list[str], bool]] = {
    'nuget': ([r'\.slnx?$', r'\.(cs|fs|vb)proj$'], True),
    'npm': ([r'^package\.json$'], True),
    'uv': ([r'^uv\.lock$'], True),
    'pip': ([r'^(requirements[^/]*\.txt|pyproject\.toml|Pipfile|setup\.py)$'], True),
    'gomod': ([r'^go\.mod$'], False),
    'cargo': ([r'^Cargo\.toml$'], True),
    'gradle': ([r'^(build|settings)\.gradle(\.kts)?$'], True),
    'docker': ([r'^Dockerfile[^/]*$|\.Dockerfile$'], False),
    'dotnet-sdk': ([r'^global\.json$'], False),
    'gitsubmodule': ([r'^\.gitmodules$'], False),
}
ORDER = [*ECOSYSTEMS, 'github-actions']
KNOWN_KEYS = {'package-ecosystem', 'directory', 'directories', 'schedule', 'groups', 'cooldown'}


def _topmost(dirs: set[str]) -> list[str]:
    return sorted(d for d in dirs if not any(d != o and (o == '/' or d.startswith(o + '/')) for o in dirs))


def detect(tree: list[str], enabled: list[str]) -> dict[str, list[str]]:
    """Maps each enabled ecosystem to the directories holding its manifests."""
    tree = [p for p in tree if not IGNORED_DIRS.search(p)]
    found: dict[str, list[str]] = {}
    for eco, (patterns, topmost) in ECOSYSTEMS.items():
        if eco not in enabled:
            continue
        for pattern in patterns:
            rx = re.compile(pattern)
            dirs = {'/' + posixpath.dirname(p) for p in tree if rx.search(posixpath.basename(p))}
            dirs = {d.rstrip('/') or '/' for d in dirs}
            if dirs:
                found[eco] = _topmost(dirs) if topmost else sorted(dirs)
                break
    # A directory with a uv lockfile is handled by the uv ecosystem, not pip.
    if 'uv' in found and 'pip' in found:
        found['pip'] = [d for d in found['pip'] if d not in found['uv']]
        if not found['pip']:
            del found['pip']
    if 'github-actions' in enabled:
        dirs = set()
        if any(p.startswith('.github/workflows/') for p in tree):
            dirs.add('/')
        # Dependabot only scans workflows and a root action.yml, composite actions need listing.
        for p in tree:
            if re.search(r'(^|/)action\.ya?ml$', p):
                dirs.add('/' + posixpath.dirname(p) if posixpath.dirname(p) else '/')
        if dirs:
            found['github-actions'] = sorted(dirs)
    return found


def schedule_for(repo: str, section: dict) -> tuple[str, str]:
    """An explicit slot from the config, otherwise a stable pseudo-random one."""
    for day, repos in section.get('schedule', {}).items():
        names = [r.lower() for r in repos]
        if repo.lower() in names:
            return day, f'{6 + names.index(repo.lower()) * 2 % 18:02d}:00'
    digest = int(hashlib.sha1(repo.lower().encode()).hexdigest(), 16)
    return DAYS[digest % 7], f'{6 + (digest // 7 % 9) * 2:02d}:00'


def render(org: str, ecosystems: dict[str, list[str]], day: str, time: str, cooldown_days: int = 0) -> str:
    out = [HEADER.format(org=org), 'version: 2', 'updates:']
    for eco in ORDER:
        dirs = ecosystems.get(eco)
        if not dirs:
            continue
        out += ['', f'  - package-ecosystem: {eco}']
        if len(dirs) == 1:
            out.append(f'    directory: {dirs[0]}')
        else:
            out.append('    directories:')
            out += [f'      - {d}' for d in dirs]
        out += ['    schedule:', '      interval: weekly', f'      day: {day}', f"      time: '{time}'"]
        if cooldown_days:
            out += ['    cooldown:', f'      default-days: {cooldown_days}']
        out += ['    groups:', f'      {eco}:', '        patterns:', "          - '*'"]
    return '\n'.join(out) + '\n'


def custom_keys(text: str) -> list[str]:
    """Settings in an existing file that regeneration would silently drop."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    keys = set(data) - {'version', 'updates'}
    for entry in data.get('updates') or []:
        if isinstance(entry, dict):
            keys |= set(entry) - KNOWN_KEYS
    return sorted(keys)


def _ecosystems_in(text: str) -> set[str]:
    return set(re.findall(r'package-ecosystem:\s*[\'"]?([\w-]+)', text))


def check(repo: Repo, cfg: Config) -> Result:
    section = cfg.section('dependabot')
    result = Result()
    if not applies(repo, section):
        return result
    override = {k.lower(): v for k, v in section.get('repos', {}).items()}.get(repo.name.lower(), {})
    if override.get('skip'):
        return result

    ecosystems = detect(repo.tree, override.get('ecosystems', section.get('ecosystems', ORDER)))
    ecosystems.update(override.get('directories', {}))
    existing_path = PATH if PATH in repo.tree else ALT_PATH if ALT_PATH in repo.tree else None
    current = repo.read(existing_path) if existing_path else None

    if not ecosystems:
        if current is not None:
            result.notes.append(f'{existing_path} exists but no supported manifests were found')
        return result
    if current is not None and not override.get('force'):
        extra = custom_keys(current)
        if extra:
            result.notes.append(f'{existing_path} uses custom keys ({", ".join(extra)}), set force or skip for this repo in org.toml')
            return result

    day, time = schedule_for(repo.name, section)
    wanted = render(repo.org, ecosystems, day, time, section.get('cooldown_days', 0))
    if current == wanted and existing_path == PATH:
        return result
    result.files[PATH] = wanted
    if existing_path == ALT_PATH:
        result.files[ALT_PATH] = None
    before = _ecosystems_in(current or '')
    added, dropped = sorted(set(ecosystems) - before), sorted(before - set(ecosystems))
    result.details.append(f'{"regenerate" if current else "add"} `{PATH}` ({", ".join(e for e in ORDER if e in ecosystems)}), {day} {time}')
    if current and added:
        result.details.append(f'newly covered: {", ".join(added)}')
    if dropped:
        result.details.append(f'no longer listed, no manifests found: {", ".join(dropped)}')
    return result
