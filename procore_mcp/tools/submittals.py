"""Submittals."""

from mcp.server.mcpserver.exceptions import ToolError

from .. import procore, shape
from ..cache import TTLCache
from ..config import COMPANY_ID
from ..server import READ_ONLY, mcp

GROUPS     = ('Open', 'Closed', 'Draft')
STATUS_TTL = 3600  # seconds; statuses change about never, and each lookup is a Procore request
_statuses: TTLCache[str, list[dict]] = TTLCache()


async def statuses() -> list[dict]:
    """
    The company's submittal statuses: id, name, and the group (Open, Closed, Draft) each belongs to.

    Procore identifies them by company-specific ids, so they are fetched once an hour and cached.
    """
    if (rows := _statuses.get(COMPANY_ID)) is None:
        rows, _ = await procore.get_all(f'/rest/v1.0/companies/{COMPANY_ID}/submittal_statuses', limit=200)
        _statuses.set(COMPANY_ID, rows, STATUS_TTL)

    return rows


async def status_ids(status: str) -> list[int]:
    """Ids of the statuses whose group or exact name matches, case-insensitively."""
    wanted = status.strip().lower()
    ids = [s['id'] for s in await statuses() if wanted in (str(s.get('status', '')).lower(), str(s.get('name', '')).lower())]
    if not ids:
        known = ', '.join(sorted({s['name'] for s in await statuses()}))
        raise ToolError(f'Unknown submittal status {status!r}. Use a group ({", ".join(GROUPS)}) or one of: {known}')

    return ids


@mcp.tool(title='List submittals', annotations=READ_ONLY)
async def list_submittals(
    project_id: int,
    status: str | None = 'Open',
    ball_in_court_id: int | None = None,
    responsible_contractor_id: int | None = None,
    search: str | None = None,
    current_revision_only: bool = True,
    limit: int = 100,
) -> dict:
    """
    List submittals on a project: open ones unless status says otherwise, None meaning every status.

    status is a group (Open, Closed, Draft) or a status name the company configured, such as
    "In Review". Each item: id, number, title, revision, status, status_group, due_date,
    submit_by, required_on_site_date, ball_in_court, responsible_contractor, submittal_manager,
    spec_section, type. current_revision_only=False also lists superseded revisions.
    search matches title and number. Use list_projects to find the project_id.
    """
    params: dict = {}
    if status:
        params['filters[status_id][]'] = await status_ids(status)
    if ball_in_court_id:
        params['filters[ball_in_court_id]'] = ball_in_court_id
    if responsible_contractor_id:
        params['filters[responsible_contractor_id]'] = responsible_contractor_id
    if search:
        params['filters[query]'] = search
    if current_revision_only:
        params['filters[current_revision]'] = 'true'
    rows, total = await procore.get_all(f'/rest/v1.1/projects/{project_id}/submittals', params, limit=limit)
    items = [{
        'id': s.get('id'),
        'number': s.get('formatted_number') or s.get('number'),
        'title': s.get('title'),
        'revision': s.get('revision'),
        'status': shape.name(s.get('status')),
        'status_group': (s.get('status') or {}).get('status'),
        'due_date': s.get('due_date'),
        'submit_by': s.get('submit_by'),
        'required_on_site_date': s.get('required_on_site_date'),
        'ball_in_court': shape.names(s.get('ball_in_court')),
        'responsible_contractor': shape.name(s.get('responsible_contractor')),
        'submittal_manager': shape.name(s.get('submittal_manager')),
        'spec_section': (s.get('specification_section') or {}).get('label'),
        'type': shape.name(s.get('type')),
    } for s in rows]

    return shape.listing(items, total)
