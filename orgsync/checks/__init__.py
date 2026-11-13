"""The checks, in the order they are applied."""

from dataclasses import dataclass

from . import actions, branches, community, files, msbuild, rulesets, settings


@dataclass(frozen=True)
class Check:
    name: str
    destructive: bool = False  # only applied when named explicitly with --check


# Rulesets come before settings so long-lived branches are protected before merge behaviour changes.
CHECKS = [
    Check('files'),
    Check('community'),
    Check('actions'),
    Check('msbuild'),
    Check('rulesets'),
    Check('settings'),
    Check('branches', destructive=True),
]
BY_NAME = {c.name: c for c in CHECKS}


def run(name: str, repo, cfg):
    modules = {'files': files, 'community': community, 'actions': actions, 'msbuild': msbuild,
               'rulesets': rulesets, 'settings': settings, 'branches': branches}
    return modules[name].check(repo, cfg)
