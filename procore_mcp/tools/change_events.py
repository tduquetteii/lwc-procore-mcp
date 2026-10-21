"""
Change events: the pipeline of potential cost and revenue changes on a project.

A change event is raised before any change order exists, so it is the earliest sign of
exposure: an RFI that will cost money, an owner request, a field condition. Procore keeps
the money on the event's line items (`change_items`), each with its latest cost, latest
revenue, budget code, and vendor; the tool sums those per event and lists the vendors and
budget codes so the model sees exposure without a request per event. Status names are
configured per company, so the row carries both the name and the status it maps to.
Procore's status, type, and reason filters on this endpoint travel in a JSON body, which
a GET from a tool does not send; the model filters the rows instead.
"""

from .. import batch, procore, shape
from ..server import READ_ONLY, mcp

PATH = '/rest/v1.1/change_events'  # v1.1 is the version that carries change_items on the list rows


def _unique(values) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def _total(items: list[dict], key: str) -> float | None:
    amounts = [shape.amount((i.get(key) or {}).get('amount')) for i in items]
    return sum(a for a in amounts if a is not None) if any(a is not None for a in amounts) else None


def _vendor(item: dict) -> str | None:
    vendor = (item.get('cost_impact') or {}).get('vendor') or {}
    return shape.name(vendor.get('confirmed')) or shape.name(vendor.get('proposed'))


def _budget_code(item: dict) -> str | None:
    code = item.get('budget_code') or {}
    return ' - '.join(part for part in (code.get('flat_code'), code.get('description')) if part) or None


def _row(project_id: int, e: dict) -> dict:
    status = e.get('status') or {}
    origin = e.get('event_origin') or {}
    items = e.get('change_items') or []
    return {
        'id': e.get('id'),
        'project_id': project_id,
        'number': e.get('number'),
        'title': e.get('title'),
        'status': status.get('name'),
        'status_group': status.get('mapped_to_status'),
        'scope': e.get('scope'),
        'change_type': shape.name(e.get('change_type')),
        'change_reason': (e.get('change_reason') or {}).get('change_reason'),
        'origin': origin.get('display_name'),
        'origin_type': origin.get('origin_type'),
        'created_at': e.get('created_at'),
        'updated_at': e.get('updated_at'),
        'created_by': shape.name(e.get('created_by')),
        'line_items': len(items),
        'cost_total': _total(items, 'latest_cost_values'),
        'revenue_total': _total(items, 'latest_revenue_values'),
        'vendors': _unique(map(_vendor, items)),
        'budget_codes': _unique(map(_budget_code, items)),
    }


async def _project(project_id: int, params: dict, limit: int) -> tuple[list[dict], int | None]:
    rows, total = await procore.get_all(PATH, {'project_id': project_id} | params, limit=limit)

    return [_row(project_id, e) for e in rows], total


@mcp.tool(title='List change events', annotations=READ_ONLY)
async def list_change_events(project_ids: list[int], created_at: str | None = None, updated_at: str | None = None, limit: int = 100) -> dict:
    """
    List the change events on one or more projects: potential changes raised before a change
    order exists, every status. This is the pipeline of cost and revenue exposure.

    project_ids takes up to 25 ids per call; take them from list_projects. Each item: id,
    project_id, number, title, status (the company's status name), status_group (what it
    maps to, such as open, closed, void), scope (tbd, in_scope, out_of_scope), change_type,
    change_reason, origin and origin_type (the RFI or other record that raised it),
    created_at, updated_at, created_by, line_items (count), cost_total and revenue_total
    (the latest amounts across the line items, None when none is priced), vendors, and
    budget_codes. Approved change orders are listed by list_change_orders.
    created_at and updated_at take a date range as "YYYY-MM-DD...YYYY-MM-DD". limit applies
    per project. A project the user lacks the Change Events tool on is listed under errors.
    """
    params: dict = {}
    if created_at:
        params['filters[created_at]'] = created_at
    if updated_at:
        params['filters[updated_at]'] = updated_at

    return await batch.per_project(project_ids, lambda pid: _project(pid, params, limit), sort_key=lambda r: (r['project_id'], r['number'] or ''))
