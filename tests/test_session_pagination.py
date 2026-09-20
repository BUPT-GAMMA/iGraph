import base64
import json

import pytest
from fastapi import HTTPException

from backend.session_pagination import session_page


def test_all_pages_cover_more_than_200_sessions_with_timestamp_ties():
    rows = [{'session_id': f's-{i:04}', 'updated_at': i // 3} for i in range(235)]
    cursor, seen = None, []
    while True:
        page, cursor = session_page(rows, limit=50, cursor=cursor, activity=lambda row: row['updated_at'])
        assert len(page) <= 50
        seen.extend(row['session_id'] for row in page)
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 235
    assert seen[0] == 's-0234' and seen[-1] == 's-0000'


def test_cursor_is_stable_when_newer_rows_are_added_or_deleted():
    rows = [{'session_id': str(i), 'updated_at': i} for i in range(10)]
    _, cursor = session_page(rows, limit=4, cursor=None, activity=lambda row: row['updated_at'])
    rows = rows[:-1] + [{'session_id': 'new', 'updated_at': 20}]
    page, _ = session_page(rows, limit=4, cursor=cursor, activity=lambda row: row['updated_at'])
    assert [row['session_id'] for row in page] == ['5', '4', '3', '2']


@pytest.mark.parametrize('cursor', ['invalid', '====', base64.urlsafe_b64encode(json.dumps(['bad', 1]).encode()).decode()])
def test_invalid_cursor_returns_client_error(cursor):
    with pytest.raises(HTTPException) as error:
        session_page([], limit=50, cursor=cursor, activity=lambda row: 0)
    assert error.value.status_code == 400
