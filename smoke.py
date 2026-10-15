"""
Offline smoke checks. No running server, no Procore sign-in.

    uv run smoke.py

Loads .env when present, otherwise uses placeholders for the four required variables.
Checks the OAuth routes, the auth gate on /mcp, the metadata every tool must carry, the
identity sent in the MCP handshake, and every tool against a fake Procore that answers
from fixtures: request paths and query parameters, pagination, filtering, trimming, rate
limiting, and how Procore errors reach the model. Exits 1 if any check fails. Checks with
a real token are a job for the MCP Inspector or your MCP client.
"""

import asyncio
import base64
import logging
import os
import re
import sys
import time
from types import SimpleNamespace

import httpx
from dotenv import load_dotenv

load_dotenv()
for key, placeholder in {
    'PUBLIC_URL': 'https://smoke.invalid',
    'PROCORE_CLIENT_ID': 'smoke-client-id',
    'PROCORE_CLIENT_SECRET': 'smoke-client-secret',
    'PROCORE_COMPANY_ID': '1',
}.items():
    os.environ.setdefault(key, placeholder)
logging.disable(logging.INFO)  # keep the SDK's request log out of the report

import main  # noqa: E402  needs the environment above
from mcp.server.auth.provider import AccessToken  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from procore_mcp import config, procore  # noqa: E402
from procore_mcp.auth import ProcoreUnavailable  # noqa: E402
from procore_mcp.server import verifier  # noqa: E402
from procore_mcp.tools import daily_log, me, projects, punch_items, rfis, submittals  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

failed: list[str] = []


def check(label: str, ok: bool) -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failed.append(label)


# ------------- FAKE PROCORE -------------
# Keyed by request path. Lists are served a page at a time with a Total header, like Procore.
PERSON = {'id': 9, 'login': 'pat@example.com', 'name': 'Pat Manager'}
FIXTURES: dict[str, dict | list] = {
    '/rest/v1.0/me': PERSON | {'extra': 'dropped'},
    '/rest/v1.0/projects': [
        {'id': 1, 'name': 'Active A', 'project_number': '100', 'active': True, 'city': 'Portland', 'state_code': 'ME', 'extra': 'dropped'},
        {'id': 2, 'name': 'Inactive B', 'project_number': '200', 'active': False, 'city': 'Nashua', 'state_code': 'NH', 'extra': 'dropped'},
        {'id': 3, 'name': 'Active C', 'project_number': '300', 'active': True, 'city': 'Boston', 'state_code': 'MA', 'extra': 'dropped'},
    ],
    '/rest/v1.0/projects/7/rfis': [
        {'id': 11, 'number': '4', 'full_number': 'RFI-004', 'subject': 'Door hardware', 'status': 'open', 'due_date': '2026-10-15',
         'ball_in_courts': [{'id': 1, 'name': 'Ann Architect - SID'}], 'assignees': [{'id': 1, 'name': 'Ann Architect', 'response_required': True}],
         'responsible_contractor': {'id': 5, 'name': 'Acme Doors'}, 'rfi_manager': PERSON,
         'cost_impact': {'status': 'tbd', 'value': None}, 'schedule_impact': {'status': 'no_impact', 'value': None},
         'priority': {'name': 'High', 'value': 'high'}, 'link': 'https://app.procore.com/7/project/rfi/show/11', 'extra': 'dropped'},
        {'id': 12, 'number': '5', 'full_number': 'RFI-005', 'subject': 'Slab thickness', 'status': 'open', 'due_date': '2026-10-20',
         'ball_in_courts': [], 'assignees': [], 'responsible_contractor': None, 'rfi_manager': PERSON,
         'cost_impact': {'status': 'n_a', 'value': None}, 'schedule_impact': {'status': 'n_a', 'value': None}, 'priority': None, 'link': None},
    ],
    f'/rest/v1.0/companies/{config.COMPANY_ID}/submittal_statuses': [
        {'id': 100, 'name': 'Open', 'status': 'Open', 'is_default': True},
        {'id': 101, 'name': 'In Review', 'status': 'Open', 'is_default': False},
        {'id': 102, 'name': 'Approved', 'status': 'Closed', 'is_default': False},
    ],
    '/rest/v1.1/projects/7/submittals': [
        {'id': 21, 'number': '3', 'formatted_number': '03-003', 'title': 'Vinyl windows', 'revision': '1', 'current_revision': True,
         'status': {'id': 101, 'name': 'In Review', 'status': 'Open'}, 'due_date': '2026-10-20', 'submit_by': '2026-10-01', 'required_on_site_date': '2026-11-15',
         'ball_in_court': [{'id': 1, 'name': 'Ann Architect', 'initials': 'AA', 'vendor': {'id': 5, 'name': 'SID'}}],
         'responsible_contractor': {'id': 6, 'name': 'Glass Co'}, 'submittal_manager': PERSON,
         'specification_section': {'id': 3, 'number': '08560', 'description': 'Vinyl Windows', 'label': '08560 Vinyl Windows'},
         'type': {'id': 42, 'name': 'Product Data'}, 'extra': 'dropped'},
    ],
    '/rest/v1.1/punch_items': [
        {'id': 31, 'position': 12, 'name': 'Scratched door frame', 'description': 'x' * 400, 'status': 'Open', 'workflow_status': 'initiated',
         'priority': 'high', 'due_date': '2026-10-18', 'location': {'id': 1, 'name': 'Level 1>Room 101', 'node_name': 'Room 101'},
         'trade': {'id': 2, 'name': '08 - Doors'}, 'assignees': [{'id': 4, 'login': 'sam@example.com', 'name': 'Sam Sub'}],
         'ball_in_court': [{'id': 4, 'name': 'Sam Sub'}], 'punch_item_manager': PERSON, 'final_approver': PERSON,
         'punch_item_type': {'id': 1, 'name': 'Deficiency'}, 'extra': 'dropped'},
    ],
    '/rest/v1.0/projects/7/manpower_logs': [
        {'id': 41, 'date': '2026-10-06', 'num_workers': 6, 'num_hours': '8.0', 'man_hours': '48.0', 'vendor': {'id': 5, 'name': 'Acme Electric'},
         'trade': {'id': 1, 'name': 'Electrical'}, 'location': None, 'notes': 'Rough-in level 2'},
        {'id': 42, 'date': '2026-10-07', 'num_workers': 4, 'num_hours': '8.0', 'man_hours': '32.0', 'vendor': None,
         'contact': {'id': 8, 'name': 'Self-perform crew'}, 'trade': None, 'location': {'id': 1, 'name': 'Level 2'}, 'notes': None},
    ],
    '/rest/v1.0/projects/7/notes_logs': [
        {'id': 51, 'date': '2026-10-06', 'comment': 'Inspector on site', 'created_by': PERSON, 'location': {'id': 1, 'name': 'Level 1'}},
    ],
    '/rest/v1.1/projects/7/daily_logs/weather_logs': [
        {'id': 61, 'date': '2026-10-06', 'time': '2026-10-06T11:00:00Z', 'sky': 'Overcast', 'temperature': 'Cool', 'average': '55',
         'precipitation': 'Rain', 'wind': 'Breezy', 'ground': 'Wet', 'calamity': None, 'is_weather_delay': 1, 'comments': 'Pour postponed'},
    ],
}
requests: list[httpx.Request] = []


scenario: dict = {}              # knobs for one check: 429_once, 429_far, 503_once, 503_far, remaining, me_status, slow
inflight = {'now': 0, 'max': 0}  # concurrent requests the fake has seen


def answer(request: httpx.Request) -> httpx.Response:
    path, now = request.url.path, int(time.time())
    limits = {'X-Rate-Limit-Limit': '25', 'X-Rate-Limit-Remaining': scenario.get('remaining', '20'), 'X-Rate-Limit-Reset': str(now + 1)}
    if path == '/rest/v1.0/me' and scenario.get('me_status'):
        return httpx.Response(scenario['me_status'], json={'errors': 'nope'}, headers={'X-Rate-Limit-Reset': str(now + 30)})
    if scenario.pop('429_once', False):
        return httpx.Response(429, json={'message': 'Too Many Requests'}, headers=limits | {'X-Rate-Limit-Remaining': '0'})
    if scenario.get('429_far'):
        return httpx.Response(429, json={'message': 'Too Many Requests'}, headers=limits | {'X-Rate-Limit-Remaining': '0', 'X-Rate-Limit-Reset': str(now + 90)})
    if scenario.pop('503_once', False):
        return httpx.Response(503, text='maintenance', headers={'Retry-After': '1'})
    if scenario.get('503_far'):
        return httpx.Response(503, text='maintenance', headers={'Retry-After': '120'})
    if '/projects/403/' in path:
        return httpx.Response(403, json={'errors': 'You do not have access to this tool'})
    body = FIXTURES.get(path)
    if body is None:
        return httpx.Response(404, json={'error': f'no fixture for {path}'})
    if isinstance(body, list):
        q = request.url.params
        if path == '/rest/v1.0/projects' and q.get('filters[by_status]', 'Active') == 'Active':
            body = [p for p in body if p['active']]  # Procore lists active projects unless asked for All
        page, per_page = int(q.get('page', 1)), int(q.get('per_page', 100))
        return httpx.Response(200, json=body[(page - 1) * per_page:page * per_page], headers=limits | {'Total': str(len(body)), 'Per-Page': str(per_page)})
    return httpx.Response(200, json=body, headers=limits)


async def fake_procore(request: httpx.Request) -> httpx.Response:
    requests.append(request)
    inflight['now'] += 1
    inflight['max'] = max(inflight['max'], inflight['now'])
    try:
        if scenario.get('slow'):
            await asyncio.sleep(0.05)
        return answer(request)
    finally:
        inflight['now'] -= 1


def fake_client(timeout: float = 30) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(fake_procore), base_url=config.PROCORE_API)


procore.client = fake_client  # the token verifier goes through the same seam
procore.get_access_token = lambda: SimpleNamespace(token='smoke-token', subject='9')
procore.PAGE_SIZE = 2  # small pages so the fixtures exercise pagination


def fresh() -> None:
    """Reset the fake and the client's rate-limit memory between checks."""
    scenario.clear()
    requests.clear()
    inflight['now'] = inflight['max'] = 0
    procore._pause_until = 0.0
    procore._gate = asyncio.Semaphore(procore.CONCURRENCY)  # a fresh gate per event loop


def run(coro):
    """Run a tool call against the fake, clearing the request log first; asyncio.run makes a new loop, so the gate is remade too."""
    requests.clear()
    procore._gate = asyncio.Semaphore(procore.CONCURRENCY)
    return asyncio.run(coro)


def error(coro) -> str:
    """The ToolError message a call raises, or '' when it succeeds."""
    try:
        run(coro)
    except ToolError as exc:
        return str(exc)
    return ''


def query(request: httpx.Request) -> dict[str, list[str]]:
    q = request.url.params
    return {k: q.get_list(k) for k in q}


def wait_in(message: str) -> int:
    found = re.search(r'wait (\d+)s', message)
    return int(found.group(1)) if found else -1


# ------------- TOOL METADATA -------------
print('-- tools')
tools = asyncio.run(main.mcp.list_tools())
check('six tools registered', sorted(t.name for t in tools) == ['get_daily_log', 'get_me', 'list_projects', 'list_punch_items', 'list_rfis', 'list_submittals'])
for t in tools:
    a = t.annotations
    one_hint = a is not None and (a.read_only_hint is True) != (a.destructive_hint is True)
    check(f'{t.name}: title set', bool(t.title))
    check(f'{t.name}: description set', bool(t.description and t.description.strip()))
    check(f'{t.name}: exactly one of read_only_hint / destructive_hint', one_hint)
schema = next(t for t in tools if t.name == 'get_daily_log').model_dump(by_alias=True)['inputSchema']
check('get_daily_log requires both dates, so the model must ask for them', set(schema.get('required', [])) == {'project_id', 'start_date', 'end_date'})


# ------------- SERVER IDENTITY -------------
print('-- handshake')
check('server name set', bool(main.mcp.name))
check('website_url set', bool(main.mcp.website_url))
icons = main.mcp.icons or []
check('one icon advertised', len(icons) == 1)
if icons:
    prefix = 'data:image/svg+xml;base64,'
    check('icon is a base64 SVG data URI', icons[0].src.startswith(prefix))
    if icons[0].src.startswith(prefix):
        payload = base64.b64decode(icons[0].src[len(prefix):])
        check('icon payload is SVG', payload.lstrip().startswith(b'<svg'))
        check('icon payload matches icon.svg', payload == config.ICON_PATH.read_bytes())


# ------------- PROCORE CLIENT -------------
print('-- procore client')
result = run(me.get_me())
check('get_me: GET /rest/v1.0/me with company_id', requests[0].url.path == '/rest/v1.0/me' and query(requests[0]) == {'company_id': [config.COMPANY_ID]})
check('sends the user token and the company header', requests[0].headers['Authorization'] == 'Bearer smoke-token' and requests[0].headers['Procore-Company-Id'] == config.COMPANY_ID)
check('get_me: trimmed to id, name, login', result == PERSON)
message = error(rfis.list_rfis(403))
check('Procore error reaches the model with status and body', '403' in message and 'do not have access' in message)
run(rfis.list_rfis(7))
check('walks pages until a short page (2 rows, 2 per page: 2 requests)', [query(r)['page'] for r in requests] == [['1'], ['2']])


# ------------- RATE LIMITING -------------
print('-- rate limiting')
fresh()
scenario['429_once'] = True
started = time.monotonic()
result = run(me.get_me())
check('429 with a short reset: waits, retries once, succeeds', result == PERSON and len(requests) == 2 and time.monotonic() - started >= 0.9)
fresh()
scenario['429_far'] = True
message = error(me.get_me())
check('429 with a long reset: no retry, model told how long to wait', len(requests) == 1 and 'rate limit' in message and 85 <= wait_in(message) <= 92)
scenario.clear()
message = error(me.get_me())
check('limit remembered: next call fails fast without a request', len(requests) == 0 and 85 <= wait_in(message) <= 92)
fresh()
scenario['remaining'] = '0'
run(me.get_me())
scenario.clear()
started = time.monotonic()
run(me.get_me())
check('X-Rate-Limit-Remaining 0 pauses the next call until reset', len(requests) == 1 and time.monotonic() - started >= 0.9)
fresh()
untripped = httpx.Response(429, headers={'X-Rate-Limit-Remaining': '20', 'X-Rate-Limit-Reset': str(int(time.time()) + 3500)})
procore.note_limits(untripped)
check('429 whose headers describe an untripped window: default short pause, not its far reset', procore.retry_after(untripped) == 5 and 0 < procore.pause_left() <= 5)
fresh()
scenario['503_once'] = True
result = run(me.get_me())
check('503 with a short Retry-After: retried once, succeeds', result == PERSON and len(requests) == 2)
fresh()
scenario['503_far'] = True
message = error(me.get_me())
check('503 with a long Retry-After: no retry, named as an outage not a limit', len(requests) == 1 and 'unavailable (503)' in message and 'rate limit' not in message and 118 <= wait_in(message) <= 121)
scenario.clear()
message = error(me.get_me())
check('outage remembered: next call fails fast with the same cause', len(requests) == 0 and 'unavailable (503)' in message)
fresh()
scenario['slow'] = True


async def eight():
    return await asyncio.gather(*(me.get_me() for _ in range(8)))


results = run(eight())
check('parallel calls queue behind a 4-wide gate', inflight['max'] == procore.CONCURRENCY == 4 and len(requests) == 8 and all(r == PERSON for r in results))
fresh()


# ------------- LIST_PROJECTS -------------
print('-- list_projects')
projects._cache.clear()
result = run(projects.list_projects())
q = query(requests[0])
check('asks Procore for active projects, 300 per page', requests[0].url.path == '/rest/v1.0/projects' and q['filters[by_status]'] == ['Active'] and q['per_page'] == ['300'] and q['company_id'] == [config.COMPANY_ID])
check('active_only=True lists only active projects', [p['id'] for p in result['items']] == [1, 3])
check('envelope reports total and returned', (result['total'], result['returned']) == (2, 2))
check('rows trimmed to documented keys', all(set(p) == {'id', 'name', 'project_number', 'active', 'city', 'state_code'} for p in result['items']))
run(projects.list_projects())
check('project list cached per user for a minute: no request', len(requests) == 0)
procore.get_access_token = lambda: SimpleNamespace(token='other-token', subject='9')
run(projects.list_projects())
check('the same user on another token shares the cache: no request', len(requests) == 0)
procore.get_access_token = lambda: SimpleNamespace(token='other-token', subject='10')
run(projects.list_projects())
check('another user gets their own request', len(requests) == 1)
procore.get_access_token = lambda: SimpleNamespace(token='smoke-token', subject='9')
result = run(projects.list_projects(active_only=False))
check('active_only=False asks for All and keeps inactive projects', query(requests[0])['filters[by_status]'] == ['All'] and [p['id'] for p in result['items']] == [1, 2, 3])


# ------------- LIST_RFIS -------------
print('-- list_rfis')
result = run(rfis.list_rfis(7, overdue=True, search='door', limit=1))
q = query(requests[0])
check('GET /rest/v1.0/projects/{id}/rfis', requests[0].url.path == '/rest/v1.0/projects/7/rfis')
check('default status filter is open, web_index view requested', q['filters[status]'] == ['open'] and q['serializer_view'] == ['web_index'])
check('overdue and search forwarded', q['filters[overdue]'] == ['true'] and q['search'] == ['door'])
check('limit caps rows while total still reports the Procore count', (result['returned'], result['total'], len(requests)) == (1, 2, 1))
row = result['items'][0]
check('number prefers full_number', row['number'] == 'RFI-004')
check('people flattened to names', row['ball_in_court'] == ['Ann Architect - SID'] and row['assignees'] == ['Ann Architect'] and row['responsible_contractor'] == 'Acme Doors')
check('priority flattened, link kept', row['priority'] == 'High' and row['link'].endswith('/show/11'))
run(rfis.list_rfis(7, status=None))
check('status=None sends no status or overdue filter', 'filters[status]' not in query(requests[0]) and 'filters[overdue]' not in query(requests[0]))
result = run(rfis.list_rfis(7))
check('empty people and null objects become [] and None', result['items'][1]['ball_in_court'] == [] and result['items'][1]['responsible_contractor'] is None and result['items'][1]['priority'] is None)


# ------------- LIST_SUBMITTALS -------------
print('-- list_submittals')
STATUSES_PATH = f'/rest/v1.0/companies/{config.COMPANY_ID}/submittal_statuses'
result = run(submittals.list_submittals(7, status='in review'))
check('status name resolved through the company statuses first', requests[0].url.path == STATUSES_PATH)
q = query(requests[-1])
check('GET /rest/v1.1/projects/{id}/submittals with the matching status id', requests[-1].url.path == '/rest/v1.1/projects/7/submittals' and q['filters[status_id][]'] == ['101'])
check('current revisions only by default', q['filters[current_revision]'] == ['true'])
row = result['items'][0]
check('status name and group both reported', (row['status'], row['status_group']) == ('In Review', 'Open'))
check('number, spec section, people, type flattened', row['number'] == '03-003' and row['spec_section'] == '08560 Vinyl Windows' and row['ball_in_court'] == ['Ann Architect'] and row['type'] == 'Product Data')
run(submittals.list_submittals(7, status='open', search='window', current_revision_only=False))
q = query(requests[-1])
check('a status group matches every status in the group', q['filters[status_id][]'] == ['100', '101'])
check('statuses cached: no second statuses request', all(r.url.path != STATUSES_PATH for r in requests))
check('search forwarded, current_revision_only=False drops the filter', q['filters[query]'] == ['window'] and 'filters[current_revision]' not in q)
message = error(submittals.list_submittals(7, status='Bogus'))
check('unknown status names the known ones', 'Bogus' in message and 'In Review' in message and 'Approved' in message)


# ------------- TOKEN VERIFIER -------------
print('-- token verifier')
fresh()
verifier.cache.clear()
access = run(verifier.verify_token('tok-a'))
check('valid token: /me called with that token, identity cached', access is not None and access.subject == '9' and requests[0].headers['Authorization'] == 'Bearer tok-a' and verifier.cache.peek('tok-a') is not None)
run(verifier.verify_token('tok-a'))
check('cached token: no Procore call', len(requests) == 0)
verifier.cache.clear()
scenario['slow'] = True


async def five():
    return await asyncio.gather(*(verifier.verify_token('tok-b') for _ in range(5)))


results = run(five())
check('five concurrent cold checks share one /me call', len(requests) == 1 and all(r is not None and r.subject == '9' for r in results))
fresh()
verifier.cache.clear()
scenario['slow'] = True


async def mixed():
    return await asyncio.gather(*(me.get_me() for _ in range(6)), verifier.verify_token('tok-e'))


results = run(mixed())
check('a cold check queues behind the gate with tool calls', inflight['max'] == procore.CONCURRENCY and len(requests) == 7 and results[-1] is not None)
fresh()
scenario['me_status'] = 401
check('401 from /me: token rejected and dropped from cache', run(verifier.verify_token('tok-a')) is None and verifier.cache.peek('tok-a') is None)
fresh()
stale = AccessToken(token='tok-c', client_id='x', scopes=[], subject='9', expires_at=int(time.time()) + 100)
verifier.cache.set('tok-c', stale, -1)
scenario['me_status'] = 429
check('429 from /me with an expired cache entry: keeps serving it', run(verifier.verify_token('tok-c')) is stale and verifier.cache.get('tok-c') is stale)
dead = AccessToken(token='tok-d', client_id='x', scopes=[], subject='9', expires_at=int(time.time()) - 1)
verifier.cache.set('tok-d', dead, -1)
served = True
try:
    run(verifier.verify_token('tok-d'))
except ProcoreUnavailable:
    served = False
check('an entry past its ceiling: dropped and refused, not served', not served and verifier.cache.peek('tok-d') is None)
outcome: object = 'returned'
try:
    run(verifier.verify_token('tok-cold'))
except ProcoreUnavailable as exc:
    outcome = exc.retry_after
check("429 from /me with nothing cached: ProcoreUnavailable carrying Procore's wait", isinstance(outcome, int) and 25 <= outcome <= 31)
check('that 429 set the shared pause, so the cold check sent nothing', len(requests) == 0 and 25 <= procore.pause_left() <= 30)
fresh()


# ------------- LIST_PUNCH_ITEMS -------------
print('-- list_punch_items')
result = run(punch_items.list_punch_items(7, priority='high', search='frame'))
q = query(requests[0])
check('GET /rest/v1.1/punch_items with project_id', requests[0].url.path == '/rest/v1.1/punch_items' and q['project_id'] == ['7'])
check('default status open; priority and search forwarded', q['filters[status]'] == ['open'] and q['filters[priority]'] == ['high'] and q['filters[query]'] == ['frame'])
row = result['items'][0]
check('number is the Procore position', row['number'] == 12)
check('location, trade, people flattened', row['location'] == 'Level 1>Room 101' and row['trade'] == '08 - Doors' and row['assignees'] == ['Sam Sub'] and row['manager'] == 'Pat Manager')
check('description cut to 300 characters', len(row['description']) == 300 and row['description'].endswith('...'))


# ------------- GET_DAILY_LOG -------------
print('-- get_daily_log')
result = run(daily_log.get_daily_log(7, '2026-10-06', '2026-10-07'))
paths = sorted({r.url.path for r in requests})
check('fetches manpower, notes, and weather', paths == ['/rest/v1.0/projects/7/manpower_logs', '/rest/v1.0/projects/7/notes_logs', '/rest/v1.1/projects/7/daily_logs/weather_logs'])
check('date range forwarded as start_date and end_date', all(query(r)['start_date'] == ['2026-10-06'] and query(r)['end_date'] == ['2026-10-07'] for r in requests))
check('totals summed across the range', result['totals'] == {'workers': 10, 'man_hours': 80.0})
check('manpower company falls back to contact', [m['company'] for m in result['manpower']] == ['Acme Electric', 'Self-perform crew'])
check('notes and weather flattened', result['notes'][0]['created_by'] == 'Pat Manager' and result['weather'][0]['weather_delay'] is True and result['weather'][0]['average_temperature'] == '55')
check('single day accepted', run(daily_log.get_daily_log(7, '2026-10-06', '2026-10-06'))['end_date'] == '2026-10-06')
check('bad date format rejected', 'YYYY-MM-DD' in error(daily_log.get_daily_log(7, '10/06/2026', '2026-10-07')))
check('end before start rejected', 'before' in error(daily_log.get_daily_log(7, '2026-10-07', '2026-10-06')))
check('range over 31 days rejected', '31 days' in error(daily_log.get_daily_log(7, '2026-09-01', '2026-10-15')))


# ------------- HTTP ROUTES -------------
print('-- routes')
fresh()
with TestClient(main.app) as client:
    r = client.get('/health')
    check('GET /health -> 200 ok', r.status_code == 200 and r.text == 'ok')

    meta = client.get('/.well-known/oauth-authorization-server').json()
    check('discovery: issuer matches PUBLIC_URL', meta.get('issuer') == config.ISSUER)
    check('discovery: authorize and token endpoints on this host',
          meta.get('authorization_endpoint') == f'{config.PUBLIC_URL}/authorize' and meta.get('token_endpoint') == f'{config.PUBLIC_URL}/token')

    prm = client.get('/.well-known/oauth-protected-resource/mcp').json()
    check('protected resource: names this issuer', any(s.rstrip('/') == config.PUBLIC_URL for s in prm.get('authorization_servers', [])))
    check('protected resource: resource is /mcp', prm.get('resource', '').rstrip('/') == f'{config.PUBLIC_URL}/mcp')

    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}
    accept = {'Accept': 'application/json, text/event-stream'}
    r = client.post('/mcp', json=body, headers=accept)
    check('POST /mcp without token -> 401', r.status_code == 401)
    check('401 carries WWW-Authenticate with resource_metadata', 'resource_metadata' in r.headers.get('www-authenticate', ''))

    scenario['me_status'] = 401
    r = client.post('/mcp', json=body, headers=accept | {'Authorization': 'Bearer tok-bad'})
    check('bad token -> 401 invalid_token', r.status_code == 401 and 'invalid_token' in r.headers.get('www-authenticate', ''))
    scenario['me_status'] = 429
    r = client.post('/mcp', json=body, headers=accept | {'Authorization': 'Bearer tok-cold-2'})
    check('Procore outage during token check -> 503 with Retry-After, not 401', r.status_code == 503 and r.headers.get('retry-after', '').isdigit())
    fresh()  # that 429 set the shared pause; clear it so a good token can be checked

    init = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'smoke', 'version': '0'}}}
    r = client.post('/mcp', json=init, headers=accept | {'Authorization': 'Bearer tok-ok'})
    check('initialize with a good token -> 200 and no session id: stateless', r.status_code == 200 and 'mcp-session-id' not in r.headers and main.mcp.name in r.text)
    r = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}, headers=accept | {'Authorization': 'Bearer tok-ok'})
    check('tools/list without a session id -> 200 with the tools: stateless', r.status_code == 200 and 'list_projects' in r.text)

    r = client.get('/authorize', params={'redirect_uri': 'https://client.example/cb', 'state': 'abc'}, follow_redirects=False)
    location = r.headers.get('location', '')
    check('GET /authorize redirects to Procore login', r.status_code in (302, 307) and location.startswith(f'{config.PROCORE_LOGIN}/oauth/authorize?'))
    check('redirect carries client_id, redirect_uri, state',
          all(s in location for s in (f'client_id={config.CLIENT_ID}', 'redirect_uri=https%3A%2F%2Fclient.example%2Fcb', 'state=abc')))
    r = client.get('/authorize', follow_redirects=False)
    check('GET /authorize without redirect_uri -> 400 invalid_request, not 500', r.status_code == 400 and r.json().get('error') == 'invalid_request')


print(f'\n{len(failed)} failed' if failed else '\nall checks passed')
sys.exit(1 if failed else 0)
