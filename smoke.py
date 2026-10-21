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
from procore_mcp.tools import budget, change_events, change_orders, commitments, daily_log, me, prime_contracts, projects, punch_items, rfis, schedule, submittals, vendors  # noqa: E402
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
        {'id': 1, 'name': 'Active A', 'project_number': '100', 'active': True, 'city': 'Portland', 'state_code': 'ME', 'start_date': '2026-03-01', 'completion_date': '2027-02-28',
         'stage': 'Course of Construction', 'project_stage': {'id': 3, 'name': 'Course of Construction'}, 'extra': 'dropped'},
        {'id': 2, 'name': 'Inactive B', 'project_number': '200', 'active': False, 'city': 'Nashua', 'state_code': 'NH', 'start_date': '2025-01-15', 'completion_date': '2025-12-20',
         'stage': 'Complete', 'project_stage': None, 'extra': 'dropped'},
        {'id': 3, 'name': 'Active C', 'project_number': '300', 'active': True, 'city': 'Boston', 'state_code': 'MA', 'start_date': '2026-06-01', 'completion_date': None,
         'stage': None, 'project_stage': {'id': 2, 'name': 'Pre-Construction'}, 'extra': 'dropped'},
        {'id': 4, 'name': 'Inactive D', 'project_number': '400', 'active': False, 'city': 'Salem', 'state_code': 'NH', 'start_date': '2024-01-15', 'completion_date': '2024-11-30',
         'stage': 'Complete', 'project_stage': None, 'extra': 'dropped'},
    ],
    '/rest/v1.1/vendors': [
        {'id': 5, 'name': 'Acme Electric', 'is_active': True, 'prequalified': True, 'city': 'Portland', 'state_code': 'ME', 'business_phone': '207-555-0100', 'email_address': 'bids@acme.example',
         'trades': [{'id': 1, 'name': 'Electrical', 'active': True}], 'vendor_group': {'id': 1, 'name': 'Subcontractor'}, 'standard_cost_codes': [{'id': 1, 'full_code': '26-0500', 'name': 'Electrical'}],
         'primary_contact': {'id': 70, 'first_name': 'Eve', 'last_name': 'Volt', 'email_address': 'eve@acme.example'}, 'extra': 'dropped'},
        {'id': 6, 'name': 'Glass Co', 'is_active': True, 'prequalified': False, 'city': 'Boston', 'state_code': 'MA', 'business_phone': None, 'email_address': None,
         'trades': [{'id': 2, 'name': 'Glazing'}, {'id': 3, 'name': 'Curtain Wall'}], 'vendor_group': {'id': 1, 'name': 'Subcontractor'}, 'primary_contact': {'id': 72, 'first_name': 'Gus', 'last_name': 'Pane'},
         'standard_cost_codes': [{'id': 2, 'full_code': '08-8000', 'name': 'Glazing'}, {'id': 3, 'full_code': None, 'name': None}]},
        {'id': 7, 'name': 'Idle Plumbing', 'is_active': True, 'prequalified': None, 'city': 'Nashua', 'state_code': 'NH', 'business_phone': '603-555-0101', 'email_address': None,
         'trades': [{'id': 4, 'name': 'Plumbing'}], 'vendor_group': {'id': 1, 'name': 'Subcontractor'}, 'primary_contact': {'id': 71, 'first_name': 'Pipa', 'last_name': None}},
        {'id': 8, 'name': 'Gone Roofing', 'is_active': False, 'prequalified': None, 'city': None, 'state_code': None, 'business_phone': None, 'email_address': None,
         'trades': [{'id': 5, 'name': 'Roofing'}], 'vendor_group': None, 'primary_contact': None},
        {'id': 9, 'name': 'Old Drywall', 'is_active': True, 'prequalified': None, 'city': 'Salem', 'state_code': 'NH', 'business_phone': None, 'email_address': None,
         'trades': [{'id': 6, 'name': 'Drywall'}], 'vendor_group': {'id': 1, 'name': 'Subcontractor'}, 'primary_contact': None},
    ],
    '/rest/v1.1/projects/1/vendors': [
        {'id': 5, 'name': 'Acme Electric', 'is_active': True, 'trades': [{'id': 1, 'name': 'Electrical'}], 'vendor_group': {'id': 1, 'name': 'Subcontractor'}, 'extra': 'dropped'},
        {'id': 6, 'name': 'Glass Co', 'is_active': True, 'trades': [], 'vendor_group': None, 'primary_contact': None},  # the project copy carries none of the company record's classification
        {'id': 8, 'name': 'Gone Roofing', 'is_active': False, 'trades': [{'id': 5, 'name': 'Roofing'}], 'vendor_group': None},
    ],
    '/rest/v1.1/projects/3/vendors': [
        {'id': 5, 'name': 'Acme Electric', 'is_active': True, 'trades': [{'id': 1, 'name': 'Electrical'}], 'vendor_group': {'id': 1, 'name': 'Subcontractor'}},
    ],
    '/rest/v1.1/projects/2/vendors': [
        {'id': 9, 'name': 'Old Drywall', 'is_active': True, 'trades': [], 'vendor_group': None},
    ],
    '/rest/v1.1/projects/4/vendors': [],
    '/rest/v1.0/work_order_contracts': [  # rows carry project_id: the fake serves one project's rows at a time, see answer()
        {'id': 81, 'project_id': 1, 'number': 'SC-001', 'title': 'Glazing', 'status': 'Approved', 'executed': True, 'vendor': {'id': 6, 'name': 'Glass Co'},
         'grand_total': '125000.0', 'approved_change_orders': '2500.0', 'pending_change_orders': '0.0', 'contract_date': '2026-03-15', 'issued_on_date': '2026-03-16',
         'contract_start_date': '2026-04-01', 'contract_estimated_completion_date': '2026-09-30', 'actual_completion_date': None, 'percentage_paid': '40.0',
         'remaining_balance_outstanding': '75000.0', 'extra': 'dropped'},
        {'id': 82, 'project_id': 1, 'number': 'SC-002', 'title': 'Electrical', 'status': 'Draft', 'executed': False, 'vendor': {'id': 5, 'name': 'Acme Electric'}, 'grand_total': '90000.0'},
        {'id': 83, 'project_id': 3, 'number': 'SC-001', 'title': 'Storefront', 'status': 'Out For Signature', 'executed': False, 'vendor': {'id': 6, 'name': 'Glass Co'}, 'grand_total': '40000.0'},
        {'id': 84, 'project_id': 3, 'number': 'SC-002', 'title': 'Flooring', 'status': 'Approved', 'executed': True, 'vendor': {'id': 11, 'name': 'Contract Only Flooring'}, 'grand_total': '30000.0'},
    ],
    '/rest/v1.0/purchase_order_contracts': [
        {'id': 91, 'project_id': 1, 'number': 'PO-001', 'title': 'Lumber package', 'status': 'Received', 'executed': True, 'vendor': {'id': 12, 'company': 'Standale Lumber'},
         'grand_total': '18000.0', 'delivery_date': '2026-05-01', 'extra': 'dropped'},
    ],
    '/rest/v1.1/change_events': [
        {'id': 201, 'project_id': 1, 'number': '003', 'title': 'Rock in footing', 'scope': 'in_scope', 'created_at': '2026-05-02T14:00:00Z', 'updated_at': '2026-05-09T09:00:00Z',
         'status': {'id': 3, 'name': 'Pending', 'mapped_to_status': 'open'}, 'change_type': {'id': 1, 'name': 'Owner Change', 'abbreviation': 'OC'},
         'change_reason': {'id': 2, 'change_reason': 'Unforeseen Conditions'}, 'event_origin': {'origin_id': 11, 'origin_type': 'rfi', 'display_name': 'RFI-004', 'web_page_url': None},
         'created_by': PERSON, 'change_items': [
             {'id': 1, 'budget_code': {'id': 5, 'flat_code': '03-3000', 'description': 'Concrete'}, 'latest_cost_values': {'amount': '4500.0'}, 'latest_revenue_values': {'amount': '5200.0'},
              'cost_impact': {'vendor': {'confirmed': {'id': 13, 'name': 'Rock Crushers'}, 'proposed': None}}},
             {'id': 2, 'budget_code': {'id': 6, 'flat_code': '31-2000', 'description': 'Earthwork'}, 'latest_cost_values': {'amount': '1000.0'}, 'latest_revenue_values': {'amount': None},
              'cost_impact': {'vendor': {'confirmed': None, 'proposed': {'id': 14, 'name': 'Dig Co'}}}},
         ], 'extra': 'dropped'},
        {'id': 202, 'project_id': 1, 'number': '001', 'title': 'Owner adds canopy', 'scope': 'tbd', 'status': {'id': 4, 'name': 'Void', 'mapped_to_status': 'void'},
         'change_type': None, 'change_reason': None, 'event_origin': None, 'created_by': None, 'change_items': []},
        {'id': 203, 'project_id': 3, 'number': '001', 'title': 'Flooring upgrade', 'scope': 'in_scope', 'status': {'id': 3, 'name': 'Pending', 'mapped_to_status': 'open'}, 'change_items': []},
    ],
    '/rest/v1.0/change_order_packages': [
        {'id': 301, 'project_id': 1, 'number': '001', 'title': 'Canopy', 'status': 'approved', 'executed': True, 'revision': 0, 'contract_id': 71, 'contract': {'vendor': {'id': 20, 'name': 'Big Owner LLC'}},
         'grand_total': '5200.0', 'due_date': '2026-06-01', 'reviewed_at': '2026-05-20T10:00:00Z', 'signed_change_order_received_date': '2026-05-22', 'invoiced_date': None, 'paid_date': None,
         'created_at': '2026-05-10T10:00:00Z', 'updated_at': '2026-05-22T10:00:00Z', 'extra': 'dropped'},
        {'id': 302, 'project_id': 1, 'number': '002', 'title': 'Rock excavation', 'status': 'pending', 'executed': False, 'contract_id': 81, 'contract': {'vendor': {'id': 6, 'name': 'Glass Co'}}, 'grand_total': '4500.0'},
        {'id': 303, 'project_id': 3, 'number': '001', 'title': 'Flooring upgrade', 'status': 'approved', 'executed': True, 'contract_id': 84, 'contract': None, 'grand_total': '2000.0'},
    ],
    '/rest/v1.0/prime_contracts': [
        {'id': 71, 'project_id': 1, 'number': 'PC-1', 'title': 'Active A GMP', 'status': 'Approved', 'executed': True, 'grand_total': '2500000.0', 'approved_change_orders': '5200.0', 'retainage_percent': '5.0',
         'vendor': {'id': 20, 'name': 'Big Owner LLC', 'trades': [], 'standard_cost_codes': []}, 'contractor': {'id': 1, 'name': 'Us Construction'}, 'architect': {'id': 30, 'name': 'Ann Architect', 'login': 'ann@example.com'},
         'contract_date': '2026-02-01', 'issued_on_date': '2026-02-02', 'signed_contract_received_date': '2026-02-10', 'contract_start_date': '2026-03-01', 'contract_estimated_completion_date': '2027-02-28',
         'actual_completion_date': None, 'contract_termination_date': None, 'updated_at': '2026-05-22T10:00:00Z', 'extra': 'dropped'},
        {'id': 73, 'project_id': 3, 'number': 'PC-1', 'title': 'Active C', 'status': 'Draft', 'executed': False, 'grand_total': '400000.0', 'vendor': None, 'contractor': None, 'architect': None},
    ],
    '/rest/v1.0/budget_views': [
        {'id': 7, 'project_id': 1, 'name': 'Procore Standard Budget', 'role': None, 'description': '', 'created_at': '2026-01-01T00:00:00Z', 'links': {'detail_rows': 'x', 'summary_rows': 'y'}},
        {'id': 8, 'project_id': 1, 'name': 'Forecasting', 'role': 'forecasting'},
    ],
    '/rest/v1.0/budget_views/7/detail_rows': [
        {'id': 501, 'deletable': False, 'company_id': 1, 'company': 'Us', 'project_id': 1, 'wbs_code_id': 9, 'biller_id': 1, 'root_cost_code_id': 2, 'cost_code_id': 3, 'cost_code_origin_id': None, 'category_id': 4,
         'project': 'Active A', 'biller': 'Active A', 'biller_type': 'Project', 'root_cost_code': '03 - Concrete', 'cost_code': '03-3000 - Concrete', 'category': 'Subcontract',
         'original_budget_amount': '100000.00', 'budget_modifications': '-2000.00', 'budget_forecast': {'id': 1, 'amount': '101500.00', 'automatically_calculated': True}, 'currency_configuration': {'currency_iso_code': 'USD'},
         'unbudgeted_reason': None, 'Revised Budget': '103500.00', 'Committed Costs': '98000.00', 'Projected Over Under': '-2000.00', 'Notes': 'watch rebar'},
        {'id': None, 'deletable': False, 'company_id': 1, 'company': 'Us', 'project_id': 1, 'biller': 'Phase 2', 'biller_type': 'SubJob', 'root_cost_code': '09 - Finishes', 'cost_code': '09-6000 - Flooring', 'category': 'Subcontract',
         'original_budget_amount': '0.00', 'budget_modifications': '0.00', 'budget_forecast': None, 'Revised Budget': '0.00', 'Committed Costs': '30000.00', 'Projected Over Under': '-30000.00', 'unbudgeted_reason': 'Committed cost'},
    ],
    '/rest/v1.0/budget_views/8/detail_rows': [],
    '/rest/v1.0/tasks': [
        {'id': 601, 'project_id': 1, 'name': 'Active A > Foundations', 'task_name': 'Foundations', 'wbs': '1.1', 'start_datetime': '2026-04-01T04:00:00Z', 'finish_datetime': '2026-05-15T04:00:00Z', 'percentage': 100,
         'milestone': False, 'critical_path': True, 'schedule_duration': 32, 'actual_start': '2026-04-01T04:00:00Z', 'actual_finish': '2026-05-20T04:00:00Z', 'baseline_start': '2026-04-01T04:00:00Z',
         'baseline_finish': '2026-05-15T04:00:00Z', 'start_variance': 0, 'finish_variance': 3, 'resource_name': 'Rock Crushers, Dig Co', 'parent_id': 600, 'has_children': False, 'updated_at': '2026-05-20T04:00:00Z', 'extra': 'dropped'},
        {'id': 602, 'project_id': 1, 'name': 'Active A > Dry In', 'task_name': 'Dry In', 'wbs': '1.2', 'start_datetime': '2026-08-01T04:00:00Z', 'finish_datetime': '2026-08-01T04:00:00Z', 'percentage': 0,
         'milestone': True, 'critical_path': True, 'schedule_duration': 0, 'parent_id': 600, 'has_children': False},
        {'id': 603, 'project_id': 1, 'name': 'Active A > Landscaping', 'task_name': 'Landscaping', 'wbs': '1.3', 'start_datetime': '2026-10-01T04:00:00Z', 'finish_datetime': '2026-10-20T04:00:00Z', 'percentage': 0,
         'milestone': False, 'critical_path': False, 'schedule_duration': 14, 'parent_id': 600, 'has_children': False},
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
    if '/projects/403/' in path or request.url.params.get('project_id') == '403':
        return httpx.Response(403, json={'errors': 'You do not have access to this tool'})
    body = FIXTURES.get(path)
    if body is None:
        return httpx.Response(404, json={'error': f'no fixture for {path}'})
    if isinstance(body, list):
        q = request.url.params
        if path == '/rest/v1.0/projects' and q.get('filters[by_status]', 'Active') == 'Active':
            body = [p for p in body if p['active']]  # Procore lists active projects unless asked for All
        if body and 'project_id' in body[0] and q.get('project_id'):
            body = [c for c in body if str(c['project_id']) == q['project_id']]  # Procore lists one project's records at a time
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
check('fourteen tools registered', sorted(t.name for t in tools) == ['get_daily_log', 'get_me', 'list_budget_rows', 'list_change_events', 'list_change_orders', 'list_commitments', 'list_company_vendors', 'list_prime_contracts', 'list_project_vendors', 'list_projects', 'list_punch_items', 'list_rfis', 'list_schedule_tasks', 'list_submittals'])
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
check('rows trimmed to documented keys', all(set(p) == {'id', 'name', 'project_number', 'active', 'city', 'state_code', 'start_date', 'completion_date', 'stage'} for p in result['items']))
check('stage prefers project_stage name, falls back to the stage string', [p['stage'] for p in result['items']] == ['Course of Construction', 'Pre-Construction'])
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
check('active_only=False asks for All and keeps inactive projects', query(requests[0])['filters[by_status]'] == ['All'] and [p['id'] for p in result['items']] == [1, 2, 3, 4])
check('inactive project keeps its completion_date and stage string', result['items'][1]['completion_date'] == '2025-12-20' and result['items'][1]['stage'] == 'Complete')


# ------------- LIST_PROJECT_VENDORS / LIST_COMPANY_VENDORS -------------
print('-- list_project_vendors')
result = run(vendors.list_project_vendors(1, search='acme'))
q = query(requests[0])
check('GET /rest/v1.1/projects/{id}/vendors, extended view, search forwarded', requests[0].url.path == '/rest/v1.1/projects/1/vendors' and q['view'] == ['extended'] and q['filters[search]'] == ['acme'])
check('only that endpoint is read', {r.url.path for r in requests} == {'/rest/v1.1/projects/1/vendors'})
check('envelope reports Procore total', (result['total'], result['returned']) == (3, 3))
row = result['items'][0]
check('row trimmed to documented keys', set(row) == {'id', 'name', 'trades', 'cost_codes', 'vendor_group', 'active', 'prequalified', 'city', 'state_code', 'phone', 'email', 'primary_contact'})
check('trades and vendor group flattened to names', row['trades'] == ['Electrical'] and row['vendor_group'] == 'Subcontractor')
check('project copy returned as Procore sends it: empty trades stay empty', result['items'][1]['trades'] == [] and result['items'][1]['cost_codes'] == [] and result['items'][1]['vendor_group'] is None)
check('null group and absent contact become None', result['items'][2]['vendor_group'] is None and result['items'][2]['primary_contact'] is None)
result = run(vendors.list_project_vendors(1, trade='electric'))
check('trade filter matches trade names case-insensitively, total counts the kept rows', [v['name'] for v in result['items']] == ['Acme Electric'] and result['total'] == 1)

print('-- list_company_vendors')
result = run(vendors.list_company_vendors(limit=10))
q = query(requests[0])
check('GET /rest/v1.1/vendors with company_id, extended view', requests[0].url.path == '/rest/v1.1/vendors' and q['company_id'] == [config.COMPANY_ID] and q['view'] == ['extended'] and 'filters[search]' not in q)
check('walks every page of the directory', result['returned'] == 5 and len(requests) == 3)
row = result['items'][0]
check('contact flattened to a name, phone and email kept', row['primary_contact'] == 'Eve Volt' and row['phone'] == '207-555-0100' and row['email'] == 'bids@acme.example' and row['prequalified'] is True)
check('cost codes rendered as code - name, empty ones dropped', row['cost_codes'] == ['26-0500 - Electrical'] and result['items'][1]['cost_codes'] == ['08-8000 - Glazing'] and result['items'][2]['cost_codes'] == [])
check('contact with only a first name', result['items'][2]['primary_contact'] == 'Pipa')
run(vendors.list_company_vendors(search='glass'))
check('search forwarded', query(requests[0])['filters[search]'] == ['glass'])
result = run(vendors.list_company_vendors(trade='08-80'))
check('trade filter matches cost codes too', [v['name'] for v in result['items']] == ['Glass Co'])


# ------------- LIST_COMMITMENTS -------------
print('-- list_commitments')
result = run(commitments.list_commitments([1]))
check('reads both commitment endpoints with project_id and extended view', {r.url.path for r in requests} == {'/rest/v1.0/purchase_order_contracts', '/rest/v1.0/work_order_contracts'} and all(query(r)['project_id'] == ['1'] and query(r)['view'] == ['extended'] for r in requests))
check('rows merged, sorted by vendor, total combined, no errors', [r['vendor'] for r in result['items']] == ['Acme Electric', 'Glass Co', 'Standale Lumber'] and result['total'] == 3 and result['errors'] == [])
row = result['items'][1]
check('subcontract row trimmed and typed', row['type'] == 'subcontract' and row['project_id'] == 1 and row['title'] == 'Glazing' and row['executed'] is True and row['grand_total'] == 125000.0 and row['percent_paid'] == 40.0 and row['estimated_completion_date'] == '2026-09-30' and 'extra' not in row)
check('purchase order vendor read from company, delivery date kept', result['items'][2]['type'] == 'purchase_order' and result['items'][2]['vendor_id'] == 12 and result['items'][2]['delivery_date'] == '2026-05-01')
check('draft contract has no amounts beyond its total', result['items'][0]['status'] == 'Draft' and result['items'][0]['grand_total'] == 90000.0 and result['items'][0]['percent_paid'] is None)
result = run(commitments.list_commitments([1, 3, 403]))
check('a batch reads every project', sorted(query(r)['project_id'][0] for r in requests if r.url.path.endswith('work_order_contracts') and query(r)['page'] == ['1']) == ['1', '3', '403'])
check('items tagged with their project, sorted by vendor then project', [(r['vendor'], r['project_id']) for r in result['items']] == [('Acme Electric', 1), ('Contract Only Flooring', 3), ('Glass Co', 1), ('Glass Co', 3), ('Standale Lumber', 1)] and result['total'] == 5)
check('a project the user cannot read is listed under errors, the others still return', [e['project_id'] for e in result['errors']] == [403] and '403' in result['errors'][0]['error'])
run(commitments.list_commitments([1], kind='subcontract', status='Approved'))
check('kind limits to one endpoint, status forwarded', {r.url.path for r in requests} == {'/rest/v1.0/work_order_contracts'} and query(requests[0])['filters[status]'] == ['Approved'])
check('empty project_ids rejected', 'at least one' in error(commitments.list_commitments([])))
check('too many project_ids rejected, naming the cap', 'at most 25' in error(commitments.list_commitments(list(range(26)))))


# ------------- LIST_CHANGE_EVENTS -------------
print('-- list_change_events')
result = run(change_events.list_change_events([1, 3, 403], created_at='2026-01-01...2026-12-31'))
check('GET /rest/v1.1/change_events per project, date range forwarded', {r.url.path for r in requests} == {'/rest/v1.1/change_events'} and sorted(query(r)['project_id'][0] for r in requests if query(r)['page'] == ['1']) == ['1', '3', '403'] and query(requests[0])['filters[created_at]'] == ['2026-01-01...2026-12-31'])
check('events sorted by project then number, 403 under errors', [(e['project_id'], e['number']) for e in result['items']] == [(1, '001'), (1, '003'), (3, '001')] and result['total'] == 3 and [e['project_id'] for e in result['errors']] == [403])
row = result['items'][1]
check('status name and group, type, reason, origin flattened', (row['status'], row['status_group'], row['change_type'], row['change_reason'], row['origin'], row['origin_type'], row['created_by']) == ('Pending', 'open', 'Owner Change', 'Unforeseen Conditions', 'RFI-004', 'rfi', 'Pat Manager'))
check('line items summed: cost and revenue totals, vendors confirmed or proposed, budget codes', row['line_items'] == 2 and row['cost_total'] == 5500.0 and row['revenue_total'] == 5200.0 and row['vendors'] == ['Rock Crushers', 'Dig Co'] and row['budget_codes'] == ['03-3000 - Concrete', '31-2000 - Earthwork'] and 'extra' not in row)
check('event with no line items has None totals and empty lists; null nested objects survive', result['items'][0]['cost_total'] is None and result['items'][0]['vendors'] == [] and result['items'][0]['change_type'] is None and result['items'][0]['origin'] is None)
run(change_events.list_change_events([1]))
check('no date filters sent unless asked', 'filters[created_at]' not in query(requests[0]) and 'filters[updated_at]' not in query(requests[0]))


# ------------- LIST_CHANGE_ORDERS -------------
print('-- list_change_orders')
result = run(change_orders.list_change_orders([1, 3]))
check('GET /rest/v1.0/change_order_packages per project', {r.url.path for r in requests} == {'/rest/v1.0/change_order_packages'} and sorted(query(r)['project_id'][0] for r in requests if query(r)['page'] == ['1']) == ['1', '3'])
check('packages from both projects, Procore total kept, no errors', [(c['project_id'], c['number']) for c in result['items']] == [(1, '001'), (1, '002'), (3, '001')] and result['total'] == 3 and result['errors'] == [])
row = result['items'][0]
check('row trimmed: contract vendor flattened, amount a number, dates kept', row['vendor'] == 'Big Owner LLC' and row['vendor_id'] == 20 and row['contract_id'] == 71 and row['grand_total'] == 5200.0 and row['signed_date'] == '2026-05-22' and row['reviewed_at'] == '2026-05-20T10:00:00Z' and row['executed'] is True and 'extra' not in row)
check('package with a null contract still returns', result['items'][2]['vendor'] is None and result['items'][2]['vendor_id'] is None)
result = run(change_orders.list_change_orders([1, 3], status='approved'))
check('status filter applied after the read, total counts the kept rows', [c['id'] for c in result['items']] == [301, 303] and result['total'] == 2)
run(change_orders.list_change_orders([1], contract_id=81))
check('contract_id forwarded', query(requests[0])['contract_id'] == ['81'])


# ------------- LIST_PRIME_CONTRACTS -------------
print('-- list_prime_contracts')
result = run(prime_contracts.list_prime_contracts([1, 3, 403]))
check('GET /rest/v1.0/prime_contracts per project, 403 under errors', {r.url.path for r in requests} == {'/rest/v1.0/prime_contracts'} and [e['project_id'] for e in result['errors']] == [403] and result['total'] == 2)
row = result['items'][0]
check('parties flattened to names, amounts numbers, dates renamed', (row['owner'], row['owner_id'], row['contractor'], row['architect']) == ('Big Owner LLC', 20, 'Us Construction', 'Ann Architect') and row['grand_total'] == 2500000.0 and row['approved_change_orders'] == 5200.0 and row['retainage_percent'] == 5.0 and row['signed_date'] == '2026-02-10' and row['estimated_completion_date'] == '2027-02-28' and 'extra' not in row)
check('draft contract with no parties still returns', result['items'][1]['project_id'] == 3 and result['items'][1]['status'] == 'Draft' and result['items'][1]['owner'] is None and result['items'][1]['approved_change_orders'] is None)


# ------------- LIST_BUDGET_ROWS -------------
print('-- list_budget_rows')
result = run(budget.list_budget_rows(1))
check("reads the views, then the first view's detail rows with budget_row_type=all", list(dict.fromkeys(r.url.path for r in requests)) == ['/rest/v1.0/budget_views', '/rest/v1.0/budget_views/7/detail_rows'] and query(requests[-1])['project_id'] == ['1'] and query(requests[-1])['budget_row_type'] == ['all'])
check('view read and the alternatives named', result['view'] == {'id': 7, 'name': 'Procore Standard Budget', 'role': None} and [v['id'] for v in result['views']] == [7, 8] and result['returned'] == 2)
row = result['items'][0]
check('fixed fields trimmed, amounts numbers', (row['cost_code'], row['category'], row['sub_job'], row['budgeted']) == ('03-3000 - Concrete', 'Subcontract', None, True) and row['original_budget'] == 100000.0 and row['budget_modifications'] == -2000.0 and row['forecast'] == 101500.0)
check('view columns passed through by name, numeric where they parse', row['columns'] == {'Revised Budget': 103500.0, 'Committed Costs': 98000.0, 'Projected Over Under': -2000.0, 'Notes': 'watch rebar'})
check('unbudgeted sub job line flagged', result['items'][1]['budgeted'] is False and result['items'][1]['sub_job'] == 'Phase 2' and result['items'][1]['forecast'] is None and result['items'][1]['columns']['Committed Costs'] == 30000.0)
run(budget.list_budget_rows(1, view_id=8))
check('view_id picks another view', requests[-1].url.path == '/rest/v1.0/budget_views/8/detail_rows')
check('unknown view_id rejected, naming the views', '7 (Procore Standard Budget)' in error(budget.list_budget_rows(1, view_id=99)))
check('project without budget views rejected', 'no budget views' in error(budget.list_budget_rows(2)))


# ------------- LIST_SCHEDULE_TASKS -------------
print('-- list_schedule_tasks')
result = run(schedule.list_schedule_tasks(1, search='dry', updated_at='2026-05-01...2026-05-31'))
q = query(requests[0])
check('GET /rest/v1.0/tasks with project_id, normal view, query and updated_at forwarded', requests[0].url.path == '/rest/v1.0/tasks' and q['project_id'] == ['1'] and q['view'] == ['normal'] and q['filters[query]'] == ['dry'] and q['filters[updated_at]'] == ['2026-05-01...2026-05-31'])
check('envelope reports Procore total', (result['total'], result['returned']) == (3, 3))
row = result['items'][0]
check('row trimmed and renamed', (row['name'], row['wbs'], row['start'], row['finish'], row['percent_complete'], row['critical'], row['duration_days'], row['finish_variance'], row['resources']) == ('Foundations', '1.1', '2026-04-01T04:00:00Z', '2026-05-15T04:00:00Z', 100, True, 32, 3, 'Rock Crushers, Dig Co') and 'extra' not in row)
result = run(schedule.list_schedule_tasks(1, milestones_only=True, limit=1))
check('milestones_only reads the whole schedule before cutting to limit, total counts the kept rows', len(requests) == 2 and [t['name'] for t in result['items']] == ['Dry In'] and result['total'] == 1)
result = run(schedule.list_schedule_tasks(1, critical_only=True))
check('critical_only keeps the critical path', [t['name'] for t in result['items']] == ['Foundations', 'Dry In'])


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
