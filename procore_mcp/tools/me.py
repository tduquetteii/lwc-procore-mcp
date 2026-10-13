"""The signed-in user."""

from .. import procore, shape
from ..config import COMPANY_ID
from ..server import READ_ONLY, mcp


@mcp.tool(title='Who am I', annotations=READ_ONLY)
async def get_me() -> dict:
    """
    The Procore user this connection acts as: id, name, and login (email).

    Use it when the user asks who is signed in or whose permissions the data reflects.
    """
    return shape.pick(await procore.get('/rest/v1.0/me', {'company_id': COMPANY_ID}), 'id', 'name', 'login')
