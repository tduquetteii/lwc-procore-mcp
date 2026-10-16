"""
Directory vendors: the companies in the company directory and in a project's directory.

Two thin wrappers over Procore's directory endpoints. Procore's project-level copy of a
company record comes back without trades, cost codes, or vendor group even when the company
record carries them, so the company directory is where classification lives. Trades and cost
codes are matched by name on our side because their ids are company-specific and the model
never has them. Who is working where is a question for list_commitments; the report that
answers it is the model's to write.
"""

from .. import procore, shape
from ..config import COMPANY_ID
from ..server import READ_ONLY, mcp

VIEW = {'view': 'extended'}  # the view Procore documents trades, vendor_group, and cost codes on


def _cost_code(code: dict) -> str | None:
    return ' - '.join(part for part in (code.get('full_code'), code.get('name')) if part) or None


def _vendor(v: dict) -> dict:
    contact = v.get('primary_contact') or {}
    return {
        'id': v.get('id'),
        'name': v.get('name'),
        'trades': shape.names(v.get('trades')),
        'cost_codes': [c for c in map(_cost_code, v.get('standard_cost_codes') or []) if c],
        'vendor_group': shape.name(v.get('vendor_group')),
        'active': v.get('is_active'),
        'prequalified': v.get('prequalified'),
        'city': v.get('city'),
        'state_code': v.get('state_code'),
        'phone': v.get('business_phone'),
        'email': v.get('email_address'),
        'primary_contact': ' '.join(p for p in (contact.get('first_name'), contact.get('last_name')) if p) or None,
    }


def _in_trade(vendor: dict, trade: str | None) -> bool:
    """True when no trade was asked for, or one of the vendor's trades or cost codes contains it, case-insensitively."""
    return not trade or any(trade.lower() in t.lower() for t in vendor['trades'] + vendor['cost_codes'])


async def _list(path: str, params: dict, trade: str | None, limit: int) -> dict:
    rows, total = await procore.get_all(path, params, limit=limit)
    items = [v for v in map(_vendor, rows) if _in_trade(v, trade)]

    return shape.listing(items, None if trade else total)  # a trade filter is applied here, so Procore's count no longer describes the items


@mcp.tool(title='List project vendors', annotations=READ_ONLY)
async def list_project_vendors(project_id: int, search: str | None = None, trade: str | None = None, limit: int = 100) -> dict:
    """
    List the companies in a project's directory: every contractor, supplier, owner, and
    consultant added to that project. Being in the directory does not mean holding a
    contract; list_commitments says who is actually under contract.

    Each item: id, name, trades, cost_codes, vendor_group, active, prequalified, city,
    state_code, phone, email, primary_contact. Procore leaves trades, cost codes, and vendor
    group off this project-level copy; list_company_vendors has them. search matches the
    company name; trade keeps companies whose trade or cost code names contain the text.
    Use list_projects to find the project_id.
    """
    params: dict = dict(VIEW)
    if search:
        params['filters[search]'] = search

    return await _list(f'/rest/v1.1/projects/{project_id}/vendors', params, trade, limit)


@mcp.tool(title='List company vendors', annotations=READ_ONLY)
async def list_company_vendors(search: str | None = None, trade: str | None = None, limit: int = 100) -> dict:
    """
    List the companies in the company-wide Procore directory: the whole pool of
    subcontractors and suppliers available, whether or not they are on a project now.

    Each item: id, name, trades, cost_codes, vendor_group, active, prequalified, city,
    state_code, phone, email, primary_contact. search matches the company name; trade keeps
    companies whose trade or cost code names contain the text, such as "flooring" or
    "09-6000". The directory holds well over a thousand companies, so filter or raise limit
    (up to 1000) deliberately. Vendor ids here match vendor_id on commitments.
    """
    params: dict = {'company_id': COMPANY_ID} | VIEW
    if search:
        params['filters[search]'] = search

    return await _list('/rest/v1.1/vendors', params, trade, limit)
