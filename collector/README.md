# collector/

The standalone ZFS status collector: one bash script plus a systemd timer.
It needs nothing but `bash`, `jq`, `flock` and the ZFS tools (OpenZFS ≥ 2.3,
for `zpool … -j`); `smartctl` is used when present. It suits hosts where you
don't want a Python or Nix install, such as a stock Proxmox VE node.

It writes `status.json` (the full report) and appends to `events.jsonl` in
`/var/lib/zfs-monitor`, using the same schema that `zfs-monitor serve` and
the Python `zfs-monitor collect` use.

## Install

As root on the ZFS host, from this directory:

```
./install.sh                          # owner 101000:101000 (uid 1000 in an unprivileged LXC)
./install.sh --uid 100033 --gid 100033 --output-dir /var/lib/zfs-monitor
./install.sh --no-chown               # leave the files owned by root
./install.sh --dry-run                # show what it would do
./install.sh --uninstall [--purge]    # --purge also removes the env file and output dir
```

It checks for `jq`, `flock` and `zpool -j` support, installs the script and
units, writes the settings below to `/etc/zfs-monitor/collector.env`, enables
the timer and runs one collection. Re-running it upgrades in place; the env
file is only rewritten when a setting flag is given.

Or by hand:

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

Set these environment variables in `/etc/zfs-monitor/collector.env` (read by
the unit if present), or in a drop-in made with
`systemctl edit zfs-status-collector.service`:

| Variable | Default | Meaning |
|----------|---------|---------|
| `ZFS_MONITOR_OUTPUT_DIR` | `/var/lib/zfs-monitor` | where `status.json` / `events.jsonl` go |
| `ZFS_MONITOR_UID` | `101000` | owner of the written files; set empty to leave ownership alone |
| `ZFS_MONITOR_GID` | `101000` | group of the written files; set empty to leave ownership alone |
| `ZFS_MONITOR_FILE_MODE` | `0644` | mode of the written files |

`101000` is uid/gid 1000 inside an unprivileged Proxmox LXC. With that default,
a server running in the container as a normal user can read a bind-mounted
output directory.

## Compared with `zfs-monitor collect`

The Python collector in `src/zfs_monitor/` is a port of this script that adds
command timeouts, event de-duplication and rotation, SCSI/SAS SMART and a
fail-safe that refuses to publish an empty report (see "Behaviour fixes" in
the top-level README). Use it where you can install the package. Use this
script where a single dependency-light file is the better fit.
