import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integrations'))
from source_freshness import effective_source_state


class SourceFreshnessTest(unittest.TestCase):
    def state(self, stamp, now):
        return effective_source_state({'status': 'FRESH', 'last_success': stamp}, datetime.fromisoformat(now))[0]

    def test_weekend_does_not_expire_until_monday_grace(self):
        stamp = '2026-10-16T19:04:00-03:00'
        self.assertEqual(self.state(stamp, '2026-10-19T10:03:00-03:00'), 'FRESH')
        self.assertEqual(self.state(stamp, '2026-10-19T10:03:01-03:00'), 'STALE')

    def test_workday_missed_slot_expires_after_grace(self):
        stamp = '2026-10-16T08:04:00-03:00'
        self.assertEqual(self.state(stamp, '2026-10-16T11:03:00-03:00'), 'FRESH')
        self.assertEqual(self.state(stamp, '2026-10-16T11:03:01-03:00'), 'STALE')

    def test_missing_ambiguous_future_and_failed_reads_are_stale(self):
        now = datetime.fromisoformat('2026-10-16T12:00:00-03:00')
        for status in [{}, {'status': 'STALE', 'last_success': '2026-10-16T11:00:00-03:00'},
                       {'status': 'FRESH', 'last_success': '2026-10-16T11:00:00'},
                       {'status': 'FRESH', 'last_success': '2026-10-16T13:00:00-03:00'}]:
            with self.subTest(status=status):
                self.assertEqual(effective_source_state(status, now)[0], 'STALE')


if __name__ == '__main__':
    unittest.main()
