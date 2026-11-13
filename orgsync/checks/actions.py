"""GitHub Actions: one pinned version per action org-wide, an allowlist, and banned-action removal."""

import datetime
import re

import yaml

from .. import gh
from ..config import Config, applies
from ..model import Repo, Result, join_lines, split_lines

WORKFLOW_FILES = r'^\.github/workflows/[^/]+\.ya?ml$|(^|/)action\.ya?ml$'

USES_RE = re.compile(
    r'^(?P<pre>\s*(?:-\s+)?uses:\s*)(?P<q>[\'"]?)(?P<action>[^@\s\'"]+)@(?P<ref>[^\s\'"#]+)(?P=q)(?P<rest>.*)$'
)
SHA_RE = re.compile(r'^[0-9a-f]{40}$')
VERSION_RE = re.compile(r'^v?(\d+(?:\.\d+)*)$')


def action_repo(action: str) -> str:
    """`github/codeql-action/init` lives in `github/codeql-action`."""
    return '/'.join(action.split('/')[:2])


def is_external(action: str) -> bool:
    return not action.startswith(('./', 'docker://'))


def parse_version(tag: str | None) -> tuple[int, ...] | None:
    m = VERSION_RE.match(tag or '')
    return tuple(int(p) for p in m[1].split('.')) if m else None


class Resolver:
    """Decides the one version every repo should use for an action, and its commit SHA."""

    def __init__(self, cfg: dict, now: datetime.datetime | None = None):
        self.pins: dict[str, str] = cfg.get('pin', {})
        self.cutoff = (now or datetime.datetime.now(datetime.UTC)) - datetime.timedelta(days=cfg.get('min_release_age_days', 0))
        self._latest: dict[str, str | None] = {}
        self._sha: dict[tuple[str, str], str | None] = {}

    def latest(self, repo: str) -> str | None:
        if repo in self.pins:
            return self.pins[repo]
        if repo not in self._latest:
            self._latest[repo] = self._fetch_latest(repo)
        return self._latest[repo]

    def _fetch_latest(self, repo: str) -> str | None:
        try:
            releases = gh.api(f'repos/{repo}/releases?per_page=50')
        except gh.GhError:
            return None
        best = None
        for rel in releases:
            version = parse_version(rel['tag_name'])
            # Skips things like codeql-bundle-* tags and releases still inside the cooldown.
            if version is None or rel['draft'] or rel['prerelease'] or not rel['published_at']:
                continue
            if datetime.datetime.fromisoformat(rel['published_at']) > self.cutoff:
                continue
            if best is None or version > best[0]:
                best = (version, rel['tag_name'])
        if best or releases:
            return best[1] if best else None
        # No releases at all: fall back to plain tags, which carry no date to apply the cooldown to.
        try:
            tags = [t['name'] for t in gh.api(f'repos/{repo}/tags?per_page=100')]
        except gh.GhError:
            return None
        versions = [(parse_version(t), t) for t in tags if parse_version(t)]
        return max(versions)[1] if versions else None

    def sha(self, repo: str, tag: str) -> str | None:
        key = (repo, tag)
        if key not in self._sha:
            try:
                self._sha[key] = gh.api(f'repos/{repo}/commits/{tag}')['sha']
            except gh.GhError:
                self._sha[key] = None
        return self._sha[key]


def pin_line(line: str, resolver: Resolver, log: list[str], problems: list[str]) -> str:
    m = USES_RE.match(line)
    if m is None or not is_external(m['action']):
        return line
    action, ref = m['action'], m['ref']
    repo = action_repo(action)
    comment = re.search(r'#\s*(\S+)', m['rest'])
    current = (comment[1] if comment else None) if SHA_RE.match(ref) else ref
    target = resolver.latest(repo)
    current_version, target_version = parse_version(current), parse_version(target)
    # Never downgrade: a repo may already be ahead of the cooldown window.
    if target is None or (current_version and target_version and current_version > target_version):
        target = current
    if target is None:
        problems.append(f'`{action}` is pinned to a SHA with no version comment, cannot tell what it is')
        return line
    sha = resolver.sha(repo, target)
    if sha is None:
        problems.append(f'`{action}@{target}` does not resolve to a commit')
        return line
    updated = f'{m["pre"]}{action}@{sha} # {target}'
    if ref == sha and current == target:
        # Already correct, leave spacing and any trailing comment untouched.
        return line
    if updated != line:
        if current != target:
            major = current_version and target_version and current_version[0] != target_version[0]
            log.append(f'`{action}` {current or ref[:7]} -> {target}{" (major)" if major else ""}')
        elif not SHA_RE.match(ref):
            log.append(f'`{action}` pinned to the SHA of {target}')
        elif ref != sha:
            log.append(f'`{action}` SHA did not match {target}, corrected')
    return updated


# --- banned action rewriting ---

_PLAIN_SCALAR = re.compile(r'^[A-Za-z0-9_./$(][^#\n]*$')


def _scalar(value) -> str:
    text = str(value)
    if _PLAIN_SCALAR.match(text) and ': ' not in text and not text.endswith((' ', ':')) and text not in ('true', 'false', 'null'):
        return text
    return "'" + text.replace("'", "''") + "'"


def gh_release_step(step: dict, indent: str) -> list[str] | None:
    """Rewrites a softprops/action-gh-release step as a `gh release create` call.

    Returns None for anything it does not fully understand, so the step is left for a human.
    """
    if set(step) - {'name', 'uses', 'with', 'if', 'env'}:
        return None
    inputs = dict(step.get('with') or {})
    known = {'tag_name', 'name', 'draft', 'prerelease', 'generate_release_notes', 'files', 'body', 'body_path', 'target_commitish', 'token'}
    if set(inputs) - known:
        return None

    # Every expression goes through env so nothing is interpolated into the script itself.
    env = dict(step.get('env') or {})
    env['GH_TOKEN'] = inputs.get('token', '${{ github.token }}')
    env['TAG'] = inputs.get('tag_name', '${{ github.ref_name }}')
    args = ['--repo "$GITHUB_REPOSITORY"']
    if 'target_commitish' in inputs:
        env['TARGET'] = inputs['target_commitish']
        args.append('--target "$TARGET"')
    elif env['TAG'] == '${{ github.ref_name }}':
        args.append('--verify-tag')
    for key, name, flag in (('name', 'TITLE', '--title'), ('body', 'NOTES', '--notes'), ('body_path', 'NOTES_FILE', '--notes-file')):
        if key in inputs:
            env[name] = inputs[key]
            args.append(f'{flag} "${name}"')
    for key, name, flag in (('draft', 'DRAFT', '--draft'), ('prerelease', 'PRERELEASE', '--prerelease'), ('generate_release_notes', 'GENERATE_NOTES', '--generate-notes')):
        value = inputs.get(key)
        if value is True or value == 'true':
            args.append(flag)
        elif isinstance(value, str) and '${{' in value:
            env[name] = value
            args.append(f'{flag}="${name}"')
        elif value not in (None, False, 'false'):
            return None
    if not any(a.startswith(('--notes', '--generate-notes')) for a in args):
        # Without any notes option gh would try to open an editor.
        args.append('--notes ""')
    files = [f.strip() for f in str(inputs.get('files', '')).splitlines() if f.strip()]
    # Globs are left unquoted for the shell to expand, so only allow harmless characters.
    if any(not re.fullmatch(r'[A-Za-z0-9_./*?\[\]{},-]+', f) for f in files):
        return None
    if any('\n' in str(v) for v in env.values()):
        return None

    lines = [f'{indent}- name: {_scalar(step.get("name", "Create release"))}']
    if 'if' in step:
        lines.append(f'{indent}  if: {_scalar(step["if"])}')
    lines.append(f'{indent}  env:')
    lines += [f'{indent}    {k}: {_scalar(v)}' for k, v in env.items()]
    lines.append(f'{indent}  run: |')
    command = ['gh release create "$TAG"', *args, *files]
    for i, part in enumerate(command):
        pad = '' if i == 0 else '  '
        lines.append(f'{indent}    {pad}{part}{" \\" if i < len(command) - 1 else ""}')
    return lines


REWRITERS = {'softprops/action-gh-release': gh_release_step}


def _steps(node):
    """Yields every mapping node in the document that has a `uses` key."""
    if isinstance(node, yaml.MappingNode):
        if any(k.value == 'uses' for k, _ in node.value):
            yield node
        for _, value in node.value:
            yield from _steps(value)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from _steps(item)


def rewrite_banned(text: str, log: list[str]) -> str:
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return text
    lines, newline, trailing = split_lines(text)
    # Bottom-up so earlier line numbers stay valid while replacing.
    for node in sorted(_steps(root), key=lambda n: -n.start_mark.line):
        uses = next(v.value for k, v in node.value if k.value == 'uses')
        action = str(uses).split('@')[0]
        rewriter = REWRITERS.get(action_repo(action))
        if rewriter is None:
            continue
        # Re-indent the first line so the slice parses as a standalone mapping.
        step = yaml.safe_load(' ' * node.start_mark.column + text[node.start_mark.index:node.end_mark.index])
        start, end = node.start_mark.line, node.end_mark.line
        # The end mark normally sits on the next token, so its line is only ours if we end mid-line.
        if end < len(lines) and lines[end][:node.end_mark.column].strip():
            end += 1
        # The node's end mark sits on the next token, so hand trailing blanks and comments back.
        while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith('#')):
            end -= 1
        dash = re.match(r'^(\s*)- ', lines[start])
        replacement = rewriter(step, dash[1]) if dash else None
        if replacement is None:
            continue
        lines[start:end] = replacement
        log.append(f'replaced `{action}` with a `gh release create` step')
    return join_lines(lines, newline, trailing)


def check(repo: Repo, cfg: Config, resolver: Resolver) -> Result:
    section = cfg.section('actions')
    result = Result()
    if not applies(repo, section):
        return result
    paths = repo.find(WORKFLOW_FILES)
    repo.load(paths)
    allowed_owners = {o.lower() for o in section.get('allowed_owners', [])}
    allowed = {a.lower() for a in section.get('allowed', [])}
    banned: dict[str, str] = {k.lower(): v for k, v in section.get('banned', {}).items()}
    seen: dict[str, str] = {}

    for path in paths:
        original = repo.read(path)
        if original is None:
            continue
        log: list[str] = []
        problems: list[str] = []
        text = rewrite_banned(original, log) if section.get('rewrite_banned', True) else original
        lines, newline, trailing = split_lines(text)
        for line in lines:
            m = USES_RE.match(line)
            if m and is_external(m['action']):
                seen.setdefault(action_repo(m['action']), path)
        lines = [pin_line(line, resolver, log, problems) for line in lines]
        text = join_lines(lines, newline, trailing)
        if text != original:
            result.files[path] = text
        result.details += [f'{path}: {entry}' for entry in dict.fromkeys(log)]
        result.notes += [f'{path}: {entry}' for entry in dict.fromkeys(problems)]

    for action, path in sorted(seen.items()):
        key = action.lower()
        if key in banned:
            result.notes.append(f'banned action `{action}` in {path}: {banned[key]}')
        elif key.split('/')[0] not in allowed_owners and key not in allowed:
            result.notes.append(f'`{action}` in {path} is not on the allowlist')
    return result
