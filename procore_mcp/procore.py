"""
HTTP client for Procore's REST API, acting as the signed-in user.

Every call attaches the user's token and the company header. Failures are raised as
ToolError so the model reads Procore's status and message, for example a 403 saying the
user lacks the RFI tool on that project, instead of a generic tool failure.

Rate limits. Procore enforces an hourly limit and a 10-second spike limit, answers 429 when
either is hit, and counts failed requests too. So: requests pass through a small concurrency
gate, parallel agents queue instead of bursting; a response that says the limit is spent sets
a pause every later request honors; a 429 or 503 is retried once after the wait Procore names
when that wait is short; and a longer wait is reported to the model as a ToolError naming the
cause, a limit or an outage, and the seconds, which costs nothing against the limit.
"""

import asyncio
import random
import time

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError

from .config import COMPANY_ID, PROCORE_API

PAGE_SIZE      = 100   # accepted by every v1 list endpoint; projects caps at 300, v2 endpoints at 100
MAX_ROWS       = 1000  # ceiling for `limit`, so one call cannot flood the model's context
CONCURRENCY    = 4     # Procore requests in flight per process; further callers wait their turn
RETRY_WAIT_MAX = 10    # seconds; the spike window; a longer wait is reported rather than slept
THROTTLED      = (429, 503)

_gate = asyncio.Semaphore(CONCURRENCY)
_pause_until = 0.0   # set when Procore reports the limit spent or answers 503; nothing is sent before then
_pause_reason = ''   # what set it, for the error the model reads


def client(timeout: float = 30) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=PROCORE_API, timeout=timeout)


def token() -> str:
    return get_access_token().token


def user() -> str | None:
    """Procore id of the signed-in user, as the token verifier resolved it; the same across that user's tokens."""
    return get_access_token().subject


def retry_after(response: httpx.Response | None, default: int = 5) -> int:
    """Seconds Procore asks us to wait, from Retry-After or X-Rate-Limit-Reset; `default` when neither is usable."""
    if response is None:
        return default
    value = response.headers.get('Retry-After', '')
    if value.isdigit():
        return max(1, int(value))
    value = response.headers.get('X-Rate-Limit-Reset', '')
    if value.isdigit() and response.headers.get('X-Rate-Limit-Remaining', '0') == '0':
        # Reset ends the spent window only when Remaining is 0. On a spike 429 the headers may
        # describe the untripped hourly window instead, whose reset is up to an hour away.
        return max(1, int(value) - int(time.time()))

    return default


def _headers() -> dict[str, str]:
    return {'Authorization': f'Bearer {token()}', 'Procore-Company-Id': COMPANY_ID}


def _reason(response: httpx.Response) -> str:
    return 'unavailable (503)' if response.status_code == 503 else 'rate limit reached'


def note_limits(response: httpx.Response) -> None:
    """Remember a limit or an outage this response reports, so nothing is sent before it passes."""
    global _pause_until, _pause_reason
    if response.status_code in THROTTLED or response.headers.get('X-Rate-Limit-Remaining') == '0':
        until = time.time() + retry_after(response)
        if until > _pause_until:
            _pause_until, _pause_reason = until, _reason(response)


def pause_left() -> float:
    """Seconds until a limit Procore reported resets; 0 when requests may go out."""
    return max(0.0, _pause_until - time.time())


async def _pause(path: str) -> None:
    """Wait out a limit Procore already reported, or tell the model how long it is when too long to hold."""
    wait = pause_left()
    if wait <= 0:
        return
    if wait > RETRY_WAIT_MAX:
        raise ToolError(f'Procore {_pause_reason}; wait {int(wait) + 1}s before calling again ({path})')
    await asyncio.sleep(wait + random.uniform(0.1, 0.5))


async def _get(http: httpx.AsyncClient, path: str, params: dict | None) -> httpx.Response:
    for _ in range(2):
        async with _gate:
            await _pause(path)  # inside the gate, so a limit reported while this caller queued is honored too
            try:
                response = await http.get(path, params=params, headers=_headers())
            except httpx.HTTPError as exc:
                raise ToolError(f'Procore request failed for {path}: {exc}') from exc
        note_limits(response)
        if response.status_code not in THROTTLED:
            break
    else:
        raise ToolError(f'Procore {_reason(response)} again after a retry; wait {retry_after(response)}s before calling again ({path})')
    if response.status_code >= 400:
        raise ToolError(f'Procore returned {response.status_code} for {path}: {response.text[:500]}')

    return response


async def get(path: str, params: dict | None = None) -> dict | list:
    """One GET; returns the decoded JSON body."""
    async with client() as http:
        return (await _get(http, path, params)).json()


async def get_all(path: str, params: dict | None = None, limit: int = PAGE_SIZE, per_page: int | None = None) -> tuple[list, int | None]:
    """
    Walk a paginated list endpoint (page / per_page) up to `limit` rows.

    Returns (rows, total). `total` is Procore's count for the query, read from the Total
    header, or None when Procore omits it. Compare it with len(rows) to tell whether rows
    were cut off.
    """
    limit = max(1, min(limit, MAX_ROWS))
    per_page = min(limit, per_page or PAGE_SIZE)
    rows: list = []
    total: int | None = None
    page = 1
    async with client() as http:
        while len(rows) < limit:
            response = await _get(http, path, {**(params or {}), 'page': page, 'per_page': per_page})
            batch = response.json()
            if not isinstance(batch, list):
                raise ToolError(f'Procore returned an unexpected shape for {path}: expected a list')
            rows.extend(batch)
            if response.headers.get('Total', '').isdigit():
                total = int(response.headers['Total'])
            if len(batch) < per_page:
                break
            page += 1

    return rows[:limit], total
