# collector/

The standalone ZFS status collector: one bash script plus a systemd timer.
It needs nothing but `bash`, `jq`, `flock` and the ZFS tools (OpenZFS ≥ 2.3,
for `zpool … -j`); `smartctl` is used when present. It suits hosts where you
don't want a Python or Nix install, such as a stock Proxmox VE node.

It writes `status.json` (the full report) and appends to `events.jsonl` in
`/var/lib/zfs-monitor`, using the same schema that `zfs-monitor serve` and
the Python `zfs-monitor collect` use.

## Install

```
cp zfs-status-collector.sh /usr/local/bin/
chmod 755 /usr/local/bin/zfs-status-collector.sh
cp zfs-status-collector.service zfs-status-collector.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now zfs-status-collector.timer
```

The timer runs it every 120 seconds. Run it once by hand with
`zfs-status-collector.sh`, which prints a one-line summary plus any notices.

## Configuration

Set these environment variables, for example in a drop-in made with
`systemctl edit zfs-status-collector.service` (`[Service]` /
`Environment=ZFS_MONITOR_UID=0`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `ZFS_MONITOR_OUTPUT_DIR` | `/var/lib/zfs-monitor` | where `status.json` / `events.jsonl` go |
| `ZFS_MONITOR_UID` | `101000` | owner of the written files |
| `ZFS_MONITOR_GID` | `101000` | group of the written files |

`101000` is uid/gid 1000 inside an unprivileged Proxmox LXC. With that default,
a server running in the container as a normal user can read a bind-mounted
output directory.

## Compared with `zfs-monitor collect`

The Python collector in `src/zfs_monitor/` is a port of this script that adds
command timeouts, event de-duplication and rotation, SCSI/SAS SMART and a
fail-safe that refuses to publish an empty report (see "Behaviour fixes" in
the top-level README). Use it where you can install the package. Use this
script where a single dependency-light file is the better fit.
