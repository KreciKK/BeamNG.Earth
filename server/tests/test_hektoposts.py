"""Polish U-1 hectometre posts (hektoposts.py): placement, mesh and signstream wiring."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hektoposts  # noqa: E402
import roads  # noqa: E402
import signstream  # noqa: E402


class Frame:
    enu_to_geodetic = staticmethod(lambda x, y, z: (np.asarray(y, float), np.asarray(x, float), z))
    geodetic_to_enu = staticmethod(lambda la, lo, h: (np.asarray(lo, float), np.asarray(la, float),
                                                       np.asarray(h, float)))


BOX = (-50.0, -50.0, 1200.0, 50.0)
SPEC = roads.Spec(9.0, "paved", False, "primary", False)


def line(n=1000.0):
    return np.c_[np.linspace(0.0, n, 51), np.zeros(51)]


def elev(la, lo):
    return np.asarray(la, float) * 0.0 + 5.0


def place(rows=None, routes=None, box=BOX, decks=(), clear=None, urban_fn=None):
    rows = rows if rows is not None else [("a", SPEC, line(), 4.5)]
    return hektoposts.place(Frame, rows, routes or {}, elev, box, decks, clear or (), urban_fn)


class FormatTest(unittest.TestCase):
    def test_route_prefixes(self):
        self.assertEqual(hektoposts.format_ref("pl:motorway", "2"), "A2")
        self.assertEqual(hektoposts.format_ref("pl:expressway", "S8"), "S8")
        self.assertEqual(hektoposts.format_ref("pl:national", "DK7"), "7")
        self.assertEqual(hektoposts.format_ref("pl:regional", "123"), "123")

    def test_route_label_picks_best_network(self):
        routes = [("e-road", "E77", 0.0, 1.0), ("pl:national", "7", 0.0, 1.0)]
        self.assertEqual(hektoposts.route_label(routes), "7")


class PlaceTest(unittest.TestCase):
    def test_both_sides_every_100_m(self):
        signs = place()
        self.assertEqual(len(signs), 20)  # 0..900 both sides (end exclusive, like kantstolpar)
        ys = sorted({round(s["centre"][1], 3) for s in signs})
        self.assertEqual(ys, [-5.7, 5.7])  # half 4.5 + OUT_M 1.2
        xs = sorted({round(s["centre"][0], 3) for s in signs if s["centre"][1] > 0})
        self.assertEqual(xs, [float(i) for i in range(0, 1000, 100)])

    def test_chainage_km_hm(self):
        signs = place()
        at_400 = [s for s in signs if abs(s["centre"][0] - 400.0) < 1e-6]
        self.assertTrue(at_400)
        self.assertEqual({(s["km"], s["hm"]) for s in at_400}, {(0, 4)})
        at_1200 = place(rows=[("a", SPEC, line(2500.0), 4.5)],
                        box=(-50.0, -50.0, 2600.0, 50.0))
        hit = [s for s in at_1200 if abs(s["centre"][0] - 1200.0) < 1e-6]
        self.assertEqual({(s["km"], s["hm"]) for s in hit}, {(1, 2)})

    def test_ref_only_on_hectometre_zero(self):
        routes = {"a": [("pl:national", "44", 0.0, 1.0)]}
        signs = place(routes=routes)
        zeros = [s for s in signs if s["hm"] == 0]
        others = [s for s in signs if s["hm"] != 0]
        self.assertTrue(zeros and all(s["ref"] == "44" for s in zeros))
        self.assertTrue(others and all(s["ref"] == "" for s in others))

    def test_urban_and_link_and_wrong_class_skipped_via_wanted(self):
        self.assertFalse(hektoposts.wanted(SPEC._replace(urban=True)))
        self.assertFalse(hektoposts.wanted(SPEC._replace(link=True)))
        self.assertFalse(hektoposts.wanted(SPEC._replace(cls="tertiary")))
        self.assertTrue(hektoposts.wanted(SPEC._replace(cls="secondary")))

    def test_sidewalks_mean_town_no_posts(self):
        # mapped sidewalks (sides) → town street even if urban land-cover is false
        town = SPEC._replace(sides=(1, -1))
        self.assertFalse(hektoposts.wanted(town))
        self.assertEqual(place(rows=[("a", town, line(), 4.5)]), [])

    def test_built_up_point_skips_post(self):
        def urban_fn(la, lo):
            # built-up only near x≈400 in our Frame (enu x -> lon)
            return np.asarray(lo, float) > 350.0
        signs = place(urban_fn=urban_fn)
        self.assertTrue(signs)
        self.assertTrue(all(s["centre"][0] <= 350.0 for s in signs))

    def test_no_posts_on_median_between_carriageways(self):
        sp = roads.Spec(4.5, "paved", False, "trunk", True, 2.5, 1)
        a = np.array([[0.0, -500.0], [0.0, 500.0]])
        b = np.array([[6.0, 500.0], [6.0, -500.0]])
        rows = [("a", sp, a, 2.25), ("b", sp._replace(dir=1), b, 2.25)]
        signs = place(rows=rows, clear=rows, box=(-128.0, -128.0, 128.0, 128.0))
        xs = sorted({round(s["centre"][0], 2) for s in signs})
        self.assertEqual(xs, [-3.45, 9.45])  # OUT_M=1.2

    def test_crossing_street_clears_junction(self):
        cross = roads.Spec(6.0, "paved", False, "residential", False)
        side = np.c_[np.full(2, 500.0), [-80.0, 80.0]]
        host = [("a", SPEC, line(), 4.5)]
        signs = place(rows=host, clear=host + [("b", cross, side, 3.0)])
        self.assertTrue(all(abs(s["centre"][0] - 500.0) > 10.0 for s in signs))

    def test_deck_clears_posts(self):
        deck = {"deck": True, "n": np.array([[500.0, -20.0], [500.0, 20.0]]), "w": 12.0}
        signs = place(decks=[deck])
        self.assertTrue(all(abs(s["centre"][0] - 500.0) > 5.0 for s in signs))


class MeshTest(unittest.TestCase):
    def sign(self, **kw):
        base = dict(centre=[0.0, 0.0, 5.0], travel=[1.0, 0.0], ref="7", km=12, hm=0, height=1.0)
        base.update(kw)
        return base

    def test_mesh_has_geometry_and_materials(self):
        mesh = hektoposts.build([self.sign()], (0.0, 0.0, 0.0))
        self.assertIsNotNone(mesh)
        p, t, uv, mats, mi = mesh
        self.assertTrue(np.isfinite(p).all())
        self.assertEqual(uv.shape, (len(p), 2))
        self.assertEqual(mats, hektoposts.MATERIALS)
        self.assertTrue(set(mi).issubset(set(range(len(mats)))))
        self.assertIn("bne_post_red", mats)
        self.assertIn("bne_hecto_plate", mats)
        self.assertIn("bne_guide_letters_black", mats)

    def test_digits_drawn(self):
        mesh = hektoposts.Mesh((0.0, 0.0, 0.0))
        with mock.patch.object(mesh, "text_top", wraps=mesh.text_top) as text:
            mesh.post(self.sign(ref="44", km=101, hm=0))
        self.assertEqual([c.args[0] for c in text.call_args_list], ["44", "101", "0"])
        tops = [c.args[1] for c in text.call_args_list]
        self.assertGreater(tops[0], hektoposts.RED_HI)
        self.assertLess(tops[1], hektoposts.RED_LO - hektoposts.GAP_RED_M + 1e-9)
        self.assertLess(tops[2], tops[1] - hektoposts.GAP_DIGITS_M)
        self.assertGreater(tops[2], 0.55, "hm stays in the upper white face, not near the ground")

    def test_digit_quads_stay_on_lower_white_face(self):
        mesh = hektoposts.Mesh((0.0, 0.0, 0.0))
        mesh.post(self.sign(ref="", km=0, hm=1))
        p, t, _, mats, mi = mesh.result()
        letters = mats.index("bne_guide_letters_black")
        verts = t[mi == letters].ravel()
        z = p[verts, 2] - 5.0
        self.assertTrue(len(z))
        self.assertTrue((z < hektoposts.RED_LO - 0.005).all(), "digits must sit below the red band")
        self.assertGreater(float(z.min()), 0.50)
        plate = mats.index("bne_hecto_plate")
        self.assertTrue((mi == plate).any(), "hm sits on a grey sticker plate")
        # Plate must not climb into the km band (photo: km is free on white, plate only around hm).
        pz = p[t[mi == plate].ravel(), 2] - 5.0
        km_bottom = hektoposts.RED_LO - hektoposts.GAP_RED_M - hektoposts.KM_H
        self.assertTrue((pz.max() <= km_bottom - hektoposts.GAP_DIGITS_M + 0.01))


class SignstreamTest(unittest.TestCase):
    def test_shape_file_announced(self):
        mesh = hektoposts.build([dict(centre=[10.0, 0.0, 5.0], travel=[1.0, 0.0],
                                      ref="7", km=3, hm=0, height=1.0)], (0.0, 0.0, 0.0))
        with tempfile.TemporaryDirectory() as tmp:
            path, n = signstream.write_tile(tmp, "/session", 2, 0, 0,
                                            (None, None, None, None, None, mesh, None))
            data = json.loads((Path(tmp) / Path(path).name).read_text(encoding="utf-8"))
            self.assertIn("hektoposts", data)
            self.assertTrue((Path(tmp) / "hektoposts_2_0_0.cdae").is_file())
            self.assertEqual(signstream.shape_files(2, 0, 0)["hektoposts"], "hektoposts_2_0_0.cdae")
            self.assertGreater(n, 0)

    def test_build_tile_country_gate(self):
        self.assertIsNone(hektoposts.build_tile(None, Frame, 0, 0, elev, None, country="SE"))
        self.assertIsNone(hektoposts.build_tile(None, Frame, 0, 0, elev, None, country="PL"))


if __name__ == "__main__":
    unittest.main()
