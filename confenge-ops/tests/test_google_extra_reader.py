"""Unit tests for the Google reader; every HTTP response is local and fake."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("google_extra_reader", ROOT / "integrations" / "google_extra_reader.py")
subject = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(subject)


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, params=None, timeout=20):
        self.calls.append((url, params, timeout))
        return self.responses.pop(0)


def metadata():
    return {"properties": {"title": subject.EXPECTED_TITLE}, "sheets": [{"properties": {"sheetId": 0, "title": "Propostas", "gridProperties": {"rowCount": 1000, "columnCount": 84}}}]}


def header():
    row = ["" for _ in range(84)]
    for index, label in subject.CRITICAL_HEADERS.items():
        row[index] = label
    return row


class GoogleExtraReaderTest(unittest.TestCase):
    def test_reads_only_fixed_google_get_endpoints_and_preserves_null_cells(self):
        first = [header(), [None, "", "", "value"]]
        session = FakeSession([FakeResponse(200, metadata()), FakeResponse(200, {"range": "Propostas!A1:CF100", "majorDimension": "ROWS", "values": first}), FakeResponse(200, {"values": []})])
        snapshot = subject.read_snapshot(session, "test-sheet-id", sleep=lambda _: None)
        self.assertIsNone(snapshot["values"][1][0])
        self.assertTrue(snapshot["read_only"])
        self.assertEqual("FRESH", snapshot["source_status"])
        self.assertEqual(3, len(session.calls))
        self.assertTrue(all(url.startswith("https://sheets.googleapis.com/v4/spreadsheets/") for url, _, _ in session.calls))
        self.assertTrue(all(timeout == 20 for _, _, timeout in session.calls))
        self.assertTrue(all(call[1]["valueRenderOption"] == "FORMATTED_VALUE" for call in session.calls[1:]))

    def test_metadata_and_critical_headers_are_validated(self):
        bad = metadata()
        bad["sheets"][0]["properties"]["gridProperties"]["columnCount"] = 83
        with self.assertRaisesRegex(subject.ReaderError, "unexpected_sheet_bounds"):
            subject._sheet_properties(bad)
        bad_headers = [header()]
        bad_headers[0][19] = "wrong"
        with self.assertRaisesRegex(subject.ReaderError, "invalid_header_19"):
            subject.validate_headers(bad_headers)

    def test_incremental_fetches_chunks_and_flags_cross_lot_duplicates(self):
        initial = [header(), ["" for _ in range(84)]]
        initial[1][19], initial[1][52] = "same", "lot-a"
        later = [[] for _ in range(400)]
        later[0] = ["" for _ in range(84)]
        later[0][19], later[0][52] = "same", "lot-b"
        later[2] = ["" for _ in range(84)]
        later[2][19], later[2][52] = "final", "lot-c"
        empty = {"values": []}
        session = FakeSession([
            FakeResponse(200, metadata()), FakeResponse(200, {"values": initial}),
            FakeResponse(200, {"values": [["later-id"]]}),
            FakeResponse(200, {"values": later}), FakeResponse(200, empty), FakeResponse(200, empty),
        ])
        snapshot = subject.read_snapshot(session, "test-sheet-id", sleep=lambda _: None)
        self.assertEqual(1, snapshot["validation"]["duplicate_ids_different_lots"])
        self.assertEqual(6, len(session.calls))
        self.assertIn("Propostas%21A101%3ACF500", session.calls[3][0])
        self.assertEqual([], snapshot["values"][101])  # blank rows retain their source offset

    def test_retries_are_bounded_and_retry_after_is_capped(self):
        session = FakeSession([FakeResponse(429, headers={"Retry-After": "99"}), FakeResponse(503), FakeResponse(200, {"ok": True})])
        delays = []
        self.assertEqual({"ok": True}, subject.get_json(session, "/x", sleep=delays.append))
        self.assertEqual(3, len(session.calls))
        self.assertEqual(5.0, delays[0])
        self.assertLessEqual(delays[1], 5.0)

    def test_failure_marks_stale_without_replacing_prior_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, status = root / "snapshot.json", root / "status.json"
            output.write_text(json.dumps({"good": True}), encoding="utf-8")
            status.write_text(json.dumps({"last_success": "2026-10-16T00:00:00-03:00"}), encoding="utf-8")
            original = subject.live_session
            subject.live_session = lambda _: FakeSession([FakeResponse(403)])
            try:
                with self.assertRaisesRegex(subject.ReaderError, "http_403"):
                    subject.run(output, status, root / "ignored.json", "test-sheet-id")
            finally:
                subject.live_session = original
            self.assertEqual({"good": True}, json.loads(output.read_text(encoding="utf-8")))
            stale = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual("STALE", stale["status"])
            self.assertEqual("2026-10-16T00:00:00-03:00", stale["last_success"])


if __name__ == "__main__":
    unittest.main()
