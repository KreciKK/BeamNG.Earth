"""routing: the GPS's whole-route planning (OSRM and Valhalla response handling, simplification)."""
import json
import sys
import unittest
import urllib.parse
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import netfetch  # noqa: E402
import routing  # noqa: E402


def encode_polyline(points, precision=6):
    """The inverse of routing.decode_polyline, for fixtures."""
    factor = 10 ** precision
    out, prev = [], [0, 0]
    for lat, lon in points:
        cur = [int(round(lat * factor)), int(round(lon * factor))]
        for i in range(2):
            delta = cur[i] - prev[i]
            prev[i] = cur[i]
            delta = ~(delta << 1) if delta < 0 else delta << 1
            while delta >= 0x20:
                out.append(chr((0x20 | (delta & 0x1f)) + 63))
                delta >>= 5
            out.append(chr(delta + 63))
    return "".join(out)


class RoutingTest(unittest.TestCase):
    def test_simplify_keeps_corners(self):
        line = [(39.0, -76.0 + i * 1e-5) for i in range(50)] + [(39.0 + i * 1e-5, -76.0 + 49e-5) for i in range(1, 50)]
        s = routing.simplify(line)
        self.assertEqual(len(s), 3, "a straight run, a corner, a straight run")
        self.assertEqual(s[0], line[0])
        self.assertEqual(s[-1], line[-1])

    def test_parse(self):
        data = {"code": "Ok", "routes": [{"distance": 1234.5, "duration": 99.0,
                                         "geometry": {"coordinates": [[-76.59, 39.28], [-76.60, 39.28], [-76.60, 39.29]]}}]}
        r = routing.parse(data)
        self.assertEqual(r["points"][0], [39.28, -76.59])
        self.assertEqual((r["dist"], r["dur"]), (1234.5, 99.0))
        with self.assertRaises(ValueError):
            routing.parse({"code": "NoRoute", "routes": []})

    def test_resample_even_steps(self):
        line = [(39.0, -76.0), (39.0, -75.9)]              # ~8.6 km east, one straight segment
        r = routing.resample(line, max_points=20)
        self.assertEqual(r[0][2], 0.0)
        self.assertAlmostEqual(r[-1][0], line[-1][0], places=6)
        self.assertAlmostEqual(r[-1][1], line[-1][1], places=6)
        for a, b in zip(r, r[1:]):
            self.assertGreater(b[2], a[2])

    def test_resample_short_line_one_point(self):
        self.assertEqual(routing.resample([(39.0, -76.0)]), [(39.0, -76.0, 0.0)])
        self.assertEqual(routing.resample([]), [])

    def test_url(self):
        u = routing.url(39.28, -76.59, 38.9, -77.03)
        self.assertIn("-76.590000,39.280000;-77.030000,38.900000", u)
        self.assertTrue(u.startswith("https://routing.openstreetmap.de/"))
        self.assertNotIn("bearings", u)
        self.assertNotIn("exclude", u)
        self.assertTrue(routing.url(39.28, -76.59, 38.9, -77.03, 370).endswith("&bearings=10,60;"))
        self.assertIsNone(routing.exclude_class("primary"))
        self.assertIsNone(routing.exclude_class("off"))
        self.assertEqual(routing.exclude_class("Motorway"), "motorway")

    def test_polyline6_roundtrip(self):
        raw = [(50.0647, 19.945), (50.07, -19.95), (39.28, -76.59)]
        got = routing.decode_polyline(encode_polyline(raw))
        self.assertEqual(len(got), len(raw))
        for a, b in zip(raw, got):
            self.assertAlmostEqual(a[0], b[0], places=5)
            self.assertAlmostEqual(a[1], b[1], places=5)

    def test_avoid_motorway_asks_valhalla(self):
        raw = [(50.0647, 19.945), (50.1, 19.9), (50.12, 19.85)]
        payload = {"trip": {"summary": {"length": 12.5, "time": 900},
                             "legs": [{"shape": encode_polyline(raw)}]}}

        class Net:
            def __init__(self):
                self.urls = []

            def get(self, url, kind=None):
                self.urls.append(url)
                return type("R", (), {"body": json.dumps(payload).encode()})()

        net = Net()
        r = routing.plan(50.0647, 19.945, 50.12, 19.85, net=net, bearing=370, exclude="motorway")
        self.assertEqual(len(net.urls), 1)
        asked = json.loads(urllib.parse.unquote(net.urls[0].split("json=", 1)[1]))
        self.assertTrue(net.urls[0].startswith("https://valhalla1.openstreetmap.de/route?"))
        self.assertEqual(asked["costing_options"]["auto"], {"use_highways": 0})
        self.assertEqual(asked["locations"][0]["heading"], 10)
        self.assertEqual((r["dist"], r["dur"]), (12500.0, 900.0))
        self.assertEqual(r["points"][0], [50.0647, 19.945])
        self.assertEqual(r["points"][-1], [50.12, 19.85])

    def test_avoid_retries_without_heading_on_400(self):
        raw = [(50.0, 19.0), (50.1, 19.1)]
        payload = {"trip": {"summary": {"length": 8.0, "time": 400},
                             "legs": [{"shape": encode_polyline(raw)}]}}

        class Net:
            def __init__(self):
                self.urls = []

            def get(self, url, kind=None):
                self.urls.append(url)
                if len(self.urls) == 1:
                    raise netfetch.FetchFailed("heading", url, "valhalla1.openstreetmap.de", status=400)
                return type("R", (), {"body": json.dumps(payload).encode()})()

        net = Net()
        r = routing.plan(50.0, 19.0, 50.1, 19.1, net=net, bearing=90, exclude="toll")
        self.assertEqual(len(net.urls), 2)
        first = json.loads(urllib.parse.unquote(net.urls[0].split("json=", 1)[1]))
        second = json.loads(urllib.parse.unquote(net.urls[1].split("json=", 1)[1]))
        self.assertEqual(first["locations"][0]["heading"], 90)
        self.assertNotIn("heading", second["locations"][0])
        self.assertEqual(second["costing_options"]["auto"], {"use_tolls": 0})
        self.assertEqual(r["dist"], 8000.0)
        with self.assertRaises(netfetch.FetchFailed):
            routing.plan(50.0, 19.0, 50.1, 19.1, net=type("Down", (), {
                "get": staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                    netfetch.FetchFailed("down", "", "h", status=500)))})(), bearing=90, exclude="ferry")


if __name__ == "__main__":
    unittest.main()


class RouteProfileSmoothTest(unittest.TestCase):
    def test_spikes_of_a_sample_or_two_go_the_climb_stays(self):
        # (the ground under a bridge, an embankment's bump: the map's chart drew them as peaks, the user 2026-10-27)
        import earthd
        climb = np.linspace(10.0, 40.0, 40)
        z = climb.copy()
        z[10] += 15.0           # a bump one sample long
        z[25:27] -= 12.0        # a valley under a bridge, two samples
        got = earthd.route_profile_smooth(z)
        self.assertLess(float(np.abs(got - climb)[2:-2].max()), 2.0)     # (of 12-15 m spikes)
        self.assertEqual(len(got), len(z))
        self.assertTrue(np.allclose(earthd.route_profile_smooth([5.0, 6.0]), [5.0, 6.0]))
