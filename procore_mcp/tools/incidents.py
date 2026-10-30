"""
Incidents: the safety incidents logged on a project, as Procore's Incidents tool holds them.

An incident row is the event: what happened, when, where, the hazard and contributing
factors, and whether it is OSHA recordable. The injuries, near misses, property damages,
and environmental records within it are counted (records_count) but live on their own
endpoints, which this tool does not read; a safety report starts from the event. Procore
filters status, recordable, and the event date range itself, so those are forwarded.
"""

from typing import Literal

from .. import batch, procore, shape
from ..server import READ_ONLY, mcp

Status = Literal['open', 'closed']


def _path(project_id: int) -> str:
    return f'/rest/v1.0/projects/{project_id}/incidents'


def _row(project_id: int, i: dict) -> dict:
    return {
        'id': i.get('id'),
        'project_id': project_id,
        'number': i.get('number'),
        'title': i.get('title'),
        'status': i.get('status'),
        'event_date': i.get('event_date'),
        'time_unknown': bool(i.get('time_unknown')),
        'recordable': bool(i.get('recordable')),
        'private': bool(i.get('private')),
        'description': shape.text(i.get('description')),
        'location': shape.name(i.get('location')),
        'hazard': shape.name(i.get('hazard')),
        'contributing_condition': shape.name(i.get('contributing_condition')),
        'contributing_behavior': shape.name(i.get('contributing_behavior')),
        'records_count': i.get('records_count'),
        'actions_count': i.get('actions_count'),
        'witness_statements_count': i.get('witness_statements_count'),
        'open_observations_count': i.get('open_observations_count'),
        'closed_observations_count': i.get('closed_observations_count'),
        'attachments_count': i.get('attachments_count'),
        'created_by': shape.name(i.get('created_by')),
        'created_at': i.get('created_at'),
        'updated_at': i.get('updated_at'),
    }


async def _project(project_id: int, params: dict, limit: int) -> tuple[list[dict], int | None]:
    rows, total = await procore.get_all(_path(project_id), params, limit=limit)

    return [_row(project_id, i) for i in rows], total


@mcp.tool(title='List incidents', annotations=READ_ONLY)
async def list_incidents(
    project_ids: list[int],
    status: Status | None = None,
    recordable: bool | None = None,
    event_date: str | None = None,
    updated_at: str | None = None,
    limit: int = 100,
) -> dict:
    """
    List the safety incidents logged on one or more projects, every status unless status
    is given.

    project_ids takes up to 25 ids per call; take them from list_projects. Each item: id,
    project_id, number, title, status (open, closed), event_date, time_unknown,
    recordable (OSHA recordable), private, description, location, hazard,
    contributing_condition, contributing_behavior, records_count (injuries, near misses,
    property damages, and environmental records on the incident), actions_count,
    witness_statements_count, open_observations_count, closed_observations_count,
    attachments_count, created_by, created_at, updated_at. recordable=true keeps the
    recordable incidents only, recordable=false the rest. event_date and updated_at take a
    date range as "YYYY-MM-DD...YYYY-MM-DD". limit applies per project. A project the
    user lacks the Incidents tool on is listed under errors.
    """
    params: dict = {}
    if status:
        params['filters[status]'] = status
    if recordable is not None:
        params['filters[recordable]'] = 'true' if recordable else 'false'
    if event_date:
        params['filters[event_date]'] = event_date
    if updated_at:
        params['filters[updated_at]'] = updated_at

    return await batch.per_project(project_ids, lambda pid: _project(pid, params, limit), sort_key=lambda r: (r['project_id'], r['event_date'] or '', r['number'] or 0))
