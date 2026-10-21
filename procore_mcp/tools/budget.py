"""
Budget: a project's budget lines as one of its budget views shows them.

Procore's budget is read through a budget view: a company-configured set of columns
(Original Budget, Approved COs, Revised Budget, Committed Costs, Projected Costs, and so
on) over the project's budget lines. The detail rows endpoint returns the line's cost code
and category as fixed fields and every visible column of the view as an extra key named
after the column. The names are the company's, so the tool passes them through under
`columns` rather than guessing a mapping. One request finds the project's views and a second
reads the rows; the tool picks the first view unless told which. Budgets are per project and
a partial budget misleads, so the default limit is higher than the other list tools'.
"""

from mcp.server.mcpserver.exceptions import ToolError

from .. import procore, shape
from ..server import READ_ONLY, mcp

VIEWS   = '/rest/v1.0/budget_views'
LIMIT   = 500  # rows per budget by default; a budget of a few hundred lines is read whole
FIXED   = {  # keys the row describes itself with; every other key is a column of the view
    'id', 'deletable', 'company_id', 'company', 'project_id', 'wbs_code_id', 'biller_id', 'root_cost_code_id', 'cost_code_id',
    'cost_code_origin_id', 'category_id', 'project', 'biller', 'biller_type', 'root_cost_code', 'cost_code', 'category',
    'budget_modifications', 'original_budget_amount', 'budget_forecast', 'currency_configuration', 'unbudgeted_reason',
}


def _row(r: dict) -> dict:
    forecast = r.get('budget_forecast') or {}
    return {
        'cost_code': r.get('cost_code'),
        'category': r.get('category'),
        'sub_job': r.get('biller') if r.get('biller_type') == 'SubJob' else None,
        'budgeted': r.get('id') is not None,
        'original_budget': shape.amount(r.get('original_budget_amount')),
        'budget_modifications': shape.amount(r.get('budget_modifications')),
        'forecast': shape.amount(forecast.get('amount')),
        'columns': {k: a if (a := shape.amount(v)) is not None else v for k, v in r.items() if k not in FIXED},
    }


@mcp.tool(title='List budget rows', annotations=READ_ONLY)
async def list_budget_rows(project_id: int, view_id: int | None = None, limit: int = LIMIT) -> dict:
    """
    List a project's budget, line by line, as one of its budget views shows it: the cost
    code, the fixed budget amounts, and every column of the view.

    Returns view (the one read: id, name, role), views (every view the project has), and the
    items. Each item: cost_code, category, sub_job, budgeted (False for a line that exists
    only through costs), original_budget, budget_modifications, forecast, and columns, a
    mapping of the view's column names to their values, such as "Revised Budget",
    "Committed Costs", "Direct Costs", "Projected Costs", "Projected Over Under"; the names
    are whatever the company configured, so read them from the first call. Sum columns
    across items for the project total. The first view is read unless view_id names
    another. Use list_projects to find the project_id.
    """
    views, _ = await procore.get_all(VIEWS, {'project_id': project_id})
    choices = [shape.pick(v, 'id', 'name', 'role') for v in views]
    if not choices:
        raise ToolError(f'project {project_id} has no budget views; the Budget tool is not set up on it')
    view = next((v for v in choices if v['id'] == view_id), None) if view_id else choices[0]
    if view is None:
        raise ToolError(f'view_id {view_id} is not a budget view of project {project_id}; it has ' + ', '.join(f'{v["id"]} ({v["name"]})' for v in choices))
    rows, total = await procore.get_all(f'{VIEWS}/{view["id"]}/detail_rows', {'project_id': project_id, 'budget_row_type': 'all'}, limit=limit)

    return {'view': view, 'views': choices} | shape.listing([_row(r) for r in rows], total)
