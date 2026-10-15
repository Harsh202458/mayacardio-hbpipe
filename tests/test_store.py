"""Upload bookkeeping of the device store: acknowledged chunk-id ranges, live/backlog lanes."""
import tempfile
import unittest

from hbpipe.common.codec import pack
from hbpipe.device.store import Batch, DeviceStore


class UploadTrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DeviceStore(self.tmp.name)
        b = Batch(new_sessions=[("s", "boot")])
        b.chunks = [("s", 1, i * 10, 10, pack([[1, 2, 3]] * 10)) for i in range(50)]
        self.store.commit(b)
        self.conn = self.store.new_connection()

    def tearDown(self):
        self.conn.close()
        self.store.conn.close()
        self.tmp.cleanup()

    def ids(self, batch):
        return [r[0] for r in batch["chunks"]]

    def test_live_lane_newest_then_backlog(self):
        up = self.store.build_upload(self.conn, live_chunks=10, backlog_chunks=15)
        self.assertEqual(self.ids(up), list(range(1, 16)) + list(range(41, 51)))
        self.assertTrue(up["full"])

    def test_ack_ranges_and_counts(self):
        up = self.store.build_upload(self.conn, live_chunks=10, backlog_chunks=15)
        self.store.mark_uploaded(self.conn, up, {})
        self.assertEqual(self.store.counts()["samples_in_cloud"], 250)
        self.assertEqual(self.store.counts()["backlog"], 250)
        up2 = self.store.build_upload(self.conn, live_chunks=100)
        self.assertEqual(self.ids(up2), list(range(16, 41)))
        self.store.mark_uploaded(self.conn, up2, {})
        self.assertEqual(self.store.counts()["backlog"], 0)
        self.assertEqual(self.conn.execute("SELECT * FROM acked").fetchall(), [(1, 51)])
        self.assertEqual(self.store.build_upload(self.conn)["chunks"], [])

    def test_unacknowledged_batch_is_resent(self):
        up = self.store.build_upload(self.conn, live_chunks=100)
        again = self.store.build_upload(self.conn, live_chunks=100)  # no ack in between
        self.assertEqual(self.ids(up), self.ids(again))


if __name__ == "__main__":
    unittest.main()
