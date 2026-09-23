"""Regression tests for Phase 20 phone-call visibility in normal chat."""
import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.models import Conversation, Message
from app.routers.chat_routes import _build_recent_phone_call_context


def _query_result(rows):
    query = MagicMock()
    query.filter.return_value = query
    query.order_by.return_value = query
    query.limit.return_value = query
    query.all.return_value = rows
    return query


class PhoneCallContextTests(unittest.TestCase):
    def test_reports_no_saved_calls_without_falling_back_to_email(self):
        db = MagicMock()
        db.query.return_value = _query_result([])

        result = _build_recent_phone_call_context(db, user_id=7)

        self.assertIn("no incoming calls", result.lower())
        self.assertIn("never substitute email", result.lower())
        db.query.assert_called_once_with(Conversation)

    def test_includes_call_time_and_transcript(self):
        call = SimpleNamespace(
            id=12,
            title="📞 Phone Call: +919999999999 (2026-09-23 20:30)",
            created_at=datetime.datetime(2026, 9, 23, 20, 30),
        )
        messages = [
            SimpleNamespace(role="assistant", content="How can I help?", created_at=None),
            SimpleNamespace(role="user", content="Please call me tomorrow.", created_at=None),
        ]
        db = MagicMock()
        db.query.side_effect = [_query_result([call]), _query_result(messages)]

        result = _build_recent_phone_call_context(db, user_id=7)

        self.assertIn("+919999999999", result)
        self.assertIn("received 2026-09-23 20:30", result)
        self.assertIn("JARVIS: How can I help?", result)
        self.assertIn("Caller: Please call me tomorrow.", result)
        self.assertIn("never say you cannot check phone calls", result)


if __name__ == "__main__":
    unittest.main()
