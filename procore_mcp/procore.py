"""
HTTP client for Procore's REST API, acting as the signed-in user.

Every call attaches the user's token and the company header. Failures are raised as
ToolError so the model reads Procore's status and message, for example a 403 saying the
user lacks the RFI tool on that project, instead of a generic tool failure.
"""

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError

from .config import COMPANY_ID, PROCORE_API

PAGE_SIZE = 100   # accepted by every v1 list endpoint; projects caps at 300, v2 endpoints at 100
MAX_ROWS  = 1000  # ceiling for `limit`, so one call cannot flood the model's context


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=PROCORE_API, timeout=30)


def _headers() -> dict[str, str]:
    return {'Authorization': f'Bearer {get_access_token().token}', 'Procore-Company-Id': COMPANY_ID}


async def _get(http: httpx.AsyncClient, path: str, params: dict | None) -> httpx.Response:
    try:
        response = await http.get(path, params=params, headers=_headers())
    except httpx.HTTPError as exc:
        raise ToolError(f'Procore request failed for {path}: {exc}') from exc
    if response.status_code >= 400:
        raise ToolError(f'Procore returned {response.status_code} for {path}: {response.text[:500]}')

    return response


async def get(path: str, params: dict | None = None) -> dict | list:
    """One GET; returns the decoded JSON body."""
    async with client() as http:
        return (await _get(http, path, params)).json()


async def get_all(path: str, params: dict | None = None, limit: int = PAGE_SIZE) -> tuple[list, int | None]:
    """
    Walk a paginated list endpoint (page / per_page) up to `limit` rows.

    Returns (rows, total). `total` is Procore's count for the query, read from the Total
    header, or None when Procore omits it. Compare it with len(rows) to tell whether rows
    were cut off.
    """
    limit = max(1, min(limit, MAX_ROWS))
    per_page = min(limit, PAGE_SIZE)
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
