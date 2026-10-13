"""
Offline smoke checks. No running server, no Procore sign-in.

    uv run smoke.py

Loads .env when present, otherwise uses placeholders for the four required variables.
Checks the OAuth routes, the auth gate on /mcp, the metadata every tool must carry, the
identity sent in the MCP handshake, and every tool against a fake Procore that answers
from fixtures: request paths and query parameters, pagination, filtering, trimming, and
how Procore errors reach the model. Exits 1 if any check fails. Checks with a real token
are a job for the MCP Inspector or your MCP client.
"""

import asyncio
import base64
import logging
import os
import sys
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
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from procore_mcp import config, procore  # noqa: E402
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


def fake_procore(request: httpx.Request) -> httpx.Response:
    requests.append(request)
    path = request.url.path
    if '/projects/403/' in path:
        return httpx.Response(403, json={'errors': 'You do not have access to this tool'})
    body = FIXTURES.get(path)
    if body is None:
        return httpx.Response(404, json={'error': f'no fixture for {path}'})
    if isinstance(body, list):
        page, per_page = int(request.url.params.get('page', 1)), int(request.url.params.get('per_page', 100))
        return httpx.Response(200, json=body[(page - 1) * per_page:page * per_page], headers={'Total': str(len(body)), 'Per-Page': str(per_page)})
    return httpx.Response(200, json=body)


procore.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake_procore), base_url=config.PROCORE_API)
procore.get_access_token = lambda: SimpleNamespace(token='smoke-token')
procore.PAGE_SIZE = 2  # small pages so the fixtures exercise pagination


def run(coro):
    """Run a tool call against the fake, clearing the request log first."""
    requests.clear()
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


# ------------- LIST_PROJECTS -------------
print('-- list_projects')
result = run(projects.list_projects())
check('walks pages until a short page (3 rows, 2 per page: 2 requests)', [query(r)['page'] for r in requests] == [['1'], ['2']] and all(r.url.path == '/rest/v1.0/projects' for r in requests))
check('active_only=True keeps only active projects', [p['id'] for p in result['items']] == [1, 3])
check('envelope reports total and returned', (result['total'], result['returned']) == (2, 2))
check('rows trimmed to documented keys', all(set(p) == {'id', 'name', 'project_number', 'active', 'city', 'state_code'} for p in result['items']))
result = run(projects.list_projects(active_only=False))
check('active_only=False keeps all projects', [p['id'] for p in result['items']] == [1, 2, 3])


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

    r = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                    headers={'Accept': 'application/json, text/event-stream'})
    check('POST /mcp without token -> 401', r.status_code == 401)
    check('401 carries WWW-Authenticate with resource_metadata', 'resource_metadata' in r.headers.get('www-authenticate', ''))

    r = client.get('/authorize', params={'redirect_uri': 'https://client.example/cb', 'state': 'abc'}, follow_redirects=False)
    location = r.headers.get('location', '')
    check('GET /authorize redirects to Procore login', r.status_code in (302, 307) and location.startswith(f'{config.PROCORE_LOGIN}/oauth/authorize?'))
    check('redirect carries client_id, redirect_uri, state',
          all(s in location for s in (f'client_id={config.CLIENT_ID}', 'redirect_uri=https%3A%2F%2Fclient.example%2Fcb', 'state=abc')))


print(f'\n{len(failed)} failed' if failed else '\nall checks passed')
sys.exit(1 if failed else 0)
