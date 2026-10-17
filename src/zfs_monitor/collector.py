"""Collect ZFS state into one JSON report.

A port of the original zfs-status-collector.sh (bash + jq) with the same
report schema. Differences worth knowing:

* smartctl exits non-zero when a drive reports SMART problems yet still prints
  valid JSON; that output is used rather than dropping the drive.
* snapshot groups additionally carry ``newest_age_seconds`` (``age_seconds``
  stays the age of the *oldest* snapshot, as before).
* every external command has a timeout and failures become ``notices``.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .config import Config

# Result of running a command: (returncode, stdout). rc -1 = could not run.
Runner = Callable[[list[str]], "tuple[int, str]"]

_DATA_VDEVS = {
    "disk", "file", "mirror", "raidz", "raidz1", "raidz2", "raidz3",
    "draid", "replacing",
}


def make_runner(timeout: int) -> Runner:
    def run(cmd: list[str]) -> tuple[int, str]:
        try:
            p = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return -1, ""
        return p.returncode, p.stdout

    return run


def _json(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _num(v: Any, default: Any = 0) -> Any:
    """Coerce a zfs JSON value (int or numeric string) to a number."""
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return v
    try:
        s = str(v).strip().rstrip("x")
        return int(s) if re.fullmatch(r"-?\d+", s) else float(s)
    except (TypeError, ValueError):
        return default


def _prop(props: dict, name: str, default: Any = 0) -> Any:
    return _num((props.get(name) or {}).get("value"), default)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CollectionError(RuntimeError):
    """Nothing usable was collected; the previous report must be left alone."""


class Collector:
    def __init__(self, cfg: Config, run: Runner | None = None,
                 now: Callable[[], float] = time.time):
        self.cfg = cfg
        self.run = run or make_runner(cfg.collector.command_timeout_seconds)
        self.now = now
        self.notices: list[str] = []
        # Last-seen scrub end times / device error counters; persisted by
        # run_once so the journal records each event once, not every run.
        self.state: dict = {"scrubs": {}, "errors": {}}
        self._started = now()

    # -- helpers -----------------------------------------------------------

    def notice(self, msg: str) -> None:
        self.notices.append(msg)
        print(f"[notice] {msg}", file=sys.stderr)

    def _have(self, tool: str) -> bool:
        rc, _ = self.run([tool, "--version"])
        return rc != -1

    def _json_cmd(self, cmd: list[str], what: str) -> Optional[Any]:
        rc, out = self.run(cmd)
        data = _json(out) if rc == 0 else None
        if data is None:
            self.notice(f"{what} failed")
        return data

    # -- pools -------------------------------------------------------------

    def pool_list(self) -> dict[str, dict]:
        data = self._json_cmd(
            ["zpool", "list", "-j", "-p", "--json-int", "-o",
             "name,size,alloc,free,frag,cap,health"],
            "zpool list -j",
        )
        pools: dict[str, dict] = {}
        for name, p in ((data or {}).get("pools") or {}).items():
            props = p.get("properties", {})
            pools[name] = {
                "health": (props.get("health") or {}).get("value") or "UNKNOWN",
                "state": None,
                "status_message": None,
                "size_bytes": _prop(props, "size"),
                "allocated_bytes": _prop(props, "allocated"),
                "free_bytes": _prop(props, "free"),
                "fragmentation_percent": _prop(props, "fragmentation"),
                "capacity_percent": _prop(props, "capacity"),
                "scan": None,
                "resilver": {"in_progress": False, "progress_percent": None,
                             "eta_hours": None},
                "devices": [], "l2arc": [], "slog": [], "spare": [],
                "trim": {"autotrim_enabled": False, "last_done_timestamp": None},
                "features_pending": False,
                "io": {"read_ops_per_sec": 0, "write_ops_per_sec": 0,
                       "read_bytes_per_sec": 0, "write_bytes_per_sec": 0},
            }
        return pools

    @staticmethod
    def _walk_device(v: dict) -> dict:
        return {
            "name": str(v.get("name") or v.get("path") or v.get("devid") or "unknown"),
            "type": v.get("vdev_type") or "unknown",
            "state": v.get("state") or "UNKNOWN",
            "read_errors": _num(v.get("read_errors")),
            "write_errors": _num(v.get("write_errors")),
            "checksum_errors": _num(v.get("checksum_errors")),
            "children": [Collector._walk_device(c)
                         for c in (v.get("vdevs") or {}).values()],
        }

    def _days_since(self, end_time: Any) -> Optional[int]:
        if end_time is None:
            return None
        if isinstance(end_time, (int, float)):
            return int((self.now() - end_time) // 86400)
        try:
            text = re.sub(r"\s+", " ", str(end_time).strip())
            ts = datetime.strptime(text, "%a %b %d %H:%M:%S %Y")
            return int((self.now() - ts.replace(tzinfo=timezone.utc).timestamp())
                       // 86400)
        except ValueError:
            return None

    @staticmethod
    def _scan(pool: dict) -> tuple[dict | None, dict]:
        """(scan, stats). `zpool status -j` puts the scan under `scan_stats`;
        the original jq read `scan` with a nested `scan_stats`. Accept both."""
        scan = pool.get("scan_stats") or pool.get("scan")
        if not scan:
            return None, {}
        return scan, (scan.get("scan_stats") or scan)

    def pool_status(self, raw: Any) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for pool in ((raw or {}).get("pools") or {}).values():
            name = pool.get("name")
            if not name:
                continue
            root = (pool.get("vdevs") or {}).get(name) or {}
            kids = [c for c in (root.get("vdevs") or {}).values()]

            def of(types: set[str]) -> list[dict]:
                res = []
                for k in kids:
                    if (k.get("vdev_type") or "") in types:
                        res += [self._walk_device(c)
                                for c in (k.get("vdevs") or {}).values()]
                return res

            scan, stats = self._scan(pool)
            resilver = pool.get("resilver")
            rstate = (resilver or {}).get("state") or "none"
            if not resilver and scan and str(scan.get("function") or "").upper() == "RESILVER":
                # zpool reports a resilver as the pool's (single) scan.
                active = str(scan.get("state") or "").upper() == "SCANNING"
                done, total = _num(stats.get("examined"), None), _num(stats.get("to_examine"), None)
                resilver = {"state": "active" if active else "none",
                            "percent": (round(done / total * 100, 1)
                                        if active and done is not None and total else None)}
                rstate = resilver["state"]
            out[name] = {
                "state": pool.get("state") or "UNKNOWN",
                "status_message": pool.get("comment"),
                "scan": ({
                    "state": scan.get("state"),
                    "function": scan.get("function"),
                    "errors": _num(stats.get("errors")),
                    "repaired_bytes": _num(stats.get("bytes_repaired", stats.get("processed"))),
                    "days_since_last": self._days_since(scan.get("end_time")),
                } if scan else None),
                "resilver": ({
                    "in_progress": rstate in ("active", "in_progress"),
                    "progress_percent": resilver.get("percent"),
                    "eta_hours": (int(resilver["eta"] // 3600)
                                  if resilver.get("eta") else None),
                } if resilver else {"in_progress": False,
                                    "progress_percent": None, "eta_hours": None}),
                "devices": [self._walk_device(k) for k in kids
                            if (k.get("vdev_type") or "") in _DATA_VDEVS],
                "l2arc": of({"l2cache"}),
                "slog": of({"log"}),
                "spare": of({"spare"}),
            }
        return out

    def trim(self, pools: list[str]) -> dict[str, dict]:
        res = {}
        for pool in pools:
            rc, out = self.run(["zpool", "get", "-Hp", "-o", "value", "autotrim", pool])
            enabled = rc == 0 and out.strip() == "on"
            last = None
            rc, out = self.run(["zpool", "status", "-t", pool])
            if rc == 0:
                found = re.findall(r"trimmed at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", out)
                last = max(found) if found else None
            res[pool] = {"autotrim_enabled": enabled, "last_done_timestamp": last}
        return res

    def iostat(self, pools: list[str]) -> dict[str, dict]:
        secs = self.cfg.collector.iostat_seconds
        if secs <= 0 or not pools:
            return {}
        rc, out = self.run(["zpool", "iostat", "-Hp", str(secs), "2"])
        if rc != 0:
            self.notice("zpool iostat failed")
            return {}
        res = {}
        for line in out.strip().splitlines()[-len(pools):]:
            f = line.split("\t")
            if len(f) < 7:
                continue
            res[f[0]] = {
                "read_ops_per_sec": _num(f[3]), "write_ops_per_sec": _num(f[4]),
                "read_bytes_per_sec": _num(f[5]), "write_bytes_per_sec": _num(f[6]),
            }
        return res

    def features_pending(self, pools: list[str]) -> dict[str, bool]:
        res = {}
        for pool in pools:
            rc, out = self.run(["zpool", "get", "-H", "-o", "property,value", "all", pool])
            res[pool] = rc == 0 and any(
                line.split("\t")[:2][0].startswith("feature@")
                and line.split("\t")[1:2] == ["disabled"]
                for line in out.splitlines() if "\t" in line)
        return res

    # -- datasets / snapshots ----------------------------------------------

    def datasets(self) -> list[dict]:
        data = self._json_cmd(
            ["zfs", "list", "-j", "-p", "--json-int", "-o",
             "name,used,available,referenced,quota,reservation,compressratio,"
             "usedbydataset,usedbysnapshots"], "zfs list -j")
        res = []
        for ds in ((data or {}).get("datasets") or {}).values():
            p = ds.get("properties", {})
            name = ds.get("name", "")
            used, avail = _prop(p, "used"), _prop(p, "available")
            snaps = (p.get("usedbysnapshots") or {}).get("value")
            quota = _prop(p, "quota")
            res.append({
                "name": name,
                "pool": name.split("/")[0],
                "used_bytes": used,
                "available_bytes": avail,
                "referenced_bytes": _prop(p, "referenced"),
                "quota_bytes": quota or None,
                "reservation_bytes": _prop(p, "reservation"),
                "compressratio": _prop(p, "compressratio", 1),
                "capacity_percent": (int(used / (used + avail) * 100)
                                     if used + avail > 0 else None),
                "snapshot_ratio_percent": (int(_num(snaps) / used * 100)
                                           if used > 0 and snaps is not None else None),
            })
        return res

    def snapshots(self) -> tuple[list[dict], dict]:
        empty = ([], {"total_count": 0, "total_used_bytes": 0})
        data = self._json_cmd(
            ["zfs", "list", "-j", "-t", "snapshot", "-p", "--json-int", "-o",
             "name,creation,used"], "zfs list -j -t snapshot")
        if data is None:
            return empty
        groups: dict[str, list[int]] = {}
        total_used = count = 0
        for s in (data.get("datasets") or {}).values():
            p = s.get("properties", {})
            groups.setdefault(re.sub(r"@[^@]+$", "", s.get("name", "")), []).append(
                int(_prop(p, "creation")))
            total_used += int(_prop(p, "used"))
            count += 1
        now = self.now()
        out = [{
            "dataset": ds, "count": len(c),
            "newest_epoch": max(c), "oldest_epoch": min(c),
            "age_seconds": int(now - min(c)),
            "newest_age_seconds": int(now - max(c)),
        } for ds, c in sorted(groups.items())]
        return out, {"total_count": count, "total_used_bytes": total_used}

    # -- ARC ---------------------------------------------------------------

    def arc(self, path: str = "/proc/spl/kstat/zfs/arcstats") -> dict:
        try:
            stats = {}
            for line in Path(path).read_text().splitlines()[2:]:
                f = line.split()
                if len(f) == 3:
                    stats[f[0]] = int(f[2])
        except (OSError, ValueError):
            self.notice(f"{path} not readable; ARC stats unavailable")
            return {"size_bytes": 0, "c_max_bytes": 0, "hits": 0, "misses": 0,
                    "lifetime_hit_ratio": 0, "l2_hits": 0, "l2_misses": 0,
                    "l2_size_bytes": 0, "l2_hit_ratio": None}
        hits, misses = stats.get("hits", 0), stats.get("misses", 0)
        l2h, l2m = stats.get("l2_hits", 0), stats.get("l2_misses", 0)
        return {
            "size_bytes": stats.get("size", 0), "c_max_bytes": stats.get("c_max", 0),
            "hits": hits, "misses": misses,
            "lifetime_hit_ratio": round(hits / (hits + misses), 4) if hits + misses else 0,
            "l2_hits": l2h, "l2_misses": l2m,
            "l2_size_bytes": stats.get("l2_size", 0),
            "l2_hit_ratio": round(l2h / (l2h + l2m), 4) if l2h + l2m else None,
        }

    # -- SMART -------------------------------------------------------------

    @staticmethod
    def base_disk(path: str) -> str:
        """Whole-disk device for a vdev path (follows /dev/disk/by-id symlinks
        and strips partitions; smartctl rejects e.g. /dev/nvme0n1p1)."""
        real = os.path.realpath(path)
        sysfs = f"/sys/class/block/{os.path.basename(real)}"
        if os.path.exists(sysfs + "/partition"):
            return "/dev/" + os.path.basename(os.path.dirname(os.path.realpath(sysfs)))
        return real

    @staticmethod
    def device_paths(status_raw: Any) -> list[str]:
        paths: set[str] = set()

        def walk(v: dict) -> None:
            if v.get("vdev_type") == "disk" and v.get("path"):
                paths.add(Collector.base_disk(v["path"]))
            for c in (v.get("vdevs") or {}).values():
                walk(c)

        for pool in ((status_raw or {}).get("pools") or {}).values():
            walk(pool)
        return sorted(paths)

    @staticmethod
    def smart_entry(dev: str, info: dict) -> dict:
        nvme = info.get("nvme_smart_health_information_log")
        # smartctl's overall health verdict; None when the device gives none.
        passed = (info.get("smart_status") or {}).get("passed")
        if (nvme is None and "ata_smart_attributes" not in info
                and (info.get("device") or {}).get("protocol") == "SCSI"):
            return {
                "device_path": dev, "protocol": "SCSI", "smart_passed": passed,
                "model": info.get("scsi_model_name") or info.get("model_name") or "unknown",
                "temperature_celsius": (info.get("temperature") or {}).get("current"),
                "power_on_hours": (info.get("power_on_time") or {}).get("hours"),
                "percentage_used": None, "available_spare": None,
                "available_spare_threshold": None, "media_errors": 0, "critical_warning": 0,
                "reallocated_sectors": 0, "pending_sectors": 0,
                "offline_uncorrectable": 0, "udma_crc_errors": 0,
                "ssd_life_left": None, "media_wearout": None,
            }
        if nvme is not None:
            return {
                "device_path": dev, "protocol": "NVMe", "smart_passed": passed,
                "model": info.get("model_name") or "unknown",
                "temperature_celsius": (info.get("temperature") or {}).get("current"),
                "power_on_hours": (info.get("power_on_time") or {}).get("hours"),
                "percentage_used": nvme.get("percentage_used"),
                "available_spare": nvme.get("available_spare"),
                "available_spare_threshold": nvme.get("available_spare_threshold"),
                "media_errors": nvme.get("media_errors") or 0,
                "critical_warning": nvme.get("critical_warning") or 0,
                "reallocated_sectors": 0, "pending_sectors": 0,
                "offline_uncorrectable": 0, "udma_crc_errors": 0,
                "ssd_life_left": None, "media_wearout": None,
            }
        table = (info.get("ata_smart_attributes") or {}).get("table") or []

        def attr(i: int, key: str = "raw", default: Any = 0) -> Any:
            for a in table:
                if a.get("id") == i:
                    v = a.get("value") if key == "value" else (a.get("raw") or {}).get("value")
                    return default if v is None else v
            return default

        temp = (info.get("temperature") or {}).get("current")
        if temp is None:
            temp = attr(194, default=None)
            if temp is None:
                temp = attr(190, default=None)
        return {
            "device_path": dev, "protocol": "ATA", "smart_passed": passed,
            "model": info.get("model_name") or info.get("model_family") or "unknown",
            "temperature_celsius": temp,
            "reallocated_sectors": _num(attr(5)), "pending_sectors": _num(attr(197)),
            "offline_uncorrectable": _num(attr(198)), "udma_crc_errors": _num(attr(199)),
            "power_on_hours": attr(9, default=None),
            "ssd_life_left": attr(231, "value", None),
            "media_wearout": attr(233, "value", None),
            "percentage_used": None, "available_spare": None,
            "available_spare_threshold": None, "media_errors": 0, "critical_warning": 0,
        }

    def smart(self, status_raw: Any) -> list[dict]:
        if not self.cfg.collector.smart:
            return []
        if not self._have("smartctl"):
            self.notice("smartctl not available; skipping SMART collection")
            return []
        res = []
        for dev in self.device_paths(status_raw):
            # rc is a bitmask; non-zero with valid JSON still carries the data.
            _, out = self.run(["smartctl", "-iA", "--json", dev])
            info = _json(out)
            if not isinstance(info, dict):
                self.notice(f"smartctl failed for {dev}")
                continue
            res.append(self.smart_entry(dev, info))
        return res

    # -- ZED events --------------------------------------------------------

    def zed_events(self) -> list[dict]:
        limit = self.cfg.collector.zed_event_limit
        rc, out = self.run(["zpool", "events", "-j"])
        data = _json(out) if rc == 0 and out.strip() else None
        if isinstance(data, dict):
            keep = re.compile(
                r"^ereport\.fs\.zfs\.|^sysevent\.fs\.zfs\.(scrub|resilver|vdev|pool)_")
            events = [e for e in data.get("events", [])
                      if keep.search(e.get("class") or "")][-limit:]
            return [{
                "timestamp": _iso(_num((e.get("time") or [0])[0])),
                "eid": e.get("eid") or 0,
                "class": e.get("class") or "unknown",
                "message": f"{e.get('pool') or ''} {e.get('vdev_path') or ''}",
            } for e in events]
        rc, out = self.run(["zpool", "events", "-H"])
        if rc == 0 and out.strip():
            res = []
            for line in out.strip().splitlines()[-limit:]:
                f = line.split("\t", 2)
                if len(f) >= 2 and f[1]:
                    res.append({"timestamp": f[0], "class": f[1],
                                "message": f[2] if len(f) > 2 else ""})
            return res
        try:
            lines = Path("/var/log/zfs/zed.log").read_text().splitlines()[-limit:]
        except OSError:
            return []
        res = []
        for line in lines:
            f = line.split()
            if len(f) >= 5:
                res.append({"timestamp": " ".join(f[:3]), "class": f[4],
                            "message": " ".join(f[5:])})
        return res

    def journal_events(self, status_raw: Any) -> list[dict]:
        """New scrub completions and changed per-disk error counters.

        Each is emitted once (tracked in self.state), not on every run while
        the pool still reports the same finished scrub or the same errors.
        """
        ts = _iso(self.now())
        out: list[dict] = []
        seen_scrubs = self.state.setdefault("scrubs", {})
        seen_errors = self.state.setdefault("errors", {})
        current_errors: dict[str, list] = {}

        def disk_errors(v: dict) -> None:
            counts = [_num(v.get(k)) for k in ("read_errors", "write_errors", "checksum_errors")]
            if v.get("vdev_type") == "disk" and any(counts):
                dev = v.get("path") or v.get("name") or "unknown"
                current_errors[dev] = counts
                if seen_errors.get(dev) != counts:
                    out.append({"device": dev, "event": "device_errors", "timestamp": ts,
                                "read_errors": counts[0], "write_errors": counts[1],
                                "checksum_errors": counts[2]})
            for child in (v.get("vdevs") or {}).values():
                disk_errors(child)

        for pool in ((status_raw or {}).get("pools") or {}).values():
            name = pool.get("name")
            scan, stats = self._scan(pool)
            if scan and str(scan.get("state") or "").lower() in ("finished", "completed"):
                end = scan.get("end_time")
                if seen_scrubs.get(name) != str(end):
                    seen_scrubs[name] = str(end)
                    out.append({"pool": name, "event": "scrub_completed",
                                "timestamp": _iso(end) if isinstance(end, (int, float)) else ts,
                                "errors": _num(stats.get("errors")),
                                "repaired_bytes": _num(stats.get("bytes_repaired",
                                                                 stats.get("processed")))})
            disk_errors(pool)
        # Devices that went back to zero re-arm, so a later recurrence is logged.
        self.state["errors"] = current_errors
        return out

    # -- assembly ----------------------------------------------------------

    def collect(self) -> tuple[dict, list[dict]]:
        status_raw = None
        rc, out = self.run(["zpool", "status", "-j", "--json-int"])
        if rc == 0:
            status_raw = _json(out)
        if status_raw is None:
            self.notice("zpool status -j failed")

        pools = self.pool_list()
        if not pools and any(n.startswith(("zpool list", "zpool status")) for n in self.notices):
            # An empty report would read as "all pools healthy". Fail instead so
            # the old status.json ages into stale (HTTP 503) and the unit shows failed.
            raise CollectionError("zpool list/status failed; no pools collected")
        for name, st in self.pool_status(status_raw).items():
            pools.setdefault(name, {}).update(st)
        names = list(pools)
        for name, v in self.trim(names).items():
            pools[name]["trim"] = v
        for name, v in self.iostat(names).items():
            if name in pools:
                pools[name]["io"] = v
        for name, v in self.features_pending(names).items():
            pools[name]["features_pending"] = v

        groups, snap_stats = self.snapshots()
        rc, ver = self.run(["zfs", "version"])
        report = {
            "hostname": socket.gethostname() or "unknown",
            "zfs_version": (ver.splitlines()[0] if rc == 0 and ver.strip() else "unknown"),
            "collection_timestamp": _iso(self.now()),
            "collection_duration_ms": 0,  # patched below, after the work is done
            "pools": pools,
            "datasets": self.datasets(),
            "snapshot_groups": groups,
            "snapshot_stats": snap_stats,
            "arc": self.arc(),
            "smart": self.smart(status_raw),
            "zed_events": self.zed_events(),
            "notices": self.notices,
        }
        report["collection_duration_ms"] = int((self.now() - self._started) * 1000)
        return report, self.journal_events(status_raw)


# -- output ------------------------------------------------------------------

def _chown(path: str, cfg: Config) -> None:
    c = cfg.collector
    if c.owner_uid is not None or c.owner_gid is not None:
        os.chown(path, -1 if c.owner_uid is None else c.owner_uid,
                 -1 if c.owner_gid is None else c.owner_gid)


def write_report(cfg: Config, report: dict, events: list[dict]) -> Path:
    """Atomically replace status.json; append + rotate events.jsonl."""
    c = cfg.collector
    out_dir = Path(c.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".status.json.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(report, f)
        os.chmod(tmp, c.file_mode)
        _chown(tmp, cfg)
        target = out_dir / c.status_file
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    if events:
        ev = out_dir / c.events_file
        if ev.exists() and ev.stat().st_size > c.events_max_bytes:
            os.replace(ev, ev.with_name(ev.name + ".1"))
        with open(ev, "a") as f:
            for e in events:
                f.write(json.dumps(e, separators=(",", ":")) + "\n")
        os.chmod(ev, c.file_mode)
        _chown(str(ev), cfg)
    return target


def _load_state(cfg: Config) -> dict:
    try:
        with open(Path(cfg.collector.output_dir) / cfg.collector.state_file) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(cfg: Config, state: dict) -> None:
    out = Path(cfg.collector.output_dir)
    fd, tmp = tempfile.mkstemp(dir=out, prefix=".state.json.")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.replace(tmp, out / cfg.collector.state_file)


def run_once(cfg: Config, run: Runner | None = None,
             write: bool = True) -> tuple[dict, list[dict]]:
    """One collection pass. Writing happens under an exclusive lock
    (BlockingIOError if another collector holds it); a dry run needs neither
    the lock nor the output directory. Raises CollectionError when nothing
    usable was collected."""
    if not write:
        return Collector(cfg, run).collect()
    out_dir = Path(cfg.collector.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / cfg.collector.lock_file, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        c = Collector(cfg, run)
        c.state = _load_state(cfg)
        report, events = c.collect()
        write_report(cfg, report, events)
        _save_state(cfg, c.state)
        return report, events
