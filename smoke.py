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
from procore_mcp.tools import bidding, budget, change_events, change_orders, commitments, daily_log, incidents, inspections, invoices, me, prime_contracts, project_users, projects, punch_items, rfis, schedule, submittals, vendors  # noqa: E402
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
    '/rest/v1.1/projects/1/bid_packages': [
        {'id': 41, 'project_id': 1, 'number': 1, 'title': '09B Flooring', 'bid_due_date': '2026-02-15', 'status': 'closed', 'invited_bids_count': 3, 'bids_will_bid_count': 2, 'submitted_bids_count': 1, 'extra': 'dropped'},
        {'id': 42, 'project_id': 1, 'number': 2, 'title': '26 Electrical', 'bid_due_date': '2026-03-01', 'submitted_bids_count': 0},
    ],
    '/rest/v1.1/projects/2/bid_packages': [],
    '/rest/v1.1/projects/3/bid_packages': [
        {'id': 43, 'project_id': 3, 'number': 1, 'title': '08 Glazing', 'bid_due_date': '2026-06-01', 'submitted_bids_count': 2},
    ],
    '/rest/v1.1/projects/1/bid_packages/41/bids': [
        {'id': 411, 'bid_package_id': 41, 'bid_package_title': '09B Flooring', 'bid_status': 'submitted', 'submitted': True, 'awarded': True, 'is_bidder_committed': True, 'lump_sum_amount': 30000.0,
         'vendor': {'id': 11, 'name': 'Contract Only Flooring', 'avatar_url': None, 'trades': 'Flooring'}, 'invitation_last_sent_at': '2026-01-20T15:00:00Z', 'due_date': '2026-02-15', 'bidder_comments': 'x' * 400,
         'updated_at': '2026-02-14T15:00:00Z', 'bid_requester': {'company': 'Us'}, 'project': {'name': 'Active A'}, 'extra': 'dropped'},
        {'id': 412, 'bid_package_id': 41, 'bid_package_title': '09B Flooring', 'bid_status': 'will_not_bid', 'submitted': False, 'awarded': False, 'vendor': {'id': 8, 'name': 'Complete Flooring'}, 'lump_sum_amount': None},
        {'id': 413, 'bid_package_id': 41, 'bid_package_title': '09B Flooring', 'bid_status': 'undecided', 'submitted': False, 'awarded': False, 'vendor': None},
    ],
    '/rest/v1.1/projects/1/bid_packages/42/bids': [
        {'id': 421, 'bid_package_id': 42, 'bid_package_title': '26 Electrical', 'bid_status': 'will_bid', 'submitted': False, 'awarded': False, 'vendor': {'id': 5, 'name': 'Acme Electric'}},
    ],
    '/rest/v1.1/requisitions': [
        {'id': 701, 'project_id': 1, 'number': 3, 'invoice_number': 'INV-2026-07', 'status': 'approved', 'final': False, 'commitment_id': 81, 'commitment_type': 'WorkOrderContract', 'contract_name': 'Contract SC-001',
         'vendor_name': 'Glass Co', 'vendor_id': 6, 'billing_date': '2026-07-31', 'requisition_start': '2026-07-01', 'requisition_end': '2026-07-31', 'submitted_at': '2026-08-02T12:00:00Z', 'payment_date': '2026-08-30',
         'percent_complete': '40.0', 'total_claimed_amount': '20000.0',
         'summary': {'contract_sum_to_date': '127500.0', 'total_completed_and_stored_to_date': '51000.0', 'total_retainage': '2550.0', 'total_earned_less_retainage': '48450.0', 'less_previous_certificates_for_payment': '28500.0',
                     'current_payment_due': '19950.0', 'balance_to_finish_including_retainage': '79050.0', 'formatted_period': 'July 2026'}, 'extra': 'dropped'},
        {'id': 702, 'project_id': 1, 'number': 1, 'invoice_number': 'PO-1', 'status': 'draft', 'commitment_id': 91, 'commitment_type': 'PurchaseOrderContract', 'contract_name': 'PO-001', 'vendor_name': 'Standale Lumber', 'summary': None},
        {'id': 703, 'project_id': 3, 'number': 1, 'invoice_number': 'A', 'status': 'under_review', 'commitment_id': 84, 'commitment_type': 'WorkOrderContract', 'vendor_name': 'Contract Only Flooring', 'vendor_id': 11},
    ],
    '/rest/v1.0/payment_applications': [
        {'id': 801, 'project_id': 1, 'number': 2, 'invoice_number': 'OI-002', 'status': 'approved', 'formatted_contract_company': 'Big Owner LLC', 'contract': {'id': 71, 'type': 'prime_contract', 'title': 'Active A GMP'},
         'billing_date': '2026-07-31', 'period_start': '2026-07-01', 'period_end': '2026-07-31', 'percent_complete': '35.0', 'total_amount_accrued_this_period': '210000.0', 'total_amount_paid': '650000.0',
         'g702': {'contract_sum_to_date': '2505200.0', 'total_completed_and_stored_to_date': '876820.0', 'total_retainage': '43841.0', 'total_earned_less_retainage': '832979.0', 'less_previous_certificates_for_payment': '650000.0',
                  'current_payment_due': '182979.0', 'balance_to_finish_including_retainage': '1672221.0'}, 'updated_at': '2026-08-05T10:00:00Z', 'extra': 'dropped'},
        {'id': 802, 'project_id': 1, 'number': 3, 'invoice_number': 'OI-003', 'status': 'draft', 'formatted_contract_company': 'Big Owner LLC', 'contract': {}, 'g702': {}},
    ],
    '/rest/v1.0/work_order_contracts/81/line_items': [
        {'id': 811, 'position': 1, 'description': 'Storefront glazing', 'cost_code': {'id': 3, 'full_code': '08-4000', 'name': 'Entrances and Storefronts', 'code': '4000'}, 'line_item_type': {'id': 2, 'name': 'Subcontract', 'base_type': 'commitment'},
         'wbs_code': {'id': 9, 'flat_code': '08-4000.S', 'description': 'Entrances and Storefronts - Subcontract'}, 'quantity': '1.0', 'uom': 'ls', 'unit_cost': '100000.0', 'amount': '100000.0', 'extended_amount': '100000.0', 'total_amount': '100000.0',
         'change_event_line_item': None, 'extra': 'dropped'},
        {'id': 812, 'position': 2, 'description': 'Rock excavation CE', 'cost_code': {'id': 5, 'full_code': '03-3000', 'name': 'Concrete'}, 'line_item_type': None, 'wbs_code': None, 'quantity': '1.0', 'uom': 'ls', 'unit_cost': '25000.0', 'amount': '25000.0',
         'change_event_line_item': {'id': 1, 'event_id': 201, 'cost_rom': '4500.0'}},
    ],
    '/rest/v1.0/purchase_order_contracts/91/line_items': [
        {'id': 911, 'position': 1, 'description': '2x6 framing', 'cost_code': {'id': 7, 'full_code': '06-1000', 'name': 'Rough Carpentry'}, 'line_item_type': {'id': 3, 'name': 'Materials'}, 'quantity': '1200.0', 'uom': 'ea', 'unit_cost': '15.0', 'amount': '18000.0'},
    ],
    '/rest/v1.0/checklist/lists': [  # groups by template, each holding its inspections; project_id on the group keeps the fake per project
        {'name': 'Concrete Pre-Pour', 'template_id': 31, 'project_id': 1, 'response_set': {'conforming_response': 'Yes', 'deficient_response': 'No'}, 'lists': [
            {'id': 501, 'number': 2, 'name': 'Pre-Pour Footings', 'list_template_id': 31, 'list_template_name': 'Concrete Pre-Pour', 'status': 'In Review', 'description': 'y' * 400, 'due_at': '2026-03-02T17:00:00Z',
             'inspection_date': '2026-03-01', 'closed_at': None, 'closed_by': None, 'trade': {'id': 2, 'name': 'Concrete', 'active': True}, 'location': {'id': 4, 'name': 'Footings', 'node_name': 'Footings'},
             'specification_section': {'id': 6, 'section': '03 30 00', 'description': 'Cast-in-Place Concrete'}, 'responsible_contractor': {'id': 12, 'name': 'Ready Mix Co'}, 'responsible_party': {'id': 9, 'login': 'pat@example.com', 'name': 'Pat Manager'},
             'inspectors': [{'id': 9, 'name': 'Pat Manager'}, {'id': 10, 'name': 'Sam Super'}], 'item_count': 10, 'conforming_item_count': 7, 'deficient_item_count': 2, 'na_item_count': 1, 'neutral_item_count': 0, 'not_inspected_item_count': 0,
             'created_by': {'id': 9, 'login': 'pat@example.com', 'name': 'Pat Manager'}, 'created_at': '2026-02-20T10:00:00Z', 'updated_at': '2026-03-01T16:00:00Z', 'attachments': [], 'extra': 'dropped'},
            {'id': 502, 'number': 1, 'name': 'Pre-Pour Slab', 'list_template_id': 31, 'status': 'Closed', 'closed_at': '2026-02-10T12:00:00Z', 'closed_by': {'id': 10, 'name': 'Sam Super'}, 'item_count': 10, 'conforming_item_count': 10, 'deficient_item_count': 0},
        ]},
        {'name': 'Safety Walk', 'template_id': 32, 'project_id': 1, 'lists': [
            {'id': 503, 'number': 3, 'name': 'Weekly Safety Walk', 'list_template_id': 32, 'list_template_name': 'Safety Walk', 'status': 'Open', 'trade': None, 'location': None, 'specification_section': None, 'responsible_contractor': None, 'inspectors': []},
        ]},
        {'name': 'Safety Walk', 'template_id': 32, 'project_id': 3, 'lists': [
            {'id': 504, 'number': 1, 'name': 'Weekly Safety Walk', 'list_template_id': 32, 'list_template_name': 'Safety Walk', 'status': 'Open'},
        ]},
    ],
    '/rest/v1.0/projects/1/incidents': [
        {'id': 601, 'number': 2, 'title': 'Ladder fall', 'status': 'open', 'event_date': '2026-04-02T14:30:00Z', 'time_unknown': False, 'recordable': True, 'private': False, 'description': 'z' * 400,
         'location': {'id': 4, 'name': 'Level 2', 'node_name': 'Level 2'}, 'hazard': {'id': 1, 'name': 'Fall', 'active': True}, 'contributing_condition': {'id': 2, 'name': 'Improper ladder setup'}, 'contributing_behavior': {'id': 3, 'name': 'Rushing'},
         'records_count': 1, 'actions_count': 2, 'witness_statements_count': 1, 'open_observations_count': 1, 'closed_observations_count': 0, 'attachments_count': 3,
         'created_by': {'id': 9, 'login': 'pat@example.com', 'name': 'Pat Manager'}, 'created_at': '2026-04-02T16:00:00Z', 'updated_at': '2026-04-03T09:00:00Z', 'custom_fields': {}, 'extra': 'dropped'},
        {'id': 602, 'number': 1, 'title': 'Near miss: dropped tool', 'status': 'closed', 'event_date': '2026-03-15T09:00:00Z', 'recordable': False, 'location': None, 'hazard': None, 'contributing_condition': None, 'contributing_behavior': None, 'records_count': 1},
    ],
    '/rest/v1.0/projects/3/incidents': [
        {'id': 603, 'number': 1, 'title': 'Property damage: fence', 'status': 'open', 'event_date': '2026-05-01T11:00:00Z', 'recordable': False, 'records_count': 1},
    ],
    '/rest/v1.0/projects/1/users': [
        {'id': 9, 'name': 'Pat Manager', 'first_name': 'Pat', 'last_name': 'Manager', 'email_address': 'pat@example.com', 'job_title': 'Project Manager', 'is_employee': True, 'is_active': True, 'business_phone': '555-0100', 'mobile_phone': '555-0101',
         'vendor': {'id': 1, 'name': 'Us Construction'}, 'permission_template': {'id': 3, 'name': 'Project Admin', 'type': 'global'}, 'last_login_at': '2026-10-20T13:00:00Z', 'address': '1 Main St', 'extra': 'dropped'},
        {'id': 10, 'name': 'Sam Super', 'first_name': 'Sam', 'last_name': 'Super', 'email_address': 'sam@example.com', 'job_title': 'Superintendent', 'is_employee': True, 'vendor': {'id': 1, 'name': 'Us Construction'}, 'permission_template': None},
        {'id': 20, 'name': 'Chris Sub', 'first_name': 'Chris', 'last_name': 'Sub', 'initials': 'CS', 'vendor': {'id': 6, 'name': 'Glass Co'}},  # the compact view: no contact fields
    ],
    '/rest/v1.0/projects/3/users': [
        {'id': 9, 'name': 'Pat Manager', 'email_address': 'pat@example.com', 'job_title': 'Project Manager', 'is_employee': True, 'vendor': {'id': 1, 'name': 'Us Construction'}},
    ],
    '/rest/v1.0/project_roles': [  # one row per user per role; project_id keeps the fake per project
        {'id': 1, 'project_id': 1, 'name': 'Pat Manager', 'role': 'Project Manager', 'user_id': 9, 'contact_id': 90, 'is_active': True, 'created_at': '2026-01-01T00:00:00Z', 'extra': 'dropped'},
        {'id': 2, 'project_id': 1, 'name': 'Pat Manager', 'role': 'Project Executive', 'user_id': 9, 'is_active': True},
        {'id': 3, 'project_id': 1, 'name': 'Sam Super', 'role': 'Superintendent', 'user_id': 10, 'is_active': True},
        {'id': 4, 'project_id': 1, 'name': 'Sam Super', 'role': 'Project Manager', 'user_id': 10, 'is_active': False},
        {'id': 5, 'project_id': 3, 'name': 'Pat Manager', 'role': 'Project Manager', 'user_id': 9, 'is_active': True},
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
    if '/projects/403/' in path or request.url.params.get('project_id') == '403' or scenario.get('deny') == path:
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
check('twenty-two tools registered', sorted(t.name for t in tools) == ['get_daily_log', 'get_me', 'list_bid_packages', 'list_bids', 'list_budget_rows', 'list_change_events', 'list_change_orders', 'list_commitment_line_items', 'list_commitments', 'list_company_vendors', 'list_incidents', 'list_inspections', 'list_owner_invoices', 'list_prime_contracts', 'list_project_users', 'list_project_vendors', 'list_projects', 'list_punch_items', 'list_rfis', 'list_schedule_tasks', 'list_subcontractor_invoices', 'list_submittals'])
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

print('-- list_commitment_line_items')
result = run(commitments.list_commitment_line_items(1, [81, 403], kind='subcontract'))
check('GET /rest/v1.0/work_order_contracts/{id}/line_items with project_id, per commitment', {r.url.path for r in requests} == {'/rest/v1.0/work_order_contracts/81/line_items', '/rest/v1.0/work_order_contracts/403/line_items'} and query(requests[0])['project_id'] == ['1'])
check('a commitment Procore refuses is listed under errors by commitment_id, the other still returns', [e['commitment_id'] for e in result['errors']] == [403] and 'Procore returned' in result['errors'][0]['error'] and result['total'] == 2)
row = result['items'][0]
check('line trimmed: cost code as code - name, type and wbs flattened, amounts numbers', (row['commitment_id'], row['position'], row['cost_code'], row['cost_code_id'], row['line_item_type'], row['wbs_code'], row['uom']) == (81, 1, '08-4000 - Entrances and Storefronts', 3, 'Subcontract', '08-4000.S', 'ls') and row['amount'] == 100000.0 and row['unit_cost'] == 100000.0 and row['change_event_id'] is None and 'extra' not in row)
check('line from a change event carries its event id; null type and wbs survive', result['items'][1]['change_event_id'] == 201 and result['items'][1]['line_item_type'] is None and result['items'][1]['wbs_code'] is None)
result = run(commitments.list_commitment_line_items(1, [91], kind='purchase_order'))
check('purchase_order kind reads the purchase order endpoint', requests[0].url.path == '/rest/v1.0/purchase_order_contracts/91/line_items' and result['items'][0]['cost_code'] == '06-1000 - Rough Carpentry' and result['items'][0]['quantity'] == 1200.0)
check('empty commitment_ids rejected by name', 'commitment_ids must name at least one' in error(commitments.list_commitment_line_items(1, [], kind='subcontract')))


# ------------- LIST_BID_PACKAGES / LIST_BIDS -------------
print('-- list_bid_packages')
result = run(bidding.list_bid_packages([1, 3, 403]))
check('GET /rest/v1.1/projects/{id}/bid_packages with view=all per project, 403 under errors', {r.url.path for r in requests} == {'/rest/v1.1/projects/1/bid_packages', '/rest/v1.1/projects/3/bid_packages', '/rest/v1.1/projects/403/bid_packages'} and all(query(r)['view'] == ['all'] and 'filter' not in query(r) for r in requests) and [e['project_id'] for e in result['errors']] == [403])
check('packages sorted by project then number, total combined', [(p['project_id'], p['number'], p['title']) for p in result['items']] == [(1, 1, '09B Flooring'), (1, 2, '26 Electrical'), (3, 1, '08 Glazing')] and result['total'] == 3)
row = result['items'][0]
check('row trimmed, counts kept when present', row['status'] == 'closed' and row['bid_due_date'] == '2026-02-15' and (row['invited_bids_count'], row['bids_will_bid_count'], row['submitted_bids_count']) == (3, 2, 1) and 'extra' not in row)
check('counts None when Procore omits them', result['items'][1]['invited_bids_count'] is None and result['items'][1]['submitted_bids_count'] == 0)
run(bidding.list_bid_packages([1], filter='open'))
check('filter forwarded', query(requests[0])['filter'] == ['open'])

print('-- list_bids')
result = run(bidding.list_bids(1))
check('reads the project\'s packages, then every package\'s bids', [r.url.path for r in requests if query(r)['page'] == ['1']] == ['/rest/v1.1/projects/1/bid_packages', '/rest/v1.1/projects/1/bid_packages/41/bids', '/rest/v1.1/projects/1/bid_packages/42/bids'])
check('bids from every package, sorted by package then vendor, total combined', [(b['bid_package_id'], b['vendor']) for b in result['items']] == [(41, None), (41, 'Complete Flooring'), (41, 'Contract Only Flooring'), (42, 'Acme Electric')] and result['total'] == 4 and result['errors'] == [])
row = result['items'][2]
check('bid trimmed: vendor flattened, flags booleans, amount a number, comments cut', (row['project_id'], row['vendor_id'], row['vendor_trades'], row['bid_status'], row['submitted'], row['awarded'], row['committed']) == (1, 11, 'Flooring', 'submitted', True, True, True) and row['lump_sum_amount'] == 30000.0 and len(row['bidder_comments']) == 300 and 'extra' not in row)
check('bid with no vendor and no amount still returns', result['items'][0]['vendor_id'] is None and result['items'][0]['bid_status'] == 'undecided' and result['items'][0]['committed'] is False and result['items'][1]['lump_sum_amount'] is None)
result = run(bidding.list_bids(1, bid_package_id=42))
check('bid_package_id reads that package only', [r.url.path for r in requests] == ['/rest/v1.1/projects/1/bid_packages/42/bids'] and [b['vendor'] for b in result['items']] == ['Acme Electric'])
result = run(bidding.list_bids(2))
check('project with no packages returns an empty listing', result == {'total': 0, 'returned': 0, 'items': [], 'errors': []})


# ------------- LIST_SUBCONTRACTOR_INVOICES / LIST_OWNER_INVOICES -------------
print('-- list_subcontractor_invoices')
result = run(invoices.list_subcontractor_invoices([1, 3, 403]))
check('GET /rest/v1.1/requisitions per project, no filters unless asked, 403 under errors', {r.url.path for r in requests} == {'/rest/v1.1/requisitions'} and sorted(query(r)['project_id'][0] for r in requests if query(r)['page'] == ['1']) == ['1', '3', '403'] and 'filters[status]' not in query(requests[0]) and [e['project_id'] for e in result['errors']] == [403])
check('invoices sorted by vendor then project then number, total combined', [(i['vendor'], i['project_id'], i['number']) for i in result['items']] == [('Contract Only Flooring', 3, 1), ('Glass Co', 1, 3), ('Standale Lumber', 1, 1)] and result['total'] == 3)
row = result['items'][1]
check('row trimmed: commitment type mapped, periods renamed, G702 flattened to numbers', (row['commitment_id'], row['commitment_type'], row['contract'], row['vendor_id'], row['period_start'], row['period_end'], row['payment_date']) == (81, 'subcontract', 'Contract SC-001', 6, '2026-07-01', '2026-07-31', '2026-08-30') and row['percent_complete'] == 40.0 and row['total_claimed_amount'] == 20000.0 and row['contract_sum_to_date'] == 127500.0 and row['current_payment_due'] == 19950.0 and row['balance_to_finish_including_retainage'] == 79050.0 and 'formatted_period' not in row and 'extra' not in row)
check('purchase order invoice typed, null summary gives None amounts', result['items'][2]['commitment_type'] == 'purchase_order' and result['items'][2]['status'] == 'draft' and result['items'][2]['current_payment_due'] is None and result['items'][2]['total_retainage'] is None)
run(invoices.list_subcontractor_invoices([1], status='approved', commitment_id=81))
q = query(requests[0])
check('status and commitment_id forwarded as filters', q['filters[status]'] == ['approved'] and q['filters[commitment_id]'] == ['81'])

print('-- list_owner_invoices')
result = run(invoices.list_owner_invoices([1, 403]))
check('GET /rest/v1.0/payment_applications per project, 403 under errors', {r.url.path for r in requests} == {'/rest/v1.0/payment_applications'} and [e['project_id'] for e in result['errors']] == [403] and result['total'] == 2)
row = result['items'][0]
check('row trimmed: owner and contract flattened, gross and paid numbers, G702 flattened', (row['number'], row['invoice_number'], row['status'], row['owner'], row['contract_id'], row['contract'], row['period_end']) == (2, 'OI-002', 'approved', 'Big Owner LLC', 71, 'Active A GMP', '2026-07-31') and row['percent_complete'] == 35.0 and row['gross_amount_this_period'] == 210000.0 and row['total_amount_paid'] == 650000.0 and row['contract_sum_to_date'] == 2505200.0 and row['current_payment_due'] == 182979.0 and 'extra' not in row)
check('draft with empty contract and g702 still returns', result['items'][1]['contract_id'] is None and result['items'][1]['current_payment_due'] is None and result['items'][1]['gross_amount_this_period'] is None)
result = run(invoices.list_owner_invoices([1], status='approved'))
check('status applied after the read, total counts the kept rows', [i['id'] for i in result['items']] == [801] and result['total'] == 1 and 'filters[status]' not in query(requests[0]))


# ------------- LIST_INSPECTIONS -------------
print('-- list_inspections')
result = run(inspections.list_inspections([1, 3, 403]))
check('GET /rest/v1.0/checklist/lists with project_id per project, one unpaged GET each, 403 under errors', {r.url.path for r in requests} == {'/rest/v1.0/checklist/lists'} and sorted(query(r)['project_id'][0] for r in requests) == ['1', '3', '403'] and all('page' not in query(r) and 'filters[status]' not in query(r) for r in requests) and [e['project_id'] for e in result['errors']] == [403])
check('template groups flattened, sorted by project then number, total combined', [(i['project_id'], i['number'], i['name']) for i in result['items']] == [(1, 1, 'Pre-Pour Slab'), (1, 2, 'Pre-Pour Footings'), (1, 3, 'Weekly Safety Walk'), (3, 1, 'Weekly Safety Walk')] and result['total'] == 4)
row = result['items'][1]
check('row trimmed: status lower-cased, nested names flattened, spec section joined, inspectors named, counts kept', (row['status'], row['template'], row['template_id'], row['trade'], row['location'], row['spec_section'], row['responsible_contractor'], row['responsible_contractor_id'], row['responsible_party'], row['inspectors'], row['closed_by']) == ('in_review', 'Concrete Pre-Pour', 31, 'Concrete', 'Footings', '03 30 00 Cast-in-Place Concrete', 'Ready Mix Co', 12, 'Pat Manager', ['Pat Manager', 'Sam Super'], None) and (row['item_count'], row['conforming_item_count'], row['deficient_item_count'], row['na_item_count']) == (10, 7, 2, 1) and len(row['description']) == 300 and 'attachments' not in row and 'extra' not in row)
check('template name falls back to the group, null nested objects give None, closed inspection keeps closed_by', result['items'][0]['template'] == 'Concrete Pre-Pour' and result['items'][0]['status'] == 'closed' and result['items'][0]['closed_by'] == 'Sam Super' and result['items'][2]['trade'] is None and result['items'][2]['inspectors'] == [] and result['items'][2]['spec_section'] is None)
result = run(inspections.list_inspections([1], status='closed'))
check('status applied after the read, total counts the kept rows', [i['id'] for i in result['items']] == [502] and result['total'] == 1 and 'filters[status]' not in query(requests[0]))
result = run(inspections.list_inspections([1], limit=1))
check('limit cuts the flattened rows, total still counts them', result['returned'] == 1 and result['total'] == 3)
run(inspections.list_inspections([1], search='pour', responsible_contractor_id=12, updated_at='2026-03-01...2026-03-31'))
q = query(requests[0])
check('search, responsible_contractor_id, updated_at forwarded as filters', q['filters[search]'] == ['pour'] and q['filters[responsible_contractor_id]'] == ['12'] and q['filters[updated_at]'] == ['2026-03-01...2026-03-31'])


# ------------- LIST_INCIDENTS -------------
print('-- list_incidents')
result = run(incidents.list_incidents([1, 3, 403]))
check('GET /rest/v1.0/projects/{id}/incidents per project, no filters unless asked, 403 under errors', {r.url.path for r in requests} == {'/rest/v1.0/projects/1/incidents', '/rest/v1.0/projects/3/incidents', '/rest/v1.0/projects/403/incidents'} and all('filters[status]' not in query(r) and 'filters[recordable]' not in query(r) for r in requests) and [e['project_id'] for e in result['errors']] == [403])
check('incidents sorted by project then event date, total combined', [(i['project_id'], i['number']) for i in result['items']] == [(1, 1), (1, 2), (3, 1)] and result['total'] == 3)
row = result['items'][1]
check('row trimmed: flags booleans, nested names flattened, counts kept, description cut', (row['title'], row['status'], row['event_date'], row['time_unknown'], row['recordable'], row['private'], row['location'], row['hazard'], row['contributing_condition'], row['contributing_behavior'], row['created_by']) == ('Ladder fall', 'open', '2026-04-02T14:30:00Z', False, True, False, 'Level 2', 'Fall', 'Improper ladder setup', 'Rushing', 'Pat Manager') and (row['records_count'], row['actions_count'], row['witness_statements_count'], row['open_observations_count'], row['attachments_count']) == (1, 2, 1, 1, 3) and len(row['description']) == 300 and 'custom_fields' not in row and 'extra' not in row)
check('null nested objects give None, absent flags False', result['items'][0]['hazard'] is None and result['items'][0]['location'] is None and result['items'][0]['recordable'] is False and result['items'][0]['private'] is False and result['items'][0]['actions_count'] is None)
run(incidents.list_incidents([1], status='open', recordable=False, event_date='2026-01-01...2026-06-30', updated_at='2026-04-01...2026-04-30'))
q = query(requests[0])
check('status, recordable, event_date, updated_at forwarded as filters', q['filters[status]'] == ['open'] and q['filters[recordable]'] == ['false'] and q['filters[event_date]'] == ['2026-01-01...2026-06-30'] and q['filters[updated_at]'] == ['2026-04-01...2026-04-30'])
run(incidents.list_incidents([1], recordable=True))
check('recordable=true forwarded as true', query(requests[0])['filters[recordable]'] == ['true'])


# ------------- LIST_PROJECT_USERS -------------
print('-- list_project_users')
result = run(project_users.list_project_users([1, 3, 403]))
check('GET /rest/v1.0/projects/{id}/users and /rest/v1.0/project_roles?project_id= per project, 403 under errors', {r.url.path for r in requests} == {'/rest/v1.0/projects/1/users', '/rest/v1.0/projects/3/users', '/rest/v1.0/projects/403/users', '/rest/v1.0/project_roles'} and sorted(query(r)['project_id'][0] for r in requests if r.url.path == '/rest/v1.0/project_roles' and query(r)['page'] == ['1']) == ['1', '3', '403'] and [e['project_id'] for e in result['errors']] == [403])
check('users sorted by project then name, total combined', [(u['project_id'], u['name']) for u in result['items']] == [(1, 'Chris Sub'), (1, 'Pat Manager'), (1, 'Sam Super'), (3, 'Pat Manager')] and result['total'] == 4)
row = result['items'][1]
check('row trimmed: company and template flattened, active roles attached per user', (row['id'], row['email'], row['job_title'], row['company'], row['company_id'], row['is_employee'], row['permission_template'], row['business_phone'], row['mobile_phone'], row['last_login_at']) == (9, 'pat@example.com', 'Project Manager', 'Us Construction', 1, True, 'Project Admin', '555-0100', '555-0101', '2026-10-20T13:00:00Z') and row['roles'] == ['Project Manager', 'Project Executive'] and 'address' not in row and 'extra' not in row)
check('inactive role dropped, compact-view user has None contact fields and no roles, roles are per project', result['items'][2]['roles'] == ['Superintendent'] and result['items'][0]['roles'] == [] and result['items'][0]['email'] is None and result['items'][0]['is_employee'] is False and result['items'][3]['roles'] == ['Project Manager'])
run(project_users.list_project_users([1], search='pat'))
check('search forwarded to the users read only', {query(r)['filters[search]'][0] for r in requests if r.url.path == '/rest/v1.0/projects/1/users'} == {'pat'} and all('filters[search]' not in query(r) for r in requests if r.url.path == '/rest/v1.0/project_roles'))
scenario['deny'] = '/rest/v1.0/project_roles'
result = run(project_users.list_project_users([1]))
fresh()
check('roles Procore refuses leave users listed with roles None', result['errors'] == [] and result['returned'] == 3 and all(u['roles'] is None for u in result['items']))


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
