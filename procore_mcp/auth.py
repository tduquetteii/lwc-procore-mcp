"""Token verification. A token is valid if Procore accepts it; results are cached for five minutes."""

import time

import httpx
from mcp.server.auth.provider import AccessToken, TokenVerifier

from .config import CLIENT_ID, PROCORE_API


class ProcoreTokenVerifier(TokenVerifier):
    def __init__(self):
        self.cache: dict[str, tuple[float, AccessToken]] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        if (hit := self.cache.get(token)) and hit[0] > time.time():
            return hit[1]
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f'{PROCORE_API}/rest/v1.0/me', headers={'Authorization': f'Bearer {token}'})
        if response.status_code != 200:
            return None

        access = AccessToken(token=token, client_id=CLIENT_ID, scopes=[], subject=str(response.json().get('id')))
        self.cache[token] = (time.time() + 300, access)

        return access
