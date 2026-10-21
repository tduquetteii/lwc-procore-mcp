"""
Commitments: a project's subcontracts (work order contracts) and purchase orders.

Procore keeps the two kinds on separate endpoints with separate status vocabularies, so the
tool reads both and merges them into one list with a `type` on each row. A commitment is the
strongest sign that a company is actually working a job: the directory only says it was
added to the project. Line items (and so cost codes) need one request per contract, so they
are not read here; the title names the scope in practice ("Flooring", "Electrical").
"""

import asyncio
from typing import Literal

from .. import batch, procore, shape
from ..server import READ_ONLY, mcp

Kind = Literal['subcontract', 'purchase_order']

ENDPOINTS = {'subcontract': '/rest/v1.0/work_order_contracts', 'purchase_order': '/rest/v1.0/purchase_order_contracts'}
VIEW      = {'view': 'extended'}  # compact is the default; amounts and dates are documented on the full record
SUBCONTRACT_STATUSES    = ('Draft', 'Out For Bid', 'Out For Signature', 'Approved', 'Complete', 'Terminated', 'Void')
PURCHASE_ORDER_STATUSES = ('Draft', 'Processing', 'Submitted', 'Partially Received', 'Received', 'Approved', 'Closed')


def _row(project_id: int, kind: str, c: dict) -> dict:
    vendor = c.get('vendor') or {}
    return {
        'id': c.get('id'),
        'project_id': project_id,
        'type': kind,
        'number': c.get('number'),
        'title': c.get('title'),
        'status': c.get('status'),
        'executed': bool(c.get('executed')),
        'vendor': vendor.get('name') or vendor.get('company'),
        'vendor_id': vendor.get('id'),
        'grand_total': shape.amount(c.get('grand_total')),
        'approved_change_orders': shape.amount(c.get('approved_change_orders')),
        'pending_change_orders': shape.amount(c.get('pending_change_orders')),
        'contract_date': c.get('contract_date'),
        'issued_on_date': c.get('issued_on_date'),
        'start_date': c.get('contract_start_date'),
        'estimated_completion_date': c.get('contract_estimated_completion_date') or c.get('estimated_completion_date'),
        'actual_completion_date': c.get('actual_completion_date'),
        'delivery_date': c.get('delivery_date'),
        'percent_paid': shape.amount(c.get('percentage_paid')),
        'remaining_balance': shape.amount(c.get('remaining_balance_outstanding')),
    }


async def _project(project_id: int, kinds: list[str], status: str | None, limit: int) -> tuple[list[dict], int]:
    """One project's commitments of the given kinds, trimmed and merged, with Procore's combined count."""
    params: dict = {'project_id': project_id} | VIEW
    if status:
        params['filters[status]'] = status
    results = await asyncio.gather(*(procore.get_all(ENDPOINTS[k], params, limit=limit) for k in kinds))
    rows = [_row(project_id, k, c) for k, (found, _) in zip(kinds, results) for c in found]
    total = sum(len(found) if count is None else count for found, count in results)

    return rows[:limit], total


@mcp.tool(title='List commitments', annotations=READ_ONLY)
async def list_commitments(project_ids: list[int], kind: Kind | None = None, status: str | None = None, limit: int = 100) -> dict:
    """
    List the commitments on one or more projects: subcontracts and purchase orders, every
    status, sorted by vendor then project. This is who is actually under contract on a job,
    as opposed to the project directory, which lists everyone added to it.

    project_ids takes up to 25 ids per call; for a company-wide picture, take the ids from
    list_projects and call this in batches, then group the items by vendor_id. Each item: id,
    project_id, type (subcontract or purchase_order), number, title (names the scope, such as
    "Flooring"), status, executed, vendor, vendor_id, grand_total, approved_change_orders,
    pending_change_orders, contract_date, issued_on_date, start_date,
    estimated_completion_date, actual_completion_date, delivery_date, percent_paid,
    remaining_balance. Draft, Out For Bid, Void, and Terminated commitments carry no work.
    kind limits to one type. status is one value: for subcontracts Draft, Out For Bid, Out For
    Signature, Approved, Complete, Terminated, Void; for purchase orders Draft, Processing,
    Submitted, Partially Received, Received, Approved, Closed. limit applies per project.
    A project the user lacks the Commitments tool on is listed under errors; the others still
    return.
    """
    kinds = [kind] if kind else list(ENDPOINTS)

    return await batch.per_project(project_ids, lambda pid: _project(pid, kinds, status, limit), sort_key=lambda r: (r['vendor'] or '', r['project_id'], r['number'] or ''))
