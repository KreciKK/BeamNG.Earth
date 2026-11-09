"""Pedestrian refuge islands (azyle) at crossings.

Sources (most useful first):
  1. Overture land_use class=traffic_island polygons (from OSM area:highway=traffic_island etc.)
  2. Infrastructure nodes tagged crossing:island=yes or traffic_calming=island

Each island gets a cobble/concrete pad drawn as a filled road-mesh polygon on the mark layer
(1 cm over asphalt so it shows through) and a raised curb ring as a sidewalk walk.
"""
import json
import logging
import math

import numpy as np

import furniture

# procedural pad when only a node is mapped (no polygon)
ISLAND_ALONG_M = 2.5
ISLAND_ACROSS_M = 2.0
NODE_NEAR_M = 4.0
ONE_ISLAND_M = 5.0
ON_JUNCTION_M = 1.0
MIN_POLY_AREA_M2 = 1.5          # smaller than a plaza; real refuges are a few m2
CURB_BAND_M = 0.6               # raised curb ring width around a mapped polygon
CROSSING_CLASSES = ("crossing", "pedestrian_crossing")
YES = ("yes", "1", "true")
# mark layer: 1 cm over asphalt (roadmesh.LIFT); cobble reads as the island surface
PAD = {"m": "italy_cobblestone", "tl": 2.5, "p": 15, "layer": "mark"}


def tagged(tags):
    """True when OpenStreetMap says this node has a pedestrian refuge island."""
    if not tags:
        return False
    if str(tags.get("crossing:island", "")).lower() in YES:
        return True
    return str(tags.get("traffic_calming", "")).lower() == "island"


def land_rings(feats_list):
    """[(outer lon/lat (k, 2), [hole lon/lat rings])] of Overture land_use class=traffic_island."""
    out = []
    if not feats_list:
        return out
    for f in feats_list:
        if f is None or not len(f):
            continue
        cls = f.attrs.get("class")
        if cls is None:
            continue
        hit = np.asarray(cls).astype(str) == "traffic_island"
        for i in np.flatnonzero(hit):
            for part in f.parts(i):
                if len(part) and len(part[0]) >= 4:
                    out.append((np.asarray(part[0], np.float64),
                                [np.asarray(h, np.float64) for h in part[1:]]))
    return out


def read_land(md, frame, keys):
    """land_rings of the map cells keys; [] when none, None when land_use cannot be read."""
    try:
        return land_rings([md.cell(k, layers=("land_use",), kind="demand").layers.get("land_use")
                           for k in keys])
    except Exception as e:
        logging.getLogger("earthd.refugeislands").debug("refuge land: %s: %s", type(e).__name__, e)
        return None


def nodes(frame, infra_list):
    """[(x, y)] of infrastructure nodes tagged as a refuge island; None when unreadable."""
    if not infra_list or all(f is None for f in infra_list):
        return None
    seen, found = set(), []
    for f in infra_list:
        if f is None or not len(f):
            continue
        cls, sub, tags = f.attrs.get("class"), f.attrs.get("subtype"), f.attrs.get("tags")
        if cls is None:
            continue
        if tags is None:
            return None
        classes = np.asarray(cls).astype(str)
        hit = np.ones(len(classes), bool)
        if sub is not None:
            hit &= np.asarray(sub).astype(str) == "transportation"
        for i in np.flatnonzero(hit):
            fid = str(f.ids[i])
            if fid in seen:
                continue
            try:
                tag = json.loads(str(tags[i]))
            except ValueError:
                tag = {}
            if not isinstance(tag, dict) or not tagged(tag):
                continue
            if str(classes[i]) not in CROSSING_CLASSES and str(tag.get("traffic_calming", "")).lower() != "island":
                continue
            seen.add(fid)
            a = f.coord_off[f.ring_off[f.part_off[i]]]
            b = f.coord_off[f.ring_off[f.part_off[i + 1]]]
            found.append(f.lonlat[a:b].mean(axis=0))
    if not found:
        return []
    ll = np.asarray(found, np.float64)
    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
    return [(float(x), float(y)) for x, y in zip(np.asarray(e), np.asarray(n))]


def read(md, frame, keys):
    """nodes of the map cells keys; None when infrastructure cannot be read."""
    try:
        return nodes(frame, [md.cell(k, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
                             for k in keys])
    except Exception as e:
        logging.getLogger("earthd.refugeislands").debug("refuge islands: %s: %s", type(e).__name__, e)
        return None


def _area2(xy):
    x, y = xy[:, 0], xy[:, 1]
    return float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _pad_chunk(xy):
    """Filled mark-layer polygon chunk for an island pad in frame xy (k, 2), CCW."""
    if len(xy) < 3:
        return None
    poly = np.c_[xy, np.zeros(len(xy))]
    return {**PAD, "w": 0.0, "poly": [[round(float(v), 3) for v in q] for q in poly], "n": []}


def _curb_walk(xy, z=0.0):
    """Closed curb sidewalk walk around ring xy (k, 2); None if too small."""
    if len(xy) < 3:
        return None
    ring = np.c_[xy, np.full(len(xy), z)]
    # ensure CCW so side=+1 (left) faces inward
    if _area2(xy) < 0:
        ring = ring[::-1]
    closed = np.vstack([ring[-1:], ring, ring[:2]])
    import roads as roadsmod
    band = roadsmod.offset_polyline(closed, CURB_BAND_M / 2)[1:-1]
    if len(band) < 2:
        return None
    return {"pts": band, "side": 1, "caps": (False, False), "ramps": [],
            "w": CURB_BAND_M, "fid": "refuge"}


def place_polys(frame, elev, rings, box):
    """(walks, chunks, gaps) for land_use traffic_island rings [(outer lon/lat, holes)] in tile box.

    gaps: [(cx, cy, half_across)] for splitting zebra paint over the island.
    """
    walks, chunks, gaps = [], [], []
    if not rings:
        return walks, chunks, gaps
    x0, y0, x1, y1 = box
    for outer, _holes in rings:
        e, n, _ = frame.geodetic_to_enu(outer[:, 1], outer[:, 0], 0.0)
        xy = np.stack([np.asarray(e), np.asarray(n)], 1)
        if np.hypot(*(xy[0] - xy[-1])) < 1e-6:
            xy = xy[:-1]
        if len(xy) < 3:
            continue
        if xy[:, 0].max() < x0 or xy[:, 0].min() > x1 or xy[:, 1].max() < y0 or xy[:, 1].min() > y1:
            continue
        a2 = _area2(xy)
        if abs(a2) / 2 < MIN_POLY_AREA_M2:
            continue
        if a2 < 0:
            xy = xy[::-1]
        c = xy.mean(0)
        # one tile owns each island (by centre); refuges are small, so no border split
        if not (x0 <= c[0] < x1 and y0 <= c[1] < y1):
            continue
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
        z = float(np.mean(np.asarray(elev(la, lo), np.float64)))
        pad = _pad_chunk(xy)
        if pad is not None:
            chunks.append(pad)
        curb = _curb_walk(xy, z)
        if curb is not None:
            walks.append(curb)
        ext = xy.max(0) - xy.min(0)
        gaps.append((float(c[0]), float(c[1]), float(max(0.6, 0.35 * min(ext)))))
    return walks, chunks, gaps


def place(roads, islands):
    """(walks, chunks, gaps) for node islands [(x, y)] snapped onto paved roads."""
    walks, chunks, gaps, taken = [], [], [], []
    if not islands:
        return walks, chunks, gaps
    lines = [(fid, spec, np.asarray(pts, np.float64)) for fid, spec, pts, _ in roads
             if spec.kind in ("paved", "cobble") and spec.cls != "motorway" and len(pts) >= 2]
    if not lines:
        return walks, chunks, gaps
    a = np.vstack([pts[:-1, :3] for _, _, pts in lines])
    b = np.vstack([pts[1:, :3] for _, _, pts in lines])
    own = np.repeat(np.arange(len(lines)), [len(pts) - 1 for _, _, pts in lines])
    d = b[:, :2] - a[:, :2]
    l2 = np.maximum((d * d).sum(1), 1e-9)
    meet = np.array([arms[0][3][:2] for arms in furniture._arms(roads).values() if len(arms) >= 3]).reshape(-1, 2)
    half_along = ISLAND_ALONG_M / 2.0
    half_across = ISLAND_ACROSS_M / 2.0
    for x, y in islands:
        t = np.clip(((np.array([x, y]) - a[:, :2]) * d).sum(1) / l2, 0.0, 1.0)
        q = a[:, :2] + d * t[:, None]
        off = np.hypot(q[:, 0] - x, q[:, 1] - y)
        i = int(np.argmin(off))
        if off[i] > NODE_NEAR_M:
            continue
        fid, spec, _ = lines[own[i]]
        c = q[i]
        if len(meet) and np.hypot(*(meet - c).T).min() < ON_JUNCTION_M:
            continue
        if any(f == fid and np.hypot(*(c - e)) < ONE_ISLAND_M for f, e in taken):
            continue
        taken.append((fid, c.copy()))
        u = d[i] / float(np.sqrt(l2[i]))
        n = np.array([-u[1], u[0]])
        z = float(a[i, 2] + t[i] * (b[i, 2] - a[i, 2]))
        # rectangle on the carriageway centre
        corners = np.array([
            c + u * half_along + n * half_across,
            c - u * half_along + n * half_across,
            c - u * half_along - n * half_across,
            c + u * half_along - n * half_across,
        ])
        pad = _pad_chunk(corners)
        if pad is not None:
            chunks.append(pad)
        curb = _curb_walk(corners, z)
        if curb is not None:
            walks.append(curb)
        gaps.append((float(c[0]), float(c[1]), half_across))
    return walks, chunks, gaps


def gap_crosswalks(crosswalks, gaps):
    """Split painted crosswalks that overlap a refuge gap into two halves (kerb to island edge)."""
    if not gaps or not crosswalks:
        return crosswalks
    out = []
    for cw in crosswalks:
        if cw.get("stop") or not cw.get("paint", True):
            out.append(cw)
            continue
        a, b = np.asarray(cw["a"], np.float64), np.asarray(cw["b"], np.float64)
        mid = 0.5 * (a + b)
        ab = b[:2] - a[:2]
        span = float(np.hypot(*ab))
        if span < 1e-3:
            out.append(cw)
            continue
        u = ab / span
        hit = None
        for cx, cy, half in gaps:
            if math.hypot(mid[0] - cx, mid[1] - cy) <= span / 2 + 0.5:
                hit = half
                break
        if hit is None:
            out.append(cw)
            continue
        p1, p2 = mid.copy(), mid.copy()
        p1[:2] = mid[:2] - u * hit
        p2[:2] = mid[:2] + u * hit
        if float(np.dot(a[:2] - mid[:2], -u)) >= float(np.dot(b[:2] - mid[:2], -u)):
            near_a, near_b = p1, p2
        else:
            near_a, near_b = p2, p1
        if float(np.hypot(*(a[:2] - near_a[:2]))) > 0.4:
            out.append(dict(cw, a=a, b=near_a))
        if float(np.hypot(*(b[:2] - near_b[:2]))) > 0.4:
            out.append(dict(cw, a=near_b, b=b))
    return out
