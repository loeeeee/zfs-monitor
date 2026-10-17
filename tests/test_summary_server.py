import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from conftest import NOW
from zfs_monitor import config as cfgmod
from zfs_monitor.server import Report, make_handler, metrics_text
from zfs_monitor.summary import compute_summary
from zfs_monitor.config import Thresholds
from zfs_monitor.webpage import findings, fmt_age, render

REPORT = {
    "pools": {"tank": {"health": "DEGRADED", "capacity_percent": 91, "fragmentation_percent": 60,
                       "features_pending": True,
                       "scan": {"state": "FINISHED", "days_since_last": 60},
                       "resilver": {"in_progress": True},
                       "devices": [{"name": "m", "state": "ONLINE", "children": [
                           {"name": "sda", "state": "FAULTED", "read_errors": 1,
                            "write_errors": 0, "checksum_errors": 2}]}]}},
    "smart": [{"temperature_celsius": 70, "percentage_used": 3, "available_spare": 90,
               "critical_warning": 0, "media_errors": 2}],
    "snapshot_groups": [{"age_seconds": 999999, "newest_age_seconds": 10},
                        {"age_seconds": 5, "newest_age_seconds": 300000}],
    "arc": {"lifetime_hit_ratio": 0.9},
    "zed_events": [{"class": "sysevent.fs.zfs.history_event"}, {"class": "ereport.fs.zfs.io"}],
    "notices": ["x"],
}


def test_summary_values():
    s = compute_summary(REPORT, 12.34, False, Thresholds())
    assert s["unhealthy_pools"] == ["tank"] and not s["all_pools_healthy"]
    assert (s["total_read_errors"], s["total_checksum_errors"]) == (1, 2)
    assert s["non_online_devices"] == ["sda"]
    assert s["pool_capacity_warning"] and s["pool_capacity_critical"]
    assert s["hot_drive_count"] == 1 and s["total_media_errors"] == 2
    assert s["any_resilver_in_progress"]
    assert s["stale_snapshot_groups"] == 1
    assert s["datasets_without_recent_snapshot"] == 1
    assert s["scrub_overdue"] and s["fragmentation_warning"]
    assert s["real_zed_events_count"] == 1
    assert s["arc_hit_ratio_ok"] is False
    assert s["collector_age_seconds"] == 12.3


def test_summary_covers_every_device_class_and_pool_diagnosis():
    data = {
        "pools": {"tank": {
            "health": "ONLINE", "error_count": 3, "msgid": "ZFS-8000-8A",
            "scan": {"state": "FINISHED", "errors": 2, "days_since_last": None},
            "trim": {"errors": 1}, "checkpoint": {"state": "EXISTS"},
            "removal": {"in_progress": True}, "raidz_expand": {"in_progress": False},
            "devices": [{"name": "raidz1-0", "state": "ONLINE", "checksum_errors": 4,
                         "children": [{"name": "sda", "state": "ONLINE", "slow_ios": 2}]}],
            "special": [{"name": "mirror-1", "state": "DEGRADED", "children": [
                {"name": "123", "was": "/dev/sdf1", "state": "UNAVAIL", "write_errors": 5}]}],
            "l2arc": [{"name": "nvme0n1", "state": "FAULTED", "read_errors": 1}],
            "spare": [{"name": "sdg", "state": "AVAIL"}, {"name": "sdh", "state": "FAULTED"}],
        }},
        "missing_pools": ["backup"],
        "smart": [{"smart_readable": False}, {"smart_prefail": True},
                  {"smart_selftest_failed": True}],
        "zed_events": [{"class": "ereport.fs.zfs.io"}, {"class": "resource.fs.zfs.statechange"}],
        "zed_running": False, "zfs_version_mismatch": True,
    }
    s = compute_summary(data, 0, False, Thresholds())
    assert s["unhealthy_pools"] == ["backup"] and not s["all_pools_healthy"]
    assert s["missing_pools"] == ["backup"] and s["any_pool_missing"]
    assert s["any_pool_data_errors"] and s["pools_with_data_errors"] == ["tank"]
    assert s["total_pool_data_errors"] == 3
    assert s["pool_status_msgids"] == ["tank:ZFS-8000-8A"]
    assert (s["total_write_errors"], s["total_read_errors"]) == (5, 1)
    assert s["non_online_devices"] == ["/dev/sdf1", "nvme0n1"]
    assert s["unavailable_spares"] == ["sdh"]
    assert s["any_interior_vdev_errors"] and s["total_slow_ios"] == 2
    assert s["any_scan_errors"] and s["pools_scrub_age_unknown"] == ["tank"]
    assert s["total_trim_errors"] == 1 and s["pools_with_checkpoint"] == ["tank"]
    assert s["any_vdev_removal_in_progress"] and not s["any_raidz_expansion_in_progress"]
    assert s["smart_unreadable_count"] == 1
    assert s["any_smart_prefail"] and s["any_smart_selftest_failed"]
    assert s["zed_error_events_count"] == 1
    assert s["zed_running"] is False and s["zfs_version_mismatch"]


def test_thresholds_are_configurable():
    s = compute_summary(REPORT, 0, False, Thresholds(capacity_critical_percent=95))
    assert s["pool_capacity_warning"] and not s["pool_capacity_critical"]


def test_empty_report_does_not_crash():
    s = compute_summary({}, 0, True, Thresholds())
    assert s["all_pools_healthy"] and s["max_temperature_celsius"] is None
    assert s["data_fresh"] is False


def test_metrics_only_numeric():
    text = metrics_text({"a": True, "b": 2, "c": "x", "d": None, "e": [1]})
    assert "zfs_monitor_a 1" in text and "zfs_monitor_b 2" in text
    assert "zfs_monitor_c" not in text and "zfs_monitor_e" not in text


def test_config_rejects_unknown_keys():
    with pytest.raises(ValueError):
        cfgmod.from_dict({"server": {"prot": 1}})
    with pytest.raises(ValueError):
        cfgmod.from_dict({"nope": {}})
    assert cfgmod.from_dict({"server": {"port": 9}}).server.port == 9


@pytest.fixture
def http_server(tmp_path):
    cfg = cfgmod.Config()
    cfg.collector.output_dir = str(tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Report(cfg)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield cfg, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_server_paths(http_server, tmp_path):
    cfg, base = http_server
    assert get(base + "/")[0] == 503  # no file yet
    (tmp_path / "status.json").write_text(json.dumps(REPORT))
    code, body = get(base + "/")
    doc = json.loads(body)
    assert code == 200 and doc["_meta"]["stale"] is False and "summary" in doc
    assert get(base + "/healthz")[0] == 200
    assert "zfs_monitor_unhealthy_pool_count 1" in get(base + "/metrics")[1]
    assert get(base + "/nope")[0] == 404
    (tmp_path / "status.json").write_text("{not json")
    assert get(base + "/")[0] == 500


def test_server_stale_is_503_and_age_not_cached(http_server, tmp_path):
    cfg, base = http_server
    cfg.thresholds.max_stale_seconds = 1
    f = tmp_path / "status.json"
    f.write_text(json.dumps(REPORT))
    a = json.loads(get(base + "/")[1])["_meta"]["file_age_seconds"]
    time.sleep(1.3)
    code, body = get(base + "/")
    assert code == 503 and json.loads(body)["_meta"]["file_age_seconds"] > a


def test_smart_failed_and_pool_count():
    s = compute_summary({"smart": [{"smart_passed": False}, {"smart_passed": None}],
                         "pools": {"a": {}}}, 0, False, Thresholds())
    assert s["any_smart_failed"] and s["pool_count"] == 1
    assert not compute_summary({"smart": [{"smart_passed": None}]}, 0, False,
                               Thresholds())["any_smart_failed"]


def test_ui_page_leads_with_what_is_wrong(http_server, tmp_path):
    cfg, base = http_server
    code, body = get(base + "/ui")
    assert code == 503 and body.startswith("<!doctype html>") and "not found" in body
    (tmp_path / "status.json").write_text(json.dumps(REPORT))
    code, body = get(base + "/ui")
    assert code == 200 and "problem(s) need attention" in body
    assert "Pool tank is DEGRADED." in body
    assert "Device tank/sda: FAULTED, 1 read errors, 2 checksum errors" in body
    assert "Pool tank is 91% full." in body
    assert "Pool tank is resilvering." in body


def test_ui_healthy_report_and_escaping():
    ok = {"pools": {"t": {"health": "ONLINE", "scan": {"days_since_last": 1},
                          "devices": [{"name": "sda", "state": "ONLINE"}]}}}
    assert findings(ok, Thresholds()) == [] and "All OK" in render(ok, Thresholds())
    page = render({"pools": {"<x>": {"health": "FAULTED"}}}, Thresholds())
    assert "<x>" not in page and "Pool &lt;x&gt; is FAULTED." in page
    assert (fmt_age(59), fmt_age(3600), fmt_age(90000)) == ("59s", "1h", "1d 1h")
