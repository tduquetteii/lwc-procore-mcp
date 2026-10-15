"""
Procore MCP server with per-user OAuth.

    /authorize  sends the user to Procore's login
    /token      forwards code and refresh exchanges to Procore, adding the client secret
    /mcp        every request carries the user's Procore token, checked against Procore's /me endpoint

Run with `uvicorn main:app`. The code lives in procore_mcp/; this module wires it into one app.
"""

from mcp.server.transport_security import TransportSecuritySettings

from procore_mcp import routes, tools  # noqa: F401  importing registers the routes and tools on mcp
from procore_mcp.auth import ProcoreOutageMiddleware
from procore_mcp.server import mcp

app = mcp.streamable_http_app(
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
# a Procore outage during the token check answers 503 + Retry-After, not 401.
app.add_middleware(ProcoreOutageMiddleware)
