"""
Change orders: change order packages on a project's prime contract and commitments.

Procore's change order packages endpoint lists the packages on every contract of a project,
prime and commitment alike, which is the change order log a project manager keeps. The row
carries the contract id and the contract's vendor (the owner on a prime contract, the sub on
a commitment); matching contract_id against list_prime_contracts and list_commitments tells
which side a package is on. The endpoint has no status filter, so status is applied here
after the read, and the count then describes the kept rows.
"""

from typing import Literal

from .. import batch, procore, shape
from ..server import READ_ONLY, mcp

PATH   = '/rest/v1.0/change_order_packages'
Status = Literal['draft', 'not_pricing', 'pricing', 'pending', 'revised', 'proceeding', 'not_proceeding', 'no_charge', 'approved', 'rejected', 'void']


def _row(project_id: int, c: dict) -> dict:
    vendor = (c.get('contract') or {}).get('vendor') or {}
    return {
        'id': c.get('id'),
        'project_id': project_id,
        'number': c.get('number'),
        'title': c.get('title'),
        'status': c.get('status'),
        'executed': bool(c.get('executed')),
        'revision': c.get('revision'),
        'contract_id': c.get('contract_id'),
        'vendor': vendor.get('name'),
        'vendor_id': vendor.get('id'),
        'grand_total': shape.amount(c.get('grand_total')),
        'due_date': c.get('due_date'),
        'reviewed_at': c.get('reviewed_at'),
        'signed_date': c.get('signed_change_order_received_date'),
        'invoiced_date': c.get('invoiced_date'),
        'paid_date': c.get('paid_date'),
        'created_at': c.get('created_at'),
        'updated_at': c.get('updated_at'),
    }


async def _project(project_id: int, params: dict, status: str | None, limit: int) -> tuple[list[dict], int | None]:
    rows, total = await procore.get_all(PATH, {'project_id': project_id} | params, limit=limit)
    items = [_row(project_id, c) for c in rows if not status or c.get('status') == status]

    return items, None if status else total  # a status filter is applied here, so Procore's count no longer describes the items


@mcp.tool(title='List change orders', annotations=READ_ONLY)
async def list_change_orders(project_ids: list[int], status: Status | None = None, contract_id: int | None = None, limit: int = 100) -> dict:
    """
    List the change order packages on one or more projects, on the prime contract and on
    commitments alike, every status unless status is given.

    project_ids takes up to 25 ids per call; take them from list_projects. Each item: id,
    project_id, number, title, status (draft, not_pricing, pricing, pending, revised,
    proceeding, not_proceeding, no_charge, approved, rejected, void), executed, revision,
    contract_id, vendor and vendor_id (the contract's other party: the owner on a prime
    contract, the subcontractor on a commitment), grand_total, due_date, reviewed_at (when
    approved or rejected), signed_date, invoiced_date, paid_date, created_at, updated_at.
    Match contract_id against list_prime_contracts and list_commitments to tell prime from
    commitment change orders. contract_id keeps one contract's packages; use it with that
    contract's project only. limit applies per project, before the status filter. A project
    the user lacks the Change Orders tool on is listed under errors.
    """
    params: dict = {}
    if contract_id:
        params['contract_id'] = contract_id

    return await batch.per_project(project_ids, lambda pid: _project(pid, params, status, limit), sort_key=lambda r: (r['project_id'], r['number'] or ''))
