import datetime
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from orgsync import apply, gh
from orgsync.checks import BY_NAME, actions, branches, community, files, msbuild, rulesets
from orgsync.config import Config
from orgsync.model import Repo, Result

NOW = datetime.datetime(2026, 11, 13, tzinfo=datetime.UTC)


WORKFLOW = '''jobs:
  release:
    steps:
      - uses: actions/checkout@v7
      - name: Create release
        uses: softprops/action-gh-release@8a0cd651e5b2365bd640854a3fa5dfc70e4875f3 # v3.0.4
        with:
          draft: true
          prerelease: ${{ contains(github.ref_name, '-rc.') }}
          generate_release_notes: true
          files: |
            artifacts/a.zip
            artifacts/b-*.AppImage

      # trailing comment
      - run: echo done
'''


class RewriteBanned(unittest.TestCase):

    def test_gh_release_step(self):
        log = []
        out = actions.rewrite_banned(WORKFLOW, log)
        self.assertNotIn('softprops', out)
        self.assertIn('          gh release create "$TAG" \\\n', out)
        self.assertIn("          PRERELEASE: ${{ contains(github.ref_name, '-rc.') }}\n", out)
        self.assertIn('            --prerelease="$PRERELEASE" \\\n', out)
        self.assertIn('            artifacts/b-*.AppImage\n\n      # trailing comment\n      - run: echo done\n', out)
        self.assertEqual(len(log), 1)

    def test_unknown_input_is_left_alone(self):
        text = WORKFLOW.replace('draft: true', 'append_body: true')
        self.assertEqual(actions.rewrite_banned(text, []), text)

    def test_step_with_id_is_left_alone(self):
        text = WORKFLOW.replace('- name: Create release', '- id: rel')
        self.assertEqual(actions.rewrite_banned(text, []), text)


PROPS = '''<Project>
    <PropertyGroup>
        <Company>Other</Company>
        <Version>1.0.0</Version>
    </PropertyGroup>
</Project>
'''


class MsBuild(unittest.TestCase):

    def test_update_existing(self):
        text, change = msbuild.set_property(PROPS, 'Company', 'OpenShock')
        self.assertIn('<Company>OpenShock</Company>', text)
        self.assertIn('Other', change)

    def test_insert_keeps_indentation(self):
        text, _ = msbuild.set_property(PROPS, 'Authors', 'A & B')
        self.assertIn('        <Version>1.0.0</Version>\n        <Authors>A &amp; B</Authors>\n    </PropertyGroup>', text)

    def test_noop(self):
        self.assertEqual(msbuild.set_property(PROPS, 'Version', '1.0.0'), (PROPS, None))


class Rulesets(unittest.TestCase):

    def test_live_defaults_do_not_count_as_drift(self):
        desired = {'name': 'x', 'rules': [{'type': 'deletion'}], 'conditions': {'ref_name': {'include': ['b', 'a'], 'exclude': []}}}
        live = {'name': 'x', 'target': 'branch', 'enforcement': 'active', 'id': 1,
                'rules': [{'type': 'deletion', 'parameters': {'x': 1}}],
                'conditions': {'ref_name': {'include': ['a', 'b'], 'exclude': []}}, 'bypass_actors': []}
        self.assertEqual(rulesets.normalize(live, like=desired), rulesets.normalize(desired))
        desired['rules'].append({'type': 'pull_request', 'parameters': {'required_approving_review_count': 1}})
        live['rules'].append({'type': 'pull_request', 'parameters': {'required_approving_review_count': 1, 'allowed_merge_methods': ['squash']}})
        self.assertEqual(rulesets.normalize(live, like=desired), rulesets.normalize(desired))
        live['rules'][1]['parameters']['required_approving_review_count'] = 2
        self.assertNotEqual(rulesets.normalize(live, like=desired), rulesets.normalize(desired))
        live['rules'][1]['parameters']['required_approving_review_count'] = 1
        live['rules'].append({'type': 'non_fast_forward'})
        self.assertNotEqual(rulesets.normalize(live, like=desired), rulesets.normalize(desired))


def make_config(root: pathlib.Path, toml: str, files: dict[str, object]) -> Config:
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content if isinstance(content, str) else json.dumps(content), encoding='utf-8')
    (root / 'org.toml').write_text('org = "Org"\n' + toml, encoding='utf-8')
    return Config(root / 'org.toml')


def ruleset(name: str, *rules: str, bypass: list | None = None) -> dict:
    return {'name': name, 'target': 'branch', 'enforcement': 'active',
            'conditions': {'ref_name': {'include': ['~DEFAULT_BRANCH'], 'exclude': []}},
            'rules': [{'type': r} for r in rules], 'bypass_actors': bypass or []}


BOT = {'actor_id': 7, 'actor_type': 'Integration', 'bypass_mode': 'always'}
RULESETS_TOML = '''
[rulesets]
prune = {prune}
[[rulesets.managed]]
file = "rulesets/shared/main.json"
[[rulesets.managed]]
file = "rulesets/shared/reviews.json"
include = ["App", "Lib"]
[rulesets.managed.extra_bypass]
app = [{{ actor_id = 7, actor_type = "Integration", bypass_mode = "always" }}]
'''


class FakeGitHub:
    """Answers the ruleset endpoints from a dict of live rulesets and records nothing else."""

    def __init__(self, live: list[dict] | None):
        self.live = live

    def __call__(self, path, method='GET', body=None, paginate=False):
        if self.live is None:
            raise gh.GhError(403, 'Upgrade to GitHub Pro')
        if '/rulesets?' in path:
            return [{'id': r['id'], 'name': r['name'], 'enforcement': r['enforcement'], 'source_type': 'Repository'} for r in self.live]
        return next(r for r in self.live if path.endswith(f'/rulesets/{r["id"]}'))


class RulesetFiles(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)

    def config(self, prune: bool = False, extra: dict | None = None) -> Config:
        return make_config(self.root, RULESETS_TOML.format(prune=str(prune).lower()), {
            'rulesets/shared/main.json': ruleset('main', 'deletion'),
            'rulesets/shared/reviews.json': ruleset('reviews', 'pull_request'),
            'rulesets/repos/Lib/tags.json': ruleset('tags', 'deletion'),
            'rulesets/repos/Lib/anything.json': ruleset('main', 'deletion', 'non_fast_forward'),
            **(extra or {}),
        })

    def plan(self, repo: Repo, cfg: Config, live: list[dict] | None):
        with mock.patch.object(gh, 'api', FakeGitHub(live)):
            return rulesets.check(repo, cfg)

    def test_shared_rulesets_follow_include(self):
        wanted = rulesets.desired_rulesets(Repo('Org', 'Other', 'main', 'abc', False), self.config())
        self.assertEqual(list(wanted), ['main'])

    def test_repo_folder_adds_and_replaces_by_name(self):
        wanted = rulesets.desired_rulesets(Repo('Org', 'lib', 'main', 'abc', False), self.config())
        self.assertEqual(sorted(wanted), ['main', 'reviews', 'tags'])
        self.assertEqual([r['type'] for r in wanted['main']['rules']], ['deletion', 'non_fast_forward'])

    def test_extra_bypass_is_per_repo(self):
        cfg = self.config()
        self.assertEqual(rulesets.desired_rulesets(Repo('Org', 'App', 'main', 'abc', False), cfg)['reviews']['bypass_actors'], [BOT])
        self.assertEqual(rulesets.desired_rulesets(Repo('Org', 'Lib', 'main', 'abc', False), cfg)['reviews']['bypass_actors'], [])

    def test_create_update_and_leave_alone(self):
        live = [{**ruleset('main', 'deletion'), 'id': 1}, {**ruleset('reviews', 'pull_request', bypass=[]), 'id': 2}]
        result = self.plan(Repo('Org', 'App', 'main', 'abc', False), self.config(), live)
        self.assertEqual([(c.method, c.endpoint) for c in result.api], [('PUT', 'repos/Org/App/rulesets/2')])
        self.assertEqual(result.api[0].body['bypass_actors'], [BOT])
        result = self.plan(Repo('Org', 'Other', 'main', 'abc', False), self.config(), [])
        self.assertEqual([(c.method, c.endpoint) for c in result.api], [('POST', 'repos/Org/Other/rulesets')])

    def test_unmanaged_is_reported_or_pruned(self):
        repo = Repo('Org', 'Other', 'main', 'abc', False)
        live = [{**ruleset('main', 'deletion'), 'id': 1}, {**ruleset('old', 'deletion'), 'id': 9}]
        result = self.plan(repo, self.config(prune=False), live)
        self.assertEqual((result.api, result.notes), ([], ['unmanaged ruleset `old` (active)']))
        result = self.plan(repo, self.config(prune=True), live)
        self.assertEqual([(c.method, c.endpoint) for c in result.api], [('DELETE', 'repos/Org/Other/rulesets/9')])
        self.assertEqual(result.notes, [])

    def test_free_plan_is_a_note_and_never_a_change(self):
        result = self.plan(Repo('Org', 'Other', 'main', 'abc', True), self.config(prune=True), None)
        self.assertEqual((result.api, len(result.notes)), ([], 1))

    def test_import_writes_only_what_has_no_file(self):
        cfg = self.config()
        live = [{**ruleset('main', 'deletion'), 'id': 1},
                {**ruleset('Tag Protection!', 'deletion', bypass=[{**BOT, 'extra': 'dropped'}]), 'id': 2, 'node_id': 'x'}]
        with mock.patch.object(gh, 'api', FakeGitHub(live)):
            written = rulesets.import_missing(Repo('Org', 'Other', 'main', 'abc', False), cfg)
        self.assertEqual(written, ['rulesets/repos/Other/tag-protection.json'])
        saved = json.loads((self.root / written[0]).read_text(encoding='utf-8'))
        self.assertEqual(saved, ruleset('Tag Protection!', 'deletion', bypass=[BOT]))
        self.assertIn('Tag Protection!', rulesets.desired_rulesets(Repo('Org', 'Other', 'main', 'abc', False), cfg))

    def test_app_bypasses_only_what_blocks_the_default_branch(self):
        cfg = self.config(extra={
            'rulesets/repos/Lib/release.json': {**ruleset('release', 'pull_request'), 'conditions': {'ref_name': {'include': ['refs/heads/release'], 'exclude': []}}},
            'rulesets/repos/Lib/named.json': {**ruleset('named', 'update', bypass=[BOT]), 'conditions': {'ref_name': {'include': ['refs/heads/dev'], 'exclude': []}}},
        })
        cfg.data['rulesets']['direct_commit_app'] = 42
        app = {'actor_id': 42, 'actor_type': 'Integration', 'bypass_mode': 'always'}
        wanted = rulesets.desired_rulesets(Repo('Org', 'Lib', 'dev', 'abc', False), cfg)
        self.assertEqual(wanted['reviews']['bypass_actors'], [app])
        self.assertEqual(wanted['named']['bypass_actors'], [BOT, app])
        # Deletion rules do not stop a commit, and `release` is not the default branch.
        self.assertEqual([wanted[n]['bypass_actors'] for n in ('main', 'tags', 'release')], [[], [], []])

    def test_folders_and_bypass_entries_must_name_a_repo(self):
        cfg = self.config(extra={'templates/own/Gone/renovate.json': '{}'})
        cfg.data['files'] = {'managed': [{'path': 'renovate.json', 'template': 't', 'overrides': 'templates/own'}]}
        self.assertEqual(cfg.unknown_repos(['app', 'LIB', 'Other']), ['templates/own/Gone'])
        self.assertEqual(cfg.unknown_repos(['Other', 'Gone']), ['rulesets/repos/Lib', 'extra_bypass.app for rulesets/shared/reviews.json'])


FILES_TOML = '''
[[files.managed]]
path = "renovate.json"
template = "templates/shared.json"
overrides = "templates/repos"
only_existing = true
'''


class ManagedFiles(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = make_config(pathlib.Path(tmp.name), FILES_TOML, {
            'templates/shared.json': 'shared',
            'templates/repos/Special/renovate.json': 'special',
        })

    def repo(self, name: str, content: str | None) -> Repo:
        repo = Repo('Org', name, 'main', 'abc', False)
        if content is not None:
            repo.tree, repo._files = ['renovate.json'], {'renovate.json': content}
        return repo

    def test_never_added_to_a_repo_without_it(self):
        repo = self.repo('Plain', None)
        self.assertEqual(files.check(repo, self.cfg).files, {})
        self.assertEqual(files.managed_paths(repo, self.cfg), set())

    def test_existing_file_follows_the_shared_template(self):
        self.assertEqual(files.check(self.repo('Plain', 'old'), self.cfg).files, {'renovate.json': 'shared'})
        self.assertEqual(files.check(self.repo('Plain', 'shared'), self.cfg).files, {})

    def test_repo_folder_replaces_the_shared_template(self):
        self.assertEqual(files.check(self.repo('special', 'old'), self.cfg).files, {'renovate.json': 'special'})

    def test_direct_files_stay_out_of_the_pull_request(self):
        self.cfg.data['files']['managed'][0]['direct'] = True
        result = files.check(self.repo('Plain', 'old'), self.cfg)
        self.assertEqual((result.direct, result.details, result.direct_details), ({'renovate.json'}, [], ['update `renovate.json`']))
        other = Result(files={'SECURITY.md': 'x'}, details=['add `SECURITY.md`'])
        results = [(BY_NAME['files'], result), (BY_NAME['community'], other)]
        self.assertEqual(apply.merge(results), ({'SECURITY.md': 'x'}, '### community\n- add `SECURITY.md`'))
        self.assertEqual(apply.merge_direct(results), ({'renovate.json': 'shared'}, '- update `renovate.json`'))

    def test_direct_commit_comes_first_and_can_stand_alone(self):
        self.cfg.data['files']['managed'][0]['direct'] = True
        results = [(BY_NAME['files'], files.check(self.repo('Plain', 'old'), self.cfg)),
                   (BY_NAME['community'], Result(files={'SECURITY.md': 'x'}))]
        calls = []
        with mock.patch.object(apply, 'deliver_files', lambda repo, files, body, direct: calls.append((sorted(files), direct)) or 'ok'):
            apply.apply(self.repo('Plain', 'old'), results, direct=False, only_direct=True)
            self.assertEqual(calls, [(['renovate.json'], True)])
            apply.apply(self.repo('Plain', 'old'), results, direct=False)
            self.assertEqual(calls[1:], [(['renovate.json'], True), (['SECURITY.md'], False)])


class Merge(unittest.TestCase):

    def test_one_change_set_per_repo(self):
        from orgsync.model import Result
        files, body = apply.merge([
            (BY_NAME['files'], Result(files={'SECURITY.md': 'x'}, details=['add `SECURITY.md`'])),
            (BY_NAME['rulesets'], Result(details=['ignored, no files'])),
            (BY_NAME['community'], Result(files={'CODE_OF_CONDUCT.md': None}, details=['remove it'])),
        ])
        self.assertEqual(files, {'SECURITY.md': 'x', 'CODE_OF_CONDUCT.md': None})
        self.assertEqual(body, '### files\n- add `SECURITY.md`\n\n### community\n- remove it')


class Community(unittest.TestCase):

    def test_snippets_ignore_line_wrapping(self):
        text = 'We acknowledge reports within 3 working\ndays (7 days at most), promise.'
        self.assertEqual(community.missing_snippets(text, ['within 3 working days (7 days at most)', 'nope']), ['nope'])

    def test_security_contact_link(self):
        good = 'blank_issues_enabled: false\ncontact_links:\n  - name: x\n    url: https://github.com/O/R/security/advisories/new\n'
        self.assertTrue(community.has_security_contact(good))
        self.assertTrue(community.has_security_contact(community.CONTACT_LINK))
        self.assertFalse(community.has_security_contact('contact_links:\n  - name: x\n    url: https://discord.gg/x\n'))
        self.assertFalse(community.has_security_contact('blank_issues_enabled: false\n'))


class Branches(unittest.TestCase):

    def branch(self, **kw):
        base = {'name': 'feat/x', 'oid': 'a' * 40, 'date': NOW - datetime.timedelta(days=200), 'unmerged': 3, 'prs': []}
        return base | kw

    def verdict(self, section=None, **kw):
        return branches.classify(self.branch(**kw), section or {}, NOW)[0]

    def test_open_pr_is_kept(self):
        self.assertEqual(self.verdict(prs=[{'number': 1, 'state': 'OPEN', 'headRefOid': 'a' * 40}]), 'keep')

    def test_merged_pr_is_deleted(self):
        self.assertEqual(self.verdict(prs=[{'number': 1, 'state': 'MERGED', 'headRefOid': 'a' * 40}]), 'delete')

    def test_commits_after_merge_are_only_reported(self):
        self.assertEqual(self.verdict(prs=[{'number': 1, 'state': 'MERGED', 'headRefOid': 'b' * 40}]), 'report')

    def test_fully_merged_is_deleted(self):
        self.assertEqual(self.verdict(unmerged=0), 'delete')

    def test_stale_unmerged_is_reported_unless_opted_in(self):
        self.assertEqual(self.verdict(), 'report')
        self.assertEqual(self.verdict({'delete_stale_unmerged': True}), 'delete')

    def test_recent_unmerged_is_kept(self):
        self.assertEqual(self.verdict(date=NOW - datetime.timedelta(days=5)), 'keep')


if __name__ == '__main__':
    unittest.main()
