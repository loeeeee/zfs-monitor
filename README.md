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

### The contract between the halves

The only interface is the output directory (default `/var/lib/zfs-monitor`):

| File | Written | Read by the server |
|------|---------|--------------------|
| `status.json` | replaced atomically (temp file + rename) on every successful run | yes; its mtime is the report's age |
| `events.jsonl` | one JSON object per line, appended on state changes | no, for log shippers and humans |
| `.collector.lock`, `.collector-state.json` | collector-internal | no |

Both collectors write the published files with mode `0644` and chown them to a
configurable owner (Python: `collector.owner_uid`/`owner_gid`, NixOS:
`collector.fileOwner`, bash: `ZFS_MONITOR_UID`/`ZFS_MONITOR_GID`). Pick the
uid the server runs as, as the host sees it.

### Deployment: ZFS host + server in an LXC (Proxmox)

On the host (root, has the pools):

1. Install one collector, either `contrib/systemd/zfs-monitor-collector.{service,timer}`
   with `contrib/config.example.json`, or the bash script in `collector/`.
2. Set the owner to the container's mapped uid: uid 1000 inside an
   unprivileged LXC is `101000` on the host (the default in both examples).
3. Enable the timer: `systemctl enable --now zfs-monitor-collector.timer`
   (bash: `zfs-status-collector.timer`).
4. Bind-mount the directory into the container read-only:
   `pct set <vmid> -mp0 /var/lib/zfs-monitor,mp=/var/lib/zfs-monitor,ro=1`.

In the container (unprivileged, no ZFS tools needed):

1. Install the package (`nix profile install <flake>` or `pip install .`).
2. Copy `contrib/config.server.example.json` to `/etc/zfs-monitor/config.json`.
3. Install `contrib/systemd/zfs-monitor-server.service` and
   `systemctl enable --now zfs-monitor-server`.
   On NixOS, use `services.zfs-monitor.server.enable = true` with
   `collector.outputDir` set to the bind-mount path.

Check from the container: `zfs-monitor -c /etc/zfs-monitor/config.json summary`.
Keep `thresholds.max_stale_seconds` above the timer interval (default 300 s vs 120 s),
so one slow run doesn't make the report go stale.

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
      - "[BODY].summary.any_pool_data_errors == false"
      - "[BODY].summary.any_device_not_online == false"
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

Top level: `hostname`, `zfs_version`, `zfs_kmod_version`,
`zfs_version_mismatch`, `collection_timestamp`, `collection_duration_ms`,
`pools`, `missing_pools`, `datasets`, `snapshot_groups`, `snapshot_stats`,
`arc`, `smart` (ATA, NVMe, SCSI/SAS), `zed_events`, `zed_running`, `notices`.
The server adds `_meta` (`file_age_seconds`, `stale`) and `summary`.

Each pool (keyed by name) has health, state, size and capacity, zpool's own
diagnosis (`status_message`, `action`, `msgid`, `moreinfo`), permanent errors
(`error_count`, `error_files`), `scan`, `resilver` (healing resilver or
sequential rebuild), `removal`, `raidz_expand`, `checkpoint`, `trim`,
`features_pending`, `io`, and one device tree per vdev class: `devices`
(data), `special`, `dedup`, `slog`, `l2arc` and `spare`. Every device carries
its state, error counters, `slow_ios`, `path`, `aux` (why it is unusable) and
`was` (last path of a device that is not present).

Set `collector.expected_pools` (NixOS: `collector.expectedPools`) to the
pools that must be imported: zpool cannot report a pool that never imported.

### Compatibility with the original collector

All earlier fields and `summary` keys are still present. Additions:
`snapshot_groups[].newest_age_seconds`, and the summary keys
`datasets_without_recent_snapshot`, `max_scrub_age_days`, `scrub_overdue`,
`max_fragmentation_percent`, `fragmentation_warning`,
`pools_with_pending_features`, `collector_notice_count`, `pool_count` and
`any_smart_failed` (smartctl's own PASSED/FAILED verdict,
`smart[].smart_passed`). `stale_snapshot_groups` still means what it did
before (oldest snapshot per dataset). Use `datasets_without_recent_snapshot`
to detect datasets whose snapshots have stopped.

Further summary keys: `missing_pools`, `any_pool_missing`,
`any_pool_data_errors`, `total_pool_data_errors`, `pools_with_data_errors`,
`pool_status_msgids`, `any_interior_vdev_errors`, `total_slow_ios`,
`unavailable_spares`, `any_scan_errors`, `pools_scrub_age_unknown`,
`total_trim_errors`, `pools_with_checkpoint`, `any_vdev_removal_in_progress`,
`any_raidz_expansion_in_progress`, `any_smart_prefail`,
`any_smart_selftest_failed`, `smart_unreadable_count`,
`zed_error_events_count`, `zed_running` and `zfs_version_mismatch`.

Fields whose content changed because the old values were wrong:

- `status_message` is zpool's status text. It used to be the pool's
  user-set `comment` property.
- `l2arc`, `slog` and `spare` were always empty: `zpool status -j` keeps those
  vdevs outside the root vdev, where the old collector looked. They now list
  the class's top-level vdevs (a log mirror has `children`).
- The error counters and `non_online_devices` also cover the special, dedup,
  log and cache vdevs. Hot spares are checked separately (`unavailable_spares`).
  A missing pool counts in `unhealthy_pools`.
- `scan.days_since_last` is the age of the last *completed scrub*, kept in
  the state file across later resilvers and running scrubs. It is null until
  the collector has seen one finish, instead of reading as ~20 000 days while
  a scrub runs.
- `trim.last_done_timestamp` is ISO 8601 UTC. It was always null before.
- `zed_events` used to hold only time and class. `zpool events` has no
  `-j`, so it is now parsed from `zpool events -v`. Routine classes are
  dropped before the `zed_event_limit` cut, and entries gain `pool`,
  `vdev_path` and `vdev_state`.
- `zfs_version` is the full first line of `zfs version` (unchanged), and
  `zfs_kmod_version` is the second line.

### Behaviour fixes over the bash collector

- Drives whose `smartctl` exits non-zero (SMART warnings) are kept, not
  dropped.
- Every command has a timeout, so a hung `zpool` or `smartctl` cannot wedge the
  timer.
- `events.jsonl` rotates. Each scrub completion or error-count change is
  recorded once, with state kept in `.collector-state.json`.
- A run that cannot read any pool, or whose `zpool status` fails, exits 1 and
  leaves the previous report to go stale, rather than publishing one with
  empty, "healthy" device trees.
- smartctl runs with `-H` (the health verdict, previously never requested),
  plus the self-test and error logs. A disk smartctl cannot open is kept as
  `smart_readable: false` instead of disappearing. Per-disk extra arguments
  (`-d sat`, ...) go in `collector.smart_device_args`.
- `events.jsonl` also records resilver completions, cancelled scans and new
  ZED error/state-change events (once per event id and boot).
- A dataset whose snapshots were all pruned keeps a `count: 0` snapshot group
  with its last snapshot time, so it stays stale instead of vanishing.
- SMART targets whole disks (partitions and by-id links are resolved) and
  handles SCSI/SAS.
- The server recomputes `_meta` on every request instead of serving a cached
  value that keeps getting older.

## Repository layout

```
src/zfs_monitor/   collector, summary, HTTP server, config, CLI
tests/             pytest suite (fake command runner, no ZFS needed)
nix/               package and NixOS module
contrib/           systemd units (collector + server) and example configs for non-NixOS hosts
collector/         standalone bash + jq collector script with systemd units```

## Development

```
nix develop            # python + pytest, PYTHONPATH=src
pytest                 # tests use a fake command runner (no ZFS needed)
nix flake check        # builds the package (runs tests) and evaluates the NixOS module
```

## License

MIT. See [`LICENSE`](LICENSE).
