"""
Bidding: bid packages and the bids within them.

Bid coverage is the front end of spreading work across subcontractors: who gets invited,
who answers, who never does. Procore lists bid packages per project and bids per package,
so list_bids reads a project's packages itself when no package is named and gathers every
bid in the project in one call. Procore documents only a handful of fields on a package
row; the invited and will-bid counts it sorts by are read when present and left None when
the view omits them.
"""

from typing import Literal

from .. import batch, procore, shape
from ..server import READ_ONLY, mcp

Filter = Literal['open', 'closed', 'hidden']

VIEW = {'view': 'all'}  # open and closed packages alike; the default view hides closed ones


def _packages(project_id: int) -> str:
    return f'/rest/v1.1/projects/{project_id}/bid_packages'


def _package(project_id: int, p: dict) -> dict:
    return {
        'id': p.get('id'),
        'project_id': project_id,
        'number': p.get('number'),
        'title': p.get('title'),
        'status': p.get('status'),
        'bid_due_date': p.get('bid_due_date'),
        'invited_bids_count': p.get('invited_bids_count'),
        'bids_will_bid_count': p.get('bids_will_bid_count'),
        'submitted_bids_count': p.get('submitted_bids_count'),
    }


def _bid(project_id: int, b: dict) -> dict:
    vendor = b.get('vendor') or {}
    return {
        'id': b.get('id'),
        'project_id': project_id,
        'bid_package_id': b.get('bid_package_id'),
        'bid_package_title': b.get('bid_package_title'),
        'vendor': vendor.get('name'),
        'vendor_id': vendor.get('id'),
        'vendor_trades': vendor.get('trades'),
        'bid_status': b.get('bid_status'),
        'submitted': bool(b.get('submitted')),
        'awarded': bool(b.get('awarded')),
        'committed': bool(b.get('is_bidder_committed')),
        'lump_sum_amount': shape.amount(b.get('lump_sum_amount')),
        'invitation_last_sent_at': b.get('invitation_last_sent_at'),
        'due_date': b.get('due_date'),
        'bidder_comments': shape.text(b.get('bidder_comments')),
        'updated_at': b.get('updated_at'),
    }


async def _project_packages(project_id: int, params: dict, limit: int) -> tuple[list[dict], int | None]:
    rows, total = await procore.get_all(_packages(project_id), params, limit=limit)

    return [_package(project_id, p) for p in rows], total


async def _package_bids(project_id: int, package_id: int, limit: int) -> tuple[list[dict], int | None]:
    rows, total = await procore.get_all(f'{_packages(project_id)}/{package_id}/bids', limit=limit)

    return [_bid(project_id, b) for b in rows], total


@mcp.tool(title='List bid packages', annotations=READ_ONLY)
async def list_bid_packages(project_ids: list[int], filter: Filter | None = None, limit: int = 100) -> dict:
    """
    List the bid packages on one or more projects: each scope that was put out to bid, open
    and closed alike unless filter says otherwise.

    project_ids takes up to 25 ids per call; take them from list_projects. Each item: id,
    project_id, number, title, status, bid_due_date, invited_bids_count, bids_will_bid_count,
    submitted_bids_count (the counts are None when Procore omits them). list_bids reads the
    bids within a package, or every bid on a project. limit applies per project. A project
    the user lacks the Bidding tool on is listed under errors.
    """
    params: dict = dict(VIEW)
    if filter:
        params['filter'] = filter

    return await batch.per_project(project_ids, lambda pid: _project_packages(pid, params, limit), sort_key=lambda r: (r['project_id'], r['number'] or 0))


@mcp.tool(title='List bids', annotations=READ_ONLY)
async def list_bids(project_id: int, bid_package_id: int | None = None, limit: int = 100) -> dict:
    """
    List the bids on a project: every company invited to a bid package and where each
    stands. One package when bid_package_id is given, otherwise every package on the
    project, open and closed.

    Each item: id, project_id, bid_package_id, bid_package_title, vendor, vendor_id,
    vendor_trades, bid_status (not_invited, undecided, will_bid, will_not_bid, submitted),
    submitted, awarded, committed, lump_sum_amount, invitation_last_sent_at, due_date,
    bidder_comments, updated_at. Who was invited but never submitted, and who is never
    invited, is read from this against list_company_vendors. limit applies per package.
    A package the user cannot read is listed under errors. Use list_projects to find the
    project_id and list_bid_packages to find a bid_package_id.
    """
    if bid_package_id:
        package_ids = [bid_package_id]
    else:
        packages, _ = await procore.get_all(_packages(project_id), VIEW, limit=procore.MAX_ROWS)
        package_ids = [p['id'] for p in packages if p.get('id')]

    return await batch.each(package_ids, lambda pkg: _package_bids(project_id, pkg, limit), 'bid_package_id', sort_key=lambda r: (r['bid_package_id'] or 0, r['vendor'] or ''))
