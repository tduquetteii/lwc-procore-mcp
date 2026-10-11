"""
Procore MCP server with per-user OAuth.

    /authorize  sends the user to Procore's login
    /token      forwards code and refresh exchanges to Procore, adding the client secret
    /mcp        every request carries the user's Procore token, checked against Procore's /me endpoint
"""

import base64
import httpx
import os
import time
from dotenv import load_dotenv
from pathlib import Path
from urllib.parse import urlencode
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Icon, ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response


load_dotenv()

PUBLIC_URL      = os.environ['PUBLIC_URL'].rstrip('/')
CLIENT_ID       = os.environ['PROCORE_CLIENT_ID']
CLIENT_SECRET   = os.environ['PROCORE_CLIENT_SECRET']
COMPANY_ID      = os.environ['PROCORE_COMPANY_ID']

PROCORE_API     = 'https://api.procore.com'
PROCORE_LOGIN   = 'https://login.procore.com'
ISSUER          = f'{PUBLIC_URL}/'
REPO_URL        = 'https://github.com/tduquetteii/lwc-procore-mcp'


# ------------- AUTH -------------
class ProcoreTokenVerifier(TokenVerifier):
    """
    A token is valid if Procore accepts it. Cached for 5 minutes.
    """
    def __init__(self):
        self.cache: dict[str, tuple[float, AccessToken]] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        if (hit := self.cache.get(token)) and hit[0] > time.time():
            return hit[1]
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f'{PROCORE_API}/rest/v1.0/me', headers={'Authorization': f'Bearer {token}'})
        if response.status_code != 200:
            return None

        access = AccessToken(token=token, client_id=CLIENT_ID, scopes=[], subject=str(response.json().get('id')))
        self.cache[token] = (time.time() + 300, access)

        return access


# Advertised in the MCP handshake (serverInfo.icons) for clients that render server icons.
# The mark is black and grey, so it is declared for light backgrounds.
ICON = Icon(
    src='data:image/svg+xml;base64,' + base64.b64encode(Path(__file__).with_name('icon.svg').read_bytes()).decode(),
    mime_type='image/svg+xml',
    sizes=['any'],
    theme='light',
)

mcp = MCPServer(
    "lwc-procore-mcp",
    website_url=REPO_URL,
    icons=[ICON],
    token_verifier=ProcoreTokenVerifier(),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(ISSUER),
        resource_server_url=AnyHttpUrl(f'{PUBLIC_URL}/mcp'),
        validate_token_resource=False,
    )
)


@mcp.custom_route('/.well-known/oauth-authorization-server', methods=['GET'])
async def oauth_metadata(request: Request) -> JSONResponse:
    """
    Procore publishes no OAuth discovery

    This function publishes one for it
    """
    return JSONResponse({
        'issuer': ISSUER,
        'authorization_endpoint': f'{PUBLIC_URL}/authorize',
        'token_endpoint': f'{PUBLIC_URL}/token',
        'response_types_supported': ['code'],
        'grant_types_supported': ['authorization_code', 'refresh_token'],
        'code_challenge_methods_supported': ['S256'],
        'token_endpoint_auth_methods_supported': ['client_secret_post', 'client_secret_basic']
    })

@mcp.custom_route('/authorize', methods=['GET'])
async def authorize(request: Request) -> RedirectResponse:
    query = request.query_params
    params = {
        'response_type': 'code',
        'client_id': CLIENT_ID,
        'redirect_uri': query['redirect_uri'],
        'state': query.get('state', '')
    }

    return RedirectResponse(f'{PROCORE_LOGIN}/oauth/authorize?{urlencode(params)}')

@mcp.custom_route('/token', methods=['POST'])
async def token(request: Request) -> Response:
    form = await request.form()
    allowed = {'grant_type', 'code', 'redirect_uri', 'refresh_token'}
    data = {k: v for k, v in form.items() if k in allowed} | {'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f'{PROCORE_LOGIN}/oauth/token', data=data)

    return Response(response.content, status_code=response.status_code, media_type='application/json')


# ------------- PROCORE -------------
async def procore_get(path: str, params: dict | None = None) -> dict | list:
    headers = {'Authorization': f'Bearer {get_access_token().token}', 'Procore-Company-Id': COMPANY_ID}
    async with httpx.AsyncClient(base_url=PROCORE_API, timeout=30) as client:
        response = await client.get(path, params=params, headers=headers)
        response.raise_for_status()

        return response.json()

@mcp.tool(title='List projects', annotations=ToolAnnotations(read_only_hint=True))
async def list_projects(active_only: bool = True) -> list[dict]:
    """
    List Procore projects the current user can see. Returns id, name, number, and location.

    Use the returned id when another tool asks for a project_id
    """
    projects = await procore_get('/rest/v1.0/projects', params={'company_id': COMPANY_ID})
    keep = ('id', 'name', 'project_number', 'active', 'city', 'state_code')
    trimmed = [{k: p.get(k) for k in keep} for p in projects]

    return [p for p in trimmed if p['active']] if active_only else trimmed


# ------------- APP -------------
@mcp.custom_route('/health', methods=['GET'])
async def health(request: Request) -> PlainTextResponse:
    return PlainTextResponse('ok')

app = mcp.streamable_http_app(transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))