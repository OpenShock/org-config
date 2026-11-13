"""The checks, in the order they are applied."""

from dataclasses import dataclass

from . import actions, branches, community, dependabot, files, msbuild, rulesets, settings


@dataclass(frozen=True)
class Check:
    name: str
    title: str  # commit subject and PR title for file changes
    destructive: bool = False  # only applied when named explicitly with --check


# Rulesets come before settings so long-lived branches are protected before merge behaviour changes.
CHECKS = [
    Check('files', 'chore: sync managed files'),
    Check('community', 'chore: sync community files'),
    Check('actions', 'chore: align GitHub Actions versions'),
    Check('dependabot', 'chore: regenerate dependabot config'),
    Check('msbuild', 'chore: align shared MSBuild properties'),
    Check('rulesets', ''),
    Check('settings', ''),
    Check('branches', '', destructive=True),
]
BY_NAME = {c.name: c for c in CHECKS}


def run(name: str, repo, cfg, resolver):
    if name == 'actions':
        return actions.check(repo, cfg, resolver)
    return {'files': files, 'community': community, 'dependabot': dependabot, 'msbuild': msbuild, 'rulesets': rulesets,
            'settings': settings, 'branches': branches}[name].check(repo, cfg)
