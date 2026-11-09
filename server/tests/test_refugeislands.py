"""Pedestrian refuge islands at mapped crossings (refugeislands.py)."""
import json
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import refugeislands  # noqa: E402
import roads  # noqa: E402


def road(fid, cls="tertiary", pts=((0.0, 0.0), (200.0, 0.0)), width=8.0, kind="paved", kmh=50.0, oneway=False):
    line = np.c_[np.array(pts, np.float64), np.zeros(len(pts))]
    return fid, roads.Spec(width, kind, True, cls, oneway, kmh=kmh), line, 0.0


class Infra:
    def __init__(self, rows):
        n = len(rows)
        self.ids = np.array([str(i) for i in range(n)], object)
        self.attrs = {"class": np.array([r[0] for r in rows], object),
                      "subtype": np.array(["transportation"] * n, object),
                      "tags": np.array([json.dumps(r[2]) for r in rows], object)}
        self.lonlat = np.array([r[1] for r in rows], np.float64)
        self.coord_off, self.ring_off, self.part_off = np.arange(n + 1), np.arange(n + 1), np.arange(n + 1)

    def __len__(self):
        return len(self.ids)


class LandUse:
    """Minimal land_use Features with class + polygon parts."""
    def __init__(self, rows):
        # rows: (class, outer lonlat (k,2))
        n = len(rows)
        self.ids = np.array([str(i) for i in range(n)], object)
        self.attrs = {"class": np.array([r[0] for r in rows], object)}
        coords, part_off, ring_off, coord_off = [], [0], [0], [0]
        for _, outer in rows:
            outer = np.asarray(outer, np.float64)
            coords.append(outer)
            coord_off.append(coord_off[-1] + len(outer))
            ring_off.append(len(coord_off) - 1)
            part_off.append(len(ring_off) - 1)
        self.lonlat = np.vstack(coords) if coords else np.zeros((0, 2))
        self.coord_off = np.array(coord_off)
        self.ring_off = np.array(ring_off)
        self.part_off = np.array(part_off)

    def __len__(self):
        return len(self.ids)

    def parts(self, i):
        a, b = self.part_off[i], self.part_off[i + 1]
        out = []
        for r in range(a, b):
            c0, c1 = self.coord_off[self.ring_off[r]], self.coord_off[self.ring_off[r + 1]]
            out.append([self.lonlat[c0:c1]])
        return out


class Frame:
    def geodetic_to_enu(self, lat, lon, h):
        # 1 deg -> 10 m (test scale); lon->x, lat->y
        return np.asarray(lon) * 10.0, np.asarray(lat) * 10.0, np.asarray(h) if np.shape(h) else 0.0

    def enu_to_geodetic(self, e, n, u):
        return np.asarray(n) / 10.0, np.asarray(e) / 10.0, np.asarray(u)


class TaggedTest(unittest.TestCase):
    def test_crossing_island_yes(self):
        self.assertTrue(refugeislands.tagged({"crossing:island": "yes"}))
        self.assertFalse(refugeislands.tagged({"crossing:island": "no"}))
        self.assertFalse(refugeislands.tagged({"crossing": "uncontrolled"}))

    def test_traffic_calming_island(self):
        self.assertTrue(refugeislands.tagged({"traffic_calming": "island"}))
        self.assertFalse(refugeislands.tagged({"traffic_calming": "choker"}))


class NodesTest(unittest.TestCase):
    def test_reads_tagged_crossing(self):
        got = refugeislands.nodes(Frame(), [Infra([
            ("crossing", (1.0, 2.0), {"crossing:island": "yes"}),
            ("crossing", (3.0, 4.0), {"crossing": "uncontrolled"}),
            ("give_way", (5.0, 6.0), {"traffic_calming": "island"})])])
        self.assertEqual(got, [(10.0, 20.0), (50.0, 60.0)])

    def test_untagged_is_empty(self):
        self.assertEqual(refugeislands.nodes(Frame(), [Infra([
            ("crossing", (1.0, 2.0), {"crossing": "uncontrolled"})])]), [])

    def test_nothing_read_means_none(self):
        self.assertIsNone(refugeislands.nodes(Frame(), [None, None]))
        cell = Infra([("crossing", (1.0, 2.0), {"crossing:island": "yes"})])
        del cell.attrs["tags"]
        self.assertIsNone(refugeislands.nodes(Frame(), [cell]))


class LandTest(unittest.TestCase):
    def test_traffic_island_class(self):
        # lon/lat square ~10 m on the Frame scale (1 deg = 10 m -> 1 unit lon = 10 m; use 0.5 = 5 m)
        sq = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)]
        rings = refugeislands.land_rings([LandUse([("traffic_island", sq), ("park", sq)])])
        self.assertEqual(len(rings), 1)
        np.testing.assert_allclose(rings[0][0][0], [0.0, 0.0])

    def test_place_polys_emits_pad_and_curb(self):
        # 4 m x 4 m island at origin in ENU: lon/lat = enu/10
        sq = [(-0.2, -0.2), (0.2, -0.2), (0.2, 0.2), (-0.2, 0.2), (-0.2, -0.2)]
        walks, chunks, gaps = refugeislands.place_polys(
            Frame(), lambda la, lo: np.zeros(np.shape(la)), [(np.array(sq), [])],
            (-50, -50, 50, 50))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["m"], "italy_cobblestone")
        self.assertEqual(chunks[0]["layer"], "mark")
        self.assertEqual(len(walks), 1)
        self.assertEqual(walks[0]["fid"], "refuge")
        self.assertEqual(len(gaps), 1)


class PlaceTest(unittest.TestCase):
    def test_tagged_node_gets_a_pad_and_curb(self):
        walks, chunks, gaps = refugeislands.place([road("a")], [(50.0, 0.0)])
        self.assertEqual(len(walks), 1)
        self.assertEqual(walks[0]["fid"], "refuge")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["layer"], "mark")
        self.assertEqual(len(gaps), 1)
        self.assertAlmostEqual(gaps[0][0], 50.0)

    def test_untagged_list_is_nothing(self):
        self.assertEqual(refugeislands.place([road("a")], []), ([], [], []))

    def test_off_the_road_or_on_gravel_or_a_motorway_is_nothing(self):
        self.assertEqual(refugeislands.place([road("a")], [(50.0, 10.0)])[0], [])
        self.assertEqual(refugeislands.place([road("a", kind="gravel")], [(50.0, 0.0)])[0], [])
        self.assertEqual(refugeislands.place([road("a", cls="motorway")], [(50.0, 0.0)])[0], [])


class GapTest(unittest.TestCase):
    def test_crosswalk_splits_over_the_island(self):
        cw = {"a": np.array([50.0, 4.0, 0.0]), "b": np.array([50.0, -4.0, 0.0]), "w": 3.0}
        out = refugeislands.gap_crosswalks([cw], [(50.0, 0.0, 1.0)])
        self.assertEqual(len(out), 2)
        for half in out:
            span = float(np.hypot(*(half["a"][:2] - half["b"][:2])))
            self.assertAlmostEqual(span, 3.0, places=5)

    def test_far_crosswalk_untouched(self):
        cw = {"a": np.array([10.0, 4.0, 0.0]), "b": np.array([10.0, -4.0, 0.0]), "w": 3.0}
        out = refugeislands.gap_crosswalks([cw], [(50.0, 0.0, 1.0)])
        self.assertEqual(len(out), 1)


class TileTest(unittest.TestCase):
    def tile(self, refuge=None, refuge_land=None):
        import test_roads as tr
        feats = tr.features([[(-300, 2), (600, 2)], [(100, 2), (100, 300)]], ["tertiary", "residential"])
        urban = lambda la, lo: np.ones(np.shape(la), bool)      # noqa: E731
        walks = []
        chunks = roads.build_tile(tr.FRAME, 0, 0, [feats], lambda a, b: np.zeros(np.shape(a)),
                                  urban, walks=walks, refuge=refuge, refuge_land=refuge_land)
        return chunks, walks

    def test_refuge_node_pad_in_chunks(self):
        chunks, walks = self.tile(refuge=[(60.0, 2.0)])
        pads = [c for c in chunks if c.get("m") == "italy_cobblestone" and c.get("layer") == "mark"]
        self.assertTrue(pads, "cobble pad on the mark layer")
        self.assertTrue(any(w.get("fid") == "refuge" for w in walks))

    def test_no_refuge_without_data(self):
        chunks, walks = self.tile()
        self.assertFalse([w for w in walks if w.get("fid") == "refuge"])
        self.assertFalse([c for c in chunks if c.get("m") == "italy_cobblestone" and c.get("layer") == "mark"])


if __name__ == "__main__":
    unittest.main()
