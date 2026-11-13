"""Repository settings: merge policy and security features."""

from .. import gh
from ..config import Config
from ..model import ApiChange, Repo, Result

# Settings that live under security_and_analysis on the repo object.
SECURITY = {'secret_scanning', 'secret_scanning_push_protection', 'dependabot_security_updates'}
# Settings with their own on/off endpoint.
TOGGLES = {
    'vulnerability_alerts': 'vulnerability-alerts',
    'private_vulnerability_reporting': 'private-vulnerability-reporting',
}


def _toggle_state(slug: str, endpoint: str) -> bool | None:
    try:
        data = gh.api(f'repos/{slug}/{endpoint}')
    except gh.GhError as e:
        if e.status == 404:
            return False
        if e.status in (403, 422):
            return None
        raise
    return True if data is None else bool(data.get('enabled'))


def check(repo: Repo, cfg: Config) -> Result:
    section = cfg.section('settings')
    result = Result()
    if repo.name.lower() in {r.lower() for r in section.get('exclude', [])}:
        return result
    desired = dict(section.get('all', {}))
    desired.update(section.get('private' if repo.private else 'public', {}))
    if not desired:
        return result

    live = gh.api(f'repos/{repo.slug}')
    patch: dict = {}
    for key, want in desired.items():
        if key in TOGGLES:
            state = _toggle_state(repo.slug, TOGGLES[key])
            if state is None:
                result.notes.append(f'{key} is not available for this repo')
            elif state != want:
                method = 'PUT' if want else 'DELETE'
                result.api.append(ApiChange(f'{key}: {state} -> {want}'.lower(), method, f'repos/{repo.slug}/{TOGGLES[key]}'))
        elif key in SECURITY:
            status = (live.get('security_and_analysis') or {}).get(key, {}).get('status')
            wanted = 'enabled' if want else 'disabled'
            if status is None:
                result.notes.append(f'{key} is not available for this repo')
            elif status != wanted:
                patch.setdefault('security_and_analysis', {})[key] = {'status': wanted}
                result.details.append(f'{key}: {status} -> {wanted}')
        elif key not in live:
            result.notes.append(f'unknown repository setting `{key}`')
        elif live[key] != want:
            patch[key] = want
            result.details.append(f'{key}: {live[key]} -> {want}'.lower())
    if patch:
        result.api.insert(0, ApiChange('; '.join(result.details), 'PATCH', f'repos/{repo.slug}', patch))
        result.details.clear()
    return result
