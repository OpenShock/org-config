"""Repo snapshots and the change set a check produces."""

import json
import re
from dataclasses import dataclass, field

from . import gh


@dataclass
class ApiChange:
    summary: str
    method: str
    endpoint: str
    body: dict | None = None


@dataclass
class Result:
    """What one check wants to change in one repo."""
    files: dict[str, str | None] = field(default_factory=dict)  # path -> new content, None deletes
    api: list[ApiChange] = field(default_factory=list)
    details: list[str] = field(default_factory=list)  # explains the changes, ends up in the PR body
    notes: list[str] = field(default_factory=list)  # needs a human, nothing is changed
    direct: set[str] = field(default_factory=set)  # paths in `files` that skip the PR and go straight to the default branch
    direct_details: list[str] = field(default_factory=list)  # explains those, ends up in the commit message

    @property
    def drift(self) -> bool:
        return bool(self.files or self.api)


@dataclass
class Repo:
    org: str
    name: str
    default_branch: str
    head_oid: str
    private: bool
    tree: list[str] = field(default_factory=list)
    truncated: bool = False
    _files: dict[str, str | None] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        return f'{self.org}/{self.name}'

    def find(self, pattern: str) -> list[str]:
        rx = re.compile(pattern)
        return [p for p in self.tree if rx.search(p)]

    def load(self, paths: list[str]) -> None:
        """Fetch several files in one GraphQL request."""
        missing = [p for p in paths if p not in self._files]
        for start in range(0, len(missing), 40):
            chunk = missing[start:start + 40]
            fields = ' '.join(
                f'f{i}: object(expression: {json.dumps(f"{self.head_oid}:{p}")}) {{ ... on Blob {{ text isTruncated }} }}'
                for i, p in enumerate(chunk)
            )
            query = f'query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {fields} }} }}'
            data = gh.graphql(query, {'owner': self.org, 'name': self.name})['repository']
            for i, p in enumerate(chunk):
                blob = data.get(f'f{i}')
                if blob and blob.get('isTruncated'):
                    self._files[p] = _raw(self.slug, p, self.head_oid)
                else:
                    self._files[p] = blob.get('text') if blob else None

    def read(self, path: str) -> str | None:
        if path not in self.tree:
            return None
        self.load([path])
        return self._files[path]


def _raw(slug: str, path: str, ref: str) -> str | None:
    try:
        return gh._run(['api', f'repos/{slug}/contents/{path}?ref={ref}', '-H', 'Accept: application/vnd.github.raw'])
    except gh.GhError as e:
        if e.status == 404:
            return None
        raise


def read_at(slug: str, path: str, ref: str) -> str | None:
    """File content on an arbitrary ref, None when absent."""
    return _raw(slug, path, ref)


_REPOS_QUERY = '''
query($org: String!, $cursor: String) {
  organization(login: $org) {
    repositories(first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name isArchived isFork isPrivate
        defaultBranchRef { name target { oid } }
      }
    }
  }
}
'''


def list_repos(org: str) -> list[Repo]:
    """Active, non-fork, non-empty repos of the org, without their trees."""
    repos, cursor = [], None
    while True:
        page = gh.graphql(_REPOS_QUERY, {'org': org, 'cursor': cursor})['organization']['repositories']
        for node in page['nodes']:
            ref = node['defaultBranchRef']
            if node['isArchived'] or node['isFork'] or ref is None:
                continue
            repos.append(Repo(org, node['name'], ref['name'], ref['target']['oid'], node['isPrivate']))
        if not page['pageInfo']['hasNextPage']:
            break
        cursor = page['pageInfo']['endCursor']
    return sorted(repos, key=lambda r: r.name.lower())


def load_tree(repo: Repo) -> None:
    data = gh.api(f'repos/{repo.slug}/git/trees/{repo.head_oid}?recursive=1')
    repo.tree = [e['path'] for e in data['tree'] if e['type'] == 'blob']
    repo.truncated = data.get('truncated', False)


def split_lines(text: str) -> tuple[list[str], str, bool]:
    """Lines, the newline style in use, and whether the text ends with a newline."""
    newline = '\r\n' if '\r\n' in text else '\n'
    return text.splitlines(), newline, text.endswith(('\n', '\r'))


def join_lines(lines: list[str], newline: str, trailing: bool) -> str:
    return newline.join(lines) + (newline if trailing else '')
