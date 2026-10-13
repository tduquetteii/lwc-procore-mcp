"""Helpers that trim Procore's nested records to the few fields the model needs."""


def name(obj) -> str | None:
    """Display name of a nested Procore object such as {'id': 5, 'name': 'Acme'}. Strings pass through."""
    if isinstance(obj, dict):
        return obj.get('name')

    return obj if isinstance(obj, str) else None


def names(objs) -> list[str]:
    """Display names of a list of nested objects, such as ball_in_court or assignees."""
    return [n for n in (name(o) for o in objs or []) if n]


def pick(record: dict, *keys: str) -> dict:
    """The named keys of a record, None where missing."""
    return {k: record.get(k) for k in keys}


def text(value, limit: int = 300) -> str | None:
    """A string cut to `limit` characters, marked with an ellipsis when cut."""
    if not isinstance(value, str):
        return None

    return value if len(value) <= limit else value[: limit - 3] + '...'


def listing(items: list, total: int | None) -> dict:
    """The envelope every list tool returns: Procore's count for the query, how many rows are here, and the rows."""
    return {'total': len(items) if total is None else total, 'returned': len(items), 'items': items}
