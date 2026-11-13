"""Command line entry point: `plan` shows drift, `apply` fixes it."""

import argparse
import difflib
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor

from . import gh
from .apply import apply
from .checks import BY_NAME, CHECKS, Check, run
from .checks.actions import Resolver
from .config import Config
from .model import Repo, Result, list_repos, load_tree

# A full plan needs a few hundred requests, do not start one that cannot finish.
MIN_BUDGET = 1000

Plan = list[tuple[Repo, list[tuple[Check, Result]]]]


def build_plan(cfg: Config, checks: list[Check], only: list[str], skip: list[str], jobs: int) -> Plan:
    repos = [r for r in list_repos(cfg.org) if r.name.lower() not in cfg.exclude]
    if only:
        wanted = {r.lower() for r in only}
        repos = [r for r in repos if r.name.lower() in wanted]
    skipped = {r.lower() for r in skip}
    repos = [r for r in repos if r.name.lower() not in skipped]
    resolver = Resolver(cfg.section('actions'))

    def examine(repo: Repo) -> list[tuple[Check, Result]]:
        load_tree(repo)
        results = []
        for check in checks:
            try:
                result = run(check.name, repo, cfg, resolver)
            except gh.GhError as e:
                result = Result(notes=[f'check failed: {e.message}'])
            if repo.truncated:
                result.notes.append('file tree was truncated by GitHub, results may be incomplete')
            results.append((check, result))
        return results

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        return list(zip(repos, pool.map(examine, repos)))


def print_plan(plan: Plan, show_diff: bool) -> tuple[int, int]:
    changes = notes = 0
    for repo, results in plan:
        lines = []
        for check, result in results:
            for path, content in result.files.items():
                lines.append(f'  [{check.name}] {"delete" if content is None else "write"} {path}')
                if show_diff:
                    before = (repo.read(path) or '').splitlines()
                    after = (content or '').splitlines()
                    lines += [f'      {d}' for d in difflib.unified_diff(before, after, 'current', 'desired', lineterm='', n=2)]
            lines += [f'  [{check.name}]   {d}' for d in result.details]
            lines += [f'  [{check.name}] {c.summary}' for c in result.api]
            lines += [f'  [{check.name}] ! {n}' for n in result.notes]
            changes += len(result.files) + len(result.api)
            notes += len(result.notes)
        if lines:
            print(f'\n== {repo.name}{" (private)" if repo.private else ""}')
            print('\n'.join(lines))
    print(f'\n{changes} change(s) to apply, {notes} note(s) needing a human, {len(plan)} repo(s) checked')
    return changes, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='orgsync', description='Keep every repo in a GitHub org in the same state.')
    parser.add_argument('command', choices=['plan', 'apply'])
    parser.add_argument('--config', default='org.toml', type=pathlib.Path)
    parser.add_argument('--only', nargs='+', default=[], metavar='REPO', help='limit to these repos')
    parser.add_argument('--skip', nargs='+', default=[], metavar='REPO', help='leave these repos out')
    parser.add_argument('--check', nargs='+', default=[], choices=list(BY_NAME), help='run only these checks')
    parser.add_argument('--diff', action='store_true', help='show file diffs in the plan')
    parser.add_argument('--direct', action='store_true', help='commit to the default branch instead of opening PRs')
    parser.add_argument('--yes', action='store_true', help='apply without asking')
    parser.add_argument('--jobs', type=int, default=8)
    args = parser.parse_args(argv)

    cfg = Config(args.config)
    if args.check:
        checks = [c for c in CHECKS if c.name in args.check]
    else:
        # Destructive checks are still planned, so their findings stay visible.
        checks = list(CHECKS)

    before = gh.rate_limits()
    low = [f'{name} {before[name]["remaining"]}' for name in ('core', 'graphql') if before[name]['remaining'] < MIN_BUDGET]
    if low:
        print(f'not enough API budget left this hour ({", ".join(low)}), try again later')
        return 2
    plan = build_plan(cfg, checks, args.only, args.skip, args.jobs)
    changes, _ = print_plan(plan, args.diff)
    after = gh.rate_limits()
    print('API budget: ' + ', '.join(
        f'{name} used {max(0, before[name]["remaining"] - after[name]["remaining"])}, {after[name]["remaining"]} of {after[name]["limit"]} left'
        for name in ('core', 'graphql')))
    if args.command == 'plan':
        return 1 if changes else 0

    held_back = [c.name for c in checks if c.destructive and c.name not in args.check]
    if held_back:
        print(f'not applying {", ".join(held_back)}: destructive, name it with --check to apply')
    todo = [(repo, check, result) for repo, results in plan for check, result in results
            if result.drift and check.name not in held_back]
    if not todo:
        print('nothing to apply')
        return 0
    login = gh.current_login()
    if cfg.bot_account and login != cfg.bot_account:
        print(f'gh is logged in as {login}, expected {cfg.bot_account}: run `gh auth switch -u {cfg.bot_account}` first')
        return 2
    if not args.yes:
        mode = 'direct commits to default branches' if args.direct else 'pull requests'
        answer = input(f'apply to {len({r.name for r, _, _ in todo})} repo(s) as {login}, file changes as {mode}? [y/N] ')
        if answer.strip().lower() != 'y':
            print('aborted')
            return 1

    failed = False
    for repo, check, result in todo:
        for line in apply(repo, check, result, args.direct):
            failed |= line.startswith('FAILED')
            print(f'{repo.name} [{check.name}] {line}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
