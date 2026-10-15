"""Projects: every project-scoped tool starts here."""

from .. import procore, shape
from ..cache import TTLCache
from ..config import COMPANY_ID
from ..server import READ_ONLY, mcp

PAGE      = 300  # Procore's cap for this endpoint; one request covers most companies
CACHE_TTL = 60   # seconds; parallel agents each ask for the project list first
KEYS      = ('id', 'name', 'project_number', 'active', 'city', 'state_code')
_cache: TTLCache[tuple[str | None, bool], tuple[list[dict], int | None]] = TTLCache(limit=500)  # (user, active_only) -> (items, total)


async def projects(active_only: bool) -> tuple[list[dict], int | None]:
    """The caller's projects, trimmed, with Procore's count for the query; cached per user for CACHE_TTL seconds."""
    key = (procore.user(), active_only)  # per user, not per token: parallel clients and a refreshed token share it
    if (hit := _cache.get(key)) is not None:
        return hit
    params = {'company_id': COMPANY_ID, 'filters[by_status]': 'Active' if active_only else 'All'}
    rows, total = await procore.get_all('/rest/v1.0/projects', params, limit=procore.MAX_ROWS, per_page=PAGE)
    listing = [shape.pick(p, *KEYS) for p in rows], total
    _cache.set(key, listing, CACHE_TTL)

    return listing


@mcp.tool(title='List projects', annotations=READ_ONLY)
async def list_projects(active_only: bool = True) -> dict:
    """
    List Procore projects the current user can see: id, name, number, and location.

    Use the returned id wherever another tool asks for a project_id.
    """
    items, total = await projects(active_only)

    return shape.listing(items, total)
