"""Projects: every project-scoped tool starts here."""

from .. import procore, shape
from ..config import COMPANY_ID
from ..server import READ_ONLY, mcp


@mcp.tool(title='List projects', annotations=READ_ONLY)
async def list_projects(active_only: bool = True) -> dict:
    """
    List Procore projects the current user can see: id, name, number, and location.

    Use the returned id wherever another tool asks for a project_id.
    """
    rows, _ = await procore.get_all('/rest/v1.0/projects', {'company_id': COMPANY_ID}, limit=procore.MAX_ROWS)
    if active_only:
        rows = [p for p in rows if p.get('active')]
    items = [shape.pick(p, 'id', 'name', 'project_number', 'active', 'city', 'state_code') for p in rows]

    return shape.listing(items, len(items))
