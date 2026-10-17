"""Configuration: a JSON file (written by the NixOS module) over built-in defaults.

Every key is optional; unknown keys are rejected so a typo in the file fails
loudly instead of silently running with a default.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Thresholds:
    # A status file older than this is reported stale (HTTP 503).
    max_stale_seconds: int = 300
    # A dataset whose newest snapshot is older than this is "stale".
    snapshot_stale_seconds: int = 172800
    hot_drive_celsius: int = 65
    capacity_warning_percent: int = 80
    capacity_critical_percent: int = 90
    arc_hit_ratio_min: float = 0.95
    # Last completed scrub older than this many days counts as overdue.
    scrub_overdue_days: int = 45
    # Pools at or above this fragmentation are flagged.
    fragmentation_warning_percent: int = 50


@dataclass
class CollectorConfig:
    output_dir: str = "/var/lib/zfs-monitor"
    status_file: str = "status.json"
    events_file: str = "events.jsonl"
    lock_file: str = ".collector.lock"
    state_file: str = ".collector-state.json"
    # Chown the output files so an unprivileged reader (the server, possibly in
    # an LXC with a mapped uid) can read them. null = leave ownership alone.
    owner_uid: int | None = None
    owner_gid: int | None = None
    file_mode: int = 0o644
    # Collect SMART data via smartctl (skipped with a notice if it is missing).
    smart: bool = True
    # Extra smartctl arguments per whole-disk path, for disks behind USB
    # bridges or RAID controllers, e.g. {"/dev/sdc": ["-d", "sat"]}.
    smart_device_args: dict[str, list[str]] = field(default_factory=dict)
    # Pools that must be imported; one that is not shows up in
    # missing_pools (zpool itself cannot report a pool it never imported).
    expected_pools: list[str] = field(default_factory=list)
    # Seconds of zpool iostat sampling (0 disables the IO section).
    iostat_seconds: int = 1
    zed_event_limit: int = 50
    # Rotate events.jsonl once it exceeds this size (one .1 generation kept).
    events_max_bytes: int = 5 * 1024 * 1024
    # Per-command timeout; a hung zpool/smartctl must not wedge the timer.
    command_timeout_seconds: int = 30


@dataclass
class ServerConfig:
    listen_address: str = "0.0.0.0"
    port: int = 8080
    # Where to read the report from; defaults to the collector's output.
    status_path: str | None = None


@dataclass
class Config:
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    thresholds: Thresholds = field(default_factory=Thresholds)

    @property
    def status_path(self) -> Path:
        if self.server.status_path:
            return Path(self.server.status_path)
        return Path(self.collector.output_dir) / self.collector.status_file


def _fill(cls: type, raw: dict[str, Any], where: str):
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown config key(s) in {where}: {sorted(unknown)}")
    return cls(**raw)


def from_dict(raw: dict[str, Any]) -> Config:
    unknown = set(raw) - {"collector", "server", "thresholds"}
    if unknown:
        raise ValueError(f"unknown top-level config key(s): {sorted(unknown)}")
    return Config(
        collector=_fill(CollectorConfig, raw.get("collector", {}), "collector"),
        server=_fill(ServerConfig, raw.get("server", {}), "server"),
        thresholds=_fill(Thresholds, raw.get("thresholds", {}), "thresholds"),
    )


def load(path: str | None) -> Config:
    if not path:
        return Config()
    with open(path, "r", encoding="utf-8") as f:
        return from_dict(json.load(f))
