"""
Procore MCP server, split by concern:

    config.py    environment variables and constants
    auth.py      token verifier: a token is valid if Procore accepts it
    server.py    the MCPServer instance, its icon, and the shared tool annotations
    routes.py    OAuth discovery and proxy routes, /health
    procore.py   HTTP client for Procore's REST API: auth headers, pagination, error surfacing
    shape.py     helpers that trim Procore records to the fields the model needs
    tools/       one module per Procore resource; each registers its tools on the server

main.py at the repository root imports routes and tools and builds the ASGI app.
"""
