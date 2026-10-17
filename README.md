# zfs-monitor

A small, dependency-free health monitor for ZFS hosts. It turns everything
worth alerting on — pool health, device errors, capacity, scrubs, resilvers,
snapshot freshness, ARC efficiency, SMART data and ZED events — into **one
JSON report**, and serves that report over HTTP with a flat `summary` block
that uptime checkers and Prometheus can alert on directly.

## Why

ZFS reports its state through a handful of CLIs (`zpool status`, `zpool list`,
`zfs list`, `zpool events`, `smartctl`, `arcstats`), each with its own format,
and the interesting bits are nested inside per-pool, per-vdev and per-disk
trees. Alerting tools such as Gatus are good at checking `[BODY].x == true`
and bad at iterating arrays.

zfs-monitor bridges that gap:

- **One place to look.** Every source is collected on a timer into a single
  `status.json`, so "is my storage OK?" is one HTTP GET.
- **Alert-ready answers.** The server derives booleans and maxima such as
  `all_pools_healthy`, `any_device_errors`, `pool_capacity_critical`,
  `scrub_overdue` and `any_smart_failed`, so alert rules are one-liners.
- **Privilege separation.** Collecting needs root (`/dev/zfs`, raw SMART).
  Serving does not. The two halves are separate processes that share only a
  directory, so the HTTP side can run unprivileged, even in a different
  guest such as an LXC with a bind mount.
- **Fails safe.** A collection that cannot read any pool leaves the previous
  report alone instead of publishing an empty one that looks healthy. Once the
  report is too old the server returns HTTP 503, so a dead collector shows up
  as a failure rather than as stale "all green".

The collector also ships as a standalone bash + jq script with a systemd
timer, in [`collector/`](collector/), for hosts where you would rather not
install Python or Nix. Both collectors write the same report schema, so either
one can feed `zfs-monitor serve`.

## How it works

```
 ZFS host (root)                               anywhere that can read the dir
┌──────────────────────────────┐              ┌──────────────────────────────┐
│ zfs-monitor collect          │  status.json │ zfs-monitor serve            │
│  (systemd timer, every 120s) │ ───────────▶ │  (unprivileged)              │
│  zpool / zfs / smartctl /    │ events.jsonl │  + _meta (age, stale)        │
│  arcstats / zpool events     │              │  + summary (derived fields)  │
└──────────────────────────────┘              └──────────────┬───────────────┘
                                                             │ HTTP
                                         /status  /healthz  /metrics
                                                             ▼
                                                Gatus, Prometheus, curl, …
```

| Half | Runs as | Where | Does |
|------|---------|-------|------|
| `collect` | root, oneshot on a timer | the host that owns the pools | writes `status.json` + `events.jsonl` atomically |
| `serve`   | unprivileged, long-running | anywhere that can read the output dir | serves the report, adds `_meta` + `summary` |

Pure stdlib Python (≥ 3.11). The only tools it needs at runtime are `zpool`,
`zfs` and `smartctl`.

## Commands

```
zfs-monitor [-c config.json] collect [--stdout]   # one report; --stdout prints instead of writing
zfs-monitor [-c config.json] serve                # HTTP server
zfs-monitor [-c config.json] summary              # print summary; exit 1 if stale/unhealthy
zfs-monitor [-c config.json] print-config         # effective configuration
```

`collect --stdout` is the quickest way to try it on a host: it needs no config,
output directory or lock.

### HTTP endpoints

| Path | Returns |
|------|---------|
| `/`, `/status` | full report + `_meta` + `summary`; **503 when stale** |
| `/healthz` | `{"ok": true}` while fresh, 503 otherwise |
| `/metrics` | Prometheus gauges `zfs_monitor_<field>` for every numeric/boolean summary field |

### Alerting examples

Gatus:

```yaml
endpoints:
  - name: zfs
    url: http://nas:8080/status
    interval: 5m
    conditions:
      - "[STATUS] == 200"
      - "[BODY].summary.all_pools_healthy == true"
      - "[BODY].summary.any_device_errors == false"
      - "[BODY].summary.pool_capacity_critical == false"
      - "[BODY].summary.any_smart_failed == false"
```

Prometheus:

```yaml
- alert: ZfsPoolUnhealthy
  expr: zfs_monitor_all_pools_healthy == 0 or zfs_monitor_data_fresh == 0
```

## Install (NixOS)

```nix
# flake input
inputs.zfs-monitor.url = "github:loeeeee/zfs-monitor";

# configuration
imports = [ inputs.zfs-monitor.nixosModules.default ];

services.zfs-monitor = {
  collector.enable = true;          # on the ZFS host
  server.enable = true;             # on the host serving the report
  server.openFirewall = true;
  thresholds.capacityWarningPercent = 85;
};
```

See `nix/module.nix` for every option. `extraSettings` merges raw JSON config
over the generated file. The collector package is built against
`boot.zfs.package` so the userland matches the running kernel module. Both
services run with systemd sandboxing. The server has no capabilities and
read-only access to the output directory.

**Non-NixOS host (e.g. Proxmox):** `nix profile install <flake>` and use the
units in `contrib/systemd/` with `contrib/config.example.json`.

## Configuration file

JSON. Every key is optional, and an unknown key is an error, so a typo fails
loudly. Sections:

- `collector`: output paths, file ownership (`owner_uid`/`owner_gid` so an
  unprivileged or uid-mapped reader can read the files), SMART/iostat toggles,
  event limits, command timeouts
- `server`: listen address and port, status path
- `thresholds`: staleness, capacity, temperature, ARC hit ratio, scrub age,
  fragmentation, snapshot age

Print the defaults with `zfs-monitor print-config`.

## Report schema

Top level: `hostname`, `zfs_version`, `collection_timestamp`,
`collection_duration_ms`, `pools` (keyed by pool name: health, state, size and
capacity, `scan`, `resilver`, device tree, `l2arc`/`slog`/`spare`, `trim`,
`features_pending`, `io`), `datasets`, `snapshot_groups`, `snapshot_stats`,
`arc`, `smart` (ATA, NVMe, SCSI/SAS), `zed_events`, `notices`. The server adds
`_meta` (`file_age_seconds`, `stale`) and `summary`.

### Compatibility with the original collector

All earlier fields and `summary` keys are unchanged. Additions:
`snapshot_groups[].newest_age_seconds`, and the summary keys
`datasets_without_recent_snapshot`, `max_scrub_age_days`, `scrub_overdue`,
`max_fragmentation_percent`, `fragmentation_warning`,
`pools_with_pending_features`, `collector_notice_count`, `pool_count` and
`any_smart_failed` (smartctl's own PASSED/FAILED verdict,
`smart[].smart_passed`). `stale_snapshot_groups` still means what it did
before (oldest snapshot per dataset). Use `datasets_without_recent_snapshot`
to detect datasets whose snapshots have stopped.

### Behaviour fixes over the bash collector

- Drives whose `smartctl` exits non-zero (SMART warnings) are kept, not
  dropped.
- Every command has a timeout, so a hung `zpool` or `smartctl` cannot wedge the
  timer.
- `events.jsonl` rotates. Each scrub completion or error-count change is
  recorded once, with state kept in `.collector-state.json`.
- A run that cannot read any pool exits 1 and leaves the previous report to go
  stale, rather than publishing an empty, "healthy" one.
- SMART targets whole disks (partitions and by-id links are resolved) and
  handles SCSI/SAS.
- The server recomputes `_meta` on every request instead of serving a cached
  value that keeps getting older.

## Repository layout

```
src/zfs_monitor/   collector, summary, HTTP server, config, CLI
tests/             pytest suite (fake command runner, no ZFS needed)
nix/               package and NixOS module
contrib/           systemd units + example config for non-NixOS hosts
collector/         standalone bash + jq collector script with systemd units```

## Development

```
nix develop            # python + pytest, PYTHONPATH=src
pytest                 # tests use a fake command runner (no ZFS needed)
nix flake check        # builds the package (runs tests) and evaluates the NixOS module
```

## License

MIT. See [`LICENSE`](LICENSE).
