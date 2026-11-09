"""placebounds.py: decode settlement rings for citylimits synthesis."""
import struct
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mapgeom  # noqa: E402
import placebounds  # noqa: E402


def _wkb_square(lon0, lat0, lon1, lat1):
    """Little-endian WKB Polygon, one exterior ring (closed)."""
    pts = [(lon0, lat0), (lon1, lat0), (lon1, lat1), (lon0, lat1), (lon0, lat0)]
    buf = bytearray()
    buf += struct.pack("<BI", 1, 3)
    buf += struct.pack("<I", 1)
    buf += struct.pack("<I", len(pts))
    for x, y in pts:
        buf += struct.pack("<dd", x, y)
    return bytes(buf)


class RingsOfTest(unittest.TestCase):
    def test_square_exterior(self):
        wkb = _wkb_square(19.08, 53.88, 19.14, 53.92)
        rows = [(1, "Cygusy", "village", "10", wkb)]
        got = placebounds._rings_of(rows)
        self.assertEqual(len(got), 1)
        name, ring = got[0]
        self.assertEqual(name, "Cygusy")
        self.assertGreaterEqual(len(ring), 4)
        self.assertAlmostEqual(float(ring[:, 0].min()), 19.08, places=5)
        self.assertAlmostEqual(float(ring[:, 1].max()), 53.92, places=5)

    def test_skips_bad_wkb(self):
        self.assertEqual(placebounds._rings_of([(1, "X", "", "", b"not-wkb")]), [])

    def test_parse_matches_mapgeom(self):
        wkb = _wkb_square(0, 0, 1, 1)
        kind, parts = mapgeom.parse_wkb(wkb)
        self.assertEqual(kind, mapgeom.POLYGON)
        self.assertEqual(len(parts), 1)


class FilterConstantsTest(unittest.TestCase):
    def test_village_in_place_values(self):
        self.assertIn("village", placebounds.PLACE_VALUES)
        self.assertLess(placebounds.MAX_BBOX_DEG, 1.0)


if __name__ == "__main__":
    unittest.main()
