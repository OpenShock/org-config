# org-sync

Keeps every repo in the OpenShock org in the same state.
GitHub only offers org-wide rulesets and policies on paid plans, so this does the same job from the outside: one config file describes the desired state, and a script compares every repo against it.

Nothing is cloned.
Everything goes through the GitHub API using the `gh` CLI's login, and file changes arrive as GitHub-signed commits.

## Usage

```sh
pip install pyyaml            # the only dependency, besides gh

python -m orgsync plan                      # read-only: show all drift
python -m orgsync plan --diff --only API    # with file diffs, one repo
python -m orgsync plan --check rulesets     # one check

python -m orgsync apply                     # one PR per repo, plus settings and rulesets
python -m orgsync apply --direct            # commit to default branches instead of opening PRs
python -m orgsync apply --check branches    # delete orphaned branches
```

`plan` exits with 1 when there is drift, so it can run on a schedule.
Branch deletions are always listed, but only count as drift when asked for with `--check branches`.
`apply` prints the plan first and asks before doing anything.
It refuses to run unless `gh` is logged in as the `bot_account` from `org.toml`.

## Rate limits

A full `plan` costs about 230 of the 5,000 hourly REST requests and about 30 of the 5,000 hourly GraphQL points, and prints what it used.
A run does not start with less than 1,000 left of either.
Requests that change something are spaced one second apart.
When GitHub answers with a rate limit error the request is retried: after the hourly window resets if the budget is exhausted, otherwise after a minute, doubling on each repeat.

## Checks

All of them are configured in [`org.toml`](org.toml).

| Check | Keeps even | How it is applied |
| --- | --- | --- |
| `files` | Managed files such as `CODEOWNERS`, copied from `templates/` | PR |
| `renovate` | An existing `renovate.json` extends the shared presets | PR |
| `community` | Rules about community files: required, forbidden, must contain, must not contain; issue template security link | Notes, PR for a missing issue template `config.yml` |
| `actions` | An allowlist of actions, and banned actions | Notes, PR for a rewritten banned action |
| `msbuild` | Selected properties in `Directory.Build.props` | PR |
| `rulesets` | Repository rulesets, shared ones and per-repo ones | API |
| `settings` | Merge policy and security features | API |
| `branches` | Deletes orphaned `feat/`, `fix/`, `chore/`, ... branches | API, only with `--check branches` |

All file changes for a repo arrive together: one commit on the `chore/org-sync` branch and one PR per repo, with the description grouped by check.
Running `apply` again while that PR is open updates it instead of opening a second one.
The branch is rebuilt every time: the default branch as it is now, plus one commit with every planned change.
So the PR never conflicts or falls behind, and never keeps something an earlier run proposed that is no longer planned.
The branch belongs to the tool, changes pushed to it by hand are lost.
Where a ruleset forbids force-pushes the branch is replaced instead, which closes the old PR and opens a new one.

Lines starting with `!` in the plan are notes: things the tool noticed but will not change on its own.

### files and community

The security policy lives in the `.github` repo, and GitHub shows it for every repo that has no `SECURITY.md` of its own.
A repo-level `SECURITY.md` replaces the org policy entirely, so local copies are removed, like the other inherited community files.
`API`, `Frontend` and `Firmware` keep hand-written policies; the `community` check verifies those still contain the shared reporting block.

`CODEOWNERS` is not inherited, so every public repo gets its own copy.

### renovate.json

The shared policy lives in the presets in `renovate/`, which Renovate reads straight from this repo.
Every repo owns its `renovate.json`: rules that only make sense for one repo are written there, after the presets.

The check only makes sure an existing `renovate.json` extends the presets, Renovate is never added to a repo.
When it has to add them, it puts them first and drops whatever the presets already provide, so a repo's file is left with its own rules only.
A file that already extends the presets is never touched again.

A rule that turns out to belong to one repo is moved out of the presets through `renovate/handover/<Repo>.json`.
The check writes the rules in that file into the repo's `renovate.json`, updating the repo's own rule when it matches the same packages.
Once that pull request is merged the handover file is deleted, and the repo owns the rule from then on.

Renovate opens a pull request for every update and merges minor, patch and digest updates itself once CI passes.
Major updates always wait for a human.

### actions

Actions that are neither from an allowed owner nor individually allowed are reported, and so are banned ones.
`softprops/action-gh-release` is also rewritten into a `gh release create` step when the tool understands every input of the step, and left for a human otherwise.
The rewritten step is stricter than the action in two ways: it fails if the release already exists, and it fails if a file pattern matches nothing.

Action versions and SHA pins are not touched, see below.

### rulesets

```
rulesets/
  shared/            used by many repos, assigned to repos in org.toml
  repos/<Repo>/      for that one repo, picked up automatically
```

Rulesets are matched by the `name` field inside the file, the filename does not matter.
A ruleset in a repo's folder with the same `name` as a shared one replaces the shared one for that repo.
`python -m orgsync import-rulesets` copies rulesets that exist on GitHub but not here into `rulesets/repos/`, without changing anything on GitHub.

Rulesets on GitHub that have no file are deleted, or only reported when `prune = false` is set in `org.toml`.
A folder in `rulesets/repos/` that names no active repo stops the run, so a renamed repo cannot lose its rulesets unnoticed.

| Shared ruleset | Repos | Effect |
| --- | --- | --- |
| `main-branches` | all | No force-push and no deletion of `master`, `main`, `develop`, `beta` and the default branch |
| `squash-merges` | public | The default branch only takes squash merges, through a pull request |
| `pull-requests` | listed in `org.toml` | Main branches need one code owner approval |

Merge methods can only be limited per target branch.
Only the default branch is limited to squash, so `develop` can still be merged into `master` with its history.
Private repos on the free plan cannot have rulesets and are reported.

### branches

A branch is a candidate only if its name starts with one of the configured prefixes and it is not protected.

| Situation | Result |
| --- | --- |
| Has an open PR | kept |
| Its PR was merged and nothing was pushed since | deleted |
| Has no commits that are missing from the default branch | deleted |
| `dependabot/*` without an open PR | deleted |
| Unmerged work, older than `stale_days` | reported, deleted only with `delete_stale_unmerged = true` |
| Unmerged work, recent | kept |

The plan prints the commit each branch pointed at, so a deleted branch can be recreated.

## What this does not do

Dependency updates belong to [Renovate](https://docs.renovatebot.com), which is installed on the org.
That covers package versions, GitHub Actions versions and their SHA pins, and Docker image tags.
org-sync does not generate `dependabot.yml` and does not bump or pin actions.
Its only part in this is making sure existing `renovate.json` files extend the shared presets.

## Adding a check

Add a module in `orgsync/checks/` with a `check(repo, cfg)` function returning a `Result`, and register it in `orgsync/checks/__init__.py`.
A `Result` holds file changes, API calls and notes; delivery is handled for you.

## Tests

```sh
python -m unittest discover -s tests
```
