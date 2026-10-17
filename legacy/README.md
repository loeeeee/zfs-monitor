# legacy/

The original scripts the `zfs_monitor` package was ported from, unchanged apart
from site-specific paths and ids. Kept as the behavioural reference for the
report schema; they are not installed or used by the flake.

| File | Origin |
|------|--------|
| `zfs-status-collector.sh` / `.service` / `.timer` | the Proxmox-host collector (bash + jq, every 120s) |
| `zfs-monitor-server.py` | the status HTTP server (as of the port) |

Known defects in these, fixed in the package: see "Behaviour fixes" in the top-level README.
