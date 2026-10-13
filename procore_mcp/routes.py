"""
OAuth routes and /health.

Procore publishes no OAuth discovery document, so this server publishes one that points at
itself, sends the user to Procore's login, and forwards token exchanges with the client
secret added. The secret never reaches the client.
"""

from urllib.parse import urlencode

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from .config import CLIENT_ID, CLIENT_SECRET, ISSUER, PROCORE_LOGIN, PUBLIC_URL
from .server import mcp


@mcp.custom_route('/.well-known/oauth-authorization-server', methods=['GET'])
async def oauth_metadata(request: Request) -> JSONResponse:
    return JSONResponse({
        'issuer': ISSUER,
        'authorization_endpoint': f'{PUBLIC_URL}/authorize',
        'token_endpoint': f'{PUBLIC_URL}/token',
        'response_types_supported': ['code'],
        'grant_types_supported': ['authorization_code', 'refresh_token'],
        'code_challenge_methods_supported': ['S256'],
        'token_endpoint_auth_methods_supported': ['client_secret_post', 'client_secret_basic'],
    })


@mcp.custom_route('/authorize', methods=['GET'])
async def authorize(request: Request) -> RedirectResponse:
    query = request.query_params
    params = {
        'response_type': 'code',
        'client_id': CLIENT_ID,
        'redirect_uri': query['redirect_uri'],
        'state': query.get('state', ''),
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


@mcp.custom_route('/health', methods=['GET'])
async def health(request: Request) -> PlainTextResponse:
    return PlainTextResponse('ok')
