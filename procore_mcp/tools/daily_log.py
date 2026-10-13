"""Daily log: manpower, notes, and weather entries."""

import asyncio
from datetime import date

from mcp.server.mcpserver.exceptions import ToolError

from .. import procore, shape
from ..server import READ_ONLY, mcp

MAX_DAYS = 31


def _day(label: str, value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ToolError(f'{label} must be a date in YYYY-MM-DD form, got {value!r}') from None


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


@mcp.tool(title='Get daily log', annotations=READ_ONLY)
async def get_daily_log(project_id: int, start_date: str, end_date: str) -> dict:
    """
    The daily log for a project over a date range: manpower (which companies were on site, with
    how many workers and hours), notes, and weather.

    Both dates are required, YYYY-MM-DD, inclusive, and at most 31 days apart; pass the same
    date twice for a single day. Procore keeps no default range here, so ask the user which
    date or range they want before calling rather than assuming today. Returns start_date,
    end_date, totals (workers and man_hours summed over the range), manpower, notes, weather.
    Use list_projects to find the project_id.
    """
    start, end = _day('start_date', start_date), _day('end_date', end_date)
    if end < start:
        raise ToolError(f'end_date {end} is before start_date {start}')
    if (end - start).days + 1 > MAX_DAYS:
        raise ToolError(f'{start} to {end} covers more than {MAX_DAYS} days; ask for a narrower range')

    params = {'start_date': start.isoformat(), 'end_date': end.isoformat()}
    (manpower, _), (notes, _), (weather, _) = await asyncio.gather(
        procore.get_all(f'/rest/v1.0/projects/{project_id}/manpower_logs', params, limit=500),
        procore.get_all(f'/rest/v1.0/projects/{project_id}/notes_logs', params, limit=500),
        procore.get_all(f'/rest/v1.1/projects/{project_id}/daily_logs/weather_logs', params, limit=500),
    )

    return {
        'start_date': start.isoformat(),
        'end_date': end.isoformat(),
        'totals': {
            'workers': int(sum(_number(m.get('num_workers')) for m in manpower)),
            'man_hours': sum(_number(m.get('man_hours')) for m in manpower),
        },
        'manpower': [{
            'date': m.get('date'),
            'company': shape.name(m.get('vendor')) or shape.name(m.get('contact')),
            'workers': m.get('num_workers'),
            'hours_each': m.get('num_hours'),
            'man_hours': m.get('man_hours'),
            'trade': shape.name(m.get('trade')),
            'location': shape.name(m.get('location')),
            'notes': m.get('notes'),
        } for m in manpower],
        'notes': [{
            'date': n.get('date'),
            'comment': n.get('comment'),
            'created_by': shape.name(n.get('created_by')),
            'location': shape.name(n.get('location')),
        } for n in notes],
        'weather': [{
            'date': w.get('date'),
            'time': w.get('time'),
            'sky': w.get('sky'),
            'temperature': w.get('temperature'),
            'average_temperature': w.get('average'),
            'precipitation': w.get('precipitation'),
            'wind': w.get('wind'),
            'ground': w.get('ground'),
            'calamity': w.get('calamity'),
            'weather_delay': bool(w.get('is_weather_delay')),
            'comments': w.get('comments'),
        } for w in weather],
    }
