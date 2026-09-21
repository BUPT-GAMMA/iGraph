"""Stable, bounded session pages over the ownership-filtered session index."""
import base64
import json
import math

from fastapi import HTTPException


def session_page(rows, *, limit, cursor, activity):
    def key(row):
        value = activity(row)
        return (value if math.isfinite(value) else 0, str(row.get('session_id') or ''))

    boundary = None
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if (not isinstance(decoded, list) or len(decoded) != 2
                    or isinstance(decoded[0], bool) or not isinstance(decoded[0], (int, float))
                    or not math.isfinite(decoded[0]) or not isinstance(decoded[1], str)):
                raise ValueError('invalid cursor')
            boundary = tuple(decoded)
        except (ValueError, TypeError, UnicodeError):
            raise HTTPException(400, 'Invalid session cursor') from None
    ordered = sorted((row for row in rows if boundary is None or key(row) < boundary), key=key, reverse=True)
    page = ordered[:limit]
    has_more = len(ordered) > limit
    next_cursor = base64.urlsafe_b64encode(json.dumps(key(page[-1])).encode()).decode() if has_more else None
    return page, next_cursor
