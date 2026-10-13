"""RFIs (requests for information)."""

from typing import Literal

from .. import procore, shape
from ..server import READ_ONLY, mcp

Status = Literal['open', 'closed', 'draft', 'closed_with_revision', 'closed_draft']


@mcp.tool(title='List RFIs', annotations=READ_ONLY)
async def list_rfis(
    project_id: int,
    status: Status | None = 'open',
    ball_in_court_id: int | None = None,
    overdue: bool = False,
    search: str | None = None,
    limit: int = 100,
) -> dict:
    """
    List RFIs on a project: open ones unless status says otherwise, None meaning every status.

    Each item: id, number, subject, status, due_date, ball_in_court (who owes the next action),
    assignees, responsible_contractor, rfi_manager, cost_impact, schedule_impact, priority, link.
    overdue=True keeps only RFIs past their due date. search matches subject or number.
    Use list_projects to find the project_id.
    """
    params: dict = {'serializer_view': 'web_index'}
    if status:
        params['filters[status]'] = status
    if ball_in_court_id:
        params['filters[ball_in_court_id]'] = ball_in_court_id
    if overdue:
        params['filters[overdue]'] = 'true'
    if search:
        params['search'] = search
    rows, total = await procore.get_all(f'/rest/v1.0/projects/{project_id}/rfis', params, limit=limit)
    items = [{
        'id': r.get('id'),
        'number': r.get('full_number') or r.get('number'),
        'subject': r.get('subject'),
        'status': r.get('status'),
        'due_date': r.get('due_date'),
        'ball_in_court': shape.names(r.get('ball_in_courts')),
        'assignees': shape.names(r.get('assignees')),
        'responsible_contractor': shape.name(r.get('responsible_contractor')),
        'rfi_manager': shape.name(r.get('rfi_manager')),
        'cost_impact': r.get('cost_impact'),
        'schedule_impact': r.get('schedule_impact'),
        'priority': shape.name(r.get('priority')),
        'link': r.get('link'),
    } for r in rows]

    return shape.listing(items, total)
