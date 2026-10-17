"""Derive a flat, scalar summary block from the raw report.

Every field is a bool/int/float/str, a list of str, or null so alert rules
(Gatus, Prometheus) can use them directly without array iteration. The first
group of keys is the original contract and must not change meaning; keys
under "additions" are new.
"""

from __future__ import annotations

from typing import Any, Iterator

from .config import Thresholds

# ZED event classes that are routine bookkeeping noise.
ZED_NOISE_CLASS = "sysevent.fs.zfs.history_event"


def walk_devices(devices: list[dict]) -> Iterator[dict]:
    """Yield all leaf devices from a nested device tree."""
    for dev in devices:
        children = dev.get("children", [])
        if children:
            yield from walk_devices(children)
        else:
            yield dev


def compute_summary(data: dict, age: float, stale: bool, t: Thresholds) -> dict[str, Any]:
    pools = data.get("pools", {})
    smart = data.get("smart", [])
    groups = data.get("snapshot_groups", [])
    arc = data.get("arc", {})
    zed = data.get("zed_events", [])

    unhealthy = [n for n, p in pools.items() if p.get("health") != "ONLINE"]

    read_e = write_e = csum_e = 0
    non_online: list[str] = []
    for pool in pools.values():
        for dev in walk_devices(pool.get("devices", [])):
            read_e += dev.get("read_errors", 0)
            write_e += dev.get("write_errors", 0)
            csum_e += dev.get("checksum_errors", 0)
            if dev.get("state") != "ONLINE":
                non_online.append(dev.get("name", "unknown"))

    capacity = {n: p.get("capacity_percent", 0) for n, p in pools.items()}
    max_cap = max(capacity.values(), default=0)

    temps = [d["temperature_celsius"] for d in smart
             if d.get("temperature_celsius") is not None]
    pct_used = [d["percentage_used"] for d in smart if d.get("percentage_used") is not None]
    spare = [d["available_spare"] for d in smart if d.get("available_spare") is not None]

    oldest = [g["age_seconds"] for g in groups if "age_seconds" in g]
    newest = [g["newest_age_seconds"] for g in groups if "newest_age_seconds" in g]
    ratio = arc.get("lifetime_hit_ratio")

    scrub_days = [p["scan"]["days_since_last"] for p in pools.values()
                  if (p.get("scan") or {}).get("days_since_last") is not None]
    frag = [p.get("fragmentation_percent", 0) for p in pools.values()]

    return {
        "all_pools_healthy": not unhealthy,
        "unhealthy_pool_count": len(unhealthy),
        "unhealthy_pools": unhealthy,

        "any_device_errors": bool(read_e or write_e or csum_e),
        "total_read_errors": read_e,
        "total_write_errors": write_e,
        "total_checksum_errors": csum_e,
        "any_device_not_online": bool(non_online),
        "non_online_devices": non_online,

        "max_pool_capacity_percent": max_cap,
        "pool_capacity_warning": max_cap >= t.capacity_warning_percent,
        "pool_capacity_critical": max_cap >= t.capacity_critical_percent,
        "highest_capacity_pool": max(capacity, key=capacity.get, default=None),

        "max_temperature_celsius": max(temps, default=None),
        "hot_drive_count": sum(1 for x in temps if x >= t.hot_drive_celsius),
        "max_ssd_percentage_used": max(pct_used, default=0),
        "min_available_spare_percent": min(spare, default=100),
        "any_smart_critical_warning": any(d.get("critical_warning", 0) != 0 for d in smart),
        "total_media_errors": sum(d.get("media_errors", 0) for d in smart),

        "any_resilver_in_progress": any(
            p.get("resilver", {}).get("in_progress", False) for p in pools.values()),
        "any_scrub_in_progress": any(
            (p.get("scan") or {}).get("state") == "SCANNING" for p in pools.values()),

        # Ages of the oldest snapshot in each dataset group (original semantics).
        "oldest_snapshot_age_seconds": max(oldest, default=None),
        "newest_snapshot_age_seconds": min(oldest, default=None),
        "stale_snapshot_groups": sum(1 for a in oldest if a > t.snapshot_stale_seconds),

        "arc_hit_ratio": ratio,
        "arc_hit_ratio_ok": ratio is not None and ratio >= t.arc_hit_ratio_min,

        "real_zed_events_count": sum(1 for e in zed if e.get("class") != ZED_NOISE_CLASS),

        "data_fresh": not stale,
        "collector_age_seconds": round(age, 1),

        # --- additions ---
        # Datasets whose *newest* snapshot is older than the threshold: the
        # actual "snapshots stopped being taken" signal.
        "datasets_without_recent_snapshot": sum(
            1 for a in newest if a > t.snapshot_stale_seconds),
        "max_scrub_age_days": max(scrub_days, default=None),
        "scrub_overdue": any(d > t.scrub_overdue_days for d in scrub_days),
        "max_fragmentation_percent": max(frag, default=0),
        "fragmentation_warning": max(frag, default=0) >= t.fragmentation_warning_percent,
        "pools_with_pending_features": [
            n for n, p in pools.items() if p.get("features_pending")],
        "collector_notice_count": len(data.get("notices", [])),
        "pool_count": len(pools),
        # smartctl's own verdict; null verdicts (not reported) do not count.
        "any_smart_failed": any(d.get("smart_passed") is False for d in smart),
    }
