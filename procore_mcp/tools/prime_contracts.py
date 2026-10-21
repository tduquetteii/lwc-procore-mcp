"""
Prime contracts: what the owner has agreed to pay for a project.

Most projects carry one prime contract, so the tool takes a batch of project ids and the
result is a portfolio: contract value, approved changes, dates, and the owner, contractor,
and architect on each. Procore's v1.0 list has no view parameter and returns the full
record, including whole vendor records for the parties, which are trimmed to names here.
"""

from .. import batch, procore, shape
from ..server import READ_ONLY, mcp

PATH     = '/rest/v1.0/prime_contracts'
STATUSES = ('Draft', 'Out For Bid', 'Out For Signature', 'Approved', 'Complete', 'Terminated')


def _row(project_id: int, c: dict) -> dict:
    return {
        'id': c.get('id'),
        'project_id': project_id,
        'number': c.get('number'),
        'title': c.get('title'),
        'status': c.get('status'),
        'executed': bool(c.get('executed')),
        'owner': shape.name(c.get('vendor')),
        'owner_id': (c.get('vendor') or {}).get('id'),
        'contractor': shape.name(c.get('contractor')),
        'architect': shape.name(c.get('architect')),
        'grand_total': shape.amount(c.get('grand_total')),
        'approved_change_orders': shape.amount(c.get('approved_change_orders')),
        'retainage_percent': shape.amount(c.get('retainage_percent')),
        'contract_date': c.get('contract_date'),
        'issued_on_date': c.get('issued_on_date'),
        'signed_date': c.get('signed_contract_received_date'),
        'start_date': c.get('contract_start_date'),
        'estimated_completion_date': c.get('contract_estimated_completion_date'),
        'actual_completion_date': c.get('actual_completion_date'),
        'termination_date': c.get('contract_termination_date'),
        'updated_at': c.get('updated_at'),
    }


async def _project(project_id: int, limit: int) -> tuple[list[dict], int | None]:
    rows, total = await procore.get_all(PATH, {'project_id': project_id}, limit=limit)

    return [_row(project_id, c) for c in rows], total


@mcp.tool(title='List prime contracts', annotations=READ_ONLY)
async def list_prime_contracts(project_ids: list[int], limit: int = 100) -> dict:
    """
    List the prime contracts on one or more projects: the owner's contract with us, its
    value, approved changes, and dates. Most projects have exactly one.

    project_ids takes up to 25 ids per call; take them from list_projects. Each item: id,
    project_id, number, title, status (Draft, Out For Bid, Out For Signature, Approved,
    Complete, Terminated), executed, owner and owner_id, contractor, architect, grand_total,
    approved_change_orders, retainage_percent, contract_date, issued_on_date, signed_date,
    start_date, estimated_completion_date, actual_completion_date, termination_date,
    updated_at. grand_total plus approved_change_orders is the revised contract value.
    Pending changes are on list_change_orders; billing is not read here. A project the user
    lacks the Prime Contracts tool on is listed under errors.
    """
    return await batch.per_project(project_ids, lambda pid: _project(pid, limit), sort_key=lambda r: (r['project_id'], r['number'] or ''))
