"""Punch items."""

from typing import Literal

from .. import procore, shape
from ..server import READ_ONLY, mcp

Status = Literal['open', 'closed', 'pending']
Priority = Literal['low', 'medium', 'high']


@mcp.tool(title='List punch items', annotations=READ_ONLY)
async def list_punch_items(
    project_id: int,
    status: Status | None = 'open',
    priority: Priority | None = None,
    trade_id: int | None = None,
    vendor_id: int | None = None,
    search: str | None = None,
    limit: int = 100,
) -> dict:
    """
    List punch items on a project: open ones unless status says otherwise, None meaning every status.

    Each item: id, number, name, status, workflow_status, priority, due_date, location, trade,
    assignees, ball_in_court, manager, final_approver, type, description (cut to 300 characters).
    search matches name and description. Use list_projects to find the project_id.
    """
    params: dict = {'project_id': project_id}
    if status:
        params['filters[status]'] = status
    if priority:
        params['filters[priority]'] = priority
    if trade_id:
        params['filters[trade_id]'] = trade_id
    if vendor_id:
        params['filters[vendor_id]'] = vendor_id
    if search:
        params['filters[query]'] = search
    rows, total = await procore.get_all('/rest/v1.1/punch_items', params, limit=limit)
    items = [{
        'id': p.get('id'),
        'number': p.get('position'),
        'name': p.get('name'),
        'status': p.get('status'),
        'workflow_status': p.get('workflow_status'),
        'priority': p.get('priority'),
        'due_date': p.get('due_date'),
        'location': shape.name(p.get('location')),
        'trade': shape.name(p.get('trade')),
        'assignees': shape.names(p.get('assignees')),
        'ball_in_court': shape.names(p.get('ball_in_court')),
        'manager': shape.name(p.get('punch_item_manager')),
        'final_approver': shape.name(p.get('final_approver')),
        'type': shape.name(p.get('punch_item_type')),
        'description': shape.text(p.get('description')),
    } for p in rows]

    return shape.listing(items, total)
