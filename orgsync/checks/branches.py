"""Orphaned topic branches: merged or abandoned work that was never cleaned up."""

import datetime
from urllib.parse import quote

from .. import gh
from ..config import Config, applies
from ..model import ApiChange, Repo, Result

QUERY = '''
query($owner: String!, $name: String!, $default: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    refs(refPrefix: "refs/heads/", first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        target { oid ... on Commit { committedDate } }
        compare(headRef: $default) { behindBy }
        associatedPullRequests(first: 20, orderBy: {field: CREATED_AT, direction: DESC}) {
          nodes { number state headRefOid }
        }
      }
    }
  }
}
'''


def classify(branch: dict, section: dict, now: datetime.datetime) -> tuple[str, str]:
    """Returns (verdict, reason) where verdict is keep, delete or report.

    `branch` has: name, oid, date, unmerged (commits not on the default branch) and prs (newest first).
    """
    prs = branch['prs']
    if any(pr['state'] == 'OPEN' for pr in prs):
        return 'keep', 'open pull request'
    merged = next((pr for pr in prs if pr['state'] == 'MERGED'), None)
    if merged and merged['headRefOid'] == branch['oid']:
        return 'delete', f'PR #{merged["number"]} was merged'
    if branch['unmerged'] == 0:
        return 'delete', 'no commits that are not on the default branch'
    if branch['name'].startswith('dependabot/'):
        return 'delete', 'dependabot branch without an open PR'

    age = (now - branch['date']).days
    if age < section.get('stale_days', 90):
        return 'keep', 'recent work'
    if merged:
        context = f'has commits pushed after PR #{merged["number"]} was merged'
    elif prs:
        context = f'PR #{prs[0]["number"]} was closed without merging'
    else:
        context = 'never had a PR'
    reason = f'{branch["unmerged"]} unmerged commits, untouched for {age} days, {context}'
    return ('delete' if section.get('delete_stale_unmerged', False) else 'report'), reason


def fetch(repo: Repo) -> list[dict]:
    branches, cursor = [], None
    while True:
        variables = {'owner': repo.org, 'name': repo.name, 'default': repo.default_branch, 'cursor': cursor}
        page = gh.graphql(QUERY, variables)['repository']['refs']
        for node in page['nodes']:
            branches.append({
                'name': node['name'],
                'oid': node['target']['oid'],
                'date': datetime.datetime.fromisoformat(node['target']['committedDate']),
                # Comparing from the branch towards the default branch, so "behind" is what only the branch has.
                'unmerged': node['compare']['behindBy'],
                'prs': node['associatedPullRequests']['nodes'],
            })
        if not page['pageInfo']['hasNextPage']:
            return branches
        cursor = page['pageInfo']['endCursor']


def check(repo: Repo, cfg: Config) -> Result:
    section = cfg.section('branches')
    result = Result()
    if not applies(repo, section):
        return result
    prefixes = tuple(section.get('prefixes', []))
    protected = {repo.default_branch, *section.get('protected', [])}
    now = datetime.datetime.now(datetime.UTC)
    for branch in fetch(repo):
        name = branch['name']
        if name in protected or not name.startswith(prefixes):
            continue
        verdict, reason = classify(branch, section, now)
        if verdict == 'delete':
            # The SHA is printed so a deleted branch can always be recreated.
            summary = f'delete branch `{name}` at {branch["oid"][:10]}: {reason}'
            result.api.append(ApiChange(summary, 'DELETE', f'repos/{repo.slug}/git/refs/heads/{quote(name)}'))
        elif verdict == 'report':
            result.notes.append(f'stale branch `{name}` at {branch["oid"][:10]}: {reason}')
    return result
