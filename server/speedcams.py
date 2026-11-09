"""Polish speed cameras and the signs that announce them.

OpenStreetMap nodes (`highway=speed_camera`) and `type=enforcement` relations
(`enforcement=maxspeed` / `average_speed`) from the bundled index
(tools/build_speedcams.py). A fixed camera also gets a D-51 information sign
upstream, at the middle of the statutory distance band. An average-speed
section gets D-51a at the relation's `from` member and D-51b at `to`.
A mapped `traffic_sign=PL:D-51` / `D-51a` / `D-51b` node replaces the
synthetic board on that approach.

Country gate: PL only (signstream field "speedcams"). The meshes are scenery.
A fixed camera with a bearing and a posted limit is also sent to the mod, which
arms the game's own speed trap on it. Average-speed sections are paired
(D-51a to the D-51b ahead) for the mod's timer.
The statute states metres, chosen from the posted speed and the road class
(D-51: 100–200 m up to 60 km/h, 200–500 m above that, 500–700 m on motorways
and expressways). D-51a / D-51b stand on the section ends, not that far ahead.
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

log = logging.getLogger("earthd.speedcams")

COUNTRIES = {"PL"}
SCHEMA = 1
INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "speedcams.npz")
# Pinned to server/data/speedcams.npz from Geofabrik poland-261107.osm.pbf.
INDEX_SHA256 = "645d31d36e8889838fe528cee97fdbacff7673706300fa0169a25ba29a654a3f"
INDEX_COLUMNS = ("lat", "lon", "osm_id", "kind", "dir_deg", "maxspeed_kmh", "mount", "source")

DEVICE_FIXED, DEVICE_AVERAGE = 0, 1
SIGN_D51, SIGN_D51A, SIGN_D51B = 2, 3, 4
DEVICE_KINDS = {DEVICE_FIXED, DEVICE_AVERAGE}
SIGN_KINDS = {SIGN_D51, SIGN_D51A, SIGN_D51B}
KIND_NAME = {
    DEVICE_FIXED: "fixed", DEVICE_AVERAGE: "average",
    SIGN_D51: "d51", SIGN_D51A: "d51a", SIGN_D51B: "d51b",
}
MOUNT_POLE, MOUNT_GANTRY = 0, 1
SOURCE_OSM, SOURCE_SYNTH = 0, 1

# Middle of each statutory band (metres along the carriageway, upstream).
WARN_NEAR_M, WARN_MID_M, WARN_FAST_M = 150.0, 350.0, 600.0
SUPPRESS_D51_M = 800.0          # a mapped D-51 this far upstream replaces the synthetic
SUPPRESS_END_M = 80.0           # a mapped D-51a / D-51b this close to from / to wins
APPROACH_DEG = 50.0             # mapped D-51 must lie on the monitored approach
ON_LINE_M = 1.5                 # a node this close to the centreline has no roadside side
WAY_NEAR_M = 40.0               # a camera beside the way, not a vertex of it
LANE_NUDGE_M = 2.0              # a device this close to the axis moves onto the shoulder
OUT_M = 1.6                     # m past the pavement edge for an information sign
SNAP_M = 55.0
LOOSE_SNAP_M = 120.0            # no host road within this: the object is dropped
CLEAR_DECK_M = 1.0
M_PER_DEG = 111320.0

CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
           "residential", "living_street", "service"}
_RANK = {"motorway": 7, "trunk": 6, "primary": 5, "secondary": 4, "tertiary": 3,
         "unclassified": 2, "residential": 1, "living_street": 1, "service": 0}
_REJECT_ENFORCEMENT = {
    "traffic_signals", "toll", "access", "maxheight", "maxweight",
    "mindistance", "seatbelt", "check",
}
_CARDINAL = {
    "n": 0.0, "nne": 22.5, "ne": 45.0, "ene": 67.5,
    "e": 90.0, "ese": 112.5, "se": 135.0, "sse": 157.5,
    "s": 180.0, "ssw": 202.5, "sw": 225.0, "wsw": 247.5,
    "w": 270.0, "wnw": 292.5, "nw": 315.0, "nnw": 337.5,
}
_SIGN_TOKENS = (("PL:D-51b", SIGN_D51B), ("PL:D-51a", SIGN_D51A), ("PL:D-51", SIGN_D51))

MATERIALS = [
    "bne_exit_metal",       # 0 pole / gantry
    "bne_merge_yellow",     # 1 camera housing (the casing is yellow)
    "bne_exit_blue",        # 2 D-51 face
    "bne_exit_white",       # 3 pictogram
    "bne_guide_black",      # 4 lens
    "bne_guide_red",        # 5 D-51b strike
]
MAT_METAL, MAT_YELLOW, MAT_BLUE, MAT_WHITE, MAT_BLACK, MAT_RED = 0, 1, 2, 3, 4, 5

_index_lock = threading.Lock()
_warned_index = False
_TEST_INDEX = None
_sections_key = None
_sections_rows = None
_fixed_key = None
_fixed_rows = None


def _finite(value):
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _angdiff(a, b):
    return abs((float(a) - float(b) + 180.0) % 360.0 - 180.0)


def offset_m(lat, lon, east, north):
    """(lat, lon) moved by local metres. Same scale the index walk uses."""
    lat = float(lat)
    mlon = M_PER_DEG * max(0.2, math.cos(math.radians(lat)))
    return lat + float(north) / M_PER_DEG, float(lon) + float(east) / mlon


def distance_m(lat1, lon1, lat2, lon2):
    """Equirectangular metres, accurate for the few hundred metres a sign sits back."""
    lat1, lon1, lat2, lon2 = float(lat1), float(lon1), float(lat2), float(lon2)
    mlon = M_PER_DEG * max(0.2, math.cos(math.radians((lat1 + lat2) * 0.5)))
    return math.hypot((lon2 - lon1) * mlon, (lat2 - lat1) * M_PER_DEG)


def bearing_deg(lat1, lon1, lat2, lon2):
    """Initial compass bearing, degrees clockwise from north."""
    lat1, lon1, lat2, lon2 = (math.radians(float(v)) for v in (lat1, lon1, lat2, lon2))
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def warn_distance_m(maxspeed_kmh, expressway=False):
    """Metres upstream for D-51: the middle of the band the statute allows."""
    if expressway:
        return WARN_FAST_M
    if not _finite(maxspeed_kmh) or float(maxspeed_kmh) <= 60.0:
        return WARN_NEAR_M
    return WARN_MID_M


def expressway(highway, ref=""):
    """Motorway, or a Polish A/S number (an expressway is sometimes tagged trunk)."""
    hw = str(highway or "")
    if hw in ("motorway", "motorway_link"):
        return True
    token = "".join(str(ref or "").upper().split())
    return len(token) >= 2 and token[0] in "AS" and token[1].isdigit()


def parse_maxspeed(raw):
    """km/h from an OSM maxspeed, or NaN (a national code, 'signals', no number)."""
    if raw is None:
        return float("nan")
    text = str(raw).strip().lower().split("@", 1)[0].strip()
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(mph|km/h|kmh|knots)?", text)
    if not match:
        return float("nan")
    value = float(match.group(1))
    unit = match.group(2) or ""
    if unit == "mph":
        value *= 1.609344
    elif unit == "knots":
        value *= 1.852
    if value < 5.0 or value > 200.0:
        return float("nan")
    return value


def parse_direction(raw):
    """('forward'|'backward'|'both'|'deg'|'unknown', [azimuths]).

    Two numeric or cardinal values about 180° apart are both monitored directions.
    `forward` / `backward` stay relative to the OSM way until assemble() resolves them.
    """
    if raw is None or str(raw).strip() == "":
        return "unknown", []
    parts = [p.strip().lower() for p in re.split(r"[;|,/]", str(raw)) if p.strip()]
    if len(parts) == 1 and parts[0] in ("forward", "backward", "both"):
        return parts[0], []
    degs = []
    for part in parts:
        if part in _CARDINAL:
            degs.append(_CARDINAL[part])
            continue
        if part in ("forward", "backward", "both"):
            continue
        try:
            degs.append(float(part) % 360.0)
        except ValueError:
            continue
    if not degs:
        return "unknown", []
    if len(degs) >= 2 and _angdiff(degs[0], degs[1]) >= 150.0:
        return "both", degs[:2]
    return "deg", degs[:1]


def parse_camera_tags(tags):
    """highway=speed_camera → camera fields, or None.

    `enforcement=traffic_signals` (and toll / access / …) is not a speed camera.
    `enforcement=average_speed` on the node marks a section gantry.
    """
    if not isinstance(tags, dict):
        return None
    if str(tags.get("highway") or "") != "speed_camera":
        return None
    enf = str(tags.get("enforcement") or "").strip().lower()
    if enf in _REJECT_ENFORCEMENT:
        return None
    mode, degs = parse_direction(tags.get("direction"))
    kind = DEVICE_AVERAGE if enf == "average_speed" else DEVICE_FIXED
    mount = MOUNT_GANTRY if str(tags.get("man_made") or "") == "gantry" else MOUNT_POLE
    return dict(kind=kind, maxspeed_kmh=parse_maxspeed(tags.get("maxspeed")),
                mode=mode, degs=degs, mount=mount)


def parse_sign_tags(tags):
    """traffic_sign=PL:D-51 / D-51a / D-51b → {kind, dir_deg}, or None."""
    if not isinstance(tags, dict):
        return None
    parts = {p.strip() for p in re.split(r"[;|,]", str(tags.get("traffic_sign") or "")) if p.strip()}
    for key, kind in _SIGN_TOKENS:
        if key not in parts:
            continue
        mode, degs = parse_direction(tags.get("direction"))
        deg = degs[0] if mode in ("deg", "both") and degs else float("nan")
        return dict(kind=kind, dir_deg=deg)
    return None


def _member(item):
    if isinstance(item, (tuple, list)) and len(item) >= 2:
        typ, ref = item[0], item[1]
        role = item[2] if len(item) > 2 else ""
    else:
        return None
    typ = str(typ or "").lower()
    if typ in ("node", "n"):
        typ = "n"
    elif typ in ("way", "w"):
        typ = "w"
    else:
        typ = typ[:1]
    try:
        ref = int(ref)
    except (TypeError, ValueError):
        return None
    return typ, ref, str(role or "").lower()


def parse_enforcement(tags, members):
    """A maxspeed or average_speed enforcement relation, or None.

    members are (type, ref, role). Only node members with role device / from / to
    are kept. Red-light, toll and the other enforcement values are refused.
    """
    if not isinstance(tags, dict) or str(tags.get("type") or "") != "enforcement":
        return None
    enf = str(tags.get("enforcement") or "").strip().lower()
    if enf not in ("maxspeed", "average_speed"):
        return None
    devices, froms, tos = [], [], []
    for item in members or ():
        got = _member(item)
        if got is None or got[0] != "n":
            continue
        _, ref, role = got
        if role == "device":
            devices.append(ref)
        elif role == "from":
            froms.append(ref)
        elif role == "to":
            tos.append(ref)
    return dict(enforcement=enf, maxspeed_kmh=parse_maxspeed(tags.get("maxspeed")),
                devices=devices, froms=froms, tos=tos)


# --- geometry on an OSM way --------------------------------------------------

def _xy(coords):
    """(n, 2) local metres and cumulative chainage of a lat/lon line."""
    lat0, lon0 = float(coords[0][0]), float(coords[0][1])
    mlon = M_PER_DEG * max(0.2, math.cos(math.radians(lat0)))
    xy = np.array([[(float(lo) - lon0) * mlon, (float(la) - lat0) * M_PER_DEG]
                   for la, lo in coords], float)
    step = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1]))
    cum = np.concatenate([[0.0], np.cumsum(step)])
    return xy, cum, lat0, lon0, mlon


def _project(xy, cum, x, y):
    best = None
    for i in range(len(xy) - 1):
        ax, ay = float(xy[i, 0]), float(xy[i, 1])
        bx, by = float(xy[i + 1, 0]), float(xy[i + 1, 1])
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        if length2 < 1e-8:
            t = 0.0
            px, py = ax, ay
        else:
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
            px, py = ax + t * dx, ay + t * dy
        dist = math.hypot(px - x, py - y)
        chain = float(cum[i]) + t * float(cum[i + 1] - cum[i])
        if best is None or dist < best[0]:
            best = (dist, i, chain, px, py, dx, dy)
    return best


def _at_chain(xy, cum, target):
    target = min(max(float(target), 0.0), float(cum[-1]))
    j = int(np.searchsorted(cum, target, side="right") - 1)
    j = max(0, min(j, len(cum) - 2))
    span = float(cum[j + 1] - cum[j])
    u = 0.0 if span < 1e-6 else (target - float(cum[j])) / span
    return (float(xy[j, 0] + u * (xy[j + 1, 0] - xy[j, 0])),
            float(xy[j, 1] + u * (xy[j + 1, 1] - xy[j, 1])))


def walk_upstream(coords, lat, lon, dir_deg, distance_m):
    """(lat, lon, walked_m) `distance_m` against monitored travel, clamped at the way ends.

    The walk stops where this OSM way stops, which is the junction when the way was
    split there. A short approach therefore gets the sign at the junction, not past it.
    """
    coords = list(coords or [])
    if len(coords) < 2 or not _finite(dir_deg):
        return None
    xy, cum, lat0, lon0, mlon = _xy(coords)
    x = (float(lon) - lon0) * mlon
    y = (float(lat) - lat0) * M_PER_DEG
    got = _project(xy, cum, x, y)
    if got is None:
        return None
    _dist, _i, chain, _px, _py, dx, dy = got
    seg = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
    # Digitization pointing with the monitored traffic: upstream is back down the chainage.
    target = chain - float(distance_m) if _angdiff(seg, dir_deg) <= 90.0 else chain + float(distance_m)
    qx, qy = _at_chain(xy, cum, target)
    walked = abs(min(max(target, 0.0), float(cum[-1])) - chain)
    return lat0 + qy / M_PER_DEG, lon0 + qx / mlon, walked


def side_bearing(coords, lat, lon):
    """Azimuth of the lane whose right shoulder the point occupies, or NaN on the axis.

    Right-hand traffic: a camera on the east side of a northbound digitization watches
    northbound traffic (that shoulder is its right).
    """
    coords = list(coords or [])
    if len(coords) < 2:
        return float("nan")
    xy, cum, lat0, lon0, mlon = _xy(coords)
    x = (float(lon) - lon0) * mlon
    y = (float(lat) - lat0) * M_PER_DEG
    got = _project(xy, cum, x, y)
    if got is None or got[0] < ON_LINE_M:
        return float("nan")
    _dist, _i, _chain, px, py, dx, dy = got
    seg = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
    a = math.radians(seg)
    # Right of travel, in (east, north).
    if (x - px) * math.cos(a) + (y - py) * (-math.sin(a)) >= 0.0:
        return seg
    return (seg + 180.0) % 360.0


def _vertex_index(way, node_id):
    try:
        return list(way["node_ids"]).index(int(node_id))
    except (ValueError, KeyError, TypeError):
        return None


def _vertex_bearing(way, index, forward=True):
    coords = way["coords"]
    index = int(index)
    if index < len(coords) - 1:
        a, b = coords[index], coords[index + 1]
    elif index > 0:
        a, b = coords[index - 1], coords[index]
    else:
        return float("nan")
    az = bearing_deg(a[0], a[1], b[0], b[1])
    return az if forward else (az + 180.0) % 360.0


def _forward_bearing(way, lat, lon, node_id):
    index = _vertex_index(way, node_id)
    if index is not None:
        return _vertex_bearing(way, index, True)
    coords = way["coords"]
    if len(coords) < 2:
        return float("nan")
    xy, cum, lat0, lon0, mlon = _xy(coords)
    x = (float(lon) - lon0) * mlon
    y = (float(lat) - lat0) * M_PER_DEG
    got = _project(xy, cum, x, y)
    if got is None:
        return float("nan")
    _dist, _i, _chain, _px, _py, dx, dy = got
    if abs(dx) + abs(dy) < 1e-8:
        return float("nan")
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def _oneway(way):
    value = way.get("oneway", 0)
    if value in (True, "yes", "1", 1):
        return 1
    if value in ("-1", "reverse", -1):
        return -1
    return 0


def _rank(way):
    hw = str(way.get("highway") or "")
    link = hw.endswith("_link")
    base = hw[:-5] if link else hw
    return (_RANK.get(base, -1), 0 if link else 1)


def _index_ways(ways):
    by_node = {}
    for i, way in enumerate(ways):
        for nid in way.get("node_ids") or ():
            by_node.setdefault(int(nid), []).append(i)
    return by_node


def _nearest_way(lat, lon, ways, max_m):
    best = None
    for way in ways:
        if _rank(way)[0] < 0 or len(way.get("coords") or []) < 2:
            continue
        xy, cum, lat0, lon0, mlon = _xy(way["coords"])
        x = (float(lon) - lon0) * mlon
        y = (float(lat) - lat0) * M_PER_DEG
        dist, *_rest = _project(xy, cum, x, y)
        if dist <= max_m and (best is None or dist < best[0]):
            best = (dist, way)
    return None if best is None else best[1]


def _choose_way(rec, ways, by_node):
    hits = [ways[i] for i in by_node.get(int(rec["osm_id"]), ()) if _rank(ways[i])[0] >= 0]
    if hits:
        return max(hits, key=_rank)
    return _nearest_way(rec["lat"], rec["lon"], ways, WAY_NEAR_M)


def _resolve_dirs(rec, way):
    """Fill dir_deg / dir2_deg. A relation bearing (dir_locked) is left as stored."""
    if rec.get("dir_locked"):
        return
    mode = rec.get("mode") or "unknown"
    degs = rec.get("degs") or []
    if mode == "deg" and degs:
        rec["dir_deg"] = float(degs[0])
        return
    if mode == "both" and len(degs) >= 2:
        rec["dir_deg"] = float(degs[0])
        rec["dir2_deg"] = float(degs[1])
        return
    if way is None:
        return
    forward = _forward_bearing(way, rec["lat"], rec["lon"], rec["osm_id"])
    if not _finite(forward):
        return
    if mode == "forward":
        rec["dir_deg"] = forward
    elif mode == "backward":
        rec["dir_deg"] = (forward + 180.0) % 360.0
    elif mode == "both":
        rec["dir_deg"] = forward
        rec["dir2_deg"] = (forward + 180.0) % 360.0
    elif _oneway(way) != 0:
        rec["dir_deg"] = forward if _oneway(way) > 0 else (forward + 180.0) % 360.0
    else:
        rec["dir_deg"] = side_bearing(way["coords"], rec["lat"], rec["lon"])


def _pair_bearing(from_id, to_id, coord, ways, by_node):
    flat, flon = coord[from_id]
    tlat, tlon = coord[to_id]
    chord = bearing_deg(flat, flon, tlat, tlon)
    shared = set(by_node.get(int(from_id), ())) & set(by_node.get(int(to_id), ()))
    if not shared:
        return chord
    way = max((ways[i] for i in shared), key=_rank)
    index = _vertex_index(way, from_id)
    az = _vertex_bearing(way, index, True) if index is not None else chord
    if _finite(az) and _angdiff(az, chord) > 90.0:
        az = (az + 180.0) % 360.0
    return az if _finite(az) else chord


def _bearing_for(node_id, pairs, bearings):
    for (frm, to), bearing in zip(pairs, bearings):
        if int(node_id) in (int(frm), int(to)):
            return bearing
    return bearings[0] if bearings else float("nan")


def _new_device(osm_id, lat, lon, kind, maxspeed, mode, degs, mount, from_relation):
    return dict(
        lat=float(lat), lon=float(lon), osm_id=int(osm_id), kind=int(kind),
        dir_deg=float("nan"), dir2_deg=float("nan"),
        maxspeed_kmh=float(maxspeed) if _finite(maxspeed) else float("nan"),
        mount=int(mount), source=SOURCE_OSM, from_relation=bool(from_relation),
        mode=mode, degs=list(degs or []), dir_locked=False,
    )


def _apply_relation(devices, parsed, coord, ways, by_node):
    """Relation maxspeed and from→to bearing replace whatever the device node said."""
    pairs = list(zip(parsed["froms"], parsed["tos"]))
    good, bearings = [], []
    for frm, to in pairs:
        if frm not in coord or to not in coord:
            continue
        good.append((frm, to))
        bearings.append(_pair_bearing(frm, to, coord, ways, by_node))
    kind = DEVICE_AVERAGE if parsed["enforcement"] == "average_speed" else DEVICE_FIXED
    targets = list(parsed["devices"])
    # Average-speed from/to are the D-51a / D-51b ends, not cameras. Only a tagged
    # `device` member becomes DEVICE_AVERAGE (a real gantry mid-section).
    if not targets:
        return
    multi = kind == DEVICE_FIXED and len(bearings) >= 2 and len(targets) == 1
    for nid in targets:
        if nid not in coord:
            continue
        lat, lon = coord[nid]
        rec = devices.get(nid)
        if rec is None:
            rec = _new_device(nid, lat, lon, kind, float("nan"), "unknown", [], MOUNT_POLE, True)
            devices[nid] = rec
        rec["kind"] = kind
        rec["from_relation"] = True
        rec["lat"], rec["lon"] = float(lat), float(lon)
        if _finite(parsed["maxspeed_kmh"]):
            rec["maxspeed_kmh"] = float(parsed["maxspeed_kmh"])
        if not bearings:
            continue
        if multi:
            rec["dir_deg"], rec["dir2_deg"] = float(bearings[0]), float(bearings[1])
        else:
            rec["dir_deg"] = float(_bearing_for(nid, good, bearings))
            rec["dir2_deg"] = float("nan")
        rec["dir_locked"] = True


def _approach_has(mapped, lat, lon, dir_deg, kind, limit_m):
    """True when a mapped sign of `kind` already stands on this upstream approach."""
    back = (float(dir_deg) + 180.0) % 360.0
    for sign in mapped:
        if int(sign["kind"]) != int(kind):
            continue
        if distance_m(lat, lon, sign["lat"], sign["lon"]) > limit_m:
            continue
        if distance_m(lat, lon, sign["lat"], sign["lon"]) < 1.0:
            return True
        if _angdiff(bearing_deg(lat, lon, sign["lat"], sign["lon"]), back) <= APPROACH_DEG:
            return True
    return False


def _near_kind(mapped, lat, lon, kind, metres):
    return any(int(s["kind"]) == int(kind) and distance_m(lat, lon, s["lat"], s["lon"]) <= metres
               for s in mapped)


def _sign(lat, lon, osm_id, kind, dir_deg, source, maxspeed_kmh=float("nan")):
    return dict(
        lat=float(lat), lon=float(lon), osm_id=int(osm_id), kind=int(kind),
        dir_deg=float(dir_deg) if _finite(dir_deg) else float("nan"),
        maxspeed_kmh=float(maxspeed_kmh) if _finite(maxspeed_kmh) else float("nan"),
        mount=MOUNT_POLE, source=int(source),
        from_relation=False,
    )


def _synthetic_fixed(devices, mapped):
    out = []
    for rec in devices:
        if int(rec["kind"]) != DEVICE_FIXED:
            continue
        way = rec.get("_way")
        if way is None:
            continue
        speed = rec["maxspeed_kmh"] if _finite(rec.get("maxspeed_kmh")) else rec.get("_way_speed")
        dist = warn_distance_m(speed, rec.get("_express"))
        for az in (rec.get("dir_deg"), rec.get("dir2_deg")):
            if not _finite(az):
                continue
            if _approach_has(mapped, rec["lat"], rec["lon"], az, SIGN_D51, SUPPRESS_D51_M):
                continue
            walked = walk_upstream(way["coords"], rec["lat"], rec["lon"], az, dist)
            if walked is None:
                continue
            lat, lon, _got = walked
            out.append(_sign(lat, lon, rec["osm_id"], SIGN_D51, az, SOURCE_SYNTH))
    return out


def _synthetic_average(relations, coord, ways, by_node, mapped):
    out = []
    for parsed in relations:
        if parsed["enforcement"] != "average_speed":
            continue
        limit = parsed.get("maxspeed_kmh", float("nan"))
        for frm, to in zip(parsed["froms"], parsed["tos"]):
            if frm not in coord or to not in coord:
                continue
            az = _pair_bearing(frm, to, coord, ways, by_node)
            flat, flon = coord[frm]
            tlat, tlon = coord[to]
            if not _near_kind(mapped, flat, flon, SIGN_D51A, SUPPRESS_END_M):
                out.append(_sign(flat, flon, frm, SIGN_D51A, az, SOURCE_SYNTH, limit))
            if not _near_kind(mapped, tlat, tlon, SIGN_D51B, SUPPRESS_END_M):
                out.append(_sign(tlat, tlon, to, SIGN_D51B, az, SOURCE_SYNTH, limit))
            # Mapped boards keep their OSM facing tag; stamp the monitored bearing and
            # the section limit so the shoulder and the timer use the travel direction.
            for sign in mapped:
                if not _finite(limit) and not _finite(az):
                    break
                if int(sign["kind"]) == SIGN_D51A and distance_m(
                        flat, flon, sign["lat"], sign["lon"]) <= SUPPRESS_END_M:
                    if _finite(az):
                        sign["dir_deg"] = float(az)
                    if _finite(limit):
                        sign["maxspeed_kmh"] = float(limit)
                elif int(sign["kind"]) == SIGN_D51B and distance_m(
                        tlat, tlon, sign["lat"], sign["lon"]) <= SUPPRESS_END_M:
                    if _finite(az):
                        sign["dir_deg"] = float(az)
                    if _finite(limit):
                        sign["maxspeed_kmh"] = float(limit)
    return out


def _public(rec):
    # Device tag / relation first; the nearest OSM way's posted limit when the
    # camera itself has none (Sztum: highway=speed_camera with no maxspeed, the
    # street beside it is 50).
    speed = rec.get("maxspeed_kmh")
    if not _finite(speed):
        speed = rec.get("_way_speed")
    return dict(
        lat=float(rec["lat"]), lon=float(rec["lon"]), osm_id=int(rec["osm_id"]),
        kind=int(rec["kind"]),
        dir_deg=float(rec["dir_deg"]) if _finite(rec.get("dir_deg")) else float("nan"),
        maxspeed_kmh=float(speed) if _finite(speed) else float("nan"),
        mount=int(rec.get("mount") or 0), source=int(rec.get("source") or 0),
        from_relation=bool(rec.get("from_relation")),
    )


def assemble(cameras, signs, relations, ways):
    """Nodes, mapped signs, enforcement relations and OSM ways → index records.

    cameras / signs: {osm_id, lat, lon, tags}. relations: {tags, members}.
    ways: {id, node_ids, coords [(lat, lon)], oneway, highway, maxspeed, ref}.
    A relation wins over the device node for kind, maxspeed and bearing.
    """
    ways = list(ways or [])
    by_node = _index_ways(ways)
    coord = {}
    for way in ways:
        for nid, ll in zip(way.get("node_ids") or (), way.get("coords") or ()):
            coord[int(nid)] = (float(ll[0]), float(ll[1]))
    for row in list(cameras or []) + list(signs or []):
        coord[int(row["osm_id"])] = (float(row["lat"]), float(row["lon"]))

    devices = {}
    for cam in cameras or []:
        parsed = parse_camera_tags(cam.get("tags") or {})
        if parsed is None:
            continue
        devices[int(cam["osm_id"])] = _new_device(
            cam["osm_id"], cam["lat"], cam["lon"], parsed["kind"], parsed["maxspeed_kmh"],
            parsed["mode"], parsed["degs"], parsed["mount"], False)

    parsed_rels = []
    for rel in relations or []:
        parsed = parse_enforcement(rel.get("tags") or {}, rel.get("members") or ())
        if parsed is not None:
            parsed_rels.append(parsed)
            _apply_relation(devices, parsed, coord, ways, by_node)

    for rec in devices.values():
        way = _choose_way(rec, ways, by_node)
        rec["_way"] = way
        _resolve_dirs(rec, way)
        if way is not None:
            rec["_express"] = expressway(way.get("highway"), way.get("ref"))
            rec["_way_speed"] = way.get("maxspeed", float("nan"))

    mapped = []
    for sign in signs or []:
        parsed = parse_sign_tags(sign.get("tags") or {})
        if parsed is None:
            continue
        mapped.append(_sign(sign["lat"], sign["lon"], sign["osm_id"], parsed["kind"],
                            parsed["dir_deg"], SOURCE_OSM))

    out = [_public(rec) for rec in devices.values()]
    out.extend(mapped)
    out.extend(_synthetic_fixed(devices.values(), mapped))
    out.extend(_synthetic_average(parsed_rels, coord, ways, by_node, mapped))
    out.sort(key=lambda r: (r["lat"], r["lon"], r["osm_id"], r["kind"]))
    return out


def summarize(records):
    """Counts the builder prints: devices, bearings, mapped boards, synthetic boards."""
    out = {name: 0 for name in ("fixed", "average", "d51", "d51a", "d51b",
                                "with_dir", "no_dir", "from_relation",
                                "synthetic_signs", "mapped_signs")}
    for rec in records:
        out[KIND_NAME[int(rec["kind"])]] += 1
        if int(rec["kind"]) in DEVICE_KINDS:
            out["with_dir" if _finite(rec.get("dir_deg")) else "no_dir"] += 1
            if rec.get("from_relation"):
                out["from_relation"] += 1
        elif int(rec.get("source") or 0) == SOURCE_SYNTH:
            out["synthetic_signs"] += 1
        else:
            out["mapped_signs"] += 1
    return out


# --- average-speed sections (mod timer; the mesh stays render-only) ----------

SECTION_MIN_M = 200.0           # a D-51b beside the start is the other direction's end
SECTION_MAX_M = 25000.0
SECTION_ANGLE = 60.0            # the chord from D-51a to D-51b, against the device bearing
_DEV_M = 250.0
_END_NEAR_M = 80.0
_END_SAME_M = 200.0
_SAME_DIR = 40.0
_OPPOSITE = 90.0


def _devs_within(devices, lat, lon, metres):
    out = []
    for dev in devices:
        dist = distance_m(lat, lon, dev["lat"], dev["lon"])
        if dist <= metres:
            out.append((dist, dev))
    out.sort(key=lambda item: item[0])
    return out


def _section_bearings(start, devices):
    """Candidate monitored bearings at a D-51a, with the section limit for each.

    OSM often tags the board's face, not the travel direction (~180° off). A
    nearby average-speed device, when present, is the travel direction. Try each
    distinct nearby device bearing, then the sign's own and its reverse.
    """
    out = []
    seen = []

    def add(az, limit):
        if not _finite(az):
            return
        az = float(az) % 360.0
        if any(_angdiff(az, prev) < 20.0 for prev in seen):
            return
        seen.append(az)
        out.append((az, float(limit) if _finite(limit) else float("nan")))

    near = _devs_within(devices, start["lat"], start["lon"], _DEV_M)
    for _dist, dev in near:
        add(dev["dir_deg"], dev.get("maxspeed_kmh"))
    add(start.get("dir_deg"), start.get("maxspeed_kmh"))
    if _finite(start.get("dir_deg")):
        add(float(start["dir_deg"]) + 180.0, start.get("maxspeed_kmh"))
    if not out and near:
        add(near[0][1]["dir_deg"], near[0][1].get("maxspeed_kmh"))
    return out


def pair_sections(records):
    """Pair each D-51a with the D-51b ahead of it.

    Try every plausible monitored bearing at the start (each nearby average
    device, and the sign facing ±180°). A D-51b a few dozen metres away is the
    oncoming section's end. Each board is used once.
    """
    records = list(records or [])
    starts = [r for r in records if int(r.get("kind", -1)) == SIGN_D51A]
    ends = [r for r in records if int(r.get("kind", -1)) == SIGN_D51B]
    devices = [r for r in records
               if int(r.get("kind", -1)) == DEVICE_AVERAGE and _finite(r.get("dir_deg"))]
    cands = []
    for si, start in enumerate(starts):
        for az, limit in _section_bearings(start, devices):
            if not _finite(limit):
                for _dist, dev in _devs_within(devices, start["lat"], start["lon"], _DEV_M):
                    if _angdiff(dev["dir_deg"], az) <= _SAME_DIR and _finite(dev.get("maxspeed_kmh")):
                        limit = float(dev["maxspeed_kmh"])
                        break
            for ei, end in enumerate(ends):
                dist = distance_m(start["lat"], start["lon"], end["lat"], end["lon"])
                if dist < SECTION_MIN_M or dist > SECTION_MAX_M:
                    continue
                chord = bearing_deg(start["lat"], start["lon"], end["lat"], end["lon"])
                if _angdiff(chord, az) > SECTION_ANGLE:
                    continue
                end_near = _devs_within(devices, end["lat"], end["lon"], _END_NEAR_M)
                if end_near and _angdiff(end_near[0][1]["dir_deg"], az) > _OPPOSITE:
                    same = [d for _dist, d in _devs_within(devices, end["lat"], end["lon"], _END_SAME_M)
                            if _angdiff(d["dir_deg"], az) <= _SAME_DIR]
                    if not same:
                        continue
                cands.append((dist, si, ei, az, limit))
    cands.sort(key=lambda item: item[0])
    used_s, used_e = set(), set()
    out = []
    seen = set()
    for dist, si, ei, az, limit in cands:
        if si in used_s or ei in used_e:
            continue
        used_s.add(si)
        used_e.add(ei)
        start, end = starts[si], ends[ei]
        sid = int(start.get("osm_id") or 0)
        if sid in seen:
            sid = -len(seen) - 1
        seen.add(sid)
        out.append(dict(
            id=sid,
            a_lat=float(start["lat"]), a_lon=float(start["lon"]),
            b_lat=float(end["lat"]), b_lon=float(end["lon"]),
            dir_deg=float(az),
            limit_kmh=float(limit) if _finite(limit) else float("nan"),
            length_m=float(dist),
        ))
    out.sort(key=lambda row: (row["a_lat"], row["a_lon"], row["id"]))
    return out


def _section_row(sec):
    row = {
        "id": int(sec["id"]),
        "a": [float(sec["a_lat"]), float(sec["a_lon"])],
        "b": [float(sec["b_lat"]), float(sec["b_lon"])],
        "dir": round(float(sec["dir_deg"]), 1),
        "len": int(round(float(sec["length_m"]))),
    }
    if _finite(sec.get("limit_kmh")):
        row["limit"] = round(float(sec["limit_kmh"]), 1)
    return row


# --- bundled index -----------------------------------------------------------

def _empty_arrays():
    return {name: np.zeros(0, dtype=dt) for name, dt in (
        ("lat", np.float64), ("lon", np.float64), ("osm_id", np.int64),
        ("kind", np.int8), ("dir_deg", np.float32), ("maxspeed_kmh", np.float32),
        ("mount", np.int8), ("source", np.int8))}


@lru_cache(maxsize=1)
def _index():
    """Latitude-sorted devices and signs, or empty arrays when the file is missing."""
    global _warned_index
    if _TEST_INDEX is not None:
        return _TEST_INDEX
    try:
        with open(INDEX_PATH, "rb") as source:
            digest = hashlib.sha256()
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
            got = digest.hexdigest()
        with np.load(INDEX_PATH, allow_pickle=False) as data:
            schema = int(np.asarray(data["schema"]).reshape(-1)[0]) if "schema" in data.files else 0
            if schema != SCHEMA:
                raise ValueError(f"speedcams schema {schema} != {SCHEMA}")
            arrays = {name: np.array(data[name]) for name in INDEX_COLUMNS}
        n = len(arrays["lat"])
        if any(a.shape != (n,) for a in arrays.values()):
            raise ValueError("speedcams column length mismatch")
        if n and (arrays["lat"][1:] < arrays["lat"][:-1]).any():
            raise ValueError("speedcams not sorted by latitude")
        for arr in arrays.values():
            arr.flags.writeable = False
        if got != INDEX_SHA256 and n and not _warned_index:
            _warned_index = True
            log.info("speedcams index SHA %s (pinned %s); using file anyway", got, INDEX_SHA256)
        return arrays
    except (OSError, ValueError, TypeError, KeyError) as error:
        if not _warned_index:
            _warned_index = True
            log.warning("speedcams index unavailable (%s); cameras omitted", error)
        return _empty_arrays()


def near(la0, lo0, la1, lo1):
    """Index rows in an inclusive lat/lon window (lo0>lo1 wraps the dateline)."""
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
            dir_deg=float(data["dir_deg"][k]), maxspeed_kmh=float(data["maxspeed_kmh"][k]),
            mount=int(data["mount"][k]), source=int(data["source"][k]),
        ))
    return out


def section_rows():
    """Average-speed sections from the loaded index, for the mod's timer.

    Each row is {id, a:[lat, lon], b:[lat, lon], dir, len, limit?}. `dir` is the
    monitored bearing (degrees clockwise from north) and `len` is the straight
    distance between the boards, in metres. `limit` is omitted when OSM has none.
    """
    global _sections_key, _sections_rows
    with _index_lock:
        data = _index()
    key = id(data)
    if key == _sections_key and _sections_rows is not None:
        return _sections_rows
    n = len(data["lat"])
    records = [dict(
        lat=float(data["lat"][i]), lon=float(data["lon"][i]),
        osm_id=int(data["osm_id"][i]), kind=int(data["kind"][i]),
        dir_deg=float(data["dir_deg"][i]), maxspeed_kmh=float(data["maxspeed_kmh"][i]),
    ) for i in range(n)]
    rows = [_section_row(sec) for sec in pair_sections(records)]
    _sections_key, _sections_rows = key, rows
    return rows


def fixed_rows():
    """Point cameras the mod can arm: a monitored bearing and a posted limit.

    Each row is {id, at:[lat, lon], dir, limit}. `dir` is degrees clockwise from
    north, `limit` is km/h. Average-speed gantries, signs, and a camera with no
    bearing or no limit are left out (the mesh still stands).
    """
    global _fixed_key, _fixed_rows
    with _index_lock:
        data = _index()
    key = id(data)
    if key == _fixed_key and _fixed_rows is not None:
        return _fixed_rows
    rows = []
    seen = set()
    n = len(data["lat"])
    for i in range(n):
        if int(data["kind"][i]) != DEVICE_FIXED:
            continue
        direction = float(data["dir_deg"][i])
        limit = float(data["maxspeed_kmh"][i])
        if not (_finite(direction) and _finite(limit)):
            continue
        sid = int(data["osm_id"][i])
        if sid in seen:
            sid = -len(seen) - 1
        seen.add(sid)
        rows.append({
            "id": sid,
            "at": [float(data["lat"][i]), float(data["lon"][i])],
            "dir": round(direction, 1),
            "limit": round(limit, 1),
        })
    _fixed_key, _fixed_rows = key, rows
    return rows


def set_index_for_tests(records):
    """Replace the loaded index (tests). Pass None to read the file again."""
    global _TEST_INDEX, _sections_key, _sections_rows, _fixed_key, _fixed_rows
    _sections_key, _sections_rows = None, None
    _fixed_key, _fixed_rows = None, None
    _index.cache_clear()
    if records is None:
        _TEST_INDEX = None
        return
    rows = sorted(records, key=lambda r: (r["lat"], r["lon"], r.get("osm_id", 0), r.get("kind", 0)))
    arrays = {
        "lat": np.array([r["lat"] for r in rows], np.float64),
        "lon": np.array([r["lon"] for r in rows], np.float64),
        "osm_id": np.array([r.get("osm_id", 0) for r in rows], np.int64),
        "kind": np.array([r["kind"] for r in rows], np.int8),
        "dir_deg": np.array([r.get("dir_deg", float("nan")) for r in rows], np.float32),
        "maxspeed_kmh": np.array([r.get("maxspeed_kmh", float("nan")) for r in rows], np.float32),
        "mount": np.array([r.get("mount", 0) for r in rows], np.int8),
        "source": np.array([r.get("source", 0) for r in rows], np.int8),
    }
    for arr in arrays.values():
        arr.flags.writeable = False
    _TEST_INDEX = arrays


def write_arrays(records, output):
    """Latitude-sorted npz. Returns the file's SHA256 (pin INDEX_SHA256 to it)."""
    rows = sorted(records, key=lambda r: (r["lat"], r["lon"], r["osm_id"], r["kind"]))
    arrays = {
        "lat": np.array([r["lat"] for r in rows], np.float64),
        "lon": np.array([r["lon"] for r in rows], np.float64),
        "osm_id": np.array([r["osm_id"] for r in rows], np.int64),
        "kind": np.array([r["kind"] for r in rows], np.int8),
        "dir_deg": np.array([r["dir_deg"] for r in rows], np.float32),
        "maxspeed_kmh": np.array([r["maxspeed_kmh"] for r in rows], np.float32),
        "mount": np.array([r["mount"] for r in rows], np.int8),
        "source": np.array([r["source"] for r in rows], np.int8),
    }
    output = os.fspath(output)
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    np.savez_compressed(output, schema=np.int32(SCHEMA), **arrays)
    digest = hashlib.sha256()
    with open(output, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# --- placement and mesh ------------------------------------------------------

def wanted(spec):
    """Any driveable paved road, including motorways (cameras stand there)."""
    if spec is None or getattr(spec, "kind", "paved") not in (None, "paved"):
        return False
    cls = str(getattr(spec, "cls", "") or "")
    base = cls[:-5] if cls.endswith("_link") else cls
    return base in CLASSES


def _road_rows(frame, feats, urban, widths, left_hand, box):
    if feats is None or not len(feats):
        return []
    specs = roads.road_specs(feats, urban, widths, left_hand)
    x0, y0, x1, y1 = box
    pad = LOOSE_SNAP_M
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


def _enu_travel(dir_deg, tangent, q, at):
    """Unit travel of the monitored traffic. Without a bearing, the lens looks at the road."""
    if _finite(dir_deg):
        a = math.radians(float(dir_deg))
        return np.array([math.sin(a), math.cos(a)])
    outward = np.asarray(q, float) - np.asarray(at, float)
    norm = float(np.linalg.norm(outward))
    if norm > 0.4:
        return outward / norm
    tangent = np.asarray(tangent, float)
    norm = float(np.linalg.norm(tangent))
    if norm < 1e-9:
        return np.array([1.0, 0.0])
    return tangent / norm


def _travel_dir_for(rec, records):
    """Monitored bearing used for the roadside offset.

    Mapped D-51 boards often store the face direction (~180° from travel). An
    average-speed device next to the board carries the real travel bearing.
    """
    kind = int(rec["kind"])
    own = rec.get("dir_deg")
    if kind not in SIGN_KINDS:
        return own
    best_az, best_d = None, SUPPRESS_END_M
    for other in records:
        if int(other.get("kind", -1)) != DEVICE_AVERAGE:
            continue
        if not _finite(other.get("dir_deg")):
            continue
        dist = distance_m(rec["lat"], rec["lon"], other["lat"], other["lon"])
        if dist <= best_d:
            best_d, best_az = dist, float(other["dir_deg"])
    if best_az is not None:
        return best_az
    return own


def place(frame, rows, records, elev, box, decks=(), left_hand=False):
    """Roadside devices and information signs whose centre falls in `box`."""
    x0, y0, x1, y1 = box
    road_list = [(fid, spec, xy, 0.0) for fid, spec, xy, _half in rows]
    side = -1.0 if left_hand else 1.0
    records = list(records or [])
    out = []
    for rec in records:
        kind = int(rec["kind"])
        # OPP ends are the D-51a / D-51b boards. A DEVICE_AVERAGE minted from the
        # relation's from/to (older indexes) must not draw a camera on top of them.
        if kind == DEVICE_AVERAGE:
            if any(int(o.get("kind", -1)) in (SIGN_D51A, SIGN_D51B)
                   and distance_m(rec["lat"], rec["lon"], o["lat"], o["lon"]) <= SUPPRESS_END_M
                   for o in records):
                continue
        e, n, _ = frame.geodetic_to_enu(rec["lat"], rec["lon"], 0.0)
        q = np.array([float(np.asarray(e).ravel()[0]), float(np.asarray(n).ravel()[0])])
        near_road = exitsigns._nearest(q, road_list) if road_list else None
        if near_road is None or near_road[0] > LOOSE_SNAP_M:
            continue
        _dist, fid, spec, _line, _k, _t, at, tangent = near_road
        half = next((h for f, _s, _xy, h in rows if f == fid),
                    float(getattr(spec, "width", 7.0)) / 2.0)
        travel = _enu_travel(_travel_dir_for(rec, records), tangent, q, at)
        right = np.array([travel[1], -travel[0]])
        if kind in DEVICE_KINDS and near_road[0] < LANE_NUDGE_M:
            centre = at + right * side * (half + 0.8)
        elif kind in DEVICE_KINDS:
            centre = q
        else:
            centre = at + right * side * (half + OUT_M)
        if not (x0 <= centre[0] < x1 and y0 <= centre[1] < y1):
            continue
        if exitsigns._on_deck(centre, decks, CLEAR_DECK_M):
            continue
        z = exitsigns._ground_z(frame, elev, centre)
        out.append(dict(
            centre=[float(centre[0]), float(centre[1]), z],
            travel=[float(travel[0]), float(travel[1])],
            kind=kind, mount=int(rec.get("mount") or 0), half=float(half),
            osm_id=rec.get("osm_id"), source=int(rec.get("source") or 0),
        ))
    return out


class Mesh(exitsigns.Mesh):
    def orient(self, sign):
        self.centre = np.asarray(sign["centre"], float)
        travel = np.asarray(sign["travel"], float)
        travel = travel / max(float(np.linalg.norm(travel)), 1e-9)
        self.face = np.r_[-travel, 0.0]          # the lens and the board face oncoming traffic
        self.right = np.array([travel[1], -travel[0], 0.0])

    def _icon(self, y, bottom, depth, scale=1.0):
        """White camera body and a black lens, on the traffic face."""
        body_w, body_h = 0.18 * scale, 0.11 * scale
        self.rectangle(body_w, bottom, body_h, depth, MAT_WHITE, offset=y)
        self.rectangle(0.055 * scale, bottom + 0.028 * scale, 0.055 * scale, depth + 0.002,
                       MAT_BLACK, offset=y + 0.02 * scale)

    def _board(self, kind):
        bottom, size, depth = 2.05, 0.72, 0.03
        self.box((-0.02, -0.045, 0.0), (0.02, 0.045, bottom), MAT_METAL)
        self.rectangle(size, bottom, size, depth, MAT_WHITE)
        self.rectangle(size - 0.06, bottom + 0.03, size - 0.06, depth + 0.002, MAT_BLUE)
        if kind == SIGN_D51A:
            self._icon(-0.12, bottom + 0.28, depth + 0.004, 0.85)
            self._icon(0.12, bottom + 0.28, depth + 0.004, 0.85)
        else:
            self._icon(0.0, bottom + 0.30, depth + 0.004, 1.15)
        if kind == SIGN_D51B:
            self.quad([(depth + 0.008, -0.22, bottom + 0.12),
                       (depth + 0.008, -0.16, bottom + 0.12),
                       (depth + 0.008, 0.22, bottom + size - 0.12),
                       (depth + 0.008, 0.16, bottom + size - 0.12)], MAT_RED)

    def _device(self, sign):
        if int(sign.get("mount") or 0) == MOUNT_GANTRY:
            span = max(3.0, float(sign.get("half") or 3.5) * 2.0)
            self.box((-0.06, -span / 2, 0.0), (0.06, -span / 2 + 0.12, 5.4), MAT_METAL)
            self.box((-0.06, span / 2 - 0.12, 0.0), (0.06, span / 2, 5.4), MAT_METAL)
            self.box((-0.05, -span / 2, 5.25), (0.08, span / 2, 5.42), MAT_METAL)
            self.box((-0.08, -0.20, 4.75), (0.18, 0.20, 5.22), MAT_YELLOW)
            self.rectangle(0.08, 4.90, 0.08, 0.20, MAT_BLACK)
            return
        self.box((-0.04, -0.04, 0.0), (0.04, 0.04, 2.85), MAT_METAL)
        self.box((-0.10, -0.18, 2.55), (0.16, 0.18, 3.05), MAT_YELLOW)
        self.rectangle(0.08, 2.70, 0.08, 0.18, MAT_BLACK)

    def add(self, sign):
        self.orient(sign)
        kind = int(sign["kind"])
        if kind in SIGN_KINDS:
            self._board(kind)
        else:
            self._device(sign)

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
               urban=None, widths=None):
    """Signstream mesh for tile (tx, ty), or None. `md` is unused (the index is bundled)."""
    del md
    if (country or "").upper() not in COUNTRIES:
        return None
    box = roads.tile_bbox(tx, ty)
    x0, y0, x1, y1 = box
    # Signs are already positioned in the index (a D-51 is up to 600 m from its
    # camera, and a tile is 256 m), so the query only needs the snap margin.
    pad = LOOSE_SNAP_M
    la, lo, _ = frame.enu_to_geodetic(np.array([x0 - pad, x1 + pad]),
                                      np.array([y0 - pad, y1 + pad]), 0.0)
    records = near(float(min(la)), float(min(lo)), float(max(la)), float(max(lo)))
    if not records:
        return None
    feats = context.around(np.column_stack([lo, la])) if context is not None else None
    rows = _road_rows(frame, feats, urban, widths, left_hand, box)
    placed = place(frame, rows, records, elev, box, decks, left_hand)
    if not placed:
        return None
    return build(placed, (float(x0), float(y0), 0.0))
