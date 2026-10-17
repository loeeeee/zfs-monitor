import json

import pytest

from zfs_monitor.config import Config

NOW = 1_800_000_000.0


def _vdev(name, vtype, state="ONLINE", path=None, kids=None, **errs):
    v = {"name": name, "vdev_type": vtype, "state": state,
         "read_errors": 0, "write_errors": 0, "checksum_errors": 0, **errs}
    if path:
        v["path"] = path
    if kids:
        v["vdevs"] = {k["name"]: k for k in kids}
    return v


ZPOOL_STATUS = {"pools": {"tank": {
    "name": "tank", "state": "ONLINE",
    "scan": {"state": "FINISHED", "end_time": NOW - 3 * 86400,
             "scan_stats": {"errors": 0, "bytes_repaired": 0}},
    "vdevs": {"tank": _vdev("tank", "root", kids=[
        _vdev("mirror-0", "mirror", kids=[
            _vdev("sda", "disk", path="/dev/sda"),
            _vdev("sdb", "disk", path="/dev/sdb", checksum_errors=2),
        ]),
        _vdev("nvme0n1", "l2cache", kids=[_vdev("nvme0n1", "disk", path="/dev/nvme0n1")]),
    ])},
}}}

ZPOOL_LIST = {"pools": {"tank": {"properties": {
    "health": {"value": "ONLINE"}, "size": {"value": 1000}, "allocated": {"value": 850},
    "free": {"value": 150}, "fragmentation": {"value": 12}, "capacity": {"value": 85}}}}}

ZFS_LIST = {"datasets": {
    "tank": {"name": "tank", "properties": {
        "used": {"value": 850}, "available": {"value": 150}, "referenced": {"value": 100},
        "quota": {"value": 0}, "reservation": {"value": 0},
        "compressratio": {"value": "1.50x"}, "usedbysnapshots": {"value": 85}}}}}

ZFS_SNAPS = {"datasets": {
    "tank@a": {"name": "tank@a", "properties": {"creation": {"value": NOW - 10 * 86400}, "used": {"value": 5}}},
    "tank@b": {"name": "tank@b", "properties": {"creation": {"value": NOW - 100}, "used": {"value": 7}}},
}}

SMART_ATA = {"model_name": "WD Red", "temperature": {"current": 70},
             "ata_smart_attributes": {"table": [
                 {"id": 5, "value": 100, "raw": {"value": 3}},
                 {"id": 9, "value": 90, "raw": {"value": 4000}}]}}
SMART_NVME = {"model_name": "Samsung", "temperature": {"current": 40},
              "power_on_time": {"hours": 10},
              "nvme_smart_health_information_log": {
                  "percentage_used": 4, "available_spare": 100,
                  "available_spare_threshold": 10, "media_errors": 0, "critical_warning": 0}}

ARC = "header\nname type data\nhits 4 900\nmisses 4 100\nsize 4 5\nc_max 4 9\n"

IOSTAT = "tank\t850\t150\t1\t2\t3\t4\n"


def make_runner(missing=()):
    """Canned command outputs keyed on the leading argv; (rc, stdout)."""
    def run(cmd):
        key = " ".join(cmd)
        if cmd[0] in missing:
            return -1, ""
        if key == "zpool status -j --json-int":
            return 0, json.dumps(ZPOOL_STATUS)
        if key.startswith("zpool list -j"):
            return 0, json.dumps(ZPOOL_LIST)
        if key.startswith("zfs list -j -t snapshot"):
            return 0, json.dumps(ZFS_SNAPS)
        if key.startswith("zfs list -j"):
            return 0, json.dumps(ZFS_LIST)
        if key.startswith("zpool get -Hp -o value autotrim"):
            return 0, "on\n"
        if key.startswith("zpool status -t"):
            return 0, "trimmed at 2026-01-02 03:04:05\n"
        if key.startswith("zpool iostat"):
            return 0, IOSTAT
        if key.startswith("zpool get -H -o property,value all"):
            return 0, "feature@foo\tdisabled\nfeature@bar\tactive\n"
        if key == "zfs version":
            return 0, "zfs-2.4.0\nzfs-kmod-2.4.0\n"
        if cmd[0] == "zpool" and cmd[1] == "events":
            return 1, ""
        if cmd[0] == "smartctl":
            if cmd[-1] == "--version":
                return 0, "smartctl 7.4"
            dev = cmd[-1]
            info = SMART_NVME if "nvme" in dev else SMART_ATA
            return 4, json.dumps(info)  # non-zero rc, valid JSON
        return 1, ""
    return run


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.collector.output_dir = str(tmp_path / "out")
    return c
