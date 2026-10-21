"""
One fetch per project, for tools that take a batch of project ids.

The questions these tools serve, who is under contract where, what change exposure is open,
what every job is worth, span every active project, and one tool call per project would be
a hundred-plus calls for the model. So a tool takes up to MAX_PROJECTS ids, runs its fetch
for each concurrently (the gate in procore.py paces the requests), tags each row with its
project_id, and reports a project the user cannot read under `errors` instead of failing
the whole call.
"""

import asyncio
from collections.abc import Awaitable, Callable

from mcp.server.mcpserver.exceptions import ToolError

from . import shape

MAX_PROJECTS = 25  # ids per call; a request or two each through the gate, well inside the minute a client allows

Fetch = Callable[[int], Awaitable[tuple[list[dict], int | None]]]  # project_id -> (rows, Procore's count)


async def per_project(project_ids: list[int], fetch: Fetch, sort_key: Callable[[dict], tuple] | None = None) -> dict:
    """Run `fetch` for every project id and merge the results into one listing with an `errors` list."""
    if not project_ids:
        raise ToolError('project_ids must name at least one project; use list_projects to find ids')
    if len(project_ids) > MAX_PROJECTS:
        raise ToolError(f'project_ids holds {len(project_ids)} ids; call with at most {MAX_PROJECTS} at a time')
    results = await asyncio.gather(*(fetch(pid) for pid in project_ids), return_exceptions=True)
    items: list[dict] = []
    errors: list[dict] = []
    total = 0
    for pid, result in zip(project_ids, results):
        if isinstance(result, ToolError):
            errors.append({'project_id': pid, 'error': str(result)})
        elif isinstance(result, BaseException):
            raise result
        else:
            rows, count = result
            items.extend(rows)
            total += len(rows) if count is None else count
    if sort_key:
        items.sort(key=sort_key)

    return shape.listing(items, total) | {'errors': errors}
