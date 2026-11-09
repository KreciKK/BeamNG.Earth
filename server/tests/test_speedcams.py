"""Speed cameras (speedcams.py): OSM tags, enforcement relations, D-51 distance, mesh."""
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import roads  # noqa: E402
import signstream  # noqa: E402
import speedcams  # noqa: E402


def ang(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


def north_way(length_m=1000.0, step=50.0, highway="primary", maxspeed=50.0, oneway=0,
              ref="", lat0=52.0, lon0=21.0, start_id=1000):
    n = int(round(length_m / step))
    coords, ids = [], []
    for i in range(n + 1):
        coords.append(speedcams.offset_m(lat0, lon0, 0.0, i * step))
        ids.append(start_id + i)
    return dict(id=1, node_ids=ids, coords=coords, oneway=oneway, highway=highway,
                maxspeed=float(maxspeed), ref=ref)


def cam(way, index, **tags):
    tags = {"highway": "speed_camera", **tags}
    return dict(osm_id=way["node_ids"][index], lat=way["coords"][index][0],
                lon=way["coords"][index][1], tags=tags)


class Frame:
    enu_to_geodetic = staticmethod(lambda x, y, z: (np.asarray(y, float), np.asarray(x, float), z))
    geodetic_to_enu = staticmethod(lambda la, lo, h: (np.asarray(lo, float), np.asarray(la, float),
                                                       np.asarray(h, float)))


def elev(la, lo):
    return np.asarray(la, float) * 0.0 + 3.0


class ParseTest(unittest.TestCase):
    def test_degrees_cardinal_and_both(self):
        got = speedcams.parse_camera_tags({"highway": "speed_camera", "direction": "90", "maxspeed": "50"})
        self.assertEqual(got["kind"], speedcams.DEVICE_FIXED)
        self.assertEqual(got["mode"], "deg")
        self.assertAlmostEqual(got["degs"][0], 90.0)
        self.assertAlmostEqual(got["maxspeed_kmh"], 50.0)
        mode, degs = speedcams.parse_direction("N;S")
        self.assertEqual(mode, "both")
        self.assertAlmostEqual(degs[0], 0.0)
        self.assertAlmostEqual(degs[1], 180.0)
        mode, degs = speedcams.parse_direction("90;270")
        self.assertEqual(mode, "both")

    def test_mph_and_non_numeric_limit(self):
        got = speedcams.parse_maxspeed("30 mph")
        self.assertAlmostEqual(got, 30 * 1.609344, places=3)
        self.assertTrue(math.isnan(speedcams.parse_maxspeed("RU:urban")))
        self.assertTrue(math.isnan(speedcams.parse_maxspeed("signals")))
        self.assertAlmostEqual(speedcams.parse_maxspeed("60 @ (07:00-19:00)"), 60.0)

    def test_red_light_and_speed_display_refused(self):
        self.assertIsNone(speedcams.parse_camera_tags(
            {"highway": "speed_camera", "enforcement": "traffic_signals"}))
        self.assertIsNone(speedcams.parse_camera_tags({"highway": "speed_display"}))
        self.assertIsNone(speedcams.parse_enforcement(
            {"type": "enforcement", "enforcement": "traffic_signals"},
            [("n", 1, "device")]))

    def test_sign_tokens_do_not_prefix_match(self):
        self.assertEqual(speedcams.parse_sign_tags({"traffic_sign": "PL:D-51"})["kind"], speedcams.SIGN_D51)
        self.assertEqual(speedcams.parse_sign_tags({"traffic_sign": "PL:D-51a"})["kind"], speedcams.SIGN_D51A)
        self.assertEqual(speedcams.parse_sign_tags({"traffic_sign": "PL:D-51b"})["kind"], speedcams.SIGN_D51B)
        self.assertIsNone(speedcams.parse_sign_tags({"traffic_sign": "PL:D-42"}))

    def test_gantry_mount(self):
        got = speedcams.parse_camera_tags({"highway": "speed_camera", "man_made": "gantry"})
        self.assertEqual(got["mount"], speedcams.MOUNT_GANTRY)

    def test_warn_bands(self):
        self.assertEqual(speedcams.warn_distance_m(50), 150.0)
        self.assertEqual(speedcams.warn_distance_m(30 * 1.609344), 150.0)
        self.assertEqual(speedcams.warn_distance_m(90), 350.0)
        self.assertEqual(speedcams.warn_distance_m(140, expressway=True), 600.0)
        self.assertEqual(speedcams.warn_distance_m(float("nan")), 150.0)
        self.assertTrue(speedcams.expressway("motorway"))
        self.assertTrue(speedcams.expressway("trunk", "S8"))
        self.assertFalse(speedcams.expressway("trunk", "DK8"))


class AssembleTest(unittest.TestCase):
    def test_forward_backward_and_side(self):
        way = north_way()
        mid = len(way["node_ids"]) // 2
        forward = speedcams.assemble([cam(way, mid, direction="forward", maxspeed="50")], [], [], [way])
        dev = next(r for r in forward if r["kind"] == speedcams.DEVICE_FIXED)
        self.assertLess(ang(dev["dir_deg"], 0.0), 5.0)
        back = speedcams.assemble([cam(way, mid, direction="backward")], [], [], [way])
        dev = next(r for r in back if r["kind"] == speedcams.DEVICE_FIXED)
        self.assertLess(ang(dev["dir_deg"], 180.0), 5.0)
        lat, lon = speedcams.offset_m(way["coords"][mid][0], way["coords"][mid][1], 8.0, 0.0)
        beside = dict(osm_id=9, lat=lat, lon=lon, tags={"highway": "speed_camera"})
        got = speedcams.assemble([beside], [], [], [way])
        dev = next(r for r in got if r["kind"] == speedcams.DEVICE_FIXED)
        self.assertLess(ang(dev["dir_deg"], 0.0), 8.0)
        # No maxspeed on the node: the way's posted limit is stored for the trap.
        self.assertAlmostEqual(dev["maxspeed_kmh"], 50.0)

    def test_both_places_two_signs(self):
        way = north_way(length_m=800.0, step=40.0)
        mid = len(way["node_ids"]) // 2
        rows = speedcams.assemble([cam(way, mid, direction="both", maxspeed="50")], [], [], [way])
        signs = [r for r in rows if r["kind"] == speedcams.SIGN_D51]
        self.assertEqual(len(signs), 2)
        dev = next(r for r in rows if r["kind"] == speedcams.DEVICE_FIXED)
        for sign in signs:
            self.assertGreater(speedcams.distance_m(dev["lat"], dev["lon"], sign["lat"], sign["lon"]), 120.0)
            self.assertLess(speedcams.distance_m(dev["lat"], dev["lon"], sign["lat"], sign["lon"]), 180.0)

    def test_relation_bearing_overrides_node_and_distance_follows_limit(self):
        way = north_way(length_m=1200.0, step=40.0, maxspeed=70.0)
        mid = len(way["node_ids"]) // 2
        frm, to = 4, mid + 4
        rel = dict(tags={"type": "enforcement", "enforcement": "maxspeed", "maxspeed": "70"},
                   members=[("n", way["node_ids"][mid], "device"),
                            ("n", way["node_ids"][frm], "from"),
                            ("n", way["node_ids"][to], "to")])
        # The node claims 50 km/h facing east; the relation measures 70 northbound.
        rows = speedcams.assemble(
            [cam(way, mid, direction="90", maxspeed="50")], [], [rel], [way])
        dev = next(r for r in rows if r["kind"] == speedcams.DEVICE_FIXED)
        self.assertTrue(dev["from_relation"])
        self.assertAlmostEqual(dev["maxspeed_kmh"], 70.0)
        self.assertLess(ang(dev["dir_deg"], 0.0), 5.0)
        sign = next(r for r in rows if r["kind"] == speedcams.SIGN_D51)
        dist = speedcams.distance_m(dev["lat"], dev["lon"], sign["lat"], sign["lon"])
        self.assertGreater(dist, 320.0)
        self.assertLess(dist, 380.0)
        # Upstream is south of the camera.
        self.assertLess(sign["lat"], dev["lat"])

    def test_bands_150_350_600(self):
        for speed, highway, ref, low, high in (
            ("50", "residential", "", 120.0, 180.0),
            ("90", "primary", "", 320.0, 380.0),
            ("140", "motorway", "A2", 560.0, 640.0),
        ):
            way = north_way(length_m=1600.0, step=40.0, highway=highway, maxspeed=float(speed), ref=ref)
            mid = len(way["node_ids"]) // 2
            rows = speedcams.assemble([cam(way, mid, direction="forward", maxspeed=speed)], [], [], [way])
            dev = next(r for r in rows if r["kind"] == speedcams.DEVICE_FIXED)
            sign = next(r for r in rows if r["kind"] == speedcams.SIGN_D51)
            dist = speedcams.distance_m(dev["lat"], dev["lon"], sign["lat"], sign["lon"])
            self.assertGreater(dist, low, speed)
            self.assertLess(dist, high, speed)

    def test_short_approach_stops_at_the_way_end(self):
        way = north_way(length_m=80.0, step=20.0)
        rows = speedcams.assemble([cam(way, len(way["node_ids"]) - 1, direction="forward", maxspeed="50")],
                                  [], [], [way])
        dev = next(r for r in rows if r["kind"] == speedcams.DEVICE_FIXED)
        sign = next(r for r in rows if r["kind"] == speedcams.SIGN_D51)
        dist = speedcams.distance_m(dev["lat"], dev["lon"], sign["lat"], sign["lon"])
        self.assertLess(dist, 100.0)
        self.assertGreater(dist, 60.0)

    def test_mapped_d51_suppresses_synthetic_on_the_approach_only(self):
        way = north_way(length_m=1000.0, step=40.0)
        mid = len(way["node_ids"]) // 2
        dev_ll = way["coords"][mid]
        upstream = speedcams.offset_m(dev_ll[0], dev_ll[1], 0.0, -200.0)
        aside = speedcams.offset_m(dev_ll[0], dev_ll[1], 40.0, 0.0)
        mapped = [dict(osm_id=7, lat=upstream[0], lon=upstream[1], tags={"traffic_sign": "PL:D-51"})]
        rows = speedcams.assemble([cam(way, mid, direction="forward", maxspeed="50")], mapped, [], [way])
        synth = [r for r in rows if r["kind"] == speedcams.SIGN_D51 and r["source"] == speedcams.SOURCE_SYNTH]
        self.assertEqual(synth, [])
        self.assertTrue(any(r["kind"] == speedcams.SIGN_D51 and r["source"] == speedcams.SOURCE_OSM for r in rows))
        mapped = [dict(osm_id=8, lat=aside[0], lon=aside[1], tags={"traffic_sign": "PL:D-51"})]
        rows = speedcams.assemble([cam(way, mid, direction="forward", maxspeed="50")], mapped, [], [way])
        synth = [r for r in rows if r["kind"] == speedcams.SIGN_D51 and r["source"] == speedcams.SOURCE_SYNTH]
        self.assertEqual(len(synth), 1)

    def test_average_section_is_end_signs_not_cameras(self):
        way = north_way(length_m=500.0, step=50.0)
        frm, to = way["node_ids"][0], way["node_ids"][-1]
        rel = dict(tags={"type": "enforcement", "enforcement": "average_speed", "maxspeed": "90"},
                   members=[("n", frm, "from"), ("n", to, "to")])
        rows = speedcams.assemble([], [], [rel], [way])
        self.assertEqual([r for r in rows if r["kind"] == speedcams.DEVICE_AVERAGE], [])
        a = next(r for r in rows if r["kind"] == speedcams.SIGN_D51A)
        b = next(r for r in rows if r["kind"] == speedcams.SIGN_D51B)
        self.assertEqual(a["osm_id"], frm)
        self.assertEqual(b["osm_id"], to)
        self.assertLess(a["lat"], b["lat"])
        self.assertAlmostEqual(a["maxspeed_kmh"], 90.0)
        self.assertAlmostEqual(a["dir_deg"], 0.0, delta=5.0)
        # A mapped D-51a on the start replaces the synthetic board and inherits the limit.
        start = way["coords"][0]
        mapped = [dict(osm_id=3, lat=start[0], lon=start[1],
                       tags={"traffic_sign": "PL:D-51a", "direction": "180"})]
        rows = speedcams.assemble([], mapped, [rel], [way])
        synth_a = [r for r in rows if r["kind"] == speedcams.SIGN_D51A and r["source"] == speedcams.SOURCE_SYNTH]
        self.assertEqual(synth_a, [])
        mapped_a = next(r for r in rows if r["kind"] == speedcams.SIGN_D51A and r["osm_id"] == 3)
        self.assertAlmostEqual(mapped_a["maxspeed_kmh"], 90.0)
        self.assertAlmostEqual(mapped_a["dir_deg"], 0.0, delta=5.0)

    def test_red_light_relation_adds_nothing(self):
        way = north_way(length_m=100.0, step=20.0)
        rel = dict(tags={"type": "enforcement", "enforcement": "traffic_signals"},
                   members=[("n", way["node_ids"][0], "device")])
        self.assertEqual(speedcams.assemble([], [], [rel], [way]), [])


class PlaceMeshTest(unittest.TestCase):
    def tearDown(self):
        speedcams.set_index_for_tests(None)

    def test_average_device_next_to_d51a_is_not_drawn(self):
        spec = roads.Spec(8.0, "paved", False, "primary", False)
        elev = lambda x, y: 3.0
        box = (-20.0, -20.0, 80.0, 20.0)
        rows = [("r1", spec, np.array([[0.0, 0.0], [60.0, 0.0]]), 4.0)]
        Frame = type("F", (), {
            "geodetic_to_enu": staticmethod(lambda la, lo, h: (np.asarray(lo, float), np.asarray(la, float), h)),
            "enu_to_geodetic": staticmethod(lambda x, y, z: (np.asarray(y, float), np.asarray(x, float), z)),
        })
        # eastbound average device on the centreline + D-51a at the same spot
        placed = speedcams.place(Frame, rows, [
            dict(lat=0.0, lon=10.0, osm_id=1, kind=speedcams.DEVICE_AVERAGE,
                 dir_deg=90.0, maxspeed_kmh=90.0, mount=0, source=0),
            dict(lat=0.0, lon=10.0, osm_id=2, kind=speedcams.SIGN_D51A,
                 dir_deg=270.0, maxspeed_kmh=90.0, mount=0, source=0),
        ], elev, box)
        kinds = [p["kind"] for p in placed]
        self.assertNotIn(speedcams.DEVICE_AVERAGE, kinds)
        self.assertIn(speedcams.SIGN_D51A, kinds)
        sign = next(p for p in placed if p["kind"] == speedcams.SIGN_D51A)
        # Travel is the device's 90° (east), so the right shoulder is -y.
        self.assertLess(sign["centre"][1], -4.0)

    def test_device_leaves_the_lane_and_sign_stands_on_the_shoulder(self):
        spec = roads.Spec(8.0, "paved", False, "primary", False)
        xy = np.c_[np.linspace(0.0, 200.0, 21), np.zeros(21)]
        rows = [("r", spec, xy, 4.0)]
        box = (0.0, -40.0, 220.0, 40.0)
        records = [
            dict(lat=0.0, lon=50.0, osm_id=1, kind=speedcams.DEVICE_FIXED, dir_deg=90.0,
                 maxspeed_kmh=50.0, mount=speedcams.MOUNT_POLE, source=speedcams.SOURCE_OSM),
            dict(lat=0.0, lon=120.0, osm_id=1, kind=speedcams.SIGN_D51, dir_deg=90.0,
                 maxspeed_kmh=float("nan"), mount=0, source=speedcams.SOURCE_SYNTH),
        ]
        placed = speedcams.place(Frame, rows, records, elev, box)
        self.assertEqual(len(placed), 2)
        device = next(p for p in placed if p["kind"] == speedcams.DEVICE_FIXED)
        sign = next(p for p in placed if p["kind"] == speedcams.SIGN_D51)
        # Eastbound right shoulder is -y.
        self.assertLess(device["centre"][1], -4.0)
        self.assertLess(sign["centre"][1], -4.0)
        self.assertAlmostEqual(device["centre"][2], 3.0, places=3)

    def test_mesh_and_signstream_field(self):
        mesh = speedcams.build([
            dict(centre=[10.0, 0.0, 3.0], travel=[1.0, 0.0], kind=speedcams.DEVICE_FIXED,
                 mount=speedcams.MOUNT_POLE, half=3.5),
            dict(centre=[30.0, -5.0, 3.0], travel=[1.0, 0.0], kind=speedcams.SIGN_D51,
                 mount=0, half=3.5),
            dict(centre=[40.0, -5.0, 3.0], travel=[1.0, 0.0], kind=speedcams.SIGN_D51A,
                 mount=0, half=3.5),
            dict(centre=[50.0, -5.0, 3.0], travel=[1.0, 0.0], kind=speedcams.SIGN_D51B,
                 mount=0, half=3.5),
        ], (0.0, 0.0, 0.0))
        self.assertIsNotNone(mesh)
        _p, _t, _uv, mats, mi = mesh
        self.assertEqual(mats, speedcams.MATERIALS)
        self.assertGreater(len(mi), 0)
        with tempfile.TemporaryDirectory() as tmp:
            path, n = signstream.write_tile(
                tmp, "/session", 4, 0, 0,
                (None, None, None, None, None, None, None, mesh))
            data = json.loads((Path(tmp) / Path(path).name).read_text(encoding="utf-8"))
            self.assertIn("speedcams", data)
            self.assertTrue((Path(tmp) / "speedcams_4_0_0.cdae").is_file())
            self.assertEqual(signstream.KINDS[-1], "speedcams")
            self.assertGreater(n, 0)

    def test_country_gate(self):
        speedcams.set_index_for_tests([])
        self.assertIsNone(speedcams.build_tile(None, Frame, 0, 0, elev, None, country="SE"))
        self.assertIsNone(speedcams.build_tile(None, Frame, 0, 0, elev, None, country="PL"))

    def test_near_filters_bbox(self):
        speedcams.set_index_for_tests([
            dict(lat=50.0, lon=19.0, osm_id=1, kind=speedcams.DEVICE_FIXED, dir_deg=0.0,
                 maxspeed_kmh=50.0, mount=0, source=0),
            dict(lat=52.0, lon=21.0, osm_id=2, kind=speedcams.SIGN_D51, dir_deg=0.0,
                 maxspeed_kmh=float("nan"), mount=0, source=1),
        ])
        hit = speedcams.near(49.0, 18.0, 51.0, 20.0)
        self.assertEqual([h["osm_id"] for h in hit], [1])

    def test_pairs_the_board_ahead(self):
        start = speedcams.offset_m(52.0, 21.0, 6.0, 0.0)
        end = speedcams.offset_m(52.0, 21.0, 6.0, 3000.0)
        behind = speedcams.offset_m(52.0, 21.0, 6.0, -30.0)
        rows = speedcams.pair_sections([
            dict(lat=52.0, lon=21.0, osm_id=1, kind=speedcams.DEVICE_AVERAGE,
                 dir_deg=0.0, maxspeed_kmh=140.0),
            dict(lat=end[0], lon=end[1], osm_id=2, kind=speedcams.DEVICE_AVERAGE,
                 dir_deg=0.0, maxspeed_kmh=140.0),
            dict(lat=start[0], lon=start[1], osm_id=10, kind=speedcams.SIGN_D51A,
                 dir_deg=float("nan"), maxspeed_kmh=float("nan")),
            dict(lat=end[0], lon=end[1], osm_id=11, kind=speedcams.SIGN_D51B,
                 dir_deg=float("nan"), maxspeed_kmh=float("nan")),
            dict(lat=behind[0], lon=behind[1], osm_id=12, kind=speedcams.SIGN_D51B,
                 dir_deg=float("nan"), maxspeed_kmh=float("nan")),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 10)
        self.assertAlmostEqual(rows[0]["length_m"], 3000.0, delta=40.0)
        self.assertAlmostEqual(rows[0]["dir_deg"], 0.0, delta=1.0)
        self.assertEqual(rows[0]["limit_kmh"], 140.0)
        self.assertAlmostEqual(rows[0]["b_lat"], end[0], places=4)

    def test_rejects_an_end_facing_the_other_way(self):
        end = speedcams.offset_m(52.0, 21.0, 0.0, 2000.0)
        rows = speedcams.pair_sections([
            dict(lat=52.0, lon=21.0, osm_id=1, kind=speedcams.DEVICE_AVERAGE,
                 dir_deg=0.0, maxspeed_kmh=120.0),
            dict(lat=end[0], lon=end[1], osm_id=2, kind=speedcams.DEVICE_AVERAGE,
                 dir_deg=180.0, maxspeed_kmh=120.0),
            dict(lat=52.0, lon=21.0, osm_id=10, kind=speedcams.SIGN_D51A,
                 dir_deg=float("nan"), maxspeed_kmh=float("nan")),
            dict(lat=end[0], lon=end[1], osm_id=11, kind=speedcams.SIGN_D51B,
                 dir_deg=float("nan"), maxspeed_kmh=float("nan")),
        ])
        self.assertEqual(rows, [])

    def test_two_sections_keep_their_own_ends(self):
        a2 = speedcams.offset_m(52.0, 21.0, 0.0, 3000.0)
        b2 = speedcams.offset_m(52.0, 21.0, 0.0, 6000.0)
        rows = speedcams.pair_sections([
            dict(lat=52.0, lon=21.0, osm_id=1, kind=speedcams.DEVICE_AVERAGE,
                 dir_deg=0.0, maxspeed_kmh=100.0),
            dict(lat=a2[0], lon=a2[1], osm_id=2, kind=speedcams.DEVICE_AVERAGE,
                 dir_deg=0.0, maxspeed_kmh=100.0),
            dict(lat=b2[0], lon=b2[1], osm_id=3, kind=speedcams.DEVICE_AVERAGE,
                 dir_deg=0.0, maxspeed_kmh=100.0),
            dict(lat=52.0, lon=21.0, osm_id=10, kind=speedcams.SIGN_D51A,
                 dir_deg=float("nan"), maxspeed_kmh=float("nan")),
            dict(lat=a2[0], lon=a2[1], osm_id=11, kind=speedcams.SIGN_D51B,
                 dir_deg=float("nan"), maxspeed_kmh=float("nan")),
            dict(lat=a2[0], lon=a2[1], osm_id=12, kind=speedcams.SIGN_D51A,
                 dir_deg=float("nan"), maxspeed_kmh=float("nan")),
            dict(lat=b2[0], lon=b2[1], osm_id=13, kind=speedcams.SIGN_D51B,
                 dir_deg=float("nan"), maxspeed_kmh=float("nan")),
        ])
        self.assertEqual(sorted(r["id"] for r in rows), [10, 12])
        by_id = {r["id"]: r["length_m"] for r in rows}
        self.assertAlmostEqual(by_id[10], 3000.0, delta=50.0)
        self.assertAlmostEqual(by_id[12], 3000.0, delta=50.0)

    def test_section_rows_omit_an_unknown_limit(self):
        end = speedcams.offset_m(52.0, 21.0, 0.0, 1500.0)
        speedcams.set_index_for_tests([
            dict(lat=52.0, lon=21.0, osm_id=1, kind=speedcams.DEVICE_AVERAGE,
                 dir_deg=0.0, maxspeed_kmh=float("nan"), mount=0, source=0),
            dict(lat=52.0, lon=21.0, osm_id=10, kind=speedcams.SIGN_D51A,
                 dir_deg=float("nan"), maxspeed_kmh=float("nan"), mount=0, source=0),
            dict(lat=end[0], lon=end[1], osm_id=11, kind=speedcams.SIGN_D51B,
                 dir_deg=float("nan"), maxspeed_kmh=float("nan"), mount=0, source=0),
        ])
        try:
            rows = speedcams.section_rows()
            self.assertEqual(len(rows), 1)
            self.assertNotIn("limit", rows[0])
            self.assertEqual(rows[0]["a"][0], 52.0)
            self.assertGreater(rows[0]["len"], 1400)
            self.assertIs(speedcams.section_rows(), rows)
        finally:
            speedcams.set_index_for_tests(None)

    def test_fixed_rows_need_a_bearing_and_a_limit(self):
        speedcams.set_index_for_tests([
            dict(lat=52.0, lon=21.0, osm_id=1, kind=speedcams.DEVICE_FIXED,
                 dir_deg=90.0, maxspeed_kmh=50.0, mount=0, source=0),
            dict(lat=52.1, lon=21.0, osm_id=2, kind=speedcams.DEVICE_FIXED,
                 dir_deg=float("nan"), maxspeed_kmh=50.0, mount=0, source=0),
            dict(lat=52.2, lon=21.0, osm_id=3, kind=speedcams.DEVICE_FIXED,
                 dir_deg=0.0, maxspeed_kmh=float("nan"), mount=0, source=0),
            dict(lat=53.0, lon=21.0, osm_id=4, kind=speedcams.DEVICE_AVERAGE,
                 dir_deg=0.0, maxspeed_kmh=70.0, mount=1, source=0),
            dict(lat=53.1, lon=21.0, osm_id=5, kind=speedcams.SIGN_D51,
                 dir_deg=0.0, maxspeed_kmh=50.0, mount=0, source=0),
        ])
        try:
            rows = speedcams.fixed_rows()
            self.assertEqual([r["id"] for r in rows], [1])
            self.assertEqual(rows[0]["at"], [52.0, 21.0])
            self.assertEqual(rows[0]["dir"], 90.0)
            self.assertEqual(rows[0]["limit"], 50.0)
            self.assertIs(speedcams.fixed_rows(), rows)
        finally:
            speedcams.set_index_for_tests(None)
