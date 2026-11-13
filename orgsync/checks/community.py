"""Community health files: mostly report-only rules about what must, and must not, be in a repo."""

import re

import yaml

from ..config import Config, applies
from ..model import Repo, Result
from .files import managed_paths

TEMPLATE_DIR = '.github/ISSUE_TEMPLATE/'
ADVISORY_SUFFIX = '/security/advisories/new'
CONTACT_LINK = '''contact_links:
  - name: Report a security vulnerability
    url: https://github.com/{org}/{repo}/security/advisories/new
    about: Never report security vulnerabilities in public issues. Report them privately here, as described in SECURITY.md.
'''


def _matching(repo: Repo, patterns: list[str]) -> list[str]:
    compiled = [re.compile(p) for p in patterns]
    return [path for path in repo.tree if any(rx.fullmatch(path) for rx in compiled)]


def _squash(text: str) -> str:
    """Hand-written files wrap lines wherever they like, so compare with whitespace collapsed."""
    return ' '.join(text.split())


def _fill(text: str, repo: Repo) -> str:
    return text.replace('{org}', repo.org).replace('{repo}', repo.name)


def missing_snippets(text: str, snippets: list[str]) -> list[str]:
    haystack = _squash(text)
    return [s for s in snippets if _squash(s) not in haystack]


def has_security_contact(config_text: str) -> bool:
    try:
        data = yaml.safe_load(config_text) or {}
    except yaml.YAMLError:
        return False
    links = data.get('contact_links') if isinstance(data, dict) else None
    return any(str(link.get('url', '')).rstrip('/').endswith(ADVISORY_SUFFIX) for link in links or [] if isinstance(link, dict))


def _issue_templates(repo: Repo, rule: dict, result: Result) -> None:
    # A repo with any template of its own inherits nothing from the org, including the org config.yml.
    if not any(p.startswith(TEMPLATE_DIR) for p in repo.tree):
        return
    path = next((p for p in (TEMPLATE_DIR + 'config.yml', TEMPLATE_DIR + 'config.yaml') if p in repo.tree), None)
    if path is None:
        if rule.get('create_missing', True):
            result.files[TEMPLATE_DIR + 'config.yml'] = _fill(CONTACT_LINK, repo)
            result.details.append(f'add `{TEMPLATE_DIR}config.yml` with a private security reporting link')
        else:
            result.notes.append(f'has its own issue templates but no `{TEMPLATE_DIR}config.yml` with a security contact link')
    elif not has_security_contact(repo.read(path) or ''):
        # Existing files keep their own settings and links, so a human adds the entry.
        result.notes.append(f'{path} has no contact link ending in `{ADVISORY_SUFFIX}`')


def check(repo: Repo, cfg: Config) -> Result:
    section = cfg.section('community')
    result = Result()

    if repo.name == '.github' and repo.private:
        result.notes.append('the .github repo is private, no repo inherits its community files')

    # A file the files check is about to add is not missing.
    managed = managed_paths(repo, cfg)
    for rule in section.get('required_files', []):
        if not applies(repo, rule) or _matching(repo, rule['any_of']):
            continue
        if not any(re.fullmatch(pattern, path) for pattern in rule['any_of'] for path in managed):
            result.notes.append(f'missing {rule["name"]}')

    for rule in section.get('forbidden_files', []):
        if not applies(repo, rule):
            continue
        for path in _matching(repo, rule['files']):
            if rule.get('delete', False):
                result.files[path] = None
                result.details.append(f'remove `{path}`: {rule["reason"]}')
            else:
                result.notes.append(f'`{path}` should not exist: {rule["reason"]}')

    for rule in section.get('must_contain', []):
        if not applies(repo, rule):
            continue
        paths = _matching(repo, rule['files'])
        if not paths and rule.get('missing', 'ignore') == 'report':
            result.notes.append(f'missing {rule["name"]}')
        repo.load(paths)
        for path in paths:
            snippets = [_fill(s, repo) for s in rule['contains']]
            for snippet in missing_snippets(repo.read(path) or '', snippets):
                result.notes.append(f'{path} does not contain `{snippet}` ({rule["name"]})')

    for rule in section.get('must_not_contain', []):
        if not applies(repo, rule):
            continue
        pattern = re.compile(rule['pattern'], re.I)
        paths = _matching(repo, rule['files'])
        repo.load(paths)
        for path in paths:
            lines = [str(n) for n, line in enumerate((repo.read(path) or '').splitlines(), 1) if pattern.search(line)]
            if lines:
                result.notes.append(f'{path} line {", ".join(lines)}: {rule["reason"]}')

    issue_templates = section.get('issue_templates', {})
    if issue_templates.get('enabled', False) and applies(repo, issue_templates):
        _issue_templates(repo, issue_templates, result)
    return result
