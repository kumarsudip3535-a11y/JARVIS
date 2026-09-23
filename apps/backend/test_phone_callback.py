"""Tests for Phase 20 structured callback extraction."""
import unittest

from app import phone_agent


class CallbackMarkerTests(unittest.TestCase):
    def test_valid_callback_is_removed_and_normalized(self):
        raw = (
            "Thanks, I have everything and Sudip will call you back. "
            '[CALLBACK_REQUEST]{"caller_name":" Anita ","reason":"Pump repair",'
            '"preferred_time":"tomorrow at 11 AM"} [END_CALL]'
        )
        spoken, callback = phone_agent.extract_callback_request_marker(raw)

        self.assertEqual(callback, {
            "caller_name": "Anita",
            "reason": "Pump repair",
            "preferred_time": "tomorrow at 11 AM",
        })
        self.assertNotIn("[CALLBACK_REQUEST]", spoken)
        self.assertNotIn("caller_name", spoken)
        self.assertIn("[END_CALL]", spoken)

    def test_incomplete_callback_is_not_saved(self):
        raw = (
            "What time should Sudip call you? "
            '[CALLBACK_REQUEST]{"caller_name":"Anita","reason":"Pump repair",'
            '"preferred_time":""}'
        )
        spoken, callback = phone_agent.extract_callback_request_marker(raw)

        self.assertIsNone(callback)
        self.assertNotIn("[CALLBACK_REQUEST]", spoken)

    def test_malformed_marker_payload_is_never_spoken(self):
        raw = 'I have your details. [CALLBACK_REQUEST]{bad json'
        spoken, callback = phone_agent.extract_callback_request_marker(raw)

        self.assertEqual(spoken, "I have your details.")
        self.assertIsNone(callback)

    def test_phone_persona_requires_three_callback_fields(self):
        persona = phone_agent.build_phone_persona_context("SS Retail Services", "")
        self.assertIn("caller asks for a callback", persona)
        self.assertIn("their name", persona)
        self.assertIn("reason", persona)
        self.assertIn("preferred callback time", persona)


if __name__ == "__main__":
    unittest.main()
