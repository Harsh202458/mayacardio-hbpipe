import json
import unittest
import zlib

from hbpipe.common.protocol import CorruptReply, ProtocolError, parse_reply, raw_data_text


def line(data, crc=None, extra="", spacing=", "):
    text = json.dumps(data, separators=(spacing, ": "))
    c = crc if crc is not None else "%08x" % (zlib.crc32(text.encode()) & 0xFFFFFFFF)
    return ('{"session_id": "5f0c2a9e", "sensor": 1, "first_index": 120000, "count": %d, '
            '"lost": [%s], "session_end": false, "crc32": "%s", "data": %s}'
            % (len(data), extra, c, text))


class ProtocolTests(unittest.TestCase):
    def test_spec_example(self):
        data = b"[[720, 1200, 1560], [708, 1180, 1534], [726, 1210, 1573]]"
        rep = parse_reply('{"session_id": "5f0c2a9e", "sensor": 1, "first_index": 120000, '
                          '"count": 3, "lost": [], "session_end": false, "crc32": "%08x", '
                          '"data": %s}' % (zlib.crc32(data), data.decode()))
        self.assertEqual(rep.count, 3)
        self.assertEqual(rep.data[2], [726, 1210, 1573])

    def test_crc_uses_exact_text(self):
        # Compact spacing: the CRC must be computed on what was sent, not a re-serialisation.
        rep = parse_reply(line([[1, 2, 3], [4, 5, 6]], spacing=","))
        self.assertEqual(rep.count, 2)

    def test_corrupt(self):
        with self.assertRaises(CorruptReply):
            parse_reply(line([[1, 2, 3]], crc="deadbeef"))

    def test_data_key_inside_reason_is_ignored(self):
        extra = '{"from_index": 1, "to_index": 2, "reason": "\\"data\\": [[9]]"}'
        rep = parse_reply(line([[1, 2, 3]], extra=extra))
        self.assertEqual(rep.lost[0].reason, '"data": [[9]]')

    def test_out_of_range_value(self):
        with self.assertRaises(CorruptReply):
            parse_reply(line([[1, 2, 1 << 23]]))

    def test_wrong_channels(self):
        with self.assertRaises(CorruptReply):
            parse_reply(line([[1, 2]]))

    def test_garbage(self):
        with self.assertRaises(ProtocolError):
            parse_reply(b'{"session_id": "x", "sens')

    def test_raw_text_empty(self):
        self.assertEqual(raw_data_text('{"a": 1, "data": []}'), "[]")


if __name__ == "__main__":
    unittest.main()
