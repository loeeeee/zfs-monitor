import json
import os

from conftest import NOW, make_runner
from zfs_monitor import collector as col


def collect(cfg, **kw):
    # arcstats lives in /proc; point at a temp file instead.
    c = col.Collector(cfg, make_runner(**kw), now=lambda: NOW)
    return c


def test_pool_merge_and_devices(cfg, tmp_path):
    c = collect(cfg)
    arc = tmp_path / "arc"
    arc.write_text("h\nn t d\nhits 4 900\nmisses 4 100\nsize 4 5\nc_max 4 9\n")
    c.arc = lambda path=str(arc): col.Collector.arc(c, path)
    report, events = c.collect()
    tank = report["pools"]["tank"]
    assert tank["health"] == "ONLINE" and tank["capacity_percent"] == 85
    assert tank["scan"]["days_since_last"] == 3
    assert tank["devices"][0]["type"] == "mirror"
    assert [d["name"] for d in tank["devices"][0]["children"]] == ["sda", "sdb"]
    assert tank["l2arc"][0]["name"] == "nvme0n1"
    assert tank["trim"] == {"autotrim_enabled": True,
                            "last_done_timestamp": "2026-01-02 03:04:05"}
    assert tank["io"]["write_bytes_per_sec"] == 4
    assert tank["features_pending"] is True
    assert report["arc"]["lifetime_hit_ratio"] == 0.9
    assert report["zfs_version"] == "zfs-2.4.0"
    assert {e["event"] for e in events} == {"scrub_completed", "device_errors"}


def test_datasets_and_snapshots(cfg):
    c = collect(cfg)
    ds = c.datasets()[0]
    assert ds["compressratio"] == 1.5 and ds["quota_bytes"] is None
    assert ds["capacity_percent"] == 85 and ds["snapshot_ratio_percent"] == 10
    groups, stats = c.snapshots()
    assert groups[0]["age_seconds"] == 10 * 86400
    assert groups[0]["newest_age_seconds"] == 100
    assert stats == {"total_count": 2, "total_used_bytes": 12}


def test_smart_keeps_drives_with_nonzero_exit(cfg):
    smart = collect(cfg).smart(__import__("conftest").ZPOOL_STATUS)
    by = {d["device_path"]: d for d in smart}
    assert by["/dev/sda"]["reallocated_sectors"] == 3
    assert by["/dev/sda"]["power_on_hours"] == 4000
    assert by["/dev/nvme0n1"]["protocol"] == "NVMe"


def test_missing_smartctl_is_a_notice(cfg):
    c = collect(cfg, missing=("smartctl",))
    assert c.smart({"pools": {}}) == []
    assert any("smartctl" in n for n in c.notices)


def test_write_report_atomic_and_rotates(cfg):
    cfg.collector.events_max_bytes = 5
    report = {"x": 1}
    path = col.write_report(cfg, report, [{"a": 1}])
    assert json.load(open(path)) == report
    col.write_report(cfg, report, [{"a": 2}])
    out = os.listdir(cfg.collector.output_dir)
    assert "events.jsonl.1" in out and not any(n.startswith(".status.json.") for n in out)


def test_lock_prevents_overlap(cfg):
    import fcntl
    os.makedirs(cfg.collector.output_dir)
    with open(os.path.join(cfg.collector.output_dir, cfg.collector.lock_file), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            col.run_once(cfg, make_runner())
        except BlockingIOError:
            return
    raise AssertionError("expected BlockingIOError")


def test_dry_run_touches_nothing(cfg):
    report, _ = col.run_once(cfg, make_runner(), write=False)
    assert "pools" in report and not os.path.exists(cfg.collector.output_dir)


def test_journal_events_are_emitted_once(cfg):
    from conftest import ZPOOL_STATUS
    c = collect(cfg)
    first = c.journal_events(ZPOOL_STATUS)
    assert {e["event"] for e in first} == {"scrub_completed", "device_errors"}
    assert c.journal_events(ZPOOL_STATUS) == []  # same scrub, same errors
    import copy
    worse = copy.deepcopy(ZPOOL_STATUS)
    worse["pools"]["tank"]["vdevs"]["tank"]["vdevs"]["mirror-0"]["vdevs"]["sdb"]["checksum_errors"] = 5
    assert [e["event"] for e in c.journal_events(worse)] == ["device_errors"]


def test_state_persists_between_runs(cfg):
    col.run_once(cfg, make_runner())
    lines1 = open(os.path.join(cfg.collector.output_dir, "events.jsonl")).read().splitlines()
    col.run_once(cfg, make_runner())
    lines2 = open(os.path.join(cfg.collector.output_dir, "events.jsonl")).read().splitlines()
    assert len(lines1) == 2 and lines2 == lines1


def test_total_failure_does_not_overwrite_report(cfg):
    import pytest
    col.run_once(cfg, make_runner())
    good = open(os.path.join(cfg.collector.output_dir, "status.json")).read()
    with pytest.raises(col.CollectionError):
        col.run_once(cfg, lambda cmd: (1, ""))
    assert open(os.path.join(cfg.collector.output_dir, "status.json")).read() == good


def test_zpool_real_scan_stats_layout_and_resilver(cfg):
    st = {"pools": {"p": {"name": "p", "state": "ONLINE", "vdevs": {"p": {"vdevs": {}}},
          "scan_stats": {"function": "RESILVER", "state": "SCANNING",
                         "examined": 25, "to_examine": 100, "errors": 0}}}}
    out = collect(cfg).pool_status(st)["p"]
    assert out["scan"]["state"] == "SCANNING" and out["scan"]["function"] == "RESILVER"
    assert out["resilver"]["in_progress"] and out["resilver"]["progress_percent"] == 25.0


def test_smart_verdict_scsi_and_partition_resolution(cfg):
    ata = col.Collector.smart_entry("/dev/sdc", {"smart_status": {"passed": False}})
    assert ata["smart_passed"] is False
    scsi = col.Collector.smart_entry("/dev/sdd", {"device": {"protocol": "SCSI"},
                                                  "temperature": {"current": 33}})
    assert scsi["protocol"] == "SCSI" and scsi["temperature_celsius"] == 33
    assert col.Collector.base_disk("/nonexistent/sdz") == "/nonexistent/sdz"
