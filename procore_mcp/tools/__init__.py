"""
One module per Procore resource. Importing this package imports each module, and each
module registers its tools on the server through the @mcp.tool decorator.

Add a resource by creating its module and listing it here.
"""

from . import daily_log, me, projects, punch_items, rfis, submittals  # noqa: F401
