"""Tests for Phase 20's Twilio-safe AI reply timeout."""
import asyncio
import time
import unittest
from unittest.mock import patch

from app.config import settings
from app.routers.phone_routes import _generate_phone_reply


class _SlowProvider:
    def generate_reply(self, messages, **kwargs):
        time.sleep(0.08)
        return "too late"


class _FastProvider:
    def generate_reply(self, messages, **kwargs):
        return f"ok:{messages[0]['content']}:{kwargs['agent_context']}"


class PhoneReplyTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_reply_is_returned(self):
        with patch.object(settings, "phone_agent_reply_timeout_seconds", 1):
            result = await _generate_phone_reply(
                _FastProvider(), [{"role": "user", "content": "hello"}], "phone persona"
            )
        self.assertEqual(result, "ok:hello:phone persona")

    async def test_slow_reply_times_out(self):
        with patch.object(settings, "phone_agent_reply_timeout_seconds", 0.01):
            with self.assertRaises(asyncio.TimeoutError):
                await _generate_phone_reply(
                    _SlowProvider(), [{"role": "user", "content": "hello"}], "phone persona"
                )


if __name__ == "__main__":
    unittest.main()
