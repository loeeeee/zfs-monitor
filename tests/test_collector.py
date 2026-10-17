import copy
import json
import os
from datetime import datetime

import pytest

from conftest import NOW, SMART_ATA, ZPOOL_EVENTS, ZPOOL_STATUS, make_runner
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
    assert tank["devices"][0]["children"][1]["slow_ios"] == 3
    assert tank["l2arc"][0]["name"] == "nvme0n1"
    assert tank["slog"][0]["name"] == "sdc"
    assert tank["spare"][0]["state"] == "AVAIL"
    faulted = tank["special"][0]["children"][1]
    assert (faulted["state"], faulted["aux"], faulted["write_errors"]) == \
        ("FAULTED", "too many errors", 7)
    assert tank["trim"] == {"autotrim_enabled": True,
                            "last_done_timestamp": col._iso(NOW - 86400),
                            "errors": 0, "unsupported_devices": 0}
    assert tank["io"]["write_bytes_per_sec"] == 4
    assert tank["features_pending"] is True
    assert report["arc"]["lifetime_hit_ratio"] == 0.9
    assert report["zfs_version"] == "zfs-2.4.0-1"
    assert report["zfs_kmod_version"] == "zfs-kmod-2.4.0-1"
    assert report["zfs_version_mismatch"] is False
    assert {e["event"] for e in events} == {"scrub_completed", "device_errors", "zed_event"}


def test_pool_diagnosis_and_permanent_errors(cfg):
    tank = collect(cfg).pool_status(ZPOOL_STATUS)["tank"]
    assert tank["error_count"] == 1 and tank["error_files"] == ["/tank/data/file.bin"]
    assert tank["msgid"] == "ZFS-8000-8A"
    assert tank["status_message"].startswith("One or more devices has experienced")
    assert "\n" not in tank["status_message"] and "\t" not in tank["status_message"]
    assert tank["action"] == "Restore the file in question if possible."


def test_missing_device_is_named_by_its_last_path(cfg):
    st = copy.deepcopy(ZPOOL_STATUS)
    mirror = st["pools"]["tank"]["vdevs"]["tank"]["vdevs"]["mirror-0"]["vdevs"]
    mirror["sda"] = {"name": "1234567890", "vdev_type": "disk", "state": "UNAVAIL",
                     "not_present": 1, "was": "/dev/sda1"}
    dev = collect(cfg).pool_status(st)["tank"]["devices"][0]["children"][0]
    assert dev["was"] == "/dev/sda1" and dev["state"] == "UNAVAIL"


def test_active_hot_spare_keeps_the_faulted_disk(cfg):
    st = {"pools": {"p": {"name": "p", "state": "DEGRADED", "vdevs": {"p": {
        "name": "p", "vdev_type": "root", "vdevs": {"spare-0": {
            "name": "spare-0", "vdev_type": "spare", "state": "DEGRADED", "vdevs": {
                "sda": {"name": "sda", "vdev_type": "disk", "state": "FAULTED"},
                "sdg": {"name": "sdg", "vdev_type": "disk", "state": "ONLINE"}}}}}}}}}
    devices = collect(cfg).pool_status(st)["p"]["devices"]
    assert devices[0]["type"] == "spare"
    assert [d["state"] for d in devices[0]["children"]] == ["FAULTED", "ONLINE"]


def test_datasets_and_snapshots(cfg):
    c = collect(cfg)
    ds = c.datasets()[0]
    assert ds["compressratio"] == 1.5 and ds["quota_bytes"] is None
    assert ds["capacity_percent"] == 85 and ds["snapshot_ratio_percent"] == 10
    groups, stats = c.snapshots()
    assert groups[0]["age_seconds"] == 10 * 86400
    assert groups[0]["newest_age_seconds"] == 100
    assert stats == {"total_count": 2, "total_used_bytes": 12}


def test_dataset_whose_snapshots_were_all_pruned_stays_stale(cfg):
    c = collect(cfg)
    c.snapshots({"tank"})
    c.run = make_runner(overrides={"zfs list -j -t snapshot": (0, '{"datasets": {}}')})
    groups, stats = c.snapshots({"tank"})
    assert groups == [{"dataset": "tank", "count": 0, "newest_epoch": NOW - 100,
                       "oldest_epoch": None, "age_seconds": 100,
                       "newest_age_seconds": 100}]
    assert stats["total_count"] == 0
    # A destroyed dataset is forgotten; an unknown dataset list changes nothing.
    assert c.snapshots(None)[0] == []
    assert "tank" in c.state["snapshot_newest"]
    assert c.snapshots(set())[0] == [] and c.state["snapshot_newest"] == {}


def test_smart_reaches_every_device_class_and_keeps_nonzero_exit(cfg):
    c = collect(cfg)
    smart = c.smart(ZPOOL_STATUS)
    by = {d["device_path"]: d for d in smart}
    assert set(by) == {f"/dev/sd{x}" for x in "abcefg"} | {"/dev/nvme0n1"}
    assert by["/dev/sda"]["reallocated_sectors"] == 3
    assert by["/dev/sda"]["power_on_hours"] == 4000
    assert by["/dev/sda"]["smart_passed"] is True and by["/dev/sda"]["smart_readable"]
    assert by["/dev/sda"]["smartctl_exit_status"] == 4
    assert by["/dev/nvme0n1"]["protocol"] == "NVMe"


def test_smartctl_asks_for_the_health_verdict_and_device_args(cfg):
    cfg.collector.smart_device_args = {"/dev/sda": ["-d", "sat"]}
    seen = []
    base = make_runner()

    def run(cmd):
        seen.append(cmd)
        return base(cmd)

    col.Collector(cfg, run, now=lambda: NOW).smart(ZPOOL_STATUS)
    sda = next(c for c in seen if c[-1] == "/dev/sda")
    assert "-H" in sda and sda[-3:] == ["-d", "sat", "/dev/sda"]
    assert sda[sda.index("-l") + 1] == "selftest"


def test_unreadable_disk_is_kept_not_dropped(cfg):
    c = collect(cfg, overrides={"smartctl -H -i -A -l selftest -l error --json /dev/sdb": (
        2, json.dumps({"smartctl": {"exit_status": 2}}))})
    by = {d["device_path"]: d for d in c.smart(ZPOOL_STATUS)}
    assert by["/dev/sdb"]["smart_readable"] is False
    assert by["/dev/sdb"]["smartctl_exit_status"] == 2
    assert any("/dev/sdb" in n for n in c.notices)


def test_smart_failure_bits_attributes_and_selftests():
    failing = copy.deepcopy(SMART_ATA)
    failing["smartctl"]["exit_status"] = 0x08 | 0x10 | 0x80
    failing["smart_status"]["passed"] = False
    failing["ata_smart_attributes"]["table"][0]["when_failed"] = "now"
    e = col.Collector.smart_entry("/dev/sda", failing)
    assert e["smart_passed"] is False and e["smart_prefail"] and e["smart_selftest_failed"]
    assert e["failing_attributes"] == ["Reallocated_Sector_Ct"]

    nvme = col.Collector.smart_entry("/dev/nvme0", {
        "nvme_smart_health_information_log": {"critical_warning": 0},
        "nvme_self_test_log": {"table": [{"self_test_result": {"value": 7}}]}})
    assert nvme["smart_selftest_failed"]

    scsi = col.Collector.smart_entry("/dev/sdd", {
        "device": {"protocol": "SCSI"}, "scsi_grown_defect_list": 12,
        "scsi_error_counter_log": {"read": {"total_uncorrected_errors": 2},
                                   "write": {"total_uncorrected_errors": 1}}})
    assert (scsi["grown_defects"], scsi["reallocated_sectors"], scsi["media_errors"]) == (12, 12, 3)


def test_missing_smartctl_is_a_notice(cfg):
    c = collect(cfg, missing=("smartctl",))
    assert c.smart({"pools": {}}) == []
    assert any("smartctl" in n for n in c.notices)


def test_zed_events_parsed_filtered_and_journaled_once(cfg, tmp_path):
    boot = tmp_path / "boot_id"
    boot.write_text("boot-a\n")
    c = collect(cfg)
    events = c.zed_events(str(boot))
    assert [e["class"] for e in events] == ["ereport.fs.zfs.checksum",
                                            "resource.fs.zfs.statechange"]
    assert events[0]["pool"] == "tank" and events[0]["vdev_path"] == "/dev/sdb1"
    assert events[0]["eid"] == 2 and events[0]["timestamp"] == col._iso(0x6b49d101)
    assert events[1]["vdev_state"] == "FAULTED"
    assert events[1]["message"] == "tank /dev/sdf1 FAULTED"
    assert [e["eid"] for e in c.zed_new] == [2, 3]

    c.zed_events(str(boot))
    assert c.zed_new == []  # same boot, nothing new
    boot.write_text("boot-b\n")
    c.zed_events(str(boot))
    assert [e["eid"] for e in c.zed_new] == [2, 3]  # ids restart after reboot


def test_zed_filter_runs_before_the_limit(cfg):
    cfg.collector.zed_event_limit = 1
    noise = "".join(
        f"Jan 15 2027 09:00:0{i}.0\tsysevent.fs.zfs.history_event\n        eid = 0x{10 + i:x}\n"
        for i in range(5))
    c = collect(cfg, overrides={"zpool events -vH": (0, ZPOOL_EVENTS + noise)})
    assert [e["class"] for e in c.zed_events()] == ["resource.fs.zfs.statechange"]


def test_zed_running_reads_proc(cfg, tmp_path):
    c = collect(cfg)
    (tmp_path / "12").mkdir()
    (tmp_path / "12" / "comm").write_text("zed\n")
    (tmp_path / "self").mkdir()
    c.proc = str(tmp_path)
    assert c.zed_running() is True
    (tmp_path / "12" / "comm").write_text("bash\n")
    assert c.zed_running() is False
    c.proc = str(tmp_path / "nope")
    assert c.zed_running() is None


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
    c = collect(cfg)
    first = c.journal_events(ZPOOL_STATUS)
    assert {e["event"] for e in first} == {"scrub_completed", "device_errors"}
    # Errors on the special vdev are journaled too.
    assert {e["device"] for e in first if e["event"] == "device_errors"} == \
        {"/dev/sdb", "/dev/sdf"}
    assert c.journal_events(ZPOOL_STATUS) == []  # same scrub, same errors
    worse = copy.deepcopy(ZPOOL_STATUS)
    worse["pools"]["tank"]["vdevs"]["tank"]["vdevs"]["mirror-0"]["vdevs"]["sdb"]["checksum_errors"] = 5
    assert [e["event"] for e in c.journal_events(worse)] == ["device_errors"]


def test_resilver_and_cancel_are_not_logged_as_scrubs(cfg):
    c = collect(cfg)
    st = copy.deepcopy(ZPOOL_STATUS)
    st["pools"]["tank"]["scan_stats"].update(function="RESILVER", end_time=NOW - 10)
    assert "resilver_completed" in [e["event"] for e in c.journal_events(st)]
    st["pools"]["tank"]["scan_stats"].update(function="SCRUB", state="CANCELED", end_time=NOW - 5)
    assert "scrub_canceled" in [e["event"] for e in c.journal_events(st)]


def test_state_persists_between_runs(cfg):
    col.run_once(cfg, make_runner())
    lines1 = open(os.path.join(cfg.collector.output_dir, "events.jsonl")).read().splitlines()
    col.run_once(cfg, make_runner())
    lines2 = open(os.path.join(cfg.collector.output_dir, "events.jsonl")).read().splitlines()
    # scrub + 2 devices with errors + 2 ZED events, once.
    assert len(lines1) == 5 and lines2 == lines1


def test_total_failure_does_not_overwrite_report(cfg):
    col.run_once(cfg, make_runner())
    good = open(os.path.join(cfg.collector.output_dir, "status.json")).read()
    with pytest.raises(col.CollectionError):
        col.run_once(cfg, lambda cmd: (1, ""))
    assert open(os.path.join(cfg.collector.output_dir, "status.json")).read() == good


def test_failed_zpool_status_fails_the_run_even_if_list_works(cfg):
    with pytest.raises(col.CollectionError):
        col.run_once(cfg, make_runner(overrides={"zpool status -j": (-1, "")}), write=False)


def test_missing_pools_and_version_mismatch(cfg):
    cfg.collector.expected_pools = ["tank", "backup"]
    c = collect(cfg, overrides={"zfs version": (0, "zfs-2.4.0-1\nzfs-kmod-2.3.4-1\n")})
    report, _ = c.collect()
    assert report["missing_pools"] == ["backup"]
    assert report["zfs_version_mismatch"] is True


def test_zpool_real_scan_stats_layout_and_resilver(cfg):
    st = {"pools": {"p": {"name": "p", "state": "ONLINE", "vdevs": {"p": {"vdevs": {}}},
          "scan_stats": {"function": "RESILVER", "state": "SCANNING",
                         "examined": 25, "to_examine": 100, "errors": 0}}}}
    out = collect(cfg).pool_status(st)["p"]
    assert out["scan"]["state"] == "SCANNING" and out["scan"]["function"] == "RESILVER"
    assert out["resilver"]["in_progress"] and out["resilver"]["progress_percent"] == 25.0


def test_sequential_rebuild_counts_as_resilver(cfg):
    st = {"pools": {"p": {"name": "p", "state": "DEGRADED", "vdevs": {"p": {}},
          "scan_stats": {"function": "SCRUB", "state": "FINISHED", "end_time": NOW,
                         "rebuild_stats": {"mirror-0": {"state": "ACTIVE",
                                                        "scanned": 10, "to_scan": 40}}}}}}
    out = collect(cfg).pool_status(st)["p"]
    assert out["resilver"] == {"in_progress": True, "progress_percent": 25.0,
                               "eta_hours": None}


def test_scrub_age_ignores_resilvers_and_running_scrubs(cfg):
    c = collect(cfg)
    st = copy.deepcopy(ZPOOL_STATUS)
    assert c.pool_status(st)["tank"]["scan"]["days_since_last"] == 3
    # A later resilver, then a scrub in progress (end_time 0), keep the age
    # of the last completed scrub.
    st["pools"]["tank"]["scan_stats"].update(function="RESILVER", end_time=NOW - 60)
    assert c.pool_status(st)["tank"]["scan"]["days_since_last"] == 3
    st["pools"]["tank"]["scan_stats"].update(function="SCRUB", state="SCANNING", end_time=0)
    assert c.pool_status(st)["tank"]["scan"]["days_since_last"] == 3
    # No completed scrub on record: unknown, not 20 000 days.
    fresh = collect(cfg)
    assert fresh.pool_status(st)["tank"]["scan"]["days_since_last"] is None


def test_scrub_age_falls_back_to_pool_history(cfg):
    def at(days):
        return datetime.fromtimestamp(NOW - days * 86400).strftime("%Y-%m-%d.%H:%M:%S")
    history = "\n".join([
        "History for 'tank':",
        f"{at(40)} zpool create tank mirror sda sdb",
        f"{at(30)} [txg:10] scan setup func=1 mintxg=0 maxtxg=10",
        f"{at(29)} [txg:20] scan done errors=0",
        f"{at(9)} zpool scrub tank",
        f"{at(9)} [txg:30] scan setup func=1 mintxg=0 maxtxg=30",
        f"{at(8)} [txg:40] scan done errors=0",
        # Later: a cancelled scrub, an error scrub and a resilver.
        f"{at(6)} [txg:50] scan setup func=1 mintxg=0 maxtxg=50",
        f"{at(6)} [txg:51] scan cancelled errors=0",
        f"{at(5)} [txg:60] error scrub setup func=3 mintxg=0 maxtxg=60",
        f"{at(5)} [txg:61] error scrub done errors=0",
        f"{at(2)} [txg:70] scan setup func=2 mintxg=3 maxtxg=70",
        f"{at(1)} [txg:80] scan done errors=0",
    ])
    st = copy.deepcopy(ZPOOL_STATUS)
    st["pools"]["tank"]["scan_stats"].update(function="RESILVER", end_time=NOW - 86400)
    c = collect(cfg, overrides={"zpool history -i tank": (0, history)})
    assert c.pool_status(st)["tank"]["scan"]["days_since_last"] == 8
    assert c.state["last_scrub_end"]["tank"] == int(NOW - 8 * 86400)


def test_removal_expansion_and_checkpoint(cfg):
    st = {"pools": {"p": {"name": "p", "state": "ONLINE", "vdevs": {"p": {}},
          "removal_stats": {"name": "sdx", "state": "SCANNING"},
          "raidz_expand_stats": {"name": "raidz1-0", "state": "FINISHED"},
          "checkpoint_stats": {"state": "EXISTS", "space": 4096}}}}
    out = collect(cfg).pool_status(st)["p"]
    assert out["removal"] == {"vdev": "sdx", "state": "SCANNING", "in_progress": True}
    assert out["raidz_expand"]["in_progress"] is False
    assert out["checkpoint"] == {"state": "EXISTS", "space_bytes": 4096}


def test_parse_events_values():
    ev = col.parse_events(ZPOOL_EVENTS)
    assert [e["fields"]["eid"] for e in ev] == [1, 2, 3]
    assert ev[1]["fields"]["pool"] == "tank"  # nested detector.pool ignored
    assert ev[1]["fields"]["time"] == [0x6b49d101, 0]


def test_smart_verdict_scsi_and_partition_resolution(cfg):
    ata = col.Collector.smart_entry("/dev/sdc", {"smart_status": {"passed": False}})
    assert ata["smart_passed"] is False
    scsi = col.Collector.smart_entry("/dev/sdd", {"device": {"protocol": "SCSI"},
                                                  "temperature": {"current": 33}})
    assert scsi["protocol"] == "SCSI" and scsi["temperature_celsius"] == 33
    assert col.Collector.base_disk("/nonexistent/sdz") == "/nonexistent/sdz"
