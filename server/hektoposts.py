"""Polish roadside hectometer posts (słupki prowadzące U-1 with U-7 km and U-8 hm).

Procedural per-tile mesh in the sign stream (country=PL): white/red posts every 100 m on rural
A/S/DK/DW-class roads, with route number, kilometre and hectometre digits. Chainage is arc length
along each carriageway (not GDDKiA inventory without OSM milestones).
"""
import logging

import numpy as np

import dirsigns
import exitsigns
import mapgeom
import motesfri
import roads
import signroads

log = logging.getLogger("earthd.hektoposts")

COUNTRIES = {"PL"}
CLASSES = {"motorway", "trunk", "primary", "secondary"}
NETWORKS = ("pl:motorway", "pl:expressway", "pl:national", "pl:regional")
SPACING_M = 100.0
OUT_M = 1.2
# U-1a proportions from a roadside photo: short white cap, thick red band, km+hm stacked
# just under the red, long empty white shaft to the ground.
HEIGHT_M = 1.00
POST_W, POST_D = 0.11, 0.07
RED_LO, RED_HI = 0.78, 0.94          # thick red near the top; ~6 cm white cap above
REF_H, KM_H, HM_H = 0.036, 0.042, 0.102   # m: U-7 42 mm, U-8 102 mm
REF_TOP = (RED_HI + HEIGHT_M) * 0.5 + REF_H * 0.35
GAP_RED_M = 0.020                    # white air under the red before km
GAP_DIGITS_M = 0.022                 # white air between km and the hm plate
PLATE_PAD = 0.008                    # grey sticker margin around the hm digit only
PAVEMENT_M = 0.8
CLEAR_DECK_M = 0.8
# white body, red band, grey hm plate, black lettering
MATERIALS = ["bne_post_white", "bne_post_red", "bne_hecto_plate", "bne_guide_letters_black"]
MAT_WHITE, MAT_RED, MAT_PLATE, MAT_LETTERS = 0, 1, 2, 3


def wanted(spec):
    """Rural A/S/DK/DW only: no town streets (urban land cover or sidewalks)."""
    if (spec is None or getattr(spec, "kind", "paved") not in (None, "paved")
            or spec.cls not in CLASSES or getattr(spec, "link", False)
            or getattr(spec, "urban", False)):
        return False
    if roads.has_sidewalks(spec):
        return False
    return True


def format_ref(network, ref):
    """Posted road number on the post (A/S prefix when Overture stores a bare number)."""
    text = "".join(str(ref or "").upper().split())
    if not text:
        return ""
    if network == "pl:motorway":
        return text if text.startswith("A") else f"A{text}"
    if network == "pl:expressway":
        return text if text.startswith("S") else f"S{text}"
    if network == "pl:national":
        return text[2:] if text.startswith("DK") else text
    if network == "pl:regional":
        return text[2:] if text.startswith("DW") else text
    return text


def route_label(routes):
    """Best PL route label for a carriageway, or ''."""
    by_net = {str(n).lower(): r for n, r, *_ in (routes or ())}
    for net in NETWORKS:
        if net in by_net:
            return format_ref(net, by_net[net])
    return ""


def stations(xy, step=SPACING_M):
    """(points (k, 2), unit tangents (k, 2), chainage m (k,)) every step along polyline xy."""
    xy = np.asarray(xy, np.float64)[:, :2]
    seg = np.hypot(*np.diff(xy, axis=0).T)
    keep = np.r_[True, seg > 1e-6]
    xy = xy[keep]
    if len(xy) < 2:
        return np.zeros((0, 2)), np.zeros((0, 2)), np.zeros(0)
    cum = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    if cum[-1] < step * 0.5:
        return np.zeros((0, 2)), np.zeros((0, 2)), np.zeros(0)
    s = np.arange(0.0, cum[-1], step)          # exclusive end: a zero tangent at the tip
    if len(s) < 1:
        return np.zeros((0, 2)), np.zeros((0, 2)), np.zeros(0)
    p = np.c_[np.interp(s, cum, xy[:, 0]), np.interp(s, cum, xy[:, 1])]
    d = np.diff(np.vstack([p, xy[-1:]]), axis=0)
    t = d / np.maximum(np.hypot(*d.T), 1e-9)[:, None]
    return p, t, s


def _rows_near(frame, feats, urban, widths, left_hand, box, placeable_only):
    """[(fid, spec, xy (n, 2), half_width)] near box. placeable_only: wanted() rural hosts; else any paved driveable."""
    if feats is None or not len(feats):
        return []
    specs = roads.road_specs(feats, urban, widths, left_hand)
    x0, y0, x1, y1 = box
    pad = 50.0
    out = []
    for i, spec in enumerate(specs):
        if spec is None or getattr(spec, "kind", "paved") not in (None, "paved"):
            continue
        if placeable_only:
            if not wanted(spec):
                continue
        elif spec.cls not in {"motorway", "trunk", "primary", "secondary", "tertiary", "residential",
                              "unclassified", "living_street", "service"}:
            continue
        fid = str(signroads.feature_id(feats, i))
        for ring in roads.ground_parts(feats, i):
            if len(ring) < 2:
                continue
            e, n = mapgeom.enu_xy(feats, frame, ring)
            xy = np.column_stack([np.asarray(e, float).ravel(), np.asarray(n, float).ravel()])
            if (xy[:, 0].max() < x0 - pad or xy[:, 0].min() > x1 + pad
                    or xy[:, 1].max() < y0 - pad or xy[:, 1].min() > y1 + pad):
                continue
            out.append((fid, spec, xy, float(spec.width) / 2.0))
    return out


def _road_rows(frame, feats, urban, widths, left_hand, box):
    """Placeable rural hosts near box."""
    return _rows_near(frame, feats, urban, widths, left_hand, box, True)


def _clear_rows(frame, feats, urban, widths, left_hand, box):
    """Every paved carriageway near box (clear posts off junctions and town streets)."""
    return _rows_near(frame, feats, urban, widths, left_hand, box, False)


def _routes_near(md, frame, box, fids):
    """{fid: [(network, ref, from, to)]} for cells covering box, limited to fids."""
    if md is None or not fids:
        return {}
    x0, y0, x1, y1 = box
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1]), np.array([y0, y1]), 0.0)
    try:
        import mapcells
        keys = tuple(mapcells.cells_near_points(la, lo, 0.002, 0.002))
    except Exception as error:
        log.debug("hektopost route cells: %s", error)
        return {}
    return dirsigns._route_rows(md, keys, {fid: True for fid in fids})


def _built_up(frame, urban_fn, xy):
    """True when ESA built-up at frame xy (urban_fn(lat, lon) -> bool array)."""
    if urban_fn is None:
        return False
    la, lo, _ = frame.enu_to_geodetic(float(xy[0]), float(xy[1]), 0.0)
    return bool(np.asarray(urban_fn(np.asarray([la]), np.asarray([lo]))).ravel()[0])


def place(frame, road_rows, routes_by_fid, elev, box, decks=(), clear_rows=(), urban_fn=None):
    """Polish U-1 posts in box: road_rows from _road_rows, elev(lat, lon) -> height.

    clear_rows: every nearby carriageway (junctions). urban_fn: skip posts on built-up land even when
    the host road's average urban flag is false (villages / town edges).
    """
    x0, y0, x1, y1 = box
    clearance = list(clear_rows) if clear_rows else list(road_rows)
    out = []
    for fid, spec, xy, half in road_rows:
        if len(xy) < 2 or not wanted(spec):
            continue
        label = route_label(routes_by_fid.get(str(fid), ()))
        # any other pavement (other class / residential cross-street): keep posts off the asphalt
        others = [(xy2, half2 + PAVEMENT_M) for f2, _, xy2, half2 in clearance
                  if f2 != fid and len(xy2) >= 2]
        p, t, chain = stations(xy)
        if not len(p):
            continue
        for side in (1.0, -1.0):
            q = p + np.c_[-t[:, 1], t[:, 0]] * side * (half + OUT_M)
            ok = (q[:, 0] >= x0) & (q[:, 0] < x1) & (q[:, 1] >= y0) & (q[:, 1] < y1)
            for line, reach in others:
                ok &= motesfri._distance(q, line) > reach
            # traffic the face greets: oneway follows the way; two-way faces the near lane
            sign = (-1.0 if int(getattr(spec, "dir", 1) or 1) < 0 else 1.0) if spec.oneway else -side
            for k in np.flatnonzero(ok):
                travel = t[k] * sign
                travel = travel / max(float(np.linalg.norm(travel)), 1e-9)
                centre = q[k]
                if exitsigns._on_deck(centre, decks, CLEAR_DECK_M):
                    continue
                if _built_up(frame, urban_fn, centre):
                    continue
                z = exitsigns._ground_z(frame, elev, centre)
                s = float(chain[k])
                km = int(s // 1000.0)
                hm = int((s % 1000.0) // 100.0)
                out.append(dict(
                    centre=[float(centre[0]), float(centre[1]), z],
                    travel=[float(travel[0]), float(travel[1])],
                    ref=label if hm == 0 else "",
                    km=km,
                    hm=hm,
                    height=HEIGHT_M,
                ))
    return out


def _layout(value, height, max_width):
    """(text, glyphs, scale, rend_w, rend_h) for a digit string fitted into height/max_width."""
    text = exitsigns._text(value)
    data = exitsigns.lettering()
    if not text:
        return "", data, 0.0, 0.0, 0.0
    raw_w = sum(data["glyphs"][c]["advance"] for c in text)
    scale = min(height / data["capHeight"], max_width / max(raw_w, 1e-6))
    return text, data, scale, raw_w * scale, scale * data["capHeight"]


class Mesh(exitsigns.Mesh):
    def text_top(self, value, top, height, max_width, depth):
        """Black digits on the traffic face: cap-line at Z=top, centred on Y=0. Returns (rend_w, rend_h)."""
        text, data, scale, rend_w, rend_h = _layout(value, height, max_width)
        if not text:
            return 0.0, 0.0
        # Anchor by the *actual* rendered cap height (max_width may shrink the glyph).
        baseline = top - rend_h
        cursor = -rend_w / 2.0
        for c in text:
            glyph = data["glyphs"][c]
            l, t, r, b = glyph["bbox"]
            u0, v0, u1, v1 = glyph["uv"]
            if r > l and b > t:
                self.quad([(depth, cursor + l * scale, baseline - b * scale),
                           (depth, cursor + r * scale, baseline - b * scale),
                           (depth, cursor + r * scale, baseline - t * scale),
                           (depth, cursor + l * scale, baseline - t * scale)],
                          MAT_LETTERS, [(u0, v0), (u1, v0), (u1, v1), (u0, v1)])
            cursor += glyph["advance"] * scale
        return rend_w, rend_h

    def hm_plate(self, depth, top, rend_w, rend_h):
        """Flat grey sticker behind the hm digit only (photo: light plate around the large number)."""
        pw = min(POST_W - 0.01, rend_w + 2 * PLATE_PAD)
        ph = rend_h + 2 * PLATE_PAD
        # Single face on the traffic side — not a thick box that swallows the km digit.
        self.quad([(depth, -pw / 2, top + PLATE_PAD - ph),
                   (depth, pw / 2, top + PLATE_PAD - ph),
                   (depth, pw / 2, top + PLATE_PAD),
                   (depth, -pw / 2, top + PLATE_PAD)], MAT_PLATE)

    def post(self, sign):
        self.centre = np.asarray(sign["centre"], float)
        travel = np.asarray(sign["travel"], float)
        travel = travel / max(float(np.linalg.norm(travel)), 1e-9)
        self.face = np.r_[-travel, 0.0]
        self.right = np.array([travel[1], -travel[0], 0.0])
        hw, hd = POST_W / 2.0, POST_D / 2.0
        h = float(sign.get("height", HEIGHT_M))
        plate_z = hd + 0.002
        ink = hd + 0.005
        # Shaft + red sleeve (the red band is the reflector — no white insert).
        self.box((-hd, -hw, -0.10), (hd, hw, RED_LO), MAT_WHITE)
        self.box((-hd - 0.001, -hw - 0.001, RED_LO), (hd + 0.001, hw + 0.001, RED_HI), MAT_RED)
        self.box((-hd, -hw, RED_HI), (hd, hw, h - 0.015), MAT_WHITE)
        self.box((-hd * 0.85, -hw * 0.85, h - 0.015), (hd * 0.85, hw * 0.85, h), MAT_WHITE)
        ref = str(sign.get("ref") or "")
        if ref:
            self.text_top(ref, min(REF_TOP, h - 0.008), REF_H, POST_W * 0.85, ink)
        # Photo layout: small centred km, then larger hm on its own grey sticker; empty white below.
        km = str(int(sign["km"]))
        hm = str(int(sign["hm"]))
        km_top = RED_LO - GAP_RED_M
        _, km_h = self.text_top(km, km_top, KM_H, POST_W * 0.50, ink)
        _, _, _, hm_w, hm_h = _layout(hm, HM_H, POST_W * 0.72)
        hm_top = km_top - km_h - GAP_DIGITS_M
        self.hm_plate(plate_z, hm_top, hm_w, hm_h)
        self.text_top(hm, hm_top, HM_H, POST_W * 0.72, ink)

    def result(self):
        if not self.t:
            return None
        return (np.asarray(self.p), np.asarray(self.t), np.asarray(self.uv),
                MATERIALS, np.asarray(self.mi))


def build(signs, origin):
    mesh = Mesh(origin)
    for sign in signs:
        mesh.post(sign)
    return mesh.result()


def build_tile(md, frame, tx, ty, elev, context, decks=(), left_hand=False, country=None,
               urban=None, widths=None):
    """Signstream mesh for tile (tx, ty), or None."""
    if (country or "").upper() not in COUNTRIES:
        return None
    box = roads.tile_bbox(tx, ty)
    x0, y0, x1, y1 = box
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1]), np.array([y0, y1]), 0.0)
    feats = context.around(np.column_stack([lo, la])) if context is not None else None
    rows = _road_rows(frame, feats, urban, widths, left_hand, box)
    if not rows:
        return None
    clear = _clear_rows(frame, feats, urban, widths, left_hand, box)
    routes = _routes_near(md, frame, box, [fid for fid, *_ in rows])
    signs = place(frame, rows, routes, elev, box, decks, clear, urban_fn=urban)
    if not signs:
        return None
    return build(signs, (float(x0), float(y0), 0.0))
