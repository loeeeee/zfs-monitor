"""Collect ZFS state into one JSON report.

A port of the original zfs-status-collector.sh (bash + jq) with the same
report schema. Differences worth knowing:

* smartctl exits non-zero when a drive reports SMART problems yet still prints
  valid JSON; that output is used rather than dropping the drive.
* snapshot groups additionally carry ``newest_age_seconds`` (``age_seconds``
  stays the age of the *oldest* snapshot, as before).
* every external command has a timeout and failures become ``notices``.
* special, dedup, log, cache and spare vdevs are read from the pool keys
  ``zpool status -j`` really puts them under (not from the root vdev).
* a failed ``zpool status`` fails the run instead of publishing a report
  with empty device trees.
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

# Pool keys `zpool status -j` puts the non-data vdev classes under (they are
# left out of the root vdev tree), and the report key each one maps to.
_CLASS_KEYS = {"special": "special", "dedup": "dedup", "logs": "slog",
               "l2cache": "l2arc", "spares": "spare"}

# At most this many permanently damaged files are listed per pool.
_ERROR_FILES_LIMIT = 50

# ZED event classes worth reporting; the rest (history_event, config_sync,
# ...) is routine bookkeeping.
_ZED_KEEP = re.compile(
    r"^ereport\.fs\.zfs\.|^resource\.fs\.zfs\."
    r"|^sysevent\.fs\.zfs\.(scrub|resilver|vdev|pool|trim)_")
# Classes that are journaled to events.jsonl when they first appear.
_ZED_JOURNAL = re.compile(r"^ereport\.|^resource\.")

# `zpool history -i` scan records: "<local %F.%T> [txg:N] scan setup func=1 ..."
# (func 1 = scrub), then "scan done", "scan cancelled" or "scan aborted, ...".
_HISTORY_SCAN = re.compile(
    r"^(\d{4}-\d\d-\d\d\.\d\d:\d\d:\d\d) \[txg:\d+\] "
    r"scan (?:setup func=(\d+)|(done|cancelled|aborted))")
_POOL_SCAN_SCRUB = 1

# NVMe self-test results meaning the test failed (not merely aborted).
_NVME_SELFTEST_FAILED = {5, 6, 7}

# Every smart[] entry carries all of these keys.
_SMART_BLANK: dict[str, Any] = {
    "protocol": "unknown", "smart_passed": None, "model": "unknown",
    "temperature_celsius": None, "power_on_hours": None,
    "percentage_used": None, "available_spare": None,
    "available_spare_threshold": None, "media_errors": 0, "critical_warning": 0,
    "reallocated_sectors": 0, "pending_sectors": 0,
    "offline_uncorrectable": 0, "udma_crc_errors": 0,
    "ssd_life_left": None, "media_wearout": None, "grown_defects": None,
    # False when smartctl could not read the device at all.
    "smart_readable": False,
    "smartctl_exit_status": None,
    # smartctl exit status bits 4, 5 and 7 (NVMe self-tests: from the log).
    "smart_prefail": False, "smart_past_failures": False,
    "smart_selftest_failed": False,
    "failing_attributes": [],
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


def _text(v: Any) -> Optional[str]:
    """zpool's multi-line, tab-indented messages as one line (None if empty)."""
    return (" ".join(str(v).split()) or None) if v else None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_value(text: str) -> Any:
    """One `zpool events -v` value: "str", "NAME" (0x5), 0x2a or 0x1 0x2."""
    text = text.strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        return text[1:end] if end > 0 else text[1:]
    nums = []
    for tok in text.split():
        try:
            nums.append(int(tok, 16) if tok.startswith("0x") else int(tok))
        except ValueError:
            return text
    if not nums:
        return text
    return nums[0] if len(nums) == 1 else nums


def parse_events(text: str) -> list[dict]:
    """Parse `zpool events -vH`: a "time<TAB>class" line per event, then its
    top-level nvpairs indented by 8 spaces (nested nvlists go deeper)."""
    events: list[dict] = []
    cur: Optional[dict] = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            f = line.split("\t", 1)
            cur = None
            if len(f) == 2 and f[1].strip():  # skips "dropped N events"
                cur = {"time_text": f[0].strip(), "class": f[1].strip(), "fields": {}}
                events.append(cur)
            continue
        if cur is None or len(line) - len(line.lstrip(" ")) != 8:
            continue
        m = re.match(r"\s+(\S+) = (.*)$", line)
        if m:
            cur["fields"][m[1]] = _event_value(m[2])
    return events


class CollectionError(RuntimeError):
    """Nothing usable was collected; the previous report must be left alone."""


class Collector:
    def __init__(self, cfg: Config, run: Runner | None = None,
                 now: Callable[[], float] = time.time):
        self.cfg = cfg
        self.run = run or make_runner(cfg.collector.command_timeout_seconds)
        self.now = now
        self.notices: list[str] = []
        # Last-seen scrub end times / device error counters / ZED event ids /
        # snapshot times; persisted by run_once so the journal records each
        # event once, not every run.
        self.state: dict = {"scrubs": {}, "errors": {}}
        # New ZED events found by zed_events(), journaled by collect().
        self.zed_new: list[dict] = []
        self.proc = "/proc"
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
             "name,size,alloc,free,ckpoint,expandsz,frag,cap,dedup,health"],
            "zpool list -j",
        )
        pools: dict[str, dict] = {}
        for name, p in ((data or {}).get("pools") or {}).items():
            props = p.get("properties", {})
            pools[name] = {
                "health": (props.get("health") or {}).get("value") or "UNKNOWN",
                "state": None,
                "status_message": None,
                "action": None,
                "msgid": None,
                "moreinfo": None,
                "error_count": 0,
                "error_files": [],
                "size_bytes": _prop(props, "size"),
                "allocated_bytes": _prop(props, "allocated"),
                "free_bytes": _prop(props, "free"),
                "checkpoint_bytes": _prop(props, "checkpoint"),
                "expand_size_bytes": _prop(props, "expandsize"),
                "dedup_ratio": _prop(props, "dedupratio", 1),
                "fragmentation_percent": _prop(props, "fragmentation"),
                "capacity_percent": _prop(props, "capacity"),
                "scan": None,
                "resilver": {"in_progress": False, "progress_percent": None,
                             "eta_hours": None},
                "removal": None, "raidz_expand": None, "checkpoint": None,
                "devices": [], "special": [], "dedup": [],
                "l2arc": [], "slog": [], "spare": [],
                "trim": {"autotrim_enabled": False, "last_done_timestamp": None,
                         "errors": 0, "unsupported_devices": 0},
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
            "path": v.get("path"),
            # Why the device is unusable ("too many errors", "cannot open", ...).
            "aux": v.get("aux"),
            # Last known path of a device that is not present (its name is
            # then only a guid).
            "was": v.get("was"),
            "read_errors": _num(v.get("read_errors")),
            "write_errors": _num(v.get("write_errors")),
            "checksum_errors": _num(v.get("checksum_errors")),
            "slow_ios": _num(v.get("slow_ios")),
            "children": [Collector._walk_device(c)
                         for c in (v.get("vdevs") or {}).values()],
        }

    @staticmethod
    def _leaves(pool: dict) -> list[dict]:
        """Raw leaf vdevs of every class (data, special, dedup, log, cache, spare)."""
        res: list[dict] = []

        def walk(v: dict) -> None:
            kids = list((v.get("vdevs") or {}).values())
            if not kids and v.get("vdev_type") != "root":
                res.append(v)
            for c in kids:
                walk(c)

        for v in (pool.get("vdevs") or {}).values():
            walk(v)
        for key in _CLASS_KEYS:
            for v in (pool.get(key) or {}).values():
                walk(v)
        return res

    def _days_since(self, end_time: Any) -> Optional[int]:
        if end_time is None:
            return None
        if isinstance(end_time, (int, float)):
            return int((self.now() - end_time) // 86400)
        try:
            # Without --json-int zpool prints ctime(3), i.e. local time.
            text = re.sub(r"\s+", " ", str(end_time).strip())
            ts = datetime.strptime(text, "%a %b %d %H:%M:%S %Y")
            return int((self.now() - ts.timestamp()) // 86400)
        except ValueError:
            return None

    def _last_scrub_from_history(self, pool: str) -> Optional[int]:
        """End of the last completed scrub per the pool's internal history.
        Used when none is remembered: zpool status only shows the latest
        scan, which may be a resilver or a cancelled/running scrub."""
        rc, out = self.run(["zpool", "history", "-i", pool])
        if rc != 0:
            return None
        func, last = None, None
        for line in out.splitlines():
            m = _HISTORY_SCAN.match(line)
            if not m:
                continue
            if m.group(2) is not None:
                func = int(m.group(2))
                continue
            if m.group(3) == "done" and func == _POOL_SCAN_SCRUB:
                last = int(datetime.strptime(
                    m.group(1), "%Y-%m-%d.%H:%M:%S").timestamp())
            func = None
        return last

    @staticmethod
    def _scan(pool: dict) -> tuple[dict | None, dict]:
        """(scan, stats). `zpool status -j` puts the scan under `scan_stats`;
        the original jq read `scan` with a nested `scan_stats`. Accept both."""
        scan = pool.get("scan_stats") or pool.get("scan")
        if not scan:
            return None, {}
        return scan, (scan.get("scan_stats") or scan)

    @staticmethod
    def _resilver(pool: dict, scan: dict | None, stats: dict) -> dict:
        """A healing resilver (the pool's scan) or a sequential rebuild
        (`zpool attach -s`, dRAID spares: scan_stats.rebuild_stats)."""
        res = {"in_progress": False, "progress_percent": None, "eta_hours": None}
        legacy = pool.get("resilver")
        if legacy:
            res.update(
                in_progress=(legacy.get("state") or "none") in ("active", "in_progress"),
                progress_percent=legacy.get("percent"),
                eta_hours=int(legacy["eta"] // 3600) if legacy.get("eta") else None)
            return res
        if (scan and str(scan.get("function") or "").upper() == "RESILVER"
                and str(scan.get("state") or "").upper() == "SCANNING"):
            done, total = _num(stats.get("examined"), None), _num(stats.get("to_examine"), None)
            res.update(in_progress=True,
                       progress_percent=(round(done / total * 100, 1)
                                         if done is not None and total else None))
            return res
        active = [r for r in ((scan or {}).get("rebuild_stats") or {}).values()
                  if str(r.get("state") or "").upper() == "ACTIVE"]
        if active:
            done = sum(_num(r.get("scanned")) for r in active)
            total = sum(_num(r.get("to_scan")) for r in active)
            res.update(in_progress=True,
                       progress_percent=round(done / total * 100, 1) if total else None)
        return res

    @staticmethod
    def _operation(stats: Any) -> Optional[dict]:
        """removal_stats / raidz_expand_stats -> {vdev, state, in_progress}."""
        if not isinstance(stats, dict):
            return None
        state = str(stats.get("state") or "").upper()
        return {"vdev": stats.get("name"), "state": state,
                "in_progress": state == "SCANNING"}

    def pool_status(self, raw: Any) -> dict[str, dict]:
        out: dict[str, dict] = {}
        # End of the last *completed scrub* per pool. zpool only keeps the
        # latest scan, so this outlives a later resilver or cancelled scrub.
        last_scrub = self.state.setdefault("last_scrub_end", {})
        for pool in ((raw or {}).get("pools") or {}).values():
            name = pool.get("name")
            if not name:
                continue
            root = (pool.get("vdevs") or {}).get(name) or {}

            scan, stats = self._scan(pool)
            if (scan and str(scan.get("function") or "").upper() == "SCRUB"
                    and str(scan.get("state") or "").upper() == "FINISHED"
                    and scan.get("end_time")):
                last_scrub[name] = scan["end_time"]
            elif last_scrub.get(name) is None:
                found = self._last_scrub_from_history(name)
                if found is not None:
                    last_scrub[name] = found

            ckpt = pool.get("checkpoint_stats")
            st = {
                "state": pool.get("state") or "UNKNOWN",
                "status_message": _text(pool.get("status")),
                "action": _text(pool.get("action")),
                "msgid": pool.get("msgid"),
                "moreinfo": pool.get("moreinfo"),
                # Blocks with permanent (unrepaired) errors. The pool can be
                # ONLINE with every device counter at zero while this is > 0.
                "error_count": _num(pool.get("error_count")),
                "error_files": list(pool.get("errlist") or [])[:_ERROR_FILES_LIMIT],
                "scan": ({
                    "state": scan.get("state"),
                    "function": scan.get("function"),
                    "errors": _num(stats.get("errors")),
                    "repaired_bytes": _num(stats.get("bytes_repaired", stats.get("processed"))),
                    "paused": bool(_num(stats.get("scrub_pause"))),
                    # Since the last completed scrub, not since whatever scan
                    # ran last (a resilver, or a scrub still running).
                    "days_since_last": self._days_since(last_scrub.get(name)),
                } if scan else None),
                "resilver": self._resilver(pool, scan, stats),
                "removal": self._operation(pool.get("removal_stats")),
                "raidz_expand": self._operation(pool.get("raidz_expand_stats")),
                "checkpoint": ({"state": ckpt.get("state"),
                                "space_bytes": _num(ckpt.get("space"))}
                               if isinstance(ckpt, dict)
                               and str(ckpt.get("state") or "").upper() != "NONE"
                               else None),
                # zpool already leaves log/special/dedup, hole and indirect
                # vdevs out of the root's children.
                "devices": [self._walk_device(k)
                            for k in (root.get("vdevs") or {}).values()],
            }
            for key, report_key in _CLASS_KEYS.items():
                st[report_key] = [self._walk_device(v)
                                  for v in (pool.get(key) or {}).values()]
            out[name] = st
        return out

    def trim(self, pools: list[str], status_raw: Any) -> dict[str, dict]:
        """autotrim plus per-device trim state from `zpool status -j -t`."""
        by_name = {p.get("name"): p
                   for p in ((status_raw or {}).get("pools") or {}).values()}
        res = {}
        for pool in pools:
            rc, out = self.run(["zpool", "get", "-Hp", "-o", "value", "autotrim", pool])
            leaves = self._leaves(by_name.get(pool) or {})
            done = [_num(v.get("trim_time"), None) for v in leaves
                    if v.get("trim_state") == "COMPLETE"]
            done = [t for t in done if t]
            res[pool] = {
                "autotrim_enabled": rc == 0 and out.strip() == "on",
                "last_done_timestamp": _iso(max(done)) if done else None,
                "errors": sum(_num(v.get("trim_errors")) for v in leaves),
                "unsupported_devices": sum(1 for v in leaves if _num(v.get("trim_notsup"))),
            }
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

    def datasets(self) -> Optional[list[dict]]:
        """Dataset list, or None when `zfs list` failed."""
        data = self._json_cmd(
            ["zfs", "list", "-j", "-p", "--json-int", "-o",
             "name,used,available,referenced,quota,reservation,compressratio,"
             "usedbydataset,usedbysnapshots"], "zfs list -j")
        if data is None:
            return None
        res = []
        for ds in (data.get("datasets") or {}).values():
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

    def snapshots(self, existing: Optional[set[str]] = None) -> tuple[list[dict], dict]:
        """Snapshot groups per dataset.

        A dataset that once had snapshots and now has none (snapshotting
        stopped and the pruner removed the rest) keeps a group with count 0
        and the remembered newest time, so it keeps reading as stale instead
        of vanishing. `existing` (dataset names) drops datasets that were
        destroyed; None means unknown, and then no group is added or dropped.
        """
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
        } for ds, c in groups.items()]

        remembered = self.state.setdefault("snapshot_newest", {})
        for ds, c in groups.items():
            remembered[ds] = max(c)
        if existing is not None:
            for ds in list(remembered):
                if ds in groups:
                    continue
                if ds not in existing:
                    del remembered[ds]
                    continue
                newest = remembered[ds]
                out.append({"dataset": ds, "count": 0,
                            "newest_epoch": newest, "oldest_epoch": None,
                            "age_seconds": int(now - newest),
                            "newest_age_seconds": int(now - newest)})
        out.sort(key=lambda g: g["dataset"])
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
        """Whole disks behind every leaf vdev, cache and spare disks included."""
        paths: set[str] = set()
        for pool in ((status_raw or {}).get("pools") or {}).values():
            for v in Collector._leaves(pool):
                if v.get("vdev_type") == "disk" and v.get("path"):
                    paths.add(Collector.base_disk(v["path"]))
        return sorted(paths)

    @staticmethod
    def smart_entry(dev: str, info: dict) -> dict:
        nvme = info.get("nvme_smart_health_information_log")
        status = _num((info.get("smartctl") or {}).get("exit_status"), 0)
        e = dict(_SMART_BLANK, device_path=dev, smart_readable=True,
                 smartctl_exit_status=status,
                 # smartctl's overall health verdict (-H); None when the
                 # device gives none.
                 smart_passed=(info.get("smart_status") or {}).get("passed"),
                 temperature_celsius=(info.get("temperature") or {}).get("current"),
                 power_on_hours=(info.get("power_on_time") or {}).get("hours"),
                 smart_prefail=bool(status & 0x10),
                 smart_past_failures=bool(status & 0x20),
                 smart_selftest_failed=bool(status & 0x80))
        if (nvme is None and "ata_smart_attributes" not in info
                and (info.get("device") or {}).get("protocol") == "SCSI"):
            counters = info.get("scsi_error_counter_log") or {}
            grown = info.get("scsi_grown_defect_list")
            e.update(protocol="SCSI",
                     model=info.get("scsi_model_name") or info.get("model_name") or "unknown",
                     grown_defects=grown,
                     reallocated_sectors=_num(grown),
                     media_errors=sum(_num((counters.get(k) or {}).get("total_uncorrected_errors"))
                                      for k in ("read", "write", "verify")))
            return e
        if nvme is not None:
            tests = (info.get("nvme_self_test_log") or {}).get("table") or []
            last = _num(((tests[0] if tests else {}).get("self_test_result") or {}).get("value"),
                        None)
            e.update(protocol="NVMe", model=info.get("model_name") or "unknown",
                     percentage_used=nvme.get("percentage_used"),
                     available_spare=nvme.get("available_spare"),
                     available_spare_threshold=nvme.get("available_spare_threshold"),
                     media_errors=nvme.get("media_errors") or 0,
                     critical_warning=nvme.get("critical_warning") or 0,
                     smart_selftest_failed=(e["smart_selftest_failed"]
                                            or last in _NVME_SELFTEST_FAILED))
            return e
        table = (info.get("ata_smart_attributes") or {}).get("table") or []

        def attr(i: int, key: str = "raw", default: Any = 0) -> Any:
            for a in table:
                if a.get("id") == i:
                    v = a.get("value") if key == "value" else (a.get("raw") or {}).get("value")
                    return default if v is None else v
            return default

        temp = e["temperature_celsius"]
        if temp is None:
            temp = attr(194, default=None)
            if temp is None:
                temp = attr(190, default=None)
        e.update(
            protocol="ATA",
            model=info.get("model_name") or info.get("model_family") or "unknown",
            temperature_celsius=temp,
            reallocated_sectors=_num(attr(5)), pending_sectors=_num(attr(197)),
            offline_uncorrectable=_num(attr(198)), udma_crc_errors=_num(attr(199)),
            power_on_hours=attr(9, default=None),
            ssd_life_left=attr(231, "value", None),
            media_wearout=attr(233, "value", None),
            failing_attributes=[a.get("name") or str(a.get("id")) for a in table
                                if a.get("when_failed") == "now"],
        )
        return e

    def smart(self, status_raw: Any) -> list[dict]:
        if not self.cfg.collector.smart:
            return []
        if not self._have("smartctl"):
            self.notice("smartctl not available; skipping SMART collection")
            return []
        res = []
        for dev in self.device_paths(status_raw):
            extra = list(self.cfg.collector.smart_device_args.get(dev, []))
            # rc is a bitmask; non-zero with valid JSON still carries the data.
            rc, out = self.run(["smartctl", "-H", "-i", "-A", "-l", "selftest",
                                "-l", "error", "--json", *extra, dev])
            info = _json(out)
            status = rc
            if isinstance(info, dict):
                status = _num((info.get("smartctl") or {}).get("exit_status"), rc)
            # Bits 0/1: bad command line, or the device could not be opened.
            # A disk that stops answering is kept as an unreadable entry
            # instead of silently disappearing.
            if not isinstance(info, dict) or status < 0 or status & 0x3:
                self.notice(f"smartctl failed for {dev}")
                res.append(dict(_SMART_BLANK, device_path=dev,
                                smartctl_exit_status=status if status >= 0 else None))
                continue
            res.append(self.smart_entry(dev, info))
        return res

    # -- ZED events --------------------------------------------------------

    @staticmethod
    def _zed_entry(e: dict) -> dict:
        f = e["fields"]
        t = f.get("time")
        sec = t[0] if isinstance(t, list) and t else t
        pool, vdev, vstate = f.get("pool"), f.get("vdev_path"), f.get("vdev_state")
        return {
            "timestamp": _iso(sec) if isinstance(sec, int) else e["time_text"],
            "eid": f.get("eid") if isinstance(f.get("eid"), int) else 0,
            "class": e["class"],
            "pool": pool, "vdev_path": vdev, "vdev_state": vstate,
            "message": " ".join(str(x) for x in (pool, vdev, vstate) if x),
        }

    def _journal_zed(self, parsed: list[dict], kept: list[dict],
                     boot_id_path: str) -> None:
        """Queue error/state-change events not journaled before. Event ids
        restart at boot, so the high-water mark is per boot id."""
        zs = self.state.setdefault("zed", {})
        try:
            boot = Path(boot_id_path).read_text().strip()
        except OSError:
            boot = None
        last = zs.get("last_eid", 0) if zs.get("boot_id") == boot else 0
        new = [e for e in kept
               if _ZED_JOURNAL.search(e["class"]) and e["eid"] > last]
        self.zed_new = [{"event": "zed_event", "timestamp": e["timestamp"],
                         "class": e["class"], "eid": e["eid"], "pool": e["pool"],
                         "vdev_path": e["vdev_path"], "vdev_state": e["vdev_state"]}
                        for e in new[-self.cfg.collector.zed_event_limit:]]
        eids = [e["fields"].get("eid") for e in parsed]
        zs["boot_id"] = boot
        zs["last_eid"] = max([x for x in eids if isinstance(x, int)] + [last])

    def zed_events(self, boot_id_path: str = "/proc/sys/kernel/random/boot_id") -> list[dict]:
        limit = self.cfg.collector.zed_event_limit
        # zpool events has no -j; -v gives the pool/vdev of each event.
        rc, out = self.run(["zpool", "events", "-vH"])
        if rc == 0:
            parsed = parse_events(out)
            # Filter before limiting, so routine events cannot push real
            # ones out of the window.
            kept = [self._zed_entry(e) for e in parsed if _ZED_KEEP.search(e["class"])]
            self._journal_zed(parsed, kept, boot_id_path)
            return kept[-limit:]
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

    def zed_running(self) -> Optional[bool]:
        """Whether a zed process exists; ZED is what acts on these events
        (hot spares, notifications). None when /proc cannot be read."""
        try:
            pids = [d for d in os.listdir(self.proc) if d.isdigit()]
        except OSError:
            return None
        for pid in pids:
            try:
                if Path(self.proc, pid, "comm").read_text().strip() == "zed":
                    return True
            except OSError:
                continue
        return False

    def journal_events(self, status_raw: Any) -> list[dict]:
        """New scrub/resilver completions and changed per-disk error counters.

        Each is emitted once (tracked in self.state), not on every run while
        the pool still reports the same finished scan or the same errors.
        """
        ts = _iso(self.now())
        out: list[dict] = []
        seen_scrubs = self.state.setdefault("scrubs", {})
        current_errors: dict[str, list] = {}
        seen_errors = self.state.setdefault("errors", {})

        for pool in ((status_raw or {}).get("pools") or {}).values():
            name = pool.get("name")
            scan, stats = self._scan(pool)
            state = str((scan or {}).get("state") or "").upper()
            if scan and state in ("FINISHED", "COMPLETED", "CANCELED"):
                end = scan.get("end_time")
                if seen_scrubs.get(name) != str(end):
                    seen_scrubs[name] = str(end)
                    func = str(scan.get("function") or "").upper()
                    kind = {"SCRUB": "scrub", "RESILVER": "resilver",
                            "ERRORSCRUB": "error_scrub"}.get(func, "scrub")
                    out.append({"pool": name,
                                "event": kind + ("_canceled" if state == "CANCELED"
                                                 else "_completed"),
                                "timestamp": _iso(end) if isinstance(end, (int, float)) else ts,
                                "errors": _num(stats.get("errors")),
                                "repaired_bytes": _num(stats.get("bytes_repaired",
                                                                 stats.get("processed")))})
            for v in self._leaves(pool):
                counts = [_num(v.get(k)) for k in
                          ("read_errors", "write_errors", "checksum_errors")]
                if not any(counts):
                    continue
                dev = v.get("path") or v.get("was") or v.get("name") or "unknown"
                current_errors[dev] = counts
                if seen_errors.get(dev) != counts:
                    out.append({"device": dev, "event": "device_errors", "timestamp": ts,
                                "read_errors": counts[0], "write_errors": counts[1],
                                "checksum_errors": counts[2]})
        # Devices that went back to zero re-arm, so a later recurrence is logged.
        self.state["errors"] = current_errors
        return out

    # -- assembly ----------------------------------------------------------

    def collect(self) -> tuple[dict, list[dict]]:
        # -v: names of files with permanent errors; -t: per-device trim state.
        rc, out = self.run(["zpool", "status", "-j", "-v", "-t", "--json-int"])
        status_raw = _json(out) if rc == 0 else None
        if not isinstance(status_raw, dict):
            status_raw = None
            self.notice("zpool status -j failed")

        pools = self.pool_list()
        if status_raw is None or (not pools and "zpool list -j failed" in self.notices):
            # Without zpool status there are no device trees, and an empty
            # tree reads as "no device errors". Fail instead so the old
            # status.json ages into stale (HTTP 503) and the unit shows failed.
            raise CollectionError("zpool list/status failed; pools not collected")
        for name, st in self.pool_status(status_raw).items():
            pools.setdefault(name, {}).update(st)
        names = list(pools)
        for name, v in self.trim(names, status_raw).items():
            pools[name]["trim"] = v
        for name, v in self.iostat(names).items():
            if name in pools:
                pools[name]["io"] = v
        for name, v in self.features_pending(names).items():
            pools[name]["features_pending"] = v

        datasets = self.datasets()
        groups, snap_stats = self.snapshots(
            None if datasets is None else {d["name"] for d in datasets})
        rc, ver = self.run(["zfs", "version"])
        lines = [x.strip() for x in ver.splitlines() if x.strip()] if rc == 0 else []
        user = lines[0] if lines else "unknown"
        kmod = lines[1] if len(lines) > 1 else None
        report = {
            "hostname": socket.gethostname() or "unknown",
            "zfs_version": user,
            "zfs_kmod_version": kmod,
            # Userland and loaded kernel module differ (upgrade without reboot).
            "zfs_version_mismatch": (kmod is not None and user != "unknown"
                                     and user.removeprefix("zfs-")
                                     != kmod.removeprefix("zfs-kmod-")),
            "collection_timestamp": _iso(self.now()),
            "collection_duration_ms": 0,  # patched below, after the work is done
            "pools": pools,
            # Configured pools that are not imported at all.
            "missing_pools": sorted(set(self.cfg.collector.expected_pools) - set(pools)),
            "datasets": datasets or [],
            "snapshot_groups": groups,
            "snapshot_stats": snap_stats,
            "arc": self.arc(),
            "smart": self.smart(status_raw),
            "zed_events": self.zed_events(),
            "zed_running": self.zed_running(),
            "notices": self.notices,
        }
        report["collection_duration_ms"] = int((self.now() - self._started) * 1000)
        return report, self.journal_events(status_raw) + self.zed_new


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
