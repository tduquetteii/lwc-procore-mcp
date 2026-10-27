"""
One fetch per id, for tools that take a batch of project (or other) ids.

The questions these tools serve, who is under contract where, what change exposure is open,
what every job is worth, span every active project, and one tool call per project would be
a hundred-plus calls for the model. So a tool takes up to MAX_PROJECTS ids, runs its fetch
for each concurrently (the gate in procore.py paces the requests), tags each row with its
project_id, and reports a project the user cannot read under `errors` instead of failing
the whole call. The same shape serves a batch of commitments or bid packages within one
project; `each` is the untyped core and `per_project` the validated, project-keyed form.
"""

import asyncio
from collections.abc import Awaitable, Callable

from mcp.server.mcpserver.exceptions import ToolError

from . import shape

MAX_PROJECTS = 25  # ids per call; a request or two each through the gate, well inside the minute a client allows

Fetch = Callable[[int], Awaitable[tuple[list[dict], int | None]]]  # id -> (rows, Procore's count)


async def each(ids: list[int], fetch: Fetch, label: str, sort_key: Callable[[dict], tuple] | None = None) -> dict:
    """Run `fetch` for every id and merge the results into one listing; a ToolError for one id lands under `errors` as {label: id, error}."""
    results = await asyncio.gather(*(fetch(i) for i in ids), return_exceptions=True)
    items: list[dict] = []
    errors: list[dict] = []
    total = 0
    for i, result in zip(ids, results):
        if isinstance(result, ToolError):
            errors.append({label: i, 'error': str(result)})
        elif isinstance(result, BaseException):
            raise result
        else:
            rows, count = result
            items.extend(rows)
            total += len(rows) if count is None else count
    if sort_key:
        items.sort(key=sort_key)

    return shape.listing(items, total) | {'errors': errors}


async def per_project(project_ids: list[int], fetch: Fetch, sort_key: Callable[[dict], tuple] | None = None, label: str = 'project_id') -> dict:
    """`each` over a batch of ids the model supplied: at least one, at most MAX_PROJECTS."""
    if not project_ids:
        raise ToolError(f'{label}s must name at least one id' + ('; use list_projects to find them' if label == 'project_id' else ''))
    if len(project_ids) > MAX_PROJECTS:
        raise ToolError(f'{label}s holds {len(project_ids)} ids; call with at most {MAX_PROJECTS} at a time')

    return await each(project_ids, fetch, label, sort_key)
