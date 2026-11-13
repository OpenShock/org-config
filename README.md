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
python -m orgsync plan --check actions      # one check

python -m orgsync apply                     # open PRs and apply settings
python -m orgsync apply --direct            # commit to default branches instead of opening PRs
python -m orgsync apply --check branches    # delete orphaned branches
```

`plan` exits with 1 when there is drift, so it can run on a schedule.
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
| `files` | Managed files such as `SECURITY.md`, copied from `templates/` | PR |
| `community` | Rules about community files: required, forbidden, must contain, must not contain; issue template security link | Notes, PR for a missing issue template `config.yml` |
| `actions` | Every `uses:` pinned to the SHA of the same release org-wide, an allowlist, banned actions | PR |
| `dependabot` | `.github/dependabot.yml`, generated from the manifests found in the repo | PR |
| `msbuild` | Selected properties in `Directory.Build.props` | PR |
| `rulesets` | Repository rulesets from `rulesets/*.json` | API |
| `settings` | Merge policy and security features | API |
| `branches` | Deletes orphaned `feat/`, `fix/`, `chore/`, ... branches | API, only with `--check branches` |

Each check that changes files uses its own branch, `chore/org-sync-<check>`, and its own PR.
Running `apply` again while a PR is open adds a commit to it instead of opening a second one.

Lines starting with `!` in the plan are notes: things the tool noticed but will not change on its own.

### files and community

`SECURITY.md` is a short stub that links to the full policy in the `.github` repo.
A repo-level `SECURITY.md` replaces the org default entirely, so the stub never restates scope or rules that could drift.
`.github`, `API`, `Frontend` and `Firmware` are excluded because they carry hand-written policies; the `community` check verifies those still contain the shared reporting block.

### actions

The version every repo should use is the newest release that is at least `min_release_age_days` old.
A repo that is already ahead of that is never downgraded.
A SHA that does not match the tag in its comment is corrected.

Banned actions are reported.
`softprops/action-gh-release` is also rewritten into a `gh release create` step when the tool understands every input of the step, and left for a human otherwise.
The rewritten step is stricter than the action in two ways: it fails if the release already exists, and it fails if a file pattern matches nothing.

### dependabot

The file is generated, so local edits are overwritten.
A file with keys the generator does not produce (`ignore`, `registries`, ...) is left alone and reported until the repo gets `force` or `skip` in `org.toml`.

### rulesets

Rulesets are matched by name.
Rulesets that exist on a repo but not in `org.toml` are reported and never deleted.
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

## Adding a check

Add a module in `orgsync/checks/` with a `check(repo, cfg)` function returning a `Result`, and register it in `orgsync/checks/__init__.py`.
A `Result` holds file changes, API calls and notes; delivery is handled for you.

## Tests

```sh
python -m unittest discover -s tests
```
