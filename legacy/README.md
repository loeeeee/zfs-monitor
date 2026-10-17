# legacy/

The original status HTTP server the `zfs_monitor` package was ported from,
unchanged apart from site-specific paths. Kept as the behavioural reference
for the `summary` block. It is not installed or used by the flake.

| File | Origin |
|------|--------|
| `zfs-monitor-server.py` | the status HTTP server (as of the port) |

The original bash collector is still maintained and lives in
[`../collector/`](../collector/).

Known defects in this server, fixed in the package: see "Behaviour fixes" in the top-level README.
