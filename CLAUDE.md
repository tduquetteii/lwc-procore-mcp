# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A remote MCP server (Python 3.15, official `mcp` SDK 2.x, Starlette, httpx) that lets an LLM client
call Procore's REST API as the signed-in user. It holds no user data: it proxies Procore's OAuth
(because Procore publishes no discovery document and its token endpoint needs the client secret),
verifies each bearer token against Procore's `/rest/v1.0/me`, and forwards the same token on every
API call with the `Procore-Company-Id` header. One container, no database. README.md is the
authoritative user-facing document; keep it in sync with code changes (see "Keep in lockstep").

## Commands

```bash
uv sync                                          # install deps from uv.lock (uv run does this implicitly)
uv run smoke.py                                  # THE test suite: offline, ~2 s, run after every change, exits 1 on failure
uv run uvicorn main:app --reload --port 8000     # run locally; needs .env (copy .env.example)
docker build -t procore-mcp . && docker run --rm -p 8000:8000 --env-file .env procore-mcp
```

- There is no pytest, no linter, no formatter configured. `smoke.py` is the whole automated check;
  do not introduce a test framework or tool config without being asked.
- `smoke.py` is one script with sections printed as `-- name` headers (`-- tools`, `-- rate limiting`,
  `-- list_rfis`, ...). There is no way to run one section in isolation; the whole file runs in a
  couple of seconds, so just run it all.
- `smoke.py` needs no `.env`: it sets placeholder env vars before importing `main`. Everything in
  `procore_mcp` reads env at import time via `config.py`, so any script must set env before importing.
- To test against the real Procore, use the MCP Inspector (`npx @modelcontextprotocol/inspector`)
  with `PUBLIC_URL=http://localhost:8000`; in PowerShell set `$env:PUBLIC_URL` on its own line first.
  Details in README "Test before deploying".
- Dependencies: `uv add <pkg>` so `uv.lock` updates; the Dockerfile runs `uv sync --locked` and will
  fail on a stale lock.

## Architecture: the request path

```
client --Bearer--> /mcp --> ProcoreTokenVerifier (auth.py) --> @mcp.tool (tools/*.py)
                                  |                                   |
                                  v                                   v
                      procore.client() GET /me              procore.get / get_all (procore.py)
                                  \__ shared _gate + pause __/               |
                                                                     shape.* trims rows
                                                                     shape.listing envelope
```

- `main.py` is wiring only: imports `routes` and `tools` (the imports are what register routes and
  tools on `mcp`), builds the stateless streamable-HTTP ASGI app, wraps it in `ProcoreOutageMiddleware`
  as the outermost layer.
- `server.py` owns the single `MCPServer` instance `mcp`, the icon, and the two annotation
  constants `READ_ONLY` / `DESTRUCTIVE`. Everything else imports `mcp` from here.
- `auth.py` and `procore.py` share rate-limit state on purpose. The verifier's `/me` check goes
  through `procore._gate` and honors `procore.pause_left()`, and feeds `procore.note_limits()`.
  Only a 401/403 from Procore rejects a token. Any other failure serves the cached identity (up to
  `MAX_STALE`) or raises `ProcoreUnavailable`, which the middleware turns into 503 + Retry-After.
  Never let a Procore hiccup surface as a 401: that makes the client drop the connection.
- `procore.py` is the only place tools talk to `api.procore.com`. Module-level `_gate`
  (semaphore, 4 wide), `_pause_until`, `_pause_reason` are process-global and intentionally so.
  `routes.py` talks to `login.procore.com` with plain httpx; that is the OAuth proxy, not the API.
- `cache.py` is a generic `TTLCache[K, V]` with `peek()` for serving lapsed entries. Three uses:
  verified identities (auth.py), per-user project lists (tools/projects.py), company submittal
  statuses (tools/submittals.py). Cache keys for user data are keyed by `procore.user()` (the
  Procore user id), not by token, so a refreshed token or a second client shares the entry.
- `batch.py` is `per_project(project_ids, fetch, sort_key)` for tools that answer a company-wide
  question: up to `MAX_PROJECTS` (25) ids, one fetch each run concurrently, rows tagged with their
  `project_id`, a `ToolError` from one project (a 403) listed under `errors` while the rest return.
  Commitments, prime contracts, change events, change orders, bid packages, both invoice tools,
  inspections, incidents, and project users use it; per-project tools with large result sets
  (budget, schedule) deliberately do not. The
  untyped core `each(ids, fetch, label, sort_key)` serves batches of other ids (`commitment_id`
  in `list_commitment_line_items`, `bid_package_id` in `list_bids`); `per_project` takes a `label`
  to validate those the same way.
- Stateless transport (`stateless_http=True`) is a deliberate choice so replicas are safe. Do not
  reintroduce per-session state.

## Adding or changing a tool

One module per Procore resource under `procore_mcp/tools/`, holding every tool for that resource
(reads now, writes later). Checklist, every item enforced either by `smoke.py` or by convention:

1. Create `tools/<resource>.py`; add it to the import line in `tools/__init__.py` (that import is
   the registration).
2. Decorate with `@mcp.tool(title='Verb noun', annotations=READ_ONLY)` or `DESTRUCTIVE`. Exactly one
   of the two; smoke fails otherwise. Imports are relative: `from .. import procore, shape` and
   `from ..server import READ_ONLY, mcp`.
3. Write the docstring for the model, not for humans: what it returns (list the item keys), which
   tool supplies its inputs ("Use list_projects to find the project_id"), and anything the model
   should ask the user before calling. Make an argument required when the model must not guess it
   (the daily-log dates are the precedent).
4. Fetch with `procore.get` (one record) or `procore.get_all` (lists; returns `(rows, total)`).
   Never call httpx directly from a tool.
5. Trim every row with `.get()` and the `shape` helpers (`pick`, `name`, `names`, `text`, `amount`). Never
   index a Procore payload with `[]`; fields are routinely null or absent. Everything returned lands
   in the model's context.
6. List tools return `shape.listing(items, total)` giving `{total, returned, items}`. Default
   `limit=100`; `procore.MAX_ROWS` caps it at 1000. A tool answering a company-wide question takes
   `project_ids` and returns through `batch.per_project` (see Architecture); `limit` is then per project.
7. Filters go to Procore as `filters[...]` query params. A `status` argument defaults to the open
   state and `None` means every status; use `Literal[...]` for fixed vocabularies.
8. Reject bad arguments with `ToolError` (`from mcp.server.mcpserver.exceptions import ToolError`),
   and name what is valid in the message. Never raise a bare exception from a tool.
9. Add to `smoke.py`: a fixture under the exact Procore path in `FIXTURES` (include an
   `'extra': 'dropped'` key so trimming is tested), a `-- <tool>` section asserting the request
   path, forwarded params, and the trimmed row shape, and update the `'six tools registered'`
   sorted-name check.
10. Update README: the Tools table, and the project layout tree if a file was added.

## Code style (as practiced, keep it)

- Single quotes for strings. Four-space indent. Line length is generous (up to ~150); do not
  reflow existing code.
- Module docstrings explain *why* the module exists and the non-obvious decisions, in prose. Keep
  them current when behavior changes; they are the design record.
- Constants are grouped at the top of the module, assignment operators aligned with spaces, each
  with a trailing comment stating the Procore fact that justifies the number
  (`CACHE_TTL = 600   # seconds a verified token is trusted; Procore access tokens live 5400`).
- Comments explain rationale, not mechanics; lower-case, usually no trailing period.
- A blank line before the final `return` of any function longer than a couple of lines.
- Modern Python is used freely: walrus, `X | None`, PEP 695 generics (`class TTLCache[K, V]`),
  `dict | dict` merges. Target is 3.15; do not add compatibility shims.
- Registration-only imports carry `# noqa: F401` with a short reason after two spaces.
- Private module state and helpers are underscore-prefixed (`_gate`, `_get`, `_headers`); smoke.py
  reaches into them deliberately (`procore._pause_until = 0.0`, `procore.client = fake_client`), so
  renaming one means updating smoke.py.

## Keep in lockstep

A code change is not done until these match it:

- `smoke.py` passes and covers the new behavior.
- README.md: Tools table, "How it works" if auth or rate limiting changed, Troubleshooting if a
  new user-visible error was added, project layout tree if a file was added. Module docstrings in
  `procore_mcp/__init__.py` and README's layout tree list the same files.
- Timing or limit constants changed in `auth.py` / `procore.py` are also described in README
  ("How it works" and "Security notes" quote the cache lifetimes).
- `pyproject.toml` version and `uv.lock` are bumped together on a release commit.

## Commits

Subject is `Type: terse summary` (`Add:`, `Fix:`, `Initial Release:`). Body: one short paragraph
on the problem and the approach, then `- file: change` bullets, article-free, one per touched file
or behavior. The `/commit-msg` skill produces this shape. Commit only when asked.

## Secrets and environment

Four env vars, all required at import time: `PUBLIC_URL` (no trailing slash; OAuth metadata is
built from it), `PROCORE_CLIENT_ID`, `PROCORE_CLIENT_SECRET`, `PROCORE_COMPANY_ID`. `.env` is
git-ignored and docker-ignored; `.env.example` is the template. The client secret is injected only
in `routes.py` `/token` and must never appear in a response or log.
