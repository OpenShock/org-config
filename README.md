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
python -m orgsync apply --only-direct       # only the files that skip the PR, see renovate.json
```

`plan` exits with 1 when there is drift, so it can run on a schedule.
`apply` prints the plan first and asks before doing anything.
It refuses to run unless `gh` is logged in as the `bot_account` from `org.toml`, or holds a GitHub App token when `bot_app = true`.

## Rate limits

A full `plan` costs about 230 of the 5,000 hourly REST requests and about 30 of the 5,000 hourly GraphQL points, and prints what it used.
A run does not start with less than 1,000 left of either.
Requests that change something are spaced one second apart.
When GitHub answers with a rate limit error the request is retried: after the hourly window resets if the budget is exhausted, otherwise after a minute, doubling on each repeat.

## Checks

All of them are configured in [`org.toml`](org.toml).

| Check | Keeps even | How it is applied |
| --- | --- | --- |
| `files` | Managed files such as `SECURITY.md` and `renovate.json`, copied from `templates/` | PR |
| `community` | Rules about community files: required, forbidden, must contain, must not contain; issue template security link | Notes, PR for a missing issue template `config.yml` |
| `actions` | An allowlist of actions, and banned actions | Notes, PR for a rewritten banned action |
| `msbuild` | Selected properties in `Directory.Build.props` | PR |
| `rulesets` | Repository rulesets, shared ones and per-repo ones | API |
| `settings` | Merge policy and security features | API |
| `branches` | Deletes orphaned `feat/`, `fix/`, `chore/`, ... branches | API, only with `--check branches` |

All file changes for a repo arrive together: one commit on the `chore/org-sync` branch and one PR per repo, with the description grouped by check.
Running `apply` again while that PR is open adds a commit to it instead of opening a second one.

Lines starting with `!` in the plan are notes: things the tool noticed but will not change on its own.

### files and community

`SECURITY.md` is a short stub that links to the full policy in the `.github` repo.
A repo-level `SECURITY.md` replaces the org default entirely, so the stub never restates scope or rules that could drift.
`.github`, `API`, `Frontend` and `Firmware` are excluded because they carry hand-written policies; the `community` check verifies those still contain the shared reporting block.

### renovate.json

Only repos that already have a `renovate.json` are kept in step, Renovate is never added to a repo.

```
renovate/                        presets, read by Renovate straight from this repo
templates/renovate/
  shared/renovate.json           the file every repo gets, it only extends the presets
  repos/<Repo>/renovate.json     the whole file for a repo with needs of its own
```

Renovate opens a pull request for every update and merges minor, patch and digest updates itself once CI passes.
Major updates always wait for a human.

Rules for everyone go into `renovate/`, and take effect as soon as they are on `master`.
Rules for one repo go into that repo's file here, as extra `extends` or `packageRules` next to the two shared presets.
A rule added to `renovate.json` in the repo itself is overwritten on the next sync.

`renovate.json` is marked `direct = true` in `org.toml`, so it is committed straight to each default branch instead of going through a pull request.
The `sync-direct` workflow does that whenever `templates/renovate/` or `org.toml` changes on `master`.
It runs as a GitHub App, which needs:

| Where | What |
| --- | --- |
| The app | Repository permission Contents: read and write, installed on all repos |
| `org-config` variable `ORG_SYNC_APP_CLIENT_ID` | The app's client ID |
| `org-config` secret `ORG_SYNC_APP_PRIVATE_KEY` | A private key of the app |
| `org.toml`, `direct_commit_app` | The app's numeric id, so the rulesets let it commit to default branches |

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
org-sync does not generate `dependabot.yml`, does not bump or pin actions, and leaves `renovate.json` alone.

## Adding a check

Add a module in `orgsync/checks/` with a `check(repo, cfg)` function returning a `Result`, and register it in `orgsync/checks/__init__.py`.
A `Result` holds file changes, API calls and notes; delivery is handled for you.

## Tests

```sh
python -m unittest discover -s tests
```
