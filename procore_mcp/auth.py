"""
Token verification. A token is valid if Procore's /rest/v1.0/me accepts it.

Results are cached per token, and concurrent requests for an unverified token share one
Procore call. Only a 401 or 403 from Procore marks a token bad. A rate limit, an outage,
or a timeout keeps serving the cached identity when there is one and otherwise surfaces
as a 503 with Retry-After, never as a 401: a 401 tells the client the token is dead and
makes it drop the connection, which is what a burst of parallel calls did when it tripped
Procore's spike limit and the /me check came back 429.

The check shares procore.py's rate-limit memory: its headers feed the pause every tool
call honors, and while a pause is on it does not send a check that would 429 and cost quota.

A cached identity is served at most MAX_STALE seconds after Procore last confirmed it, however
long Procore stays unreachable, so a token the user revoked does not outlive the outage.
"""

import asyncio
import math
import time

import httpx
from mcp.server.auth.provider import AccessToken, TokenVerifier
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import procore
from .cache import TTLCache
from .config import CLIENT_ID

CACHE_TTL     = 600   # seconds a verified token is trusted; Procore access tokens live 5400
STALE_GRACE   = 60    # seconds a cached identity is extended when Procore cannot answer
MAX_STALE     = 3600  # seconds from verification an identity may be served at all: an hourly limit window, under the token life
EVICT_AFTER   = 3600  # seconds a lapsed identity stays available to serve stale
MAX_ENTRIES   = 1000  # cache size at which lapsed identities are swept
CHECK_TIMEOUT = 10    # seconds; shorter than a tool call, the client is waiting on the handshake
BAD_TOKEN     = (401, 403)


class ProcoreUnavailable(Exception):
    """Procore could not answer the token check and nothing is cached for this token."""

    def __init__(self, status: int | None, retry_after: int):
        super().__init__(f'Procore token check unavailable (status {status}); retry after {retry_after}s')
        self.status = status
        self.retry_after = retry_after


class ProcoreTokenVerifier(TokenVerifier):
    def __init__(self):
        self.cache: TTLCache[str, AccessToken] = TTLCache(limit=MAX_ENTRIES, keep_expired=EVICT_AFTER)
        self.inflight: dict[str, asyncio.Task] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        if (access := self.cache.get(token)) is not None:
            return access
        task = self.inflight.get(token)
        if task is None:
            task = self.inflight[token] = asyncio.create_task(self._lookup(token, self.cache.peek(token)))
            task.add_done_callback(lambda done: self._settle(token, done))

        return await asyncio.shield(task)  # a cancelled waiter must not cancel the shared lookup

    def _settle(self, token: str, task: asyncio.Task) -> None:
        self.inflight.pop(token, None)
        if not task.cancelled():
            task.exception()  # mark it retrieved: a lookup every waiter abandoned must not log at GC

    async def _lookup(self, token: str, stale: AccessToken | None) -> AccessToken | None:
        async with procore._gate:  # queue with tool calls rather than past them, and see a limit they just tripped
            if (wait := procore.pause_left()) > 0:  # Procore said the limit is spent; a check now would 429 and cost quota
                return self._unavailable(token, stale, None, math.ceil(wait))
            try:
                async with procore.client(timeout=CHECK_TIMEOUT) as http:
                    response = await http.get('/rest/v1.0/me', headers={'Authorization': f'Bearer {token}'})
            except httpx.HTTPError:
                return self._unavailable(token, stale, None, procore.retry_after(None))
        procore.note_limits(response)
        if response.status_code == 200:
            try:
                subject = str(response.json().get('id'))
            except (ValueError, AttributeError):  # a 200 that is not Procore's JSON, say a proxy's status page
                return self._unavailable(token, stale, 200, procore.retry_after(response))
            access = AccessToken(token=token, client_id=CLIENT_ID, scopes=[], subject=subject, expires_at=int(time.time()) + MAX_STALE)
            self.cache.set(token, access, CACHE_TTL)
            return access
        if response.status_code in BAD_TOKEN:
            self.cache.pop(token)
            return None

        return self._unavailable(token, stale, response.status_code, procore.retry_after(response))

    def _unavailable(self, token: str, stale: AccessToken | None, status: int | None, retry: int) -> AccessToken:
        left = (stale.expires_at or 0) - time.time() if stale else 0  # expires_at is the ceiling set at verification
        if left > 0:
            self.cache.set(token, stale, min(STALE_GRACE, left))  # never past the ceiling, which the SDK enforces too
            return stale
        self.cache.pop(token)
        raise ProcoreUnavailable(status, retry)


class ProcoreOutageMiddleware:
    """Answers 503 with Retry-After when the token check raised ProcoreUnavailable."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        except ProcoreUnavailable as exc:
            response = JSONResponse(
                {'error': 'procore_unavailable', 'error_description': str(exc)},
                status_code=503,
                headers={'Retry-After': str(exc.retry_after)},
            )
            await response(scope, receive, send)
