import datetime
import unittest

from orgsync import apply
from orgsync.checks import BY_NAME, actions, branches, community, msbuild, rulesets

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
        live['rules'].append({'type': 'non_fast_forward'})
        self.assertNotEqual(rulesets.normalize(live, like=desired), rulesets.normalize(desired))


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
