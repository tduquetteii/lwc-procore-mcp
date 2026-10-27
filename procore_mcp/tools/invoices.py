"""
Invoices: what subcontractors have billed us (requisitions) and what we have billed the
owner (payment applications).

Both are pay applications in the AIA G702 sense and Procore returns the G702 summary on
each: contract sum to date, completed and stored to date, retainage, earned less retainage,
previous certificates, current payment due, balance to finish. The tools flatten those seven
onto the row so a billing report needs nothing else. Subcontractor invoices come from the
v1.1 requisitions list, which filters by status and commitment; owner invoices come from
the v1.0 payment applications list, which has no documented filters, so status is applied
after the read.
"""

from typing import Literal

from .. import batch, procore, shape
from ..server import READ_ONLY, mcp

SubStatus   = Literal['draft', 'under_review', 'revise_and_resubmit', 'approved', 'approved_as_noted', 'pending_owner_approval']
OwnerStatus = Literal['draft', 'under_review', 'revise_and_resubmit', 'approved']

REQUISITIONS = '/rest/v1.1/requisitions'           # v1.1 carries vendor_id; v1.0 has only the name
PAY_APPS     = '/rest/v1.0/payment_applications'
G702         = ('contract_sum_to_date', 'total_completed_and_stored_to_date', 'total_retainage', 'total_earned_less_retainage',
                'less_previous_certificates_for_payment', 'current_payment_due', 'balance_to_finish_including_retainage')
COMMITMENT_TYPES = {'WorkOrderContract': 'subcontract', 'PurchaseOrderContract': 'purchase_order'}  # list_commitments' vocabulary


def _g702(summary: dict | None) -> dict:
    summary = summary or {}
    return {k: shape.amount(summary.get(k)) for k in G702}


def _requisition(project_id: int, r: dict) -> dict:
    return {
        'id': r.get('id'),
        'project_id': project_id,
        'number': r.get('number'),
        'invoice_number': r.get('invoice_number'),
        'status': r.get('status'),
        'final': bool(r.get('final')),
        'commitment_id': r.get('commitment_id'),
        'commitment_type': COMMITMENT_TYPES.get(r.get('commitment_type'), r.get('commitment_type')),
        'contract': r.get('contract_name'),
        'vendor': r.get('vendor_name'),
        'vendor_id': r.get('vendor_id'),
        'billing_date': r.get('billing_date'),
        'period_start': r.get('requisition_start'),
        'period_end': r.get('requisition_end'),
        'submitted_at': r.get('submitted_at'),
        'payment_date': r.get('payment_date'),
        'percent_complete': shape.amount(r.get('percent_complete')),
        'total_claimed_amount': shape.amount(r.get('total_claimed_amount')),
    } | _g702(r.get('summary'))


def _pay_app(project_id: int, p: dict) -> dict:
    contract = p.get('contract') or {}
    return {
        'id': p.get('id'),
        'project_id': project_id,
        'number': p.get('number'),
        'invoice_number': p.get('invoice_number'),
        'status': p.get('status'),
        'owner': p.get('formatted_contract_company'),
        'contract_id': contract.get('id'),
        'contract': contract.get('title'),
        'billing_date': p.get('billing_date'),
        'period_start': p.get('period_start'),
        'period_end': p.get('period_end'),
        'percent_complete': shape.amount(p.get('percent_complete')),
        'gross_amount_this_period': shape.amount(p.get('total_amount_accrued_this_period')),
        'total_amount_paid': shape.amount(p.get('total_amount_paid')),
        'updated_at': p.get('updated_at'),
    } | _g702(p.get('g702'))


async def _requisitions(project_id: int, params: dict, limit: int) -> tuple[list[dict], int | None]:
    rows, total = await procore.get_all(REQUISITIONS, {'project_id': project_id} | params, limit=limit)

    return [_requisition(project_id, r) for r in rows], total


async def _pay_apps(project_id: int, status: str | None, limit: int) -> tuple[list[dict], int | None]:
    rows, total = await procore.get_all(PAY_APPS, {'project_id': project_id}, limit=limit)
    items = [_pay_app(project_id, p) for p in rows if not status or p.get('status') == status]

    return items, None if status else total  # a status filter is applied here, so Procore's count no longer describes the items


@mcp.tool(title='List subcontractor invoices', annotations=READ_ONLY)
async def list_subcontractor_invoices(project_ids: list[int], status: SubStatus | None = None, commitment_id: int | None = None, limit: int = 100) -> dict:
    """
    List the invoices subcontractors and suppliers have submitted against their commitments
    on one or more projects (Procore's requisitions), every status unless status is given.

    project_ids takes up to 25 ids per call; take them from list_projects. Each item: id,
    project_id, number, invoice_number, status (draft, under_review, revise_and_resubmit,
    approved, approved_as_noted, pending_owner_approval), final, commitment_id and
    commitment_type (match list_commitments), contract, vendor, vendor_id, billing_date,
    period_start, period_end, submitted_at, payment_date, percent_complete,
    total_claimed_amount, and the G702 summary: contract_sum_to_date,
    total_completed_and_stored_to_date, total_retainage, total_earned_less_retainage,
    less_previous_certificates_for_payment, current_payment_due,
    balance_to_finish_including_retainage. commitment_id keeps one commitment's invoices;
    use it with that commitment's project only. limit applies per project. A project the
    user lacks the Invoicing tool on is listed under errors.
    """
    params: dict = {}
    if status:
        params['filters[status]'] = status
    if commitment_id:
        params['filters[commitment_id]'] = commitment_id

    return await batch.per_project(project_ids, lambda pid: _requisitions(pid, params, limit), sort_key=lambda r: (r['vendor'] or '', r['project_id'], r['number'] or 0))


@mcp.tool(title='List owner invoices', annotations=READ_ONLY)
async def list_owner_invoices(project_ids: list[int], status: OwnerStatus | None = None, limit: int = 100) -> dict:
    """
    List the invoices we have billed the owner on one or more projects (Procore's payment
    applications on the prime contract), every status unless status is given.

    project_ids takes up to 25 ids per call; take them from list_projects. Each item: id,
    project_id, number, invoice_number, status (draft, under_review, revise_and_resubmit,
    approved), owner, contract_id and contract (match list_prime_contracts), billing_date,
    period_start, period_end, percent_complete, gross_amount_this_period,
    total_amount_paid, updated_at, and the G702 summary: contract_sum_to_date,
    total_completed_and_stored_to_date, total_retainage, total_earned_less_retainage,
    less_previous_certificates_for_payment, current_payment_due,
    balance_to_finish_including_retainage. The last invoice on a project is the project's
    billed-to-date position. limit applies per project, before the status filter. A project
    the user lacks the Invoicing tool on is listed under errors.
    """
    return await batch.per_project(project_ids, lambda pid: _pay_apps(pid, status, limit), sort_key=lambda r: (r['project_id'], r['number'] or 0))
