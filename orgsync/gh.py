"""Thin wrapper around the `gh` CLI, which owns authentication."""

import json
import re
import subprocess
import sys
import threading
import time


class GhError(Exception):

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


MAX_RETRIES = 5
# GitHub asks for about a second between requests that change something.
WRITE_INTERVAL = 1.0
_write_lock = threading.Lock()
_last_write = 0.0


def rate_limits() -> dict:
    """Remaining budget per resource. This endpoint does not count against the limit."""
    out = subprocess.run(['gh', 'api', 'rate_limit'], capture_output=True, text=True, check=True).stdout
    return json.loads(out)['resources']


def _rate_limit_wait(attempt: int) -> float:
    """Seconds to wait after a rate limit error."""
    try:
        exhausted = [r for r in rate_limits().values() if r['remaining'] == 0]
    except (subprocess.CalledProcessError, ValueError, KeyError):
        exhausted = []
    if exhausted:
        # Primary limit: nothing will work until the hourly window resets.
        return max(r['reset'] for r in exhausted) - time.time() + 5
    # Secondary limit: GitHub wants at least a minute, longer on repeat offences.
    return 60 * 2 ** attempt


def _pace_write() -> None:
    global _last_write
    with _write_lock:
        delay = _last_write + WRITE_INTERVAL - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        _last_write = time.monotonic()


def _run(args: list[str], stdin: str | None = None, write: bool = False) -> str:
    for attempt in range(MAX_RETRIES + 1):
        if write:
            _pace_write()
        proc = subprocess.run(['gh', *args], input=stdin, capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout
        status = re.search(r'\(HTTP (\d+)\)', proc.stderr)
        message = proc.stderr.strip() or proc.stdout.strip()
        # A plain 403 is a permission problem, only the rate limit flavours are worth retrying.
        limited = re.search(r'rate limit|abuse detection', message + proc.stdout, re.I)
        if not limited or attempt == MAX_RETRIES:
            raise GhError(int(status[1]) if status else 0, message)
        wait = max(1.0, _rate_limit_wait(attempt))
        print(f'rate limited by GitHub, waiting {wait:.0f}s', file=sys.stderr, flush=True)
        time.sleep(wait)
    raise AssertionError('unreachable')


def api(path: str, method: str = 'GET', body: dict | None = None, paginate: bool = False):
    args = ['api', path, '-X', method]
    if paginate:
        args += ['--paginate', '--slurp']
    stdin = None
    if body is not None:
        args += ['--input', '-']
        stdin = json.dumps(body)
    out = _run(args, stdin, write=method != 'GET')
    if not out.strip():
        return None
    data = json.loads(out)
    if paginate:
        data = [item for page in data for item in page]
    return data


def exists(path: str) -> bool:
    """For endpoints that answer 204 when a feature is on and 404 when it is off."""
    try:
        api(path)
        return True
    except GhError as e:
        if e.status == 404:
            return False
        raise


def graphql(query: str, variables: dict | None = None) -> dict:
    payload = json.dumps({'query': query, 'variables': variables or {}})
    out = _run(['api', 'graphql', '--input', '-'], payload, write=query.lstrip().startswith('mutation'))
    return json.loads(out)['data']


def current_login() -> str:
    return api('user')['login']
