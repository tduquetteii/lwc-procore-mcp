"""The MCPServer instance. Routes and tools register on `mcp` by importing it."""

import base64

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.types import Icon, ToolAnnotations
from pydantic import AnyHttpUrl

from .auth import ProcoreTokenVerifier
from .config import ICON_PATH, ISSUER, PUBLIC_URL, REPO_URL

ICON = Icon(
    src='data:image/svg+xml;base64,' + base64.b64encode(ICON_PATH.read_bytes()).decode(),
    mime_type='image/svg+xml',
    sizes=['any'],
    theme='light',
)

READ_ONLY   = ToolAnnotations(read_only_hint=True)
DESTRUCTIVE = ToolAnnotations(destructive_hint=True)

mcp = MCPServer(
    'lwc-procore-mcp',
    website_url=REPO_URL,
    icons=[ICON],
    token_verifier=ProcoreTokenVerifier(),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(ISSUER),
        resource_server_url=AnyHttpUrl(f'{PUBLIC_URL}/mcp'),
        validate_token_resource=False,
    ),
)
