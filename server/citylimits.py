"""Polish place-name and built-up roadside signs (E-17a / E-18a / D-42 / D-43).

Mapped OSM nodes (traffic_sign=city_limit, PL:D-42, PL:D-43) from the bundled index
(tools/build_citylimits.py). Place-name signs also synthesise where a driveable road
crosses an Overture locality polygon and no mapped city_limit is nearby. Built-up
signs (D-42/D-43) are never synthesised — only mapped nodes.

Country gate: PL only (signstream field "citylimits"), render-only under highwaySigns.
"""
from functools import lru_cache
import hashlib
import logging
import math
import os
import re
import threading

import numpy as np

import exitsigns
import mapgeom
import roads
import signroads

log = logging.getLogger("earthd.citylimits")

COUNTRIES = {"PL"}
SCHEMA = 1
INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "citylimits.npz")
# Empty schema index from `python tools/build_citylimits.py --empty`; rebuild pins a new hash.
INDEX_SHA256 = "7b94226e7f7cd414d807837fbf984424a04d876f800b0a8d7fd32b72ac0c2d9c"
INDEX_COLUMNS = ("lat", "lon", "osm_id", "kind", "dir_deg", "name")

# kind codes stored in the index / sign records
PLACE_BOTH, PLACE_BEGIN, PLACE_END = 0, 1, 2
BUILT_BEGIN, BUILT_END = 3, 4
KIND_NAME = {
    PLACE_BOTH: "place_both", PLACE_BEGIN: "place_begin", PLACE_END: "place_end",
    BUILT_BEGIN: "built_begin", BUILT_END: "built_end",
}
PLACE_KINDS = {PLACE_BOTH, PLACE_BEGIN, PLACE_END}
BUILT_KINDS = {BUILT_BEGIN, BUILT_END}

CLASSES = {"trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
           "living_street", "service"}
SNAP_M = 55.0                   # OSM nodes often sit beside the way, not on the centreline
LOOSE_SNAP_M = 120.0            # still place using road heading when farther than SNAP_M
# OUT_M set below with panel size
SUPPRESS_M = 100.0              # mapped place suppresses a synthetic of the *same* name this far
SAMPLE_M = 8.0                  # road samples for polygon inside/outside flips
CLEAR_DECK_M = 1.0
PAVEMENT_M = 0.6
# Real E-17a/E-18a are modest single-line boards (~0.5 m tall, ~0.15–0.2 m letters).
PANEL_H = 0.52
PANEL_MIN_W, PANEL_MAX_W = 0.90, 2.20
LETTER_H = 0.17
POLE_H = 2.45
STACK_GAP = 0.08                # air between stacked E-17a (top) and E-18a (bottom)
BUILT_W, BUILT_H = 0.70, 0.70   # D-42 / D-43 ~ square
DEDUPE_M = 25.0                 # nested admin rings: one begin (or end) per roadside cluster
PAIR_M = 40.0                   # enter A + leave B within this → one pole, two boards (Street View)
SAME_FACE_M = 80.0              # same name+role+travel (dual carriage / double ring) → one board
OUT_M = 1.8                     # m past the pavement edge
MATERIALS = [
    "bne_citylimit_green",      # 0 E-17a / E-18a face (glossy sheeting)
    "bne_citylimit_white",      # 1 D-42 / D-43 face + E-17a border
    "bne_exit_metal",           # 2 pole / galvanised back
    "bne_citylimit_letters",    # 3 white letters on green (retroreflective)
    "bne_guide_red",            # 4 E-18a / D-43 strike
    "bne_guide_black",          # 5 D-42 skyline
]
MAT_GREEN, MAT_WHITE, MAT_METAL, MAT_LETTERS, MAT_RED, MAT_BLACK = 0, 1, 2, 3, 4, 5

_index_lock = threading.Lock()
_warned_index = False
_TEST_INDEX = None                 # set_index_for_tests: in-memory override


def parse_tags(tags):
    """OSM tags -> {kind, name, dir_deg} or None (not a city-limit / PL built-up sign).

    Built-up (PL:D-42 / PL:D-43) wins when present alone. Combined with city_limit on the
    same node still yields the place-name sign (E-17a/E-18a); D-42/D-43 need their own node.
    """
    if not isinstance(tags, dict):
        return None
    raw = str(tags.get("traffic_sign") or "")
    parts = {p.strip() for p in re.split(r"[;|,]", raw) if p.strip()}
    place = "city_limit" in parts or "PL:E-17a" in parts or "PL:E-18a" in parts
    if not place:
        if "PL:D-42" in parts:
            return dict(kind=BUILT_BEGIN, name="", dir_deg=_dir_deg(tags))
        if "PL:D-43" in parts:
            return dict(kind=BUILT_END, name="", dir_deg=_dir_deg(tags))
        return None
    side = str(tags.get("city_limit") or "").lower()
    if side == "begin" or ("PL:E-17a" in parts and "PL:E-18a" not in parts and side != "end"):
        kind = PLACE_BEGIN
    elif side == "end" or ("PL:E-18a" in parts and "PL:E-17a" not in parts and side != "begin"):
        kind = PLACE_END
    else:
        kind = PLACE_BOTH
    name = (tags.get("name") or tags.get("name:forward") or tags.get("name:backward") or "").strip()
    return dict(kind=kind, name=name[:80], dir_deg=_dir_deg(tags))


def _dir_deg(tags):
    """Posted facing as degrees clockwise from north, or NaN if unknown / forward|backward only."""
    raw = tags.get("direction")
    if raw is None or raw == "":
        return float("nan")
    text = str(raw).strip().lower()
    if text in ("forward", "backward", "both", "entrance", "exit"):
        return float("nan")
    try:
        return float(text) % 360.0
    except ValueError:
        return float("nan")


def wanted(spec):
    """Driveable non-motorway host for place / built-up signs."""
    if (spec is None or getattr(spec, "kind", "paved") not in (None, "paved")
            or spec.cls not in CLASSES or getattr(spec, "link", False)):
        return False
    return True


# --- bundled OSM index -------------------------------------------------------

def _empty_arrays():
    return {name: np.zeros(0, dtype=dt) for name, dt in (
        ("lat", np.float64), ("lon", np.float64), ("osm_id", np.int64),
        ("kind", np.int8), ("dir_deg", np.float32), ("name", "U80"))}


@lru_cache(maxsize=1)
def _index():
    """Latitude-sorted mapped signs, or empty arrays when the index is missing / unpinned."""
    global _warned_index
    if _TEST_INDEX is not None:
        return _TEST_INDEX
    try:
        with open(INDEX_PATH, "rb") as source:
            digest = hashlib.sha256()
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
            got = digest.hexdigest()
        # Accept any schema-valid file during development; pin when a real extract ships.
        with np.load(INDEX_PATH, allow_pickle=False) as data:
            schema = int(np.asarray(data["schema"]).reshape(-1)[0]) if "schema" in data.files else 0
            if schema != SCHEMA:
                raise ValueError(f"citylimits schema {schema} != {SCHEMA}")
            arrays = {name: np.array(data[name]) for name in INDEX_COLUMNS}
        n = len(arrays["lat"])
        if any(a.shape != (n,) for a in arrays.values()):
            raise ValueError("citylimits column length mismatch")
        if n and (arrays["lat"][1:] < arrays["lat"][:-1]).any():
            raise ValueError("citylimits not sorted by latitude")
        for a in arrays.values():
            a.flags.writeable = False
        if got != INDEX_SHA256 and n and not _warned_index:
            _warned_index = True
            log.info("citylimits index SHA %s (pinned %s); using file anyway", got, INDEX_SHA256)
        return arrays
    except (OSError, ValueError, TypeError, KeyError) as error:
        if not _warned_index:
            _warned_index = True
            log.warning("citylimits index unavailable (%s); mapped signs omitted", error)
        return _empty_arrays()


def near(la0, lo0, la1, lo1):
    """Mapped signs in an inclusive lat/lon window (lo0>lo1 wraps the dateline)."""
    with _index_lock:
        data = _index()
    if not len(data["lat"]):
        return []
    i = int(np.searchsorted(data["lat"], la0, side="left"))
    j = int(np.searchsorted(data["lat"], la1, side="right"))
    lon = data["lon"][i:j]
    sel = (lon >= lo0) & (lon <= lo1) if lo0 <= lo1 else (lon >= lo0) | (lon <= lo1)
    out = []
    for k in np.flatnonzero(sel) + i:
        out.append(dict(
            lat=float(data["lat"][k]), lon=float(data["lon"][k]),
            osm_id=int(data["osm_id"][k]), kind=int(data["kind"][k]),
            name=str(data["name"][k] or ""), dir_deg=float(data["dir_deg"][k]),
            source="osm",
        ))
    return out


def set_index_for_tests(records):
    """Replace the loaded index with in-memory records (tests). Pass None to reset."""
    global _TEST_INDEX
    _index.cache_clear()
    if records is None:
        _TEST_INDEX = None
        return
    rows = sorted(records, key=lambda r: (r["lat"], r["lon"], r.get("osm_id", 0)))
    arrays = {
        "lat": np.array([r["lat"] for r in rows], np.float64),
        "lon": np.array([r["lon"] for r in rows], np.float64),
        "osm_id": np.array([r.get("osm_id", 0) for r in rows], np.int64),
        "kind": np.array([r["kind"] for r in rows], np.int8),
        "dir_deg": np.array([r.get("dir_deg", float("nan")) for r in rows], np.float32),
        "name": np.array([r.get("name", "") for r in rows], dtype="U80"),
    }
    for a in arrays.values():
        a.flags.writeable = False
    _TEST_INDEX = arrays


# --- locality polygon crossings (synthetic place signs only) -----------------

def road_crossings(xy, ring, step=SAMPLE_M):
    """Where polyline xy enters/leaves polygon ring: [(point (2,), travel (2,), entering bool)]."""
    xy = np.asarray(xy, np.float64)[:, :2]
    ring = np.asarray(ring, np.float64)[:, :2]
    if len(xy) < 2 or len(ring) < 3:
        return []
    import fast
    fast.require(0, "points_inside")
    seg = np.hypot(*np.diff(xy, axis=0).T)
    keep = np.r_[True, seg > 1e-6]
    xy = xy[keep]
    if len(xy) < 2:
        return []
    cum = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    if cum[-1] < step:
        return []
    s = np.arange(0.0, cum[-1] + step * 0.5, step)
    s = np.clip(s, 0.0, cum[-1])
    p = np.c_[np.interp(s, cum, xy[:, 0]), np.interp(s, cum, xy[:, 1])]
    closed = ring if np.allclose(ring[0], ring[-1]) else np.vstack([ring, ring[:1]])
    inside = np.asarray(fast._ef.points_inside(np.ascontiguousarray(closed), np.ascontiguousarray(p)), bool)
    out = []
    for i in range(1, len(inside)):
        if inside[i] == inside[i - 1]:
            continue
        # Crossing between samples i-1 and i: interpolate mid.
        a, b = p[i - 1], p[i]
        mid = 0.5 * (a + b)
        travel = b - a
        n = float(np.linalg.norm(travel))
        if n < 1e-9:
            continue
        travel = travel / n
        out.append((mid, travel, bool(inside[i])))
    return out


def _enu_ring(frame, ring_ll):
    """lon/lat ring (n, 2) → ENU xy (n, 2) in frame."""
    ring_ll = np.asarray(ring_ll, float)
    e, n, _ = frame.geodetic_to_enu(ring_ll[:, 1], ring_ll[:, 0], 0.0)
    return np.column_stack([np.asarray(e, float).ravel(), np.asarray(n, float).ravel()])


def _layercake_place_rings(md, frame, box):
    """[(name, ring_xy ENU)] from OSM admin boundaries (Layercake), preferred for PL villages."""
    src = getattr(md, "placebounds", None) if md is not None else None
    if src is None:
        return []
    x0, y0, x1, y1 = box
    pad = 150.0
    la, lo, _ = frame.enu_to_geodetic(np.array([x0 - pad, x1 + pad]),
                                      np.array([y0 - pad, y1 + pad]), 0.0)
    lon0, lon1 = float(min(lo)), float(max(lo))
    lat0, lat1 = float(min(la)), float(max(la))
    try:
        rows = src.rings((lon0, lat0, lon1, lat1), kind="demand")
    except Exception as error:
        log.debug("citylimits placebounds: %s", error)
        return []
    out = []
    for name, ring_ll in rows:
        ring = _enu_ring(frame, ring_ll)
        if (ring[:, 0].max() < x0 - pad or ring[:, 0].min() > x1 + pad
                or ring[:, 1].max() < y0 - pad or ring[:, 1].min() > y1 + pad):
            continue
        out.append((name, ring))
    return out


def _overture_locality_rings(md, frame, box):
    """[(name, ring_xy ENU)] from Overture locality_areas (fallback beside Layercake)."""
    reader = getattr(md, "overture", None) if md is not None else None
    if reader is None:
        return []
    x0, y0, x1, y1 = box
    pad = 80.0
    la, lo, _ = frame.enu_to_geodetic(np.array([x0 - pad, x1 + pad]), np.array([y0 - pad, y1 + pad]), 0.0)
    try:
        import mapcells
        keys = tuple(mapcells.cells_near_points(la, lo, 0.01, 0.01))
    except Exception as error:
        log.debug("citylimits locality cells: %s", error)
        return []
    out, seen = [], set()
    for key in keys:
        try:
            feats, _ = reader.query("locality_areas", key, "demand")
        except Exception as error:
            log.debug("citylimits locality_areas %s: %s", key, error)
            continue
        if feats is None or not len(feats):
            continue
        names = np.asarray(feats.attrs.get("name", [""] * len(feats))).astype(str)
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if fid in seen:
                continue
            name = str(names[i] if i < len(names) else "").strip()
            if not name:
                continue
            parts = feats.parts(i)
            if not parts or not parts[0]:
                continue
            ring_ll = np.asarray(parts[0][0], float)
            if len(ring_ll) < 3:
                continue
            e, n = mapgeom.enu_xy(feats, frame, ring_ll)
            ring = np.column_stack([np.asarray(e, float).ravel(), np.asarray(n, float).ravel()])
            if (ring[:, 0].max() < x0 - pad or ring[:, 0].min() > x1 + pad
                    or ring[:, 1].max() < y0 - pad or ring[:, 1].min() > y1 + pad):
                continue
            seen.add(fid)
            out.append((name, ring))
    return out


def _locality_rings(md, frame, box):
    """[(name, ring_xy (n,2) ENU)] for synthetic place signs: Layercake admin first, then Overture."""
    if md is None:
        return []
    # Layercake OSM admin (Cygusy/Czernin) first; Overture fills gaps where present.
    out = _layercake_place_rings(md, frame, box)
    have = {(n or "").casefold() for n, _ in out}
    for name, ring in _overture_locality_rings(md, frame, box):
        key = (name or "").casefold()
        if not key or key in have:
            continue
        out.append((name, ring))
        have.add(key)
    return out


# --- placement ---------------------------------------------------------------

def _road_rows(frame, feats, urban, widths, left_hand, box):
    if feats is None or not len(feats):
        return []
    specs = roads.road_specs(feats, urban, widths, left_hand)
    x0, y0, x1, y1 = box
    pad = 60.0
    out = []
    for i, spec in enumerate(specs):
        if not wanted(spec):
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


def _side_point(at, travel, half, side):
    right = np.array([travel[1], -travel[0]])
    return at + right * side * (half + OUT_M)


def _facing_travel(spec, travel, side):
    """Unit travel the sign face greets (traffic approaching the panel)."""
    t = np.asarray(travel, float)
    n = float(np.linalg.norm(t))
    t = t / max(n, 1e-9)
    if getattr(spec, "oneway", False):
        sign = -1.0 if int(getattr(spec, "dir", 1) or 1) < 0 else 1.0
        return t * sign
    # Two-way: face the near lane (opposite to the outward side).
    return t * (-side)


def _faces_for(kind):
    """(face_kind, along_road_sign) pairs: +1 with way tangent, -1 against.

    PLACE_BOTH (OSM city_limit without begin/end): one board each roadside — E-17a facing into
    the place, E-18a facing out. Adjacent settlements add a stacked neighbour via _pair_place_signs.
    """
    if kind == PLACE_BOTH:
        return [(PLACE_BEGIN, 1.0), (PLACE_END, -1.0)]
    if kind in (PLACE_BEGIN, BUILT_BEGIN):
        return [(kind, 1.0)]
    return [(kind, -1.0)]


def _place_mapped(frame, rows, mapped, elev, box, decks):
    """Snap OSM nodes to roads; emit one or two roadside signs per node.

    Nodes farther than SNAP_M but within LOOSE_SNAP_M still get a panel using the
    road heading (OSM city_limit is often mapped a few metres off the way).
    """
    x0, y0, x1, y1 = box
    road_list = [(fid, spec, xy, 0.0) for fid, spec, xy, _ in rows]
    out = []
    for rec in mapped:
        la, lo = rec["lat"], rec["lon"]
        e, n, _ = frame.geodetic_to_enu(la, lo, 0.0)
        q = np.array([float(np.asarray(e).ravel()[0]), float(np.asarray(n).ravel()[0])])
        near = exitsigns._nearest(q, road_list) if road_list else None
        if near is None or near[0] > LOOSE_SNAP_M:
            # No usable road: still plant at the OSM point facing east so the tile is not empty.
            at, tangent, half = q, np.array([1.0, 0.0]), 3.5
        else:
            _, fid, spec, line, k, t, at, tangent = near
            half = next((h for f, _, _, h in rows if f == fid),
                        float(getattr(spec, "width", 6.0)) / 2.0)
            if near[0] > SNAP_M:
                # Keep OSM lateral position; only borrow the road's heading.
                at = q
        kind = int(rec["kind"])
        name = rec.get("name") or ""
        for face_kind, along in _faces_for(kind):
            travel = tangent * along
            travel = travel / max(float(np.linalg.norm(travel)), 1e-9)
            centre2 = _side_point(at, travel, half, 1.0)
            # Prefer roadside; if that falls outside the tile, try the OSM point itself.
            candidates = [centre2, at, q]
            placed = False
            for centre in candidates:
                if not (x0 <= centre[0] < x1 and y0 <= centre[1] < y1):
                    continue
                if exitsigns._on_deck(centre, decks, CLEAR_DECK_M):
                    continue
                z = exitsigns._ground_z(frame, elev, centre)
                out.append(dict(
                    centre=[float(centre[0]), float(centre[1]), z],
                    travel=[float(travel[0]), float(travel[1])],
                    kind=face_kind,
                    name=name,
                    source="osm",
                    osm_id=rec.get("osm_id"),
                    at=[float(at[0]), float(at[1])],
                ))
                placed = True
                break
            if not placed:
                continue
    return out


def _ring_area(ring):
    """Approximate |shoelace| area of an ENU ring (for preferring the smallest settlement)."""
    xy = np.asarray(ring, float)[:, :2]
    if len(xy) < 3:
        return float("inf")
    x, y = xy[:, 0], xy[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) * 0.5)


def _place_synthetic(frame, rows, rings, elev, box, decks, mapped_place):
    """Locality polygon crossings; skip only when a mapped sign of the same name is nearby.

    A different name must survive (Czernin next to mapped Sztum) so _pair_place_signs can stack
    E-17a + E-18a on one pole the way Polish roads do at adjacent settlements.
    """
    x0, y0, x1, y1 = box
    mapped_place = list(mapped_place or [])
    # Prefer smaller admin polygons when several share a road crossing (sołectwo inside village).
    ranked = sorted(((name, ring, _ring_area(ring)) for name, ring in rings), key=lambda r: r[2])
    out = []
    for fid, spec, xy, half in rows:
        for name, ring, area in ranked:
            name_key = (name or "").casefold()
            for at, travel, entering in road_crossings(xy, ring):
                blocked = False
                for m_at, m_name in mapped_place:
                    if (m_name or "").casefold() != name_key:
                        continue
                    if float(np.hypot(*(np.asarray(m_at, float) - at).T)) < SUPPRESS_M:
                        blocked = True
                        break
                if blocked:
                    continue
                travel = travel / max(float(np.linalg.norm(travel)), 1e-9)
                # Two-way: E-17a into the place and E-18a out, opposite verges (Street View).
                faces = ((PLACE_BEGIN, travel), (PLACE_END, -travel)) if entering else (
                    (PLACE_END, travel), (PLACE_BEGIN, -travel))
                for face_kind, trav in faces:
                    centre2 = _side_point(at, trav, half, 1.0)
                    candidates = [centre2, at]
                    for centre in candidates:
                        if not (x0 <= centre[0] < x1 and y0 <= centre[1] < y1):
                            continue
                        if exitsigns._on_deck(centre, decks, CLEAR_DECK_M):
                            continue
                        z = exitsigns._ground_z(frame, elev, centre)
                        out.append(dict(
                            centre=[float(centre[0]), float(centre[1]), z],
                            travel=[float(trav[0]), float(trav[1])],
                            kind=face_kind,
                            name=name,
                            source="synthetic",
                            at=[float(at[0]), float(at[1])],
                            area=float(area),
                        ))
                        break
    return out


def _travel_dot(a, b):
    ta = np.asarray(a["travel"][:2], float)
    tb = np.asarray(b["travel"][:2], float)
    na = float(np.linalg.norm(ta))
    nb = float(np.linalg.norm(tb))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.dot(ta / na, tb / nb))


def _pair_place_signs(signs):
    """Collapse place-name candidates into Street View posts.

    - Nested admin rings (two BEGINs / two ENDs, same travel): keep the smaller ring.
    - Adjacent localities (BEGIN A + END B, same travel, different names): one post with
      leave_name on the begin board (E-17a above E-18a), matching Polish practice.
    Built-up signs pass through unchanged.
    """
    built = [s for s in signs if int(s["kind"]) in BUILT_KINDS]
    place = [s for s in signs if int(s["kind"]) in PLACE_KINDS]
    if len(place) < 2:
        return built + place

    def rank_key(s):
        # Mapped first, then smaller area, then stable name.
        return (
            0 if s.get("source") == "osm" else 1,
            float(s.get("area", float("inf"))),
            str(s.get("name") or ""),
        )

    order = sorted(range(len(place)), key=lambda i: rank_key(place[i]) + (i,))
    used = set()
    out = []

    def near_same_role(i, role_kind, max_m=DEDUPE_M):
        """Other unused signs of the same begin/end role, aligned travel, any name."""
        s = place[i]
        c = np.asarray(s["centre"][:2], float)
        hits = []
        for j in order:
            if j == i or j in used:
                continue
            o = place[j]
            if int(o["kind"]) != role_kind:
                continue
            if _travel_dot(s, o) < 0.5:
                continue
            if float(np.hypot(*(c - np.asarray(o["centre"][:2], float)).T)) > max_m:
                continue
            hits.append(j)
        return hits

    # Begins first so an END is not emitted alone before its neighbour BEGIN can stack it.
    begins = [i for i in order if int(place[i]["kind"]) == PLACE_BEGIN]
    ends = [i for i in order if int(place[i]["kind"]) == PLACE_END]
    both = [i for i in order if int(place[i]["kind"]) == PLACE_BOTH]

    def drop_weaker_faces(i, role_kind):
        """Nested rings nearby, plus same-name duplicates farther (parallel ways / double rings)."""
        s = place[i]
        name_key = (s.get("name") or "").casefold()
        for j in near_same_role(i, role_kind, DEDUPE_M):
            used.add(j)
        if not name_key:
            return
        for j in near_same_role(i, role_kind, SAME_FACE_M):
            if (place[j].get("name") or "").casefold() == name_key:
                used.add(j)

    for i in begins:
        if i in used:
            continue
        s = place[i]
        drop_weaker_faces(i, PLACE_BEGIN)
        c = np.asarray(s["centre"][:2], float)
        partner = None
        best_d = PAIR_M
        for j in ends:
            if j in used:
                continue
            o = place[j]
            if (o.get("name") or "").casefold() == (s.get("name") or "").casefold():
                continue
            if _travel_dot(s, o) < 0.5:
                continue
            d = float(np.hypot(*(c - np.asarray(o["centre"][:2], float)).T))
            if d < best_d:
                best_d = d
                partner = j
        if partner is not None:
            stacked = dict(s)
            stacked["leave_name"] = place[partner].get("name") or ""
            if s.get("source") != "osm" and place[partner].get("source") == "osm":
                stacked["centre"] = list(place[partner]["centre"])
                stacked["at"] = list(place[partner].get("at", stacked.get("at", stacked["centre"][:2])))
            out.append(stacked)
            used.add(i)
            used.add(partner)
            continue
        out.append(s)
        used.add(i)

    for i in ends:
        if i in used:
            continue
        drop_weaker_faces(i, PLACE_END)
        out.append(place[i])
        used.add(i)

    for i in both:
        if i in used:
            continue
        out.append(place[i])
        used.add(i)

    return built + out


def place(frame, rows, mapped, rings, elev, box, decks=()):
    """All roadside city-limit / built-up signs for the tile box."""
    mapped_signs = _place_mapped(frame, rows, mapped, elev, box, decks)
    # Same-name suppress only: (at_xy, name) from placed mapped boards and raw OSM nodes.
    mapped_place = []
    for s in mapped_signs:
        if int(s["kind"]) in PLACE_KINDS and "at" in s:
            mapped_place.append((s["at"], s.get("name") or ""))
    for rec in mapped:
        if int(rec["kind"]) not in PLACE_KINDS:
            continue
        e, n, _ = frame.geodetic_to_enu(rec["lat"], rec["lon"], 0.0)
        mapped_place.append(
            ([float(np.asarray(e).ravel()[0]), float(np.asarray(n).ravel()[0])],
             rec.get("name") or ""))
    synth = _place_synthetic(frame, rows, rings, elev, box, decks, mapped_place) if rings else []
    return _pair_place_signs(mapped_signs + synth)


# --- mesh --------------------------------------------------------------------

def _panel_width(name):
    """Fit the name with side padding; long names shrink letters instead of overflowing."""
    w = exitsigns.text_width(name or "?", LETTER_H) + 0.36
    return float(min(PANEL_MAX_W, max(PANEL_MIN_W, w)))


class Mesh(exitsigns.Mesh):
    def orient(self, sign):
        self.centre = np.asarray(sign["centre"], float)
        travel = np.asarray(sign["travel"], float)
        travel = travel / max(float(np.linalg.norm(travel)), 1e-9)
        self.face = np.r_[-travel, 0.0]          # panel faces oncoming traffic
        self.right = np.array([travel[1], -travel[0], 0.0])

    def poles(self, width, height=POLE_H):
        """Two posts under the board (real E-17a/E-18a), inset from the panel ends."""
        r = 0.025
        span = max(0.25, width * 0.32)
        for y in (-span, span):
            self.box((-0.03 - r, y - r, -0.05), (-0.03 + r, y + r, height), MAT_METAL)

    def _one_board(self, name, bottom, width, depth, strike=False):
        """Single green board with white rim; optional E-18a red diagonal on the traffic face."""
        inset = 0.04
        self.box((-depth, -width / 2, bottom), (depth, width / 2, bottom + PANEL_H), MAT_METAL)
        self.rectangle(width - 0.02, bottom + 0.01, PANEL_H - 0.02, depth + 0.001, MAT_WHITE)
        self.rectangle(width - 2 * inset, bottom + inset, PANEL_H - 2 * inset, depth + 0.002, MAT_GREEN)
        baseline = bottom + (PANEL_H - LETTER_H) * 0.52
        self._letters(name, 0.0, baseline, LETTER_H, width - 0.36, depth=depth + 0.004)
        if strike:
            self._strike(width * 0.40, bottom + 0.06, PANEL_H - 0.12, depth + 0.006)

    def place_panel(self, sign):
        """E-17a / E-18a: green face + white rim; stacked neighbour leave board when leave_name set."""
        raw = (sign.get("name") or "?").replace("ł", "l").replace("Ł", "L")
        name = exitsigns._text(raw)
        leave_raw = (sign.get("leave_name") or "").replace("ł", "l").replace("Ł", "L")
        leave = exitsigns._text(leave_raw) if leave_raw else ""
        width = max(_panel_width(name), _panel_width(leave) if leave else 0.0)
        depth = 0.035
        kind = int(sign["kind"])
        if leave:
            # Street View: E-17a (enter) above E-18a (leave neighbour) on one pole.
            top = POLE_H - PANEL_H - 0.04
            bot = top - STACK_GAP - PANEL_H
            self._one_board(name, top, width, depth, strike=False)
            self._one_board(leave, bot, width, depth, strike=True)
            self.poles(width, height=POLE_H)
            return
        bottom = POLE_H - PANEL_H - 0.04
        if kind == PLACE_BOTH:
            # Legacy double-sided same-name board (tests / old records).
            inset = 0.04
            self.box((-depth, -width / 2, bottom), (depth, width / 2, bottom + PANEL_H), MAT_METAL)
            self.rectangle(width - 0.02, bottom + 0.01, PANEL_H - 0.02, depth + 0.001, MAT_WHITE)
            self.rectangle(width - 2 * inset, bottom + inset, PANEL_H - 2 * inset, depth + 0.002, MAT_GREEN)
            baseline = bottom + (PANEL_H - LETTER_H) * 0.52
            self._letters(name, 0.0, baseline, LETTER_H, width - 0.36, depth=depth + 0.004)
            self.quad([(-depth - 0.001, width / 2 - inset, bottom + inset),
                       (-depth - 0.001, -width / 2 + inset, bottom + inset),
                       (-depth - 0.001, -width / 2 + inset, bottom + PANEL_H - inset),
                       (-depth - 0.001, width / 2 - inset, bottom + PANEL_H - inset)], MAT_GREEN)
            self._letters(name, 0.0, baseline, LETTER_H, width - 0.36, depth=-depth - 0.004, mirror=True)
            self._strike(width * 0.40, bottom + 0.06, PANEL_H - 0.12, -depth - 0.006)
        else:
            self._one_board(name, bottom, width, depth, strike=(kind == PLACE_END))
        self.poles(width)

    def built_panel(self, sign):
        """D-42 begin / D-43 end built-up silhouette."""
        width, height = BUILT_W, BUILT_H
        bottom = POLE_H - height - 0.05
        depth = 0.04
        self.box((-0.02, -width / 2, bottom), (depth, width / 2, bottom + height), MAT_METAL)
        self.rectangle(width - 0.03, bottom + 0.015, height - 0.03, depth + 0.001, MAT_WHITE)
        # Simple black skyline (three blocks).
        base = bottom + 0.08
        for y0, y1, h in ((-0.22, -0.08, 0.28), (-0.06, 0.08, 0.40), (0.10, 0.24, 0.22)):
            self.rectangle(y1 - y0, base, h, depth + 0.003, MAT_BLACK, offset=(y0 + y1) / 2)
        if int(sign["kind"]) == BUILT_END:
            self._strike(width * 0.38, bottom + 0.08, height - 0.16, depth + 0.006)
        self.poles(width)

    def _letters(self, value, y, baseline, height, max_width, depth=0.055, mirror=False):
        data = exitsigns.lettering()
        text = exitsigns._text(value)
        if not text:
            return
        scale = min(height / data["capHeight"], max_width / max(
            sum(data["glyphs"][c]["advance"] for c in text), 1e-6))
        cursor = y - sum(data["glyphs"][c]["advance"] for c in text) * scale / 2
        for c in text:
            glyph = data["glyphs"][c]
            l, t, r, b = glyph["bbox"]
            u0, v0, u1, v1 = glyph["uv"]
            if r > l and b > t:
                if mirror:
                    self.quad([(depth, cursor + r * scale, baseline - b * scale),
                               (depth, cursor + l * scale, baseline - b * scale),
                               (depth, cursor + l * scale, baseline - t * scale),
                               (depth, cursor + r * scale, baseline - t * scale)],
                              MAT_LETTERS, [(u1, v0), (u0, v0), (u0, v1), (u1, v1)])
                else:
                    self.quad([(depth, cursor + l * scale, baseline - b * scale),
                               (depth, cursor + r * scale, baseline - b * scale),
                               (depth, cursor + r * scale, baseline - t * scale),
                               (depth, cursor + l * scale, baseline - t * scale)],
                              MAT_LETTERS, [(u0, v0), (u1, v0), (u1, v1), (u0, v1)])
            cursor += glyph["advance"] * scale

    def _strike(self, half, bottom, height, depth):
        """Red diagonal bar across the panel (front or reverse face)."""
        x0, x1 = -half, half
        z0, z1 = bottom, bottom + height
        for o in (-0.028, 0.0, 0.028):
            if depth >= 0:
                self.quad([(depth, x0 + o, z0), (depth, x0 + o + 0.035, z0),
                           (depth, x1 + o + 0.035, z1), (depth, x1 + o, z1)], MAT_RED)
            else:
                self.quad([(depth, x0 + o + 0.035, z0), (depth, x0 + o, z0),
                           (depth, x1 + o, z1), (depth, x1 + o + 0.035, z1)], MAT_RED)

    def add(self, sign):
        self.orient(sign)
        kind = int(sign["kind"])
        if kind in BUILT_KINDS:
            self.built_panel(sign)
        else:
            self.place_panel(sign)

    def result(self):
        if not self.t:
            return None
        return (np.asarray(self.p), np.asarray(self.t), np.asarray(self.uv),
                MATERIALS, np.asarray(self.mi))


def build(signs, origin):
    mesh = Mesh(origin)
    for sign in signs:
        mesh.add(sign)
    return mesh.result()


def build_tile(md, frame, tx, ty, elev, context, decks=(), left_hand=False, country=None,
               urban=None, widths=None, rings=None):
    """Signstream mesh for tile (tx, ty), or None. rings: optional [(name, xy)] overriding Overture."""
    if (country or "").upper() not in COUNTRIES:
        return None
    box = roads.tile_bbox(tx, ty)
    x0, y0, x1, y1 = box
    pad = 120.0
    la, lo, _ = frame.enu_to_geodetic(np.array([x0 - pad, x1 + pad]),
                                      np.array([y0 - pad, y1 + pad]), 0.0)
    la0, la1 = float(min(la)), float(max(la))
    lo0, lo1 = float(min(lo)), float(max(lo))
    mapped = near(la0, lo0, la1, lo1)
    feats = context.around(np.column_stack([lo, la])) if context is not None else None
    rows = _road_rows(frame, feats, urban, widths, left_hand, box)
    # Synthetic crossings need roads; mapped OSM nodes still plant without a host way in-tile.
    if rings is None:
        rings = _locality_rings(md, frame, box) if rows else []
    elif not rows:
        rings = []
    if not mapped and not rings:
        return None
    signs = place(frame, rows, mapped, rings, elev, box, decks)
    if not signs:
        return None
    return build(signs, (float(x0), float(y0), 0.0))
