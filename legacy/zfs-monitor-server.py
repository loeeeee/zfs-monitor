#!/usr/bin/env python3
"""ZFS monitoring HTTP server.

Reads the comprehensive ZFS status JSON (collected by a root-owned systemd
timer on the ZFS host) from /var/lib/zfs-monitor/status.json and serves
it on port 8080.

The status schema is object-based with pool-name-keyed lookups and includes
pool health, device trees, ARC stats, SMART attributes, dataset info,
snapshot groups, ZED events, and IO performance data.

A `summary` block with pre-computed scalar fields is injected at serve time
to simplify Gatus condition expressions.
"""

import http.server
import json
import logging
import os
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("zfs-monitor")

STATUS_FILE = "/var/lib/zfs-monitor/status.json"
MAX_STALE_SECONDS = 300  # 5 minutes

# Cache the parsed document across requests; the collector only rewrites the
# file on its timer, so re-reading + re-parsing on every scrape is wasted I/O.
_cache = {}
_cache_mtime = None
_cache_size = None
# Serialized response bytes are also cached (keyed on the same mtime/size) so
# the summary computation + json.dumps run only when the file actually changes.
_cache_payload = None

# Snapshot groups older than this are considered stale.
SNAPSHOT_STALE_THRESHOLD_SECONDS = 172800  # 2 days

# Drive temperature threshold for "hot" classification.
HOT_DRIVE_CELSIUS = 65

# ZED event classes that are routine bookkeeping noise.
ZED_NOISE_CLASS = "sysevent.fs.zfs.history_event"


def _walk_devices(devices):
    """Yield all leaf disk devices from a nested device tree."""
    for dev in devices:
        children = dev.get("children", [])
        if children:
            yield from _walk_devices(children)
        else:
            yield dev


def compute_summary(data, age, stale):
    """Derive a flat summary block from the raw ZFS status JSON.

    All fields are scalar (bool, int, float, list of str, or null) so that
    Gatus conditions can reference them directly without array iteration.
    """
    pools = data.get("pools", {})
    smart = data.get("smart", [])
    snapshot_groups = data.get("snapshot_groups", [])
    arc = data.get("arc", {})
    zed_events = data.get("zed_events", [])

    # --- Pool health ---
    unhealthy_pools = [
        name for name, pool in pools.items()
        if pool.get("health") != "ONLINE"
    ]
    all_pools_healthy = len(unhealthy_pools) == 0

    # --- Device errors (walk full device tree for each pool) ---
    total_read_errors = 0
    total_write_errors = 0
    total_checksum_errors = 0
    non_online_devices = []

    for pool in pools.values():
        for dev in _walk_devices(pool.get("devices", [])):
            total_read_errors += dev.get("read_errors", 0)
            total_write_errors += dev.get("write_errors", 0)
            total_checksum_errors += dev.get("checksum_errors", 0)
            if dev.get("state") != "ONLINE":
                non_online_devices.append(dev.get("name", "unknown"))

    any_device_errors = (
        total_read_errors > 0
        or total_write_errors > 0
        or total_checksum_errors > 0
    )
    any_device_not_online = len(non_online_devices) > 0

    # --- Pool capacity ---
    capacity_by_pool = {
        name: pool.get("capacity_percent", 0)
        for name, pool in pools.items()
    }
    max_pool_capacity_percent = max(capacity_by_pool.values(), default=0)
    highest_capacity_pool = max(
        capacity_by_pool, key=capacity_by_pool.get, default=None
    )

    # --- SMART / drives ---
    temperatures = [
        d["temperature_celsius"]
        for d in smart
        if d.get("temperature_celsius") is not None
    ]
    max_temperature_celsius = max(temperatures, default=None)
    hot_drive_count = sum(
        1 for t in temperatures if t >= HOT_DRIVE_CELSIUS
    )

    nvme_pct_used = [
        d["percentage_used"]
        for d in smart
        if d.get("percentage_used") is not None
    ]
    max_ssd_percentage_used = max(nvme_pct_used, default=0)

    nvme_spare = [
        d["available_spare"]
        for d in smart
        if d.get("available_spare") is not None
    ]
    min_available_spare_percent = min(nvme_spare, default=100)

    any_smart_critical_warning = any(
        d.get("critical_warning", 0) != 0 for d in smart
    )
    total_media_errors = sum(d.get("media_errors", 0) for d in smart)

    # --- Resilver / scrub ---
    any_resilver_in_progress = any(
        pool.get("resilver", {}).get("in_progress", False)
        for pool in pools.values()
    )
    any_scrub_in_progress = any(
        (pool.get("scan") or {}).get("state") == "SCANNING"
        for pool in pools.values()
    )

    # --- Snapshots ---
    snap_ages = [g["age_seconds"] for g in snapshot_groups if "age_seconds" in g]
    oldest_snapshot_age_seconds = max(snap_ages, default=None)
    newest_snapshot_age_seconds = min(snap_ages, default=None)
    stale_snapshot_groups = sum(
        1 for a in snap_ages if a > SNAPSHOT_STALE_THRESHOLD_SECONDS
    )

    # --- ARC ---
    arc_hit_ratio = arc.get("lifetime_hit_ratio")
    arc_hit_ratio_ok = arc_hit_ratio is not None and arc_hit_ratio >= 0.95

    # --- ZED events ---
    real_zed_events_count = sum(
        1 for e in zed_events if e.get("class") != ZED_NOISE_CLASS
    )

    return {
        "all_pools_healthy": all_pools_healthy,
        "unhealthy_pool_count": len(unhealthy_pools),
        "unhealthy_pools": unhealthy_pools,

        "any_device_errors": any_device_errors,
        "total_read_errors": total_read_errors,
        "total_write_errors": total_write_errors,
        "total_checksum_errors": total_checksum_errors,
        "any_device_not_online": any_device_not_online,
        "non_online_devices": non_online_devices,

        "max_pool_capacity_percent": max_pool_capacity_percent,
        "pool_capacity_warning": max_pool_capacity_percent >= 80,
        "pool_capacity_critical": max_pool_capacity_percent >= 90,
        "highest_capacity_pool": highest_capacity_pool,

        "max_temperature_celsius": max_temperature_celsius,
        "hot_drive_count": hot_drive_count,
        "max_ssd_percentage_used": max_ssd_percentage_used,
        "min_available_spare_percent": min_available_spare_percent,
        "any_smart_critical_warning": any_smart_critical_warning,
        "total_media_errors": total_media_errors,

        "any_resilver_in_progress": any_resilver_in_progress,
        "any_scrub_in_progress": any_scrub_in_progress,

        "oldest_snapshot_age_seconds": oldest_snapshot_age_seconds,
        "newest_snapshot_age_seconds": newest_snapshot_age_seconds,
        "stale_snapshot_groups": stale_snapshot_groups,

        "arc_hit_ratio": arc_hit_ratio,
        "arc_hit_ratio_ok": arc_hit_ratio_ok,

        "real_zed_events_count": real_zed_events_count,

        "data_fresh": not stale,
        "collector_age_seconds": round(age, 1),
    }


class ZFSMonitorHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that serves ZFS status JSON."""

    def do_GET(self):
        """Handle GET requests by returning the ZFS status JSON."""
        if self.path != "/" and self.path != "/status":
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "not found"}).encode())
            return

        try:
            global _cache, _cache_mtime, _cache_size, _cache_payload
            mtime = os.path.getmtime(STATUS_FILE)
            size = os.path.getsize(STATUS_FILE)
            age = time.time() - mtime

            if _cache is not None and mtime == _cache_mtime and size == _cache_size:
                data = _cache
                payload = _cache_payload
            else:
                with open(STATUS_FILE, "r") as f:
                    data = json.load(f)
                _cache = data
                _cache_mtime = mtime
                _cache_size = size
                stale = age > MAX_STALE_SECONDS
                # Never mutate the cached document: _meta/summary are per-request
                # additions, so serve a shallow copy with them attached.
                data = dict(data)
                data["_meta"] = {
                    "file_age_seconds": round(age, 1),
                    "stale": stale,
                }
                data["summary"] = compute_summary(data, age, stale)
                payload = json.dumps(data, indent=2).encode()
                _cache_payload = payload

            stale = age > MAX_STALE_SECONDS
            status_code = 200 if not stale else 503
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        except FileNotFoundError:
            logger.warning("Status file not found: %s", STATUS_FILE)
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "ZFS status file not found",
                "detail": "The host-side collector may not be running.",
            }).encode())

        except json.JSONDecodeError as e:
            logger.error("Invalid JSON in status file: %s", e)
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "Invalid JSON in status file",
                "detail": str(e),
            }).encode())

    def log_message(self, format, *args):
        """Override to use Python logging."""
        logger.info("%s %s", self.client_address[0], format % args)


def main():
    """Start the HTTP server."""
    server = http.server.HTTPServer(("0.0.0.0", 8080), ZFSMonitorHandler)
    logger.info("ZFS Monitor HTTP server listening on 0.0.0.0:8080")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
