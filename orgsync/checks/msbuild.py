"""Directory.Build.props: enforces a set of properties without touching the rest of the file."""

import posixpath
import re
from xml.sax.saxutils import escape

from ..config import Config, applies
from ..model import Repo, Result

PROPS = r'(^|/)Directory\.Build\.props$'
PROJECTS = r'\.(cs|fs|vb)proj$'


def set_property(text: str, name: str, value: str) -> tuple[str, str | None]:
    """Returns the new text and a description of the change, or None when already correct."""
    value = escape(value)
    existing = re.search(rf'<{name}>(.*?)</{name}>', text, re.S)
    if existing:
        if existing[1].strip() == value:
            return text, None
        updated = text[:existing.start(1)] + value + text[existing.end(1):]
        return updated, f'{name}: `{existing[1].strip()}` -> `{value}`'

    # Not present: append to the first unconditional PropertyGroup, matching its indentation.
    group = re.search(r'<PropertyGroup>(.*?)^([ \t]*)</PropertyGroup>', text, re.S | re.M)
    if group is None:
        return text, None
    child = re.search(r'^([ \t]*)<', group[1].lstrip('\r\n'), re.M)
    indent = child[1] if child else group[2] + '  '
    newline = '\r\n' if '\r\n' in text else '\n'
    insert_at = group.start(2)
    updated = text[:insert_at] + f'{indent}<{name}>{value}</{name}>{newline}' + text[insert_at:]
    return updated, f'{name}: added `{value}`'


def _inside(directory: str, parent: str) -> bool:
    return directory != parent and (parent == '' or directory.startswith(parent + '/'))


def check(repo: Repo, cfg: Config) -> Result:
    section = cfg.section('msbuild')
    result = Result()
    properties: dict[str, str] = section.get('properties', {})
    if not properties or not applies(repo, section) or not repo.find(PROJECTS):
        return result
    files = repo.find(PROPS)
    # Nested props files usually import their parent, so only the top-most ones are authoritative.
    files = [p for p in files if not any(_inside(posixpath.dirname(p), posixpath.dirname(o)) for o in files)]
    if not files:
        result.notes.append('C# projects but no Directory.Build.props, shared properties live in the project files')
        return result
    repo.load(files)
    for path in files:
        original = text = repo.read(path) or ''
        for name, template in properties.items():
            value = template.replace('{org}', repo.org).replace('{repo}', repo.name)
            text, change = set_property(text, name, value)
            if change:
                result.details.append(f'{path}: {change}')
        if text != original:
            result.files[path] = text
    return result
