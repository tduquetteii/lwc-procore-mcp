"""
One module per Procore resource. Importing this package imports each module, and each
module registers its tools on the server through the @mcp.tool decorator.

Add a resource by creating its module and listing it here.
"""

from . import bidding, budget, change_events, change_orders, commitments, daily_log, incidents, inspections, invoices, me, prime_contracts, project_users, projects, punch_items, rfis, schedule, submittals, vendors  # noqa: F401
