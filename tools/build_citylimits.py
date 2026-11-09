"""Build the bundled OSM city-limit / built-up sign index (server/citylimits.py).

    python tools/build_citylimits.py --poland
    python tools/build_citylimits.py https://download.geofabrik.de/europe/poland-latest.osm.pbf
    python tools/build_citylimits.py path/to/extract.osm.pbf
    python tools/build_citylimits.py --empty

On Windows, HTTPS URLs are downloaded to a temp PBF (with MB/s logs), scanned, then deleted —
osmium cannot reliably read a curl pipe via stdin there (hangs). Linux still streams curl → osmium
through /proc/self/fd. Data: © OpenStreetMap contributors, ODbL 1.0.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import citylimits  # noqa: E402

DEFAULT_OUTPUT = ROOT / "server" / "data" / "citylimits.npz"
DEFAULT_URL = "https://download.geofabrik.de/europe/poland-latest.osm.pbf"
USER_AGENT = "BeamNG.Earth citylimits build"
LOG_EVERY_S = 5.0
LOG_EVERY_SIGNS = 500
DOWNLOAD_CHUNK = 1024 * 1024


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def records_from_json(path):
    """[{lat, lon, osm_id, tags}] -> citylimits records."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = doc["elements"] if isinstance(doc, dict) and "elements" in doc else doc
    out = []
    for row in rows:
        tags = row.get("tags") or {}
        parsed = citylimits.parse_tags(tags)
        if parsed is None:
            continue
        out.append(dict(
            lat=float(row["lat"]), lon=float(row["lon"]), osm_id=int(row.get("osm_id") or row.get("id") or 0),
            kind=parsed["kind"], name=parsed["name"], dir_deg=parsed["dir_deg"],
        ))
    return out


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


def _download_url(url, dest):
    """Download url into dest with MB progress logs (curl preferred, urllib fallback)."""
    dest = Path(dest)
    log(f"download → {dest}")
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            ["curl", "-fL", "--retry", "5", "--retry-delay", "2",
             "-A", USER_AGENT, "-o", str(dest), url],
            stderr=subprocess.PIPE,
        )
        log(f"curl pid={proc.pid} writing {dest.name}")
        stop = threading.Event()

        def watch():
            while not stop.wait(LOG_EVERY_S):
                try:
                    n = dest.stat().st_size if dest.is_file() else 0
                except OSError:
                    n = 0
                elapsed = max(time.time() - t0, 1e-3)
                log(f"downloading… {n / 1e6:.1f} MB in {elapsed:.0f}s "
                    f"({n / elapsed / 1e6:.2f} MB/s) curl={'alive' if proc.poll() is None else 'done'}")

        err_box = []

        def drain_err():
            try:
                err_box.append(proc.stderr.read() if proc.stderr else b"")
            except Exception:
                err_box.append(b"")

        threading.Thread(target=watch, name="dl-watch", daemon=True).start()
        threading.Thread(target=drain_err, name="dl-err", daemon=True).start()
        rc = proc.wait()
        stop.set()
        if rc != 0:
            msg = (err_box[0] if err_box else b"").decode("utf-8", "replace").strip() or f"exit {rc}"
            raise SystemExit(f"curl failed: {msg}")
    except FileNotFoundError:
        log("curl not found; falling back to urllib")
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as out:
            total = resp.headers.get("Content-Length")
            total_mb = f"{int(total) / 1e6:.0f} MB" if total else "?"
            log(f"HTTP {resp.status}; Content-Length={total_mb}")
            got, last = 0, t0
            while True:
                chunk = resp.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                now = time.time()
                if now - last >= LOG_EVERY_S:
                    elapsed = max(now - t0, 1e-3)
                    log(f"downloading… {got / 1e6:.1f} MB in {elapsed:.0f}s "
                        f"({got / elapsed / 1e6:.2f} MB/s)")
                    last = now
    size = dest.stat().st_size
    elapsed = max(time.time() - t0, 1e-3)
    log(f"download complete: {size / 1e6:.1f} MB in {elapsed:.0f}s ({size / elapsed / 1e6:.2f} MB/s)")
    if size < 1_000_000:
        raise SystemExit(f"download looks too small ({size} bytes) — aborting")


def _collect(fp, osmium):
    """Scan traffic_sign nodes; heartbeat every LOG_EVERY_S."""
    out = []
    t0 = time.time()
    state = {"seen": 0, "done": False}

    def heartbeat():
        while not state["done"]:
            time.sleep(LOG_EVERY_S)
            if state["done"]:
                break
            log(f"scanning… {time.time() - t0:.0f}s | traffic_sign hits={state['seen']:,} | kept={len(out):,}")

    threading.Thread(target=heartbeat, name="scan-heartbeat", daemon=True).start()
    log("osmium: scanning nodes with traffic_sign=* …")
    try:
        for o in fp.with_filter(osmium.filter.KeyFilter("traffic_sign")):
            state["seen"] += 1
            if not o.is_node() or not o.location.valid():
                continue
            tags = {t.k: t.v for t in o.tags}
            parsed = citylimits.parse_tags(tags)
            if parsed is None:
                continue
            out.append(dict(
                lat=float(o.location.lat), lon=float(o.location.lon), osm_id=int(o.id),
                kind=parsed["kind"], name=parsed["name"], dir_deg=parsed["dir_deg"],
            ))
            if len(out) == 1 or len(out) % LOG_EVERY_SIGNS == 0:
                kind = citylimits.KIND_NAME.get(parsed["kind"], str(parsed["kind"]))
                log(f"kept {len(out):,} | last osm_id={o.id} {kind} "
                    f"name={parsed['name']!r} ({time.time() - t0:.0f}s)")
    finally:
        state["done"] = True
    log(f"scan done in {time.time() - t0:.1f}s: {state['seen']:,} traffic_sign objects, "
        f"{len(out):,} kept for index")
    return out


def _scan_path(path):
    osmium = _require_osmium()
    size_mb = Path(path).stat().st_size / 1e6
    log(f"osmium opening {path} ({size_mb:.1f} MB)")
    fp = osmium.FileProcessor(str(path), osmium.osm.NODE)
    try:
        return _collect(fp, osmium)
    finally:
        # Drop the processor so Windows can delete a temp PBF afterwards.
        del fp


def _scan_linux_pipe(url):
    """curl | osmium via /proc/self/fd (Linux only)."""
    osmium = _require_osmium()
    log(f"Linux pipe stream: {url}")
    proc = subprocess.Popen(
        ["curl", "-fL", "--retry", "5", "--retry-delay", "2", "-A", USER_AGENT, url],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1024 * 1024,
    )
    log(f"curl pid={proc.pid}")

    def drain():
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    log(f"curl: {line}")
        except Exception:
            pass

    threading.Thread(target=drain, daemon=True).start()
    fd = proc.stdout.fileno()
    try:
        fp = osmium.FileProcessor(osmium.io.File(f"/proc/self/fd/{fd}", "pbf"), osmium.osm.NODE)
        return _collect(fp, osmium)
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        rc = proc.wait()
        if rc not in (0, None):
            raise SystemExit(f"curl exit {rc}")
        log(f"curl finished exit={rc}")


def records_from_pbf(src):
    """HTTPS URL or local PBF → citylimits records."""
    log(f"reading {src}")
    log(f"worker pid={os.getpid()}")
    if src.startswith(("http://", "https://")):
        if os.name == "nt" or os.environ.get("BNE_CITYLIMITS_TEMP") == "1":
            # Windows: pipe→stdin hangs with osmium; download to temp then scan.
            fd, name = tempfile.mkstemp(prefix="citylimits_", suffix=".osm.pbf")
            os.close(fd)  # must close before curl/osmium reopen the path
            tmp = Path(name)
            try:
                _download_url(src, tmp)
                return _scan_path(tmp)
            finally:
                for attempt in range(5):
                    try:
                        tmp.unlink(missing_ok=True)
                        log(f"deleted temp {tmp.name}")
                        break
                    except OSError as error:
                        if attempt == 4:
                            log(f"temp cleanup failed (safe to delete manually): {error}")
                        else:
                            time.sleep(0.5)
        return _scan_linux_pipe(src)
    path = Path(src)
    if not path.is_file():
        raise SystemExit(
            f"PBF not found: {src}\n"
            f"Try: python tools/build_citylimits.py --poland"
        )
    return _scan_path(path)


def write_index(records, output):
    """Sort by lat and write citylimits.npz; print SHA256 for pinning in citylimits.py."""
    log(f"writing {len(records):,} records → {output}")
    records = sorted(records, key=lambda r: (r["lat"], r["lon"], r["osm_id"]))
    n = len(records)
    arrays = {
        "lat": np.array([r["lat"] for r in records], np.float64),
        "lon": np.array([r["lon"] for r in records], np.float64),
        "osm_id": np.array([r["osm_id"] for r in records], np.int64),
        "kind": np.array([r["kind"] for r in records], np.int8),
        "dir_deg": np.array([r["dir_deg"] for r in records], np.float32),
        "name": np.array([r["name"] for r in records], dtype="U80"),
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output, schema=np.int32(citylimits.SCHEMA), **arrays)
    digest = sha256(output)
    log(f"{n} signs; {output.stat().st_size} bytes; SHA256 {digest}")
    counts = {}
    for r in records:
        label = citylimits.KIND_NAME.get(int(r["kind"]), str(r["kind"]))
        counts[label] = counts.get(label, 0) + 1
    log(f"counts: {dict(sorted(counts.items()))}")
    log(f"Pin INDEX_SHA256 = {digest!r} in server/citylimits.py")
    return digest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", help=f"OSM PBF path or HTTPS URL")
    ap.add_argument("--json", type=Path, help="JSON list of {lat,lon,osm_id,tags}")
    ap.add_argument("--empty", action="store_true", help="write an empty schema-valid index")
    ap.add_argument("--poland", action="store_true", help=f"fetch {DEFAULT_URL}")
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    a = ap.parse_args()
    log(f"build_citylimits start pid={os.getpid()}")
    if a.empty:
        records = []
    elif a.json:
        log(f"loading JSON {a.json}")
        records = records_from_json(a.json)
    elif a.poland:
        records = records_from_pbf(DEFAULT_URL)
    elif a.source:
        records = records_from_pbf(a.source)
    else:
        ap.error("pass a PBF path/URL, --poland, --json, or --empty")
    write_index(records, a.output)
    log("done")


if __name__ == "__main__":
    main()
