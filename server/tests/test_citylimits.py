"""Polish place-name / built-up signs (citylimits.py): tags, placement, mesh, signstream."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import citylimits  # noqa: E402
import roads  # noqa: E402
import signstream  # noqa: E402


class Frame:
    enu_to_geodetic = staticmethod(lambda x, y, z: (np.asarray(y, float), np.asarray(x, float), z))
    geodetic_to_enu = staticmethod(lambda la, lo, h: (np.asarray(lo, float), np.asarray(la, float),
                                                       np.asarray(h, float)))


BOX = (-20.0, -40.0, 220.0, 40.0)
SPEC = roads.Spec(8.0, "paved", False, "primary", False)


def elev(la, lo):
    return np.asarray(la, float) * 0.0 + 4.0


def line(n=200.0):
    return np.c_[np.linspace(0.0, n, 41), np.zeros(41)]


class ParseTagsTest(unittest.TestCase):
    def test_city_limit_both_default(self):
        got = citylimits.parse_tags({"traffic_sign": "city_limit", "name": "Kraków"})
        self.assertEqual(got["kind"], citylimits.PLACE_BOTH)
        self.assertEqual(got["name"], "Kraków")

    def test_city_limit_begin_end(self):
        self.assertEqual(citylimits.parse_tags(
            {"traffic_sign": "city_limit", "city_limit": "begin", "name": "A"})["kind"],
            citylimits.PLACE_BEGIN)
        self.assertEqual(citylimits.parse_tags(
            {"traffic_sign": "city_limit", "city_limit": "end", "name": "A"})["kind"],
            citylimits.PLACE_END)

    def test_pl_e17_e18(self):
        self.assertEqual(citylimits.parse_tags(
            {"traffic_sign": "PL:E-17a", "name": "X"})["kind"], citylimits.PLACE_BEGIN)
        self.assertEqual(citylimits.parse_tags(
            {"traffic_sign": "PL:E-18a", "name": "X"})["kind"], citylimits.PLACE_END)

    def test_built_up_mapped_only(self):
        self.assertEqual(citylimits.parse_tags({"traffic_sign": "PL:D-42"})["kind"],
                         citylimits.BUILT_BEGIN)
        self.assertEqual(citylimits.parse_tags({"traffic_sign": "PL:D-43"})["kind"],
                         citylimits.BUILT_END)
        self.assertIsNone(citylimits.parse_tags({"traffic_sign": "maxspeed"}))

    def test_combined_prefers_place(self):
        got = citylimits.parse_tags({"traffic_sign": "city_limit;PL:D-42", "name": "Town"})
        self.assertEqual(got["kind"], citylimits.PLACE_BOTH)


class IndexTest(unittest.TestCase):
    def tearDown(self):
        citylimits.set_index_for_tests(None)

    def test_near_filters_bbox(self):
        citylimits.set_index_for_tests([
            dict(lat=50.0, lon=20.0, osm_id=1, kind=citylimits.PLACE_BOTH, name="A"),
            dict(lat=51.0, lon=21.0, osm_id=2, kind=citylimits.BUILT_BEGIN, name=""),
            dict(lat=52.0, lon=22.0, osm_id=3, kind=citylimits.PLACE_END, name="B"),
        ])
        hit = citylimits.near(49.5, 19.5, 51.5, 21.5)
        self.assertEqual({h["osm_id"] for h in hit}, {1, 2})
        self.assertEqual(hit[0]["name"], "A")


class CrossingsTest(unittest.TestCase):
    @unittest.skipUnless(
        __import__("importlib").util.find_spec("fast") and
        getattr(__import__("fast", fromlist=["_ef"]), "_ef", None) is not None,
        "earthfast points_inside")
    def test_enter_and_leave(self):
        import fast
        if not hasattr(fast, "require"):
            self.skipTest("fast.require missing")
        try:
            fast.require(0, "points_inside")
        except Exception:
            self.skipTest("points_inside unavailable")
        xy = line(200.0)
        # Square covering x=50..150, y=-50..50
        ring = np.array([[50.0, -50.0], [150.0, -50.0], [150.0, 50.0], [50.0, 50.0], [50.0, -50.0]])
        crosses = citylimits.road_crossings(xy, ring, step=5.0)
        kinds = [(round(p[0]), entering) for p, _, entering in crosses]
        self.assertTrue(any(entering and 40 <= x <= 60 for x, entering in kinds), kinds)
        self.assertTrue(any((not entering) and 140 <= x <= 160 for x, entering in kinds), kinds)


class PlaceTest(unittest.TestCase):
    def tearDown(self):
        citylimits.set_index_for_tests(None)

    def test_mapped_place_snaps_to_road(self):
        rows = [("a", SPEC, line(200.0), 4.0)]
        mapped = [dict(lat=0.0, lon=100.0, osm_id=9, kind=citylimits.PLACE_BOTH,
                       name="Radom", dir_deg=float("nan"))]
        signs = citylimits.place(Frame, rows, mapped, [], elev, BOX)
        self.assertTrue(signs)
        self.assertTrue(all(s["source"] == "osm" for s in signs))
        # city_limit without begin/end → E-17a one roadside, E-18a the other.
        self.assertEqual({s["kind"] for s in signs},
                         {citylimits.PLACE_BEGIN, citylimits.PLACE_END})
        self.assertTrue(all(s["name"] == "Radom" for s in signs))
        self.assertEqual(len(signs), 2)

    def test_mapped_built_up_no_name(self):
        rows = [("a", SPEC, line(200.0), 4.0)]
        mapped = [dict(lat=0.0, lon=80.0, osm_id=3, kind=citylimits.BUILT_BEGIN,
                       name="", dir_deg=float("nan"))]
        signs = citylimits.place(Frame, rows, mapped, [], elev, BOX)
        self.assertTrue(signs)
        self.assertTrue(all(s["kind"] == citylimits.BUILT_BEGIN for s in signs))

    def test_synthetic_suppressed_near_mapped(self):
        rows = [("a", SPEC, line(200.0), 4.0)]
        mapped = [dict(lat=0.0, lon=50.0, osm_id=1, kind=citylimits.PLACE_BEGIN,
                       name="Town", dir_deg=float("nan"))]
        ring = np.array([[50.0, -30.0], [150.0, -30.0], [150.0, 30.0], [50.0, 30.0], [50.0, -30.0]])
        # Without mapped: would synthesise at ~50 and ~150. Same-name mapped suppresses the enter.
        try:
            import fast
            fast.require(0, "points_inside")
        except Exception:
            self.skipTest("points_inside unavailable")
        signs = citylimits.place(Frame, rows, mapped, [("Town", ring)], elev, BOX)
        synth = [s for s in signs if s.get("source") == "synthetic"]
        for s in synth:
            self.assertGreater(s["at"][0], 100.0, "mapped node must suppress the nearby crossing")

    def test_no_synthetic_built_up(self):
        """Built-up signs never come from polygons — only OSM."""
        rows = [("a", SPEC, line(200.0), 4.0)]
        ring = np.array([[40.0, -20.0], [160.0, -20.0], [160.0, 20.0], [40.0, 20.0], [40.0, -20.0]])
        try:
            import fast
            fast.require(0, "points_inside")
        except Exception:
            self.skipTest("points_inside unavailable")
        signs = citylimits.place(Frame, rows, [], [("Town", ring)], elev, BOX)
        self.assertTrue(all(s["kind"] in citylimits.PLACE_KINDS for s in signs))
        self.assertFalse(any(s["kind"] in citylimits.BUILT_KINDS for s in signs))

    def test_overlapping_rings_deduped(self):
        """Nested admin polygons must not stack three panels at one roadside point."""
        rows = [("a", SPEC, line(200.0), 4.0)]
        big = np.array([[40.0, -40.0], [160.0, -40.0], [160.0, 40.0], [40.0, 40.0], [40.0, -40.0]])
        small = np.array([[45.0, -20.0], [155.0, -20.0], [155.0, 20.0], [45.0, 20.0], [45.0, -20.0]])
        try:
            import fast
            fast.require(0, "points_inside")
        except Exception:
            self.skipTest("points_inside unavailable")
        signs = citylimits.place(Frame, rows, [], [("Big", big), ("Small", small)], elev, BOX)
        # Per travel direction at a chainage: one name (smaller ring). Two-way → begin+end ok.
        by_bucket = {}
        for s in signs:
            travel = np.asarray(s["travel"][:2], float)
            key = (round(s["centre"][0] / 20.0) * 20, 1 if travel[0] >= 0 else -1)
            by_bucket.setdefault(key, []).append(s["name"])
        for names in by_bucket.values():
            self.assertEqual(len(set(names)), 1, names)
        self.assertTrue(any(s["name"] == "Small" for s in signs))
        self.assertFalse(any(s["name"] == "Big" for s in signs))

    def test_same_name_begin_not_duplicated(self):
        """Parallel ways / double rings must not plant two E-17a for the same place."""
        # Two centre-lines ~50 m apart, both crossing the same settlement edge.
        rows = [
            ("a", SPEC, np.c_[np.linspace(0.0, 200.0, 41), np.zeros(41)], 4.0),
            ("b", SPEC, np.c_[np.linspace(0.0, 200.0, 41), np.full(41, 50.0)], 4.0),
        ]
        ring = np.array([[100.0, -20.0], [180.0, -20.0], [180.0, 70.0], [100.0, 70.0], [100.0, -20.0]])
        try:
            import fast
            fast.require(0, "points_inside")
        except Exception:
            self.skipTest("points_inside unavailable")
        signs = citylimits.place(Frame, rows, [], [("Szpitalna Wies", ring)], elev, BOX)
        begins = [s for s in signs
                  if int(s["kind"]) == citylimits.PLACE_BEGIN and s["name"] == "Szpitalna Wies"]
        # One begin per travel direction, not one per carriageway.
        by_dir = {}
        for s in begins:
            key = 1 if s["travel"][0] >= 0 else -1
            by_dir.setdefault(key, []).append(s)
        for group in by_dir.values():
            self.assertEqual(len(group), 1, group)

    def test_adjacent_localities_stack(self):
        """Enter A + leave B at the same crossing → one pole, E-17a above E-18a (Street View)."""
        rows = [("a", SPEC, line(200.0), 4.0)]
        # A occupies x>=100; B occupies x<100 — shared boundary at x=100.
        a = np.array([[100.0, -40.0], [180.0, -40.0], [180.0, 40.0], [100.0, 40.0], [100.0, -40.0]])
        b = np.array([[20.0, -40.0], [100.0, -40.0], [100.0, 40.0], [20.0, 40.0], [20.0, -40.0]])
        try:
            import fast
            fast.require(0, "points_inside")
        except Exception:
            self.skipTest("points_inside unavailable")
        signs = citylimits.place(Frame, rows, [], [("Sztum", a), ("Czernin", b)], elev, BOX)
        stacked = [s for s in signs if s.get("leave_name")]
        self.assertTrue(stacked, signs)
        names = {(s["name"], s["leave_name"]) for s in stacked}
        # Both directions: entering Sztum / leaving Czernin, and the reverse.
        self.assertIn(("Sztum", "Czernin"), names)
        self.assertIn(("Czernin", "Sztum"), names)

    def test_mapped_keeps_neighbour_synthetic(self):
        """Mapped Sztum must not wipe synthetic Czernin at the shared boundary."""
        rows = [("a", SPEC, line(200.0), 4.0)]
        a = np.array([[100.0, -40.0], [180.0, -40.0], [180.0, 40.0], [100.0, 40.0], [100.0, -40.0]])
        b = np.array([[20.0, -40.0], [100.0, -40.0], [100.0, 40.0], [20.0, 40.0], [20.0, -40.0]])
        mapped = [dict(lat=0.0, lon=100.0, osm_id=1, kind=citylimits.PLACE_BEGIN,
                       name="Sztum", dir_deg=float("nan"))]
        try:
            import fast
            fast.require(0, "points_inside")
        except Exception:
            self.skipTest("points_inside unavailable")
        signs = citylimits.place(Frame, rows, mapped, [("Sztum", a), ("Czernin", b)], elev, BOX)
        self.assertTrue(any(s.get("leave_name") == "Czernin" for s in signs), signs)
        self.assertTrue(any(s.get("name") == "Czernin" for s in signs), signs)


class MeshTest(unittest.TestCase):
    def test_place_and_built_meshes(self):
        signs = [
            dict(centre=[10.0, 0.0, 4.0], travel=[1.0, 0.0], kind=citylimits.PLACE_BEGIN, name="Opole"),
            dict(centre=[20.0, 0.0, 4.0], travel=[1.0, 0.0], kind=citylimits.PLACE_END, name="Opole"),
            dict(centre=[30.0, 0.0, 4.0], travel=[1.0, 0.0], kind=citylimits.BUILT_BEGIN, name=""),
            dict(centre=[40.0, 0.0, 4.0], travel=[1.0, 0.0], kind=citylimits.BUILT_END, name=""),
            dict(centre=[50.0, 0.0, 4.0], travel=[1.0, 0.0], kind=citylimits.PLACE_BEGIN,
                 name="Sztum", leave_name="Czernin"),
        ]
        mesh = citylimits.build(signs, (0.0, 0.0, 0.0))
        self.assertIsNotNone(mesh)
        p, t, uv, mats, mi = mesh
        self.assertEqual(mats, citylimits.MATERIALS)
        self.assertGreater(len(p), 0)
        self.assertEqual(len(t), len(mi))


class SignstreamTest(unittest.TestCase):
    def test_shape_file_announced(self):
        mesh = citylimits.build([
            dict(centre=[10.0, 0.0, 4.0], travel=[1.0, 0.0], kind=citylimits.PLACE_BEGIN, name="Lublin"),
        ], (0.0, 0.0, 0.0))
        with tempfile.TemporaryDirectory() as tmp:
            path, n = signstream.write_tile(
                tmp, "/session", 3, 0, 0,
                (None, None, None, None, None, None, mesh))
            data = json.loads((Path(tmp) / Path(path).name).read_text(encoding="utf-8"))
            self.assertIn("citylimits", data)
            self.assertTrue((Path(tmp) / "citylimits_3_0_0.cdae").is_file())
            self.assertEqual(signstream.shape_files(3, 0, 0)["citylimits"], "citylimits_3_0_0.cdae")
            self.assertGreater(n, 0)

    def test_build_tile_country_gate(self):
        citylimits.set_index_for_tests([])
        self.assertIsNone(citylimits.build_tile(None, Frame, 0, 0, elev, None, country="SE"))
        self.assertIsNone(citylimits.build_tile(None, Frame, 0, 0, elev, None, country="PL", rings=[]))
        citylimits.set_index_for_tests(None)


class WantedTest(unittest.TestCase):
    def test_motorway_and_link_rejected(self):
        self.assertFalse(citylimits.wanted(SPEC._replace(cls="motorway")))
        self.assertFalse(citylimits.wanted(SPEC._replace(link=True)))
        self.assertTrue(citylimits.wanted(SPEC))


class LocalityRingsTest(unittest.TestCase):
    def test_layercake_rings_converted_to_enu(self):
        class FakeBounds:
            def rings(self, bounds, kind="demand"):
                # lon/lat square around origin of the identity Frame (enu x=lon, y=lat)
                ring = np.array([[10.0, -1.0], [30.0, -1.0], [30.0, 1.0], [10.0, 1.0], [10.0, -1.0]])
                return [("Cygusy", ring)]

        md = type("MD", (), {"placebounds": FakeBounds(), "overture": None})()
        got = citylimits._locality_rings(md, Frame, BOX)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], "Cygusy")
        self.assertGreater(len(got[0][1]), 3)


if __name__ == "__main__":
    unittest.main()
