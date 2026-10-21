"""
Schedule: the tasks of a project's schedule as Procore's Schedule tool holds them.

A schedule runs to hundreds or thousands of tasks, so this tool is per project and leans on
filters: milestones and the critical path are what a progress report reads, and Procore's
own filters cover text and recent updates. Dates arrive as UTC timestamps; the model reads
the date part. Variances are Procore's, in days against the baseline.
"""

from .. import procore, shape
from ..server import READ_ONLY, mcp

PATH = '/rest/v1.0/tasks'


def _row(t: dict) -> dict:
    return {
        'id': t.get('id'),
        'name': t.get('task_name') or t.get('name'),
        'wbs': t.get('wbs'),
        'start': t.get('start_datetime'),
        'finish': t.get('finish_datetime'),
        'percent_complete': t.get('percentage'),
        'milestone': bool(t.get('milestone')),
        'critical': bool(t.get('critical_path')),
        'duration_days': t.get('schedule_duration'),
        'actual_start': t.get('actual_start'),
        'actual_finish': t.get('actual_finish'),
        'baseline_start': t.get('baseline_start'),
        'baseline_finish': t.get('baseline_finish'),
        'start_variance': t.get('start_variance'),
        'finish_variance': t.get('finish_variance'),
        'resources': t.get('resource_name'),
        'parent_id': t.get('parent_id'),
        'has_children': bool(t.get('has_children')),
        'updated_at': t.get('updated_at'),
    }


@mcp.tool(title='List schedule tasks', annotations=READ_ONLY)
async def list_schedule_tasks(
    project_id: int,
    milestones_only: bool = False,
    critical_only: bool = False,
    search: str | None = None,
    updated_at: str | None = None,
    limit: int = 100,
) -> dict:
    """
    List the tasks on a project's schedule: every task unless filtered, in schedule order.

    Each item: id, name, wbs, start, finish, percent_complete, milestone, critical,
    duration_days, actual_start, actual_finish, baseline_start, baseline_finish,
    start_variance and finish_variance (days against the baseline, positive is late),
    resources, parent_id, has_children, updated_at. milestones_only and critical_only keep
    the matching tasks, which is what a progress report reads; both read the whole schedule
    first. search matches task names. updated_at takes a date range as
    "YYYY-MM-DD...YYYY-MM-DD". Use list_projects to find the project_id.
    """
    params: dict = {'project_id': project_id, 'view': 'normal'}
    if search:
        params['filters[query]'] = search
    if updated_at:
        params['filters[updated_at]'] = updated_at
    filtered = milestones_only or critical_only
    rows, total = await procore.get_all(PATH, params, limit=procore.MAX_ROWS if filtered else limit)
    items = [_row(t) for t in rows if (not milestones_only or t.get('milestone')) and (not critical_only or t.get('critical_path'))]

    return shape.listing(items[:limit], None if filtered else total)  # a filter is applied here, so Procore's count no longer describes the items
