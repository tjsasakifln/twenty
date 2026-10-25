import importlib.util
import json
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).with_name("load-private-application-variables.py")
SPEC = importlib.util.spec_from_file_location("variable_loader", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
EXPECTED_KEYS = MODULE.EXPECTED_KEYS
SECRET_KEYS = MODULE.SECRET_KEYS
normalize_values = MODULE.normalize_values


class LoadPrivateApplicationVariablesTest(unittest.TestCase):
    def valid_payload(self):
        return {
            key: ({"stage-a": "TODO"} if key == "CUTOVER_STAGE_STATUS_MAP" else "value")
            for key in EXPECTED_KEYS
        }

    def test_accepts_exact_eight_keys_and_canonicalizes_stage_map(self):
        values = normalize_values(self.valid_payload())
        self.assertEqual(len(values), 8)
        self.assertEqual(
            json.loads(values["CUTOVER_STAGE_STATUS_MAP"]), {"stage-a": "TODO"}
        )

    def test_rejects_gate_key(self):
        payload = self.valid_payload()
        payload["CUTOVER_ENABLED"] = "true"
        with self.assertRaisesRegex(ValueError, "exactly the approved eight keys"):
            normalize_values(payload)

    def test_rejects_missing_key(self):
        payload = self.valid_payload()
        payload.pop("CUTOVER_MEMBER_ID")
        with self.assertRaisesRegex(ValueError, "exactly the approved eight keys"):
            normalize_values(payload)

    def test_rejects_non_object_stage_map(self):
        payload = self.valid_payload()
        payload["CUTOVER_STAGE_STATUS_MAP"] = "[]"
        with self.assertRaisesRegex(ValueError, "must encode an object"):
            normalize_values(payload)

    def test_secret_key_set_excludes_public_view_ids_and_gate(self):
        self.assertEqual(len(SECRET_KEYS), 6)
        self.assertNotIn("CONTRACTUAL_MILESTONES_VIEW_ID", SECRET_KEYS)
        self.assertNotIn("OPERATIONAL_CALENDAR_VIEW_ID", SECRET_KEYS)
        self.assertNotIn("CUTOVER_ENABLED", SECRET_KEYS)


if __name__ == "__main__":
    unittest.main()
