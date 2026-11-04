import unittest
from uuid import uuid4
from hell_inc.contracts import ValidationError, validate_registration


def payload(**changes):
    return {"command_id": str(uuid4()), "provider": "simulated", "identity_scope": "local-test",
            "subject_id": "visitor-001", "display_name": "小林", **changes}


class ValidationTests(unittest.TestCase):
    def test_preserves_original_strings(self):
        data = payload(subject_id="001:中文", display_name=" <script>alert(1)</script> ")
        self.assertEqual(validate_registration(data), data)

    def test_limits_and_strict_types(self):
        for change in ({"subject_id": ""}, {"subject_id": "x" * 129}, {"subject_id": " padded"},
                       {"subject_id": "padded "}, {"subject_id": 1}, {"subject_id": True},
                       {"subject_id": None}, {"display_name": ""}, {"display_name": " "},
                       {"display_name": "x" * 81}, {"display_name": []}, {"display_name": "\x00"},
                       {"command_id": "not-a-uuid"}, {"provider": "douyin"},
                       {"identity_scope": "production"}, {"performance": 100}, {"role": "employee"}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                validate_registration(payload(**change))
        for data in ([], None, "text", {"subject_id": "only"}):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                validate_registration(data)
        self.assertEqual(validate_registration(payload(subject_id="x" * 128, display_name="😀" * 80))["display_name"], "😀" * 80)
