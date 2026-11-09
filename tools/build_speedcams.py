"""Build the bundled OSM speed-camera index (server/speedcams.py).

    python tools/build_speedcams.py
    python tools/build_speedcams.py path/to/extract.osm.pbf
    python tools/build_speedcams.py --empty

Reads highway=speed_camera nodes, traffic_sign=PL:D-51/D-51a/D-51b nodes and
type=enforcement relations (maxspeed / average_speed). Way geometry resolves
forward/backward and walks D-51 back along the carriageway. Data: © OpenStreetMap
contributors, ODbL 1.0.
"""
import argparse
import os
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import speedcams  # noqa: E402

DEFAULT_PBF = ROOT / "poland-latest.osm.pbf"
DEFAULT_OUTPUT = ROOT / "server" / "data" / "speedcams.npz"
LOG_EVERY_S = 5.0
# A way is kept when it contains a wanted node, or passes within this of a camera.
NEAR_M = 45.0
CELL = 0.0012                 # ~130 m; the neighbour ring covers the near test


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _require_osmium():
    try:
        import osmium
        return osmium
    except ImportError as error:
        raise SystemExit(
            "pyosmium is required to read a PBF.\n"
            "  .\\.venv\\Scripts\\python.exe -m pip install osmium\n"
            f"({error})"
        ) from error


def _tags(obj):
    return {t.k: t.v for t in obj.tags}


def _members(obj):
    out = []
    for member in obj.members:
        typ = member.type
        if not isinstance(typ, str):
            typ = str(typ)
        out.append((typ, int(member.ref), member.role or ""))
    return out


def _heartbeat(state, label):
    while not state["done"]:
        time.sleep(LOG_EVERY_S)
        if state["done"]:
            break
        log(f"{label}… {time.time() - state['t0']:.0f}s | seen={state['seen']:,} | kept={state['kept']:,}")


def _scan(path, entities, key, label):
    osmium = _require_osmium()
    state = {"seen": 0, "kept": 0, "done": False, "t0": time.time()}
    threading.Thread(target=_heartbeat, args=(state, label), daemon=True).start()
    fp = osmium.FileProcessor(str(path), entities).with_filter(osmium.filter.KeyFilter(key))
    try:
        yield from _iter(fp, state)
    finally:
        state["done"] = True


def _iter(fp, state):
    try:
        for obj in fp:
            state["seen"] += 1
            yield obj
    except RuntimeError as error:
        if "EOF" in str(error):
            raise SystemExit(
                f"PBF ended early ({error}). The file looks truncated; "
                "Geofabrik's poland-latest.osm.pbf is the complete extract."
            ) from error
        raise


def collect_cameras(path):
    osmium = _require_osmium()
    out = []
    log("osmium: nodes with highway=* (keeping speed_camera)")
    for obj in _scan(path, osmium.osm.NODE, "highway", "cameras"):
        if not obj.is_node() or not obj.location.valid():
            continue
        tags = _tags(obj)
        if tags.get("highway") != "speed_camera":
            continue
        out.append(dict(osm_id=int(obj.id), lat=float(obj.location.lat),
                        lon=float(obj.location.lon), tags=tags))
    log(f"cameras: {len(out):,}")
    return out


def collect_signs(path):
    osmium = _require_osmium()
    out = []
    log("osmium: nodes with traffic_sign=* (keeping PL:D-51 / D-51a / D-51b)")
    for obj in _scan(path, osmium.osm.NODE, "traffic_sign", "signs"):
        if not obj.is_node() or not obj.location.valid():
            continue
        tags = _tags(obj)
        if speedcams.parse_sign_tags(tags) is None:
            continue
        out.append(dict(osm_id=int(obj.id), lat=float(obj.location.lat),
                        lon=float(obj.location.lon), tags=tags))
    log(f"mapped signs: {len(out):,}")
    return out


def collect_relations(path):
    osmium = _require_osmium()
    out = []
    log("osmium: relations with enforcement=*")
    for obj in _scan(path, osmium.osm.RELATION, "enforcement", "relations"):
        if not obj.is_relation():
            continue
        tags = _tags(obj)
        members = _members(obj)
        if speedcams.parse_enforcement(tags, members) is None:
            continue
        out.append(dict(tags=tags, members=members))
    log(f"enforcement relations kept: {len(out):,}")
    return out


class _Grid:
    """Camera positions. `near` is true when a way node is within NEAR_M of one."""

    def __init__(self, points):
        self.bins = defaultdict(list)
        for lat, lon in points:
            self.add(lat, lon)

    def add(self, lat, lon):
        self.bins[(int(float(lat) / CELL), int(float(lon) / CELL))].append((float(lat), float(lon)))

    def near(self, lat, lon):
        i = int(lat / CELL)
        j = int(lon / CELL)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for clat, clon in self.bins.get((i + di, j + dj), ()):
                    if speedcams.distance_m(lat, lon, clat, clon) <= NEAR_M:
                        return True
        return False


def _lookup(store, nid):
    try:
        loc = store.get(int(nid))
    except Exception:
        return None
    if loc is None:
        return None
    try:
        if hasattr(loc, "valid") and not loc.valid():
            return None
        return float(loc.lat), float(loc.lon)
    except Exception:
        return None


def _driveable(highway):
    hw = str(highway or "")
    base = hw[:-5] if hw.endswith("_link") else hw
    return base in speedcams.CLASSES


def collect_ways(path, wanted, points):
    """Highway ways that contain a wanted node or pass near a camera."""
    osmium = _require_osmium()
    grid = _Grid(points)
    state = {"seen": 0, "kept": 0, "done": False, "t0": time.time()}
    threading.Thread(target=_heartbeat, args=(state, "ways"), daemon=True).start()
    log("osmium: ways with highway=* and node locations")
    fp = (osmium.FileProcessor(str(path))
          .with_locations()
          .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
          .with_filter(osmium.filter.KeyFilter("highway")))
    ways = []
    extras = []
    seen_extra = set()
    try:
        resolved = False
        for obj in fp:
            state["seen"] += 1
            if not resolved:
                resolved = True
                store = fp.node_location_storage
                mem = getattr(store, "used_memory", lambda: 0)()
                log(f"node location cache {mem / 1e6:.0f} MB; resolving {len(wanted):,} ids")
                for nid in wanted:
                    if nid in seen_extra:
                        continue
                    ll = _lookup(store, nid)
                    if ll is None:
                        continue
                    extras.append(dict(osm_id=int(nid), lat=ll[0], lon=ll[1], tags={}))
                    seen_extra.add(int(nid))
                    grid.add(ll[0], ll[1])
            if not obj.is_way():
                continue
            tags = _tags(obj)
            if not _driveable(tags.get("highway")):
                continue
            ids, coords = [], []
            hit = False
            for node in obj.nodes:
                ref = int(node.ref)
                loc = node.location
                if not loc.valid():
                    if ref in wanted:
                        hit = True
                    continue
                la, lo = float(loc.lat), float(loc.lon)
                ids.append(ref)
                coords.append((la, lo))
                if not hit and (ref in wanted or grid.near(la, lo)):
                    hit = True
            if not hit or len(coords) < 2:
                continue
            oneway = tags.get("oneway") or 0
            ways.append(dict(
                id=int(obj.id), node_ids=ids, coords=coords, oneway=oneway,
                highway=tags.get("highway") or "",
                maxspeed=speedcams.parse_maxspeed(tags.get("maxspeed")),
                ref=tags.get("ref") or "",
            ))
            state["kept"] = len(ways)
    finally:
        state["done"] = True
    log(f"ways kept: {len(ways):,} (seen {state['seen']:,}); extra nodes {len(extras):,}")
    return ways, extras


def wanted_ids(cameras, signs, relations):
    ids = {int(c["osm_id"]) for c in cameras} | {int(s["osm_id"]) for s in signs}
    for rel in relations:
        for typ, ref, _role in rel["members"]:
            if str(typ).lower()[:1] == "n":
                ids.add(int(ref))
    return ids


def build(path, output):
    t0 = time.time()
    cameras = collect_cameras(path)
    signs = collect_signs(path)
    relations = collect_relations(path)
    wanted = wanted_ids(cameras, signs, relations)
    points = [(c["lat"], c["lon"]) for c in cameras] + [(s["lat"], s["lon"]) for s in signs]
    ways, extras = collect_ways(path, wanted, points)
    # Extras fill coordinates for relation members that are not speed_camera nodes.
    # Empty tags do not create a device; the relation does, when it names them.
    known = {c["osm_id"] for c in cameras} | {s["osm_id"] for s in signs}
    extras = [e for e in extras if e["osm_id"] not in known]
    log(f"assembling {len(cameras):,} cameras, {len(signs):,} signs, "
        f"{len(relations):,} relations, {len(ways):,} ways")
    records = speedcams.assemble(cameras + extras, signs, relations, ways)
    digest = speedcams.write_arrays(records, output)
    stats = speedcams.summarize(records)
    log(f"histogram: {stats}")
    log(f"{len(records):,} rows, SHA256 {digest}, {time.time() - t0:.0f}s")
    log(f"Pin INDEX_SHA256 = {digest!r} in server/speedcams.py")
    return digest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", help="OSM PBF path (default: poland-latest.osm.pbf)")
    ap.add_argument("--empty", action="store_true", help="write an empty schema-valid index")
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = ap.parse_args()
    if args.empty:
        digest = speedcams.write_arrays([], args.output)
        log(f"empty index SHA256 {digest}")
        log(f"Pin INDEX_SHA256 = {digest!r} in server/speedcams.py")
        return
    source = Path(args.source) if args.source else DEFAULT_PBF
    if not source.is_file():
        raise SystemExit(f"PBF not found: {source}")
    log(f"reading {source} ({source.stat().st_size / 1e6:.0f} MB)")
    build(source, args.output)


if __name__ == "__main__":
    main()
