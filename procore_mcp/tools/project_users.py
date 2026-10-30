"""
Project users: the people on a project's directory and the roles they hold on it.

Who runs which job is two reads in Procore: the project's users, and the project roles
(Project Manager, Superintendent, ...) which come as one row per person per role from a
separate endpoint. The tool reads both per project and attaches each user's active role
names, so "who is the PM on every active project" is one call across a batch of projects.
Procore serves the compact user view to a user without Directory permission; the tool sends
no view and takes whichever Procore allows, so the contact fields are None on that view.
A project whose roles cannot be read still returns its users, with roles None.
"""

import asyncio

from mcp.server.mcpserver.exceptions import ToolError

from .. import batch, procore
from ..server import READ_ONLY, mcp

ROLES = '/rest/v1.0/project_roles'  # project_id as a query param; one row per user per role


def _users(project_id: int) -> str:
    return f'/rest/v1.0/projects/{project_id}/users'


def _row(project_id: int, u: dict, roles: dict[int, list[str]] | None) -> dict:
    vendor = u.get('vendor') or {}
    return {
        'id': u.get('id'),
        'project_id': project_id,
        'name': u.get('name'),
        'email': u.get('email_address'),
        'job_title': u.get('job_title'),
        'company': vendor.get('name'),
        'company_id': vendor.get('id'),
        'is_employee': bool(u.get('is_employee')),
        'roles': None if roles is None else roles.get(u.get('id'), []),
        'permission_template': (u.get('permission_template') or {}).get('name'),
        'business_phone': u.get('business_phone'),
        'mobile_phone': u.get('mobile_phone'),
        'last_login_at': u.get('last_login_at'),
    }


async def _roles(project_id: int) -> dict[int, list[str]] | None:
    try:
        rows, _ = await procore.get_all(ROLES, {'project_id': project_id}, limit=procore.MAX_ROWS)
    except ToolError:
        return None  # the users still answer the question; roles are reported as unreadable
    roles: dict[int, list[str]] = {}
    for r in rows:
        if r.get('is_active', True) and r.get('user_id') and r.get('role'):
            roles.setdefault(r['user_id'], []).append(r['role'])

    return roles


async def _project(project_id: int, params: dict, limit: int) -> tuple[list[dict], int | None]:
    (rows, total), roles = await asyncio.gather(procore.get_all(_users(project_id), params, limit=limit), _roles(project_id))

    return [_row(project_id, u, roles) for u in rows], total


@mcp.tool(title='List project users', annotations=READ_ONLY)
async def list_project_users(project_ids: list[int], search: str | None = None, limit: int = 100) -> dict:
    """
    List the people on one or more projects' directories with the project roles each
    holds: the project team and the subcontractor and owner contacts.

    project_ids takes up to 25 ids per call; take them from list_projects. Each item: id,
    project_id, name, email, job_title, company and company_id (the user's company; match
    list_project_vendors), is_employee (works for us), roles (the project roles held, such
    as Project Manager or Superintendent; None when the roles could not be read),
    permission_template, business_phone, mobile_phone, last_login_at. search matches name,
    email, job title, and company name. limit applies per project. A project the user
    lacks the Directory tool on is listed under errors.
    """
    params: dict = {}
    if search:
        params['filters[search]'] = search

    return await batch.per_project(project_ids, lambda pid: _project(pid, params, limit), sort_key=lambda r: (r['project_id'], r['name'] or ''))
