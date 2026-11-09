"""OSM administrative place polygons for city-limit sign synthesis (citylimits.py).

Layercake boundaries (https://data.openstreetmap.us/layercake/boundaries.parquet, ODbL): settlement
admin polygons (village/town/city and admin_level 8–11) read per map cell like picnicsource — footer
once, then only row groups whose bbox touches the cell. Used when OpenStreetMap has no
traffic_sign=city_limit node (e.g. Cygusy) but still has the place boundary.
"""
import logging
import os
import re
import threading
import time

import numpy as np

import mapgeom
import overture
import rangeread

log = logging.getLogger("earthd.placebounds")

URL = "https://data.openstreetmap.us/layercake/boundaries.parquet"
PATH = "osm/boundaries.parquet"
VERSION_TTL_S = 7 * 86400
RETRY_S = 3600
# Settlements only: skip country/region/county. Polish sołectwo / village are often admin_level 8–10
# with or without place=*. Cap bbox so a whole gmina/powiat cannot flood a tile with crossings.
PLACE_VALUES = ("city", "town", "village", "hamlet", "isolated_dwelling", "suburb", "quarter",
                "neighbourhood", "borough", "municipality")
MAX_BBOX_DEG = 0.25             # ~15–25 km; Cygusy ~0.05°, gmina is larger


class Source:
    """Settlement admin rings near a lon/lat box, from the pinned Layercake boundaries file."""

    def __init__(self, reader, fetcher, url=URL):
        self.reader, self.fetcher, self.url = reader, fetcher, url
        self.raw_dir = reader.raw_dir
        self._lock = threading.Lock()
        self._remote = None
        self._checked = 0.0
        self._meta = None
        self._cells = {}            # (etag, lon0, lat0, lon1, lat1 rounded) -> [(name, ring_lonlat)]
        self._down = False

    def rings(self, bounds, kind="demand"):
        """[(name, lonlat (n, 2))] of settlement polygons whose bbox touches bounds.

        bounds: (lon0, lat0, lon1, lat1). Empty when the file is unreachable.
        """
        if self._down:
            return []
        lon0, lat0, lon1, lat1 = (float(v) for v in bounds)
        key = (round(lon0, 4), round(lat0, 4), round(lon1, 4), round(lat1, 4))
        for attempt in (0, 1):
            try:
                etag, remote = self._version(kind)
            except Exception as ex:
                self._down = True
                log.warning("placebounds: Layercake boundaries unavailable (%s: %s)",
                            type(ex).__name__, ex)
                return []
            cached = self._cells.get((etag,) + key)
            if cached is not None:
                return list(cached)
            try:
                rows = self._query(remote, (lon0, lat0, lon1, lat1), kind)
            except rangeread.Changed:
                if attempt:
                    raise
                log.info("placebounds: %s changed (was %s)", self.url, etag)
                self._forget()
                continue
            except Exception as ex:
                self._down = True
                log.warning("placebounds: query failed (%s: %s)", type(ex).__name__, ex)
                return []
            out = _rings_of(rows)
            self._cells[(etag,) + key] = out
            return list(out)
        return []

    def _version(self, kind):
        with self._lock:
            if self._remote is not None and time.time() - self._checked < VERSION_TTL_S:
                return self._remote
            pin = os.path.join(self.raw_dir, "placebounds", "pinned")
            etag = size = None
            try:
                with open(pin) as f:
                    etag, size, when = f.read().split("\n")[:3]
                    size, when = int(size), float(when)
                if time.time() - when > VERSION_TTL_S:
                    etag = None
            except (OSError, ValueError):
                etag = None
            if etag is None:
                down = os.path.join(self.raw_dir, "placebounds", "unreachable")
                try:
                    recent = time.time() - os.path.getmtime(down) < RETRY_S
                except OSError:
                    recent = False
                if recent:
                    raise rangeread.RangeError(f"{self.url}: unreachable at the last try")
                try:
                    r = self.fetcher.get(self.url, kind=kind, headers={"Range": "bytes=0-15"})
                    etag = r.headers.get("etag", "").strip()
                    m = rangeread.CONTENT_RANGE.match(r.headers.get("content-range", ""))
                    if not etag or not m or m.group(3) == "*":
                        raise rangeread.RangeError(f"{self.url}: no ETag or size")
                except Exception:
                    os.makedirs(os.path.dirname(down), exist_ok=True)
                    open(down, "w").close()
                    raise
                size = int(m.group(3))
                os.makedirs(os.path.dirname(pin), exist_ok=True)
                with open(pin + ".tmp", "w") as f:
                    f.write(f"{etag}\n{size}\n{time.time()}\n")
                os.replace(pin + ".tmp", pin)
            d = os.path.join(self.raw_dir, "placebounds", re.sub(r"[^A-Za-z0-9]+", "_", etag).strip("_"))
            self._remote = (etag, rangeread.RemoteFile([self.url], size, d, self.fetcher, if_match=etag))
            self._checked = time.time()
            return self._remote

    def _forget(self):
        with self._lock:
            self._remote = None
            self._meta = None
            self._cells.clear()
            try:
                os.remove(os.path.join(self.raw_dir, "placebounds", "pinned"))
            except OSError:
                pass

    def _groups(self, con, uri, remote, kind):
        with self._lock:
            got = self._meta
        if got is not None and got[0] is remote:
            return got[1]
        tail = remote.read(max(0, remote.size - overture.FOOTER_GUESS), remote.size, kind)
        n = int.from_bytes(tail[-8:-4], "little")
        if n + 8 > len(tail):
            remote.read(remote.size - n - 8, remote.size, kind)
        meta = con.execute(f"""SELECT row_group_id, row_group_num_rows, path_in_schema, stats_min, stats_max,
            dictionary_page_offset, data_page_offset, total_compressed_size FROM parquet_metadata({uri})
            ORDER BY row_group_id, column_id""").fetchall()
        groups, first = {}, 0
        for rg, nrows, col, smin, smax, dict_off, data_off, size in meta:
            g = groups.get(rg)
            if g is None:
                g = groups[rg] = {"first": first, "n": nrows, "bbox": {}, "cols": {}}
                first += nrows
            offs = [o for o in (dict_off, data_off) if o]
            if not offs:
                continue
            top, leaf = col.split(", ")[0], col.split(", ")[-1]
            g["cols"].setdefault(top, []).append((min(offs), min(offs) + size))
            if top == "bbox":
                g["bbox"][leaf] = (smin, smax)
        with self._lock:
            self._meta = (remote, groups)
        return groups

    def _query(self, remote, bounds, kind):
        lon0, lat0, lon1, lat1 = bounds
        rd = self.reader
        files = {PATH: remote}
        con, proto, errors = rd._connect(files, kind)
        try:
            uri = f"'{proto}://{PATH}'"
            groups = self._groups(con, uri, remote, kind)
            cand = _candidates(groups, bounds)
            if not cand:
                return []
            want = {"id", "bbox", "boundary", "admin_level", "place", "name", "geometry"}
            rd._fetch(files, {PATH: [r for rg in cand for c, rs in groups[rg]["cols"].items()
                                     if c in want for r in rs]}, kind)
            only = " OR ".join(f"file_row_number BETWEEN {groups[rg]['first']} AND "
                               f"{groups[rg]['first'] + groups[rg]['n'] - 1}" for rg in cand)
            box = (f"bbox.xmin <= {lon1!r} AND bbox.xmax >= {lon0!r} AND bbox.ymin <= {lat1!r} "
                   f"AND bbox.ymax >= {lat0!r}")
            size = (f"(bbox.xmax - bbox.xmin) < {MAX_BBOX_DEG!r} AND "
                    f"(bbox.ymax - bbox.ymin) < {MAX_BBOX_DEG!r}")
            places = ", ".join(repr(p) for p in PLACE_VALUES)
            # name is a list column in Layercake boundaries; first element is the primary label.
            mark = (f"boundary = 'administrative' AND {size} AND ("
                    f"lower(coalesce(place, '')) IN ({places}) OR "
                    f"TRY_CAST(admin_level AS INTEGER) BETWEEN 8 AND 11)")
            try:
                con.execute("SET enable_geoparquet_conversion = false")
            except Exception:
                pass
            rows = con.execute(f"""
                SELECT id,
                       coalesce(list_extract(name, 1), '') AS pname,
                       place, admin_level, file_row_number
                FROM read_parquet({uri}, file_row_number = true)
                WHERE ({only}) AND {box} AND ({mark})
                  AND length(coalesce(list_extract(name, 1), '')) > 0
                """).fetchall()
            if not rows:
                return []
            hit = sorted({rg for row in rows for rg in cand
                          if groups[rg]["first"] <= row[4] < groups[rg]["first"] + groups[rg]["n"]})
            rd._fetch(files, {PATH: [r for rg in hit for span in groups[rg]["cols"].get("geometry", [])
                                     for r in (span,)]}, kind)
            nums = ", ".join(str(int(row[4])) for row in rows)
            geoms = dict(con.execute(f"""SELECT file_row_number, geometry
                FROM read_parquet({uri}, file_row_number = true)
                WHERE file_row_number IN ({nums})""").fetchall())
        except Exception:
            for e in errors:
                if isinstance(e, rangeread.Changed):
                    raise e
            raise
        finally:
            con.close()
        out = []
        for fid, name, place, admin, num in rows:
            geom = geoms.get(num)
            if geom is None or not name:
                continue
            out.append((int(fid), str(name).strip(), str(place or ""), str(admin or ""), bytes(geom)))
        return out


def _candidates(groups, bounds):
    lon0, lat0, lon1, lat1 = bounds

    def stat(g, leaf, i, default):
        try:
            return float(g["bbox"][leaf][i])
        except (KeyError, TypeError, ValueError):
            return default

    return [rg for rg, g in sorted(groups.items())
            if stat(g, "xmin", 0, -1e9) <= lon1 and stat(g, "xmax", 1, 1e9) >= lon0
            and stat(g, "ymin", 0, -1e9) <= lat1 and stat(g, "ymax", 1, 1e9) >= lat0]


def _rings_of(rows):
    """[(name, lonlat (n,2))] exteriors from (id, name, place, admin, wkb) rows."""
    out, seen = [], set()
    for fid, name, _place, _admin, wkb in rows:
        if not name or not wkb:
            continue
        try:
            kind, parts = mapgeom.parse_wkb(wkb)
        except Exception:
            continue
        if kind != mapgeom.POLYGON or not parts:
            continue
        for rings in parts:
            if not rings:
                continue
            ring = np.asarray(rings[0], np.float64)
            if len(ring) < 3:
                continue
            key = (name, round(float(ring[:, 0].mean()), 5), round(float(ring[:, 1].mean()), 5))
            if key in seen:
                continue
            seen.add(key)
            out.append((name, ring))
    return out
