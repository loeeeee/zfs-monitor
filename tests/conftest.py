import json

import pytest

from zfs_monitor.config import Config

NOW = 1_800_000_000.0


def _vdev(name, vtype, state="ONLINE", path=None, kids=None, **extra):
    v = {"name": name, "vdev_type": vtype, "state": state,
         "read_errors": 0, "write_errors": 0, "checksum_errors": 0, **extra}
    if path:
        v["path"] = path
    if kids:
        v["vdevs"] = {k["name"]: k for k in kids}
    return v


# Shaped like `zpool status -j -v -t --json-int` (OpenZFS 2.3): log, special,
# cache and spare vdevs live under their own pool keys, not under the root.
ZPOOL_STATUS = {"pools": {"tank": {
    "name": "tank", "state": "ONLINE",
    "status": "One or more devices has experienced an error resulting in data\n"
              "\tcorruption.  Applications may be affected.\n",
    "action": "Restore the file in question if possible.\n",
    "msgid": "ZFS-8000-8A",
    "moreinfo": "https://openzfs.github.io/openzfs-docs/msg/ZFS-8000-8A",
    "scan_stats": {"function": "SCRUB", "state": "FINISHED",
                   "start_time": NOW - 3 * 86400 - 3600, "end_time": NOW - 3 * 86400,
                   "errors": 0, "processed": 0, "scrub_pause": 0},
    "vdevs": {"tank": _vdev("tank", "root", kids=[
        _vdev("mirror-0", "mirror", kids=[
            _vdev("sda", "disk", path="/dev/sda", trim_state="COMPLETE",
                  trim_time=NOW - 86400, trim_errors=0, trim_notsup=0),
            _vdev("sdb", "disk", path="/dev/sdb", checksum_errors=2, slow_ios=3),
        ]),
    ])},
    "special": {"mirror-1": _vdev("mirror-1", "mirror", kids=[
        _vdev("sde", "disk", path="/dev/sde"),
        _vdev("sdf", "disk", state="FAULTED", path="/dev/sdf", aux="too many errors",
              write_errors=7),
    ])},
    "logs": {"sdc": _vdev("sdc", "disk", path="/dev/sdc")},
    "l2cache": {"nvme0n1": _vdev("nvme0n1", "disk", path="/dev/nvme0n1")},
    "spares": {"sdg": _vdev("sdg", "disk", state="AVAIL", path="/dev/sdg")},
    "error_count": 1,
    "errlist": ["/tank/data/file.bin"],
}}}

ZPOOL_LIST = {"pools": {"tank": {"properties": {
    "health": {"value": "ONLINE"}, "size": {"value": 1000}, "allocated": {"value": 850},
    "free": {"value": 150}, "checkpoint": {"value": "-"}, "expandsize": {"value": "-"},
    "fragmentation": {"value": 12}, "capacity": {"value": 85},
    "dedupratio": {"value": "1.00x"}}}}}

ZFS_LIST = {"datasets": {
    "tank": {"name": "tank", "properties": {
        "used": {"value": 850}, "available": {"value": 150}, "referenced": {"value": 100},
        "quota": {"value": 0}, "reservation": {"value": 0},
        "compressratio": {"value": "1.50x"}, "usedbysnapshots": {"value": 85}}}}}

ZFS_SNAPS = {"datasets": {
    "tank@a": {"name": "tank@a", "properties": {"creation": {"value": NOW - 10 * 86400}, "used": {"value": 5}}},
    "tank@b": {"name": "tank@b", "properties": {"creation": {"value": NOW - 100}, "used": {"value": 7}}},
}}

SMART_ATA = {"smartctl": {"exit_status": 4}, "model_name": "WD Red",
             "smart_status": {"passed": True}, "temperature": {"current": 70},
             "ata_smart_attributes": {"table": [
                 {"id": 5, "name": "Reallocated_Sector_Ct", "value": 100,
                  "raw": {"value": 3}, "when_failed": ""},
                 {"id": 9, "name": "Power_On_Hours", "value": 90,
                  "raw": {"value": 4000}, "when_failed": ""}]}}
SMART_NVME = {"smartctl": {"exit_status": 0}, "model_name": "Samsung",
              "smart_status": {"passed": True}, "temperature": {"current": 40},
              "power_on_time": {"hours": 10},
              "nvme_smart_health_information_log": {
                  "percentage_used": 4, "available_spare": 100,
                  "available_spare_threshold": 10, "media_errors": 0, "critical_warning": 0}}

ARC = "header\nname type data\nhits 4 900\nmisses 4 100\nsize 4 5\nc_max 4 9\n"

IOSTAT = "tank\t850\t150\t1\t2\t3\t4\n"

# `zpool events -vH`, as printed by zpool_do_events_short/_nvprint.
ZPOOL_EVENTS = (
    "Jan 15 2027 08:00:00.000000000\tsysevent.fs.zfs.history_event\n"
    "        class = \"sysevent.fs.zfs.history_event\"\n"
    "        pool = \"tank\"\n"
    "        time = 0x6b49d100 0x0 \n"
    "        eid = 0x1\n"
    "\n"
    "Jan 15 2027 08:00:01.000000000\tereport.fs.zfs.checksum\n"
    "        class = \"ereport.fs.zfs.checksum\"\n"
    "        detector = (embedded nvlist)\n"
    "                version = 0x0\n"
    "                pool = \"nested-not-top-level\"\n"
    "        (end detector)\n"
    "        pool = \"tank\"\n"
    "        vdev_path = \"/dev/sdb1\"\n"
    "        time = 0x6b49d101 0x0 \n"
    "        eid = 0x2\n"
    "\n"
    "dropped 3 events\n"
    "Jan 15 2027 08:00:02.000000000\tresource.fs.zfs.statechange\n"
    "        class = \"resource.fs.zfs.statechange\"\n"
    "        pool = \"tank\"\n"
    "        vdev_path = \"/dev/sdf1\"\n"
    "        vdev_state = \"FAULTED\" (0x5)\n"
    "        time = 0x6b49d102 0x0 \n"
    "        eid = 0x3\n"
    "\n"
)


def make_runner(missing=(), overrides=None):
    """Canned command outputs keyed on the leading argv; (rc, stdout).
    `overrides` maps a joined-argv prefix to (rc, stdout)."""
    def run(cmd):
        key = " ".join(cmd)
        if cmd[0] in missing:
            return -1, ""
        for prefix, res in (overrides or {}).items():
            if key.startswith(prefix):
                return res
        if key == "zpool status -j -v -t --json-int":
            return 0, json.dumps(ZPOOL_STATUS)
        if key.startswith("zpool list -j"):
            return 0, json.dumps(ZPOOL_LIST)
        if key.startswith("zfs list -j -t snapshot"):
            return 0, json.dumps(ZFS_SNAPS)
        if key.startswith("zfs list -j"):
            return 0, json.dumps(ZFS_LIST)
        if key.startswith("zpool get -Hp -o value autotrim"):
            return 0, "on\n"
        if key.startswith("zpool iostat"):
            return 0, IOSTAT
        if key.startswith("zpool get -H -o property,value all"):
            return 0, "feature@foo\tdisabled\nfeature@bar\tactive\n"
        if key == "zfs version":
            return 0, "zfs-2.4.0-1\nzfs-kmod-2.4.0-1\n"
        if key == "zpool events -vH":
            return 0, ZPOOL_EVENTS
        if cmd[0] == "smartctl":
            if cmd[-1] == "--version":
                return 0, "smartctl 7.4"
            dev = cmd[-1]
            info = SMART_NVME if "nvme" in dev else SMART_ATA
            return info["smartctl"]["exit_status"], json.dumps(info)  # rc may be non-zero
        return 1, ""
    return run


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.collector.output_dir = str(tmp_path / "out")
    return c
