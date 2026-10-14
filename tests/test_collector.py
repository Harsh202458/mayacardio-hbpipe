"""Reply-handling rules of the collector, without a socket."""
import os
import tempfile
import threading
import unittest

from hbpipe.common.chunkdb import export_lines
from hbpipe.common.protocol import LostRange, Reply
from hbpipe.device.collector import GAP_RETRIES, Collector
from hbpipe.device.config import Config
from hbpipe.device.events import EventLog
from hbpipe.device.store import DeviceStore


def rows(sensor, a, b):
    k = 3 if sensor == 1 else 2
    return [[(k * i + c) for c in range(1, k + 1)] for i in range(a, b)]


def rep(sid, sensor, a, b, lost=(), end=False):
    return Reply(session_id=sid, sensor=sensor, first_index=a, count=b - a, session_end=end,
                 data=rows(sensor, a, b), lost=[LostRange(*l) for l in lost])


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=self.tmp.name)
        self.store = DeviceStore(self.tmp.name)
        self.events = EventLog(os.path.join(self.tmp.name, "events.jsonl"))
        self.col = Collector(self.cfg, self.store, self.events, threading.Event())

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def export(self, sid):
        self.col.flush()
        return "".join(export_lines(self.store.conn, sid))

    def test_duplicates_and_overlaps_stored_once(self):
        c = self.col
        c.apply(rep("s1", 1, 0, 100))
        c.apply(rep("s1", 1, 0, 100))      # duplicate reply
        c.apply(rep("s1", 1, 50, 150))     # overlap
        self.assertEqual(self.export("s1").count('"index"'), 150)

    def test_gap_is_reread_not_recorded(self):
        c = self.col
        c.apply(rep("s1", 2, 0, 100))
        c.apply(rep("s1", 2, 200, 300))    # unexplained jump
        self.assertEqual(c.state[2].cursor, 100)
        c.apply(rep("s1", 2, 100, 300))    # the re-read fills it
        out = self.export("s1")
        self.assertEqual(out.count('"index"'), 300)
        self.assertNotIn("from_index", out)

    def test_gap_recorded_after_retries(self):
        c = self.col
        c.apply(rep("s1", 2, 0, 100))
        for _ in range(GAP_RETRIES):
            c.apply(rep("s1", 2, 200, 300))
        out = self.export("s1")
        self.assertIn('"from_index": 100, "to_index": 199, "reason": "not_delivered_by_mock"', out)
        self.assertEqual(out.count('"index"'), 200)

    def test_mock_reported_loss(self):
        c = self.col
        c.apply(rep("s1", 1, 0, 100))
        c.apply(rep("s1", 1, 4000, 4100, lost=[(100, 3999, "buffer_overflow")]))
        out = self.export("s1")
        self.assertIn('"from_index": 100, "to_index": 3999, "reason": "buffer_overflow"', out)
        self.assertEqual(c.state[1].cursor, 4100)

    def test_mock_restart_lists_old_tail(self):
        c = self.col
        c.apply(rep("old", 1, 0, 100))
        c.apply(rep("old", 2, 0, 80))
        c.apply(rep("new", 1, 0, 10))
        old = self.export("old")
        self.assertIn('{"sensor": 1, "from_index": 100, "to_index": null, "reason": "mock_restart"}', old)
        self.assertIn('{"sensor": 2, "from_index": 80, "to_index": null, "reason": "mock_restart"}', old)
        c.apply(rep("old", 1, 100, 200))   # late reply from the closed session: ignored
        self.assertEqual(self.export("old").count('"index"'), 180)

    def test_state_survives_restart(self):
        c = self.col
        c.apply(rep("s1", 1, 0, 100))
        c.apply(rep("s1", 2, 0, 50))
        c.flush()
        c2 = Collector(self.cfg, self.store, self.events, threading.Event())
        self.assertEqual((c2.session, c2.state[1].cursor, c2.state[2].cursor), ("s1", 100, 50))

    def test_disk_full_mode_records_loss(self):
        c = self.col
        c.apply(rep("s1", 1, 0, 100))
        c.disk_full = True
        c.apply(rep("s1", 1, 100, 200))
        c.apply(rep("s1", 1, 200, 300))
        c.disk_full = False
        c.apply(rep("s1", 1, 300, 400))
        out = self.export("s1")
        self.assertIn('"from_index": 100, "to_index": 299, "reason": "disk_full"', out)
        self.assertEqual(out.count('"index"'), 200)

    def test_session_end_means_no_tail_loss(self):
        c = self.col
        c.apply(rep("s1", 1, 0, 100, end=True))
        c.apply(rep("s1", 2, 0, 100, end=True))
        c.apply(rep("s2", 1, 0, 10))
        self.assertNotIn("from_index", self.export("s1"))


if __name__ == "__main__":
    unittest.main()
