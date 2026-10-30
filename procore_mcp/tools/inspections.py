"""
Inspections: the checklists run on a project, as Procore's Inspections tool holds them.

Procore's checklist list endpoint answers one project at a time and returns the inspections
grouped by the template they were run from, with no documented pagination, so the tool reads
the whole response in one GET, flattens the groups, and cuts to `limit` itself. Status is
applied here too: Procore's filter takes its own capitalized vocabulary ("Open", "In Review")
and the list shows the same, so rows carry the lower-case form and the filter compares that.
The conformance counts per inspection are what an inspection report reads; the items
themselves are not listed.
"""

from typing import Literal

from .. import batch, procore, shape
from ..server import READ_ONLY, mcp

Status = Literal['open', 'in_review', 'closed']

PATH = '/rest/v1.0/checklist/lists'  # project_id as a query param; the projects/{id}/checklist/lists form is the create endpoint


def _status(value) -> str | None:
    return value.lower().replace(' ', '_') if isinstance(value, str) else None  # Procore sends "Open", "In Review", "Closed"


def _section(spec: dict | None) -> str | None:
    spec = spec or {}
    parts = [p for p in (spec.get('section'), spec.get('description')) if p]

    return ' '.join(parts) or None


def _row(project_id: int, template: str | None, i: dict) -> dict:
    return {
        'id': i.get('id'),
        'project_id': project_id,
        'number': i.get('number'),
        'name': i.get('name'),
        'template': i.get('list_template_name') or template,
        'template_id': i.get('list_template_id'),
        'status': _status(i.get('status')),
        'description': shape.text(i.get('description')),
        'trade': shape.name(i.get('trade')),
        'location': shape.name(i.get('location')),
        'spec_section': _section(i.get('specification_section')),
        'responsible_contractor': shape.name(i.get('responsible_contractor')),
        'responsible_contractor_id': (i.get('responsible_contractor') or {}).get('id'),
        'responsible_party': shape.name(i.get('responsible_party')),
        'inspectors': shape.names(i.get('inspectors')),
        'due_at': i.get('due_at'),
        'inspection_date': i.get('inspection_date'),
        'closed_at': i.get('closed_at'),
        'closed_by': shape.name(i.get('closed_by')),
        'item_count': i.get('item_count'),
        'conforming_item_count': i.get('conforming_item_count'),
        'deficient_item_count': i.get('deficient_item_count'),
        'na_item_count': i.get('na_item_count'),
        'neutral_item_count': i.get('neutral_item_count'),
        'not_inspected_item_count': i.get('not_inspected_item_count'),
        'created_by': shape.name(i.get('created_by')),
        'created_at': i.get('created_at'),
        'updated_at': i.get('updated_at'),
    }


async def _project(project_id: int, params: dict, status: str | None, limit: int) -> tuple[list[dict], int | None]:
    groups = await procore.get(PATH, {'project_id': project_id} | params)
    if not isinstance(groups, list):
        groups = []
    items = [_row(project_id, g.get('name'), i) for g in groups if isinstance(g, dict) for i in g.get('lists') or []]
    if status:
        items = [i for i in items if i['status'] == status]

    return items[:limit], len(items)


@mcp.tool(title='List inspections', annotations=READ_ONLY)
async def list_inspections(
    project_ids: list[int],
    status: Status | None = None,
    search: str | None = None,
    responsible_contractor_id: int | None = None,
    updated_at: str | None = None,
    limit: int = 100,
) -> dict:
    """
    List the inspections (checklists) on one or more projects, every status unless status
    is given.

    project_ids takes up to 25 ids per call; take them from list_projects. Each item: id,
    project_id, number, name, template and template_id (the checklist template it was run
    from), status (open, in_review, closed), description, trade, location, spec_section,
    responsible_contractor and responsible_contractor_id (match list_project_vendors),
    responsible_party, inspectors, due_at, inspection_date, closed_at, closed_by,
    item_count and the conforming, deficient, na, neutral, and not_inspected item counts,
    created_by, created_at, updated_at. Deficient items are the failures an inspection
    report lists. search matches inspection names. updated_at takes a date range as
    "YYYY-MM-DD...YYYY-MM-DD". limit applies per project, after the status filter. A
    project the user lacks the Inspections tool on is listed under errors.
    """
    params: dict = {}
    if search:
        params['filters[search]'] = search
    if responsible_contractor_id:
        params['filters[responsible_contractor_id]'] = responsible_contractor_id
    if updated_at:
        params['filters[updated_at]'] = updated_at

    return await batch.per_project(project_ids, lambda pid: _project(pid, params, status, limit), sort_key=lambda r: (r['project_id'], r['number'] or 0))
