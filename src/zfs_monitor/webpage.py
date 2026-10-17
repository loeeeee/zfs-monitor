"""Human-readable HTML view of the report (GET /ui).

Server-rendered, no JavaScript. The page leads with one verdict and a list of
what is wrong, naming the pool/device/drive; the tables below only color the
cells that need attention.
"""

from __future__ import annotations

from html import escape
from typing import Any

from .config import Thresholds
from .summary import DEVICE_GROUPS, SPARE_OK_STATES, ZED_NOISE_CLASS, walk_devices, walk_interior

CRIT, WARN, INFO = "crit", "warn", "info"


def fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    big, small = (f"{s // 3600}h", f"{s % 3600 // 60}m") if s < 86400 else \
        (f"{s // 86400}d", f"{s % 86400 // 3600}h")
    return big if small.startswith("0") else f"{big} {small}"


def _counts(dev: dict) -> str:
    return ", ".join(f"{dev[f'{k}_errors']} {k} errors" for k in ("read", "write", "checksum")
                     if dev.get(f"{k}_errors"))


def findings(doc: dict, t: Thresholds) -> list[tuple[str, str]]:
    """(level, message) for everything worth a human's attention, worst first."""
    out: list[tuple[str, str]] = []
    add = lambda level, msg: out.append((level, msg))  # noqa: E731
    meta = doc.get("_meta", {})
    pools = doc.get("pools", {})

    if meta.get("stale"):
        add(CRIT, f"Report is {fmt_age(meta.get('file_age_seconds'))} old; "
                  "the collector may not be running. Everything below may be out of date.")
    for name in doc.get("missing_pools", []):
        add(CRIT, f"Pool {name} is not imported.")

    for pname, p in pools.items():
        health = p.get("health")
        if health != "ONLINE":
            msg = f"Pool {pname} is {health}."
            if p.get("status_message"):
                msg += f" {p['status_message']}"
            add(CRIT, msg)
        if p.get("error_count"):
            files = p.get("error_files") or []
            msg = f"Pool {pname} has {p['error_count']} permanent data error(s)."
            if files:
                msg += " Damaged: " + ", ".join(files[:5]) + (" …" if len(files) > 5 else "")
            add(CRIT, msg)
        for group in DEVICE_GROUPS:
            tree = p.get(group, [])
            for dev in walk_devices(tree):
                name = f"{pname}/{dev.get('was') or dev.get('name', '?')}"
                state = dev.get("state")
                ok_states = SPARE_OK_STATES if group == "spare" else {"ONLINE"}
                bad_state = state not in ok_states
                errs = _counts(dev)
                if bad_state or errs:
                    head = f"{state}" + (f" ({dev['aux']})" if dev.get("aux") else "")
                    add(CRIT, f"Device {name}: " + ", ".join(
                        x for x in (head if bad_state else "", errs) if x))
                if dev.get("slow_ios"):
                    add(WARN, f"Device {name}: {dev['slow_ios']} slow I/Os.")
            for vdev in walk_interior(tree):
                if _counts(vdev):
                    add(CRIT, f"Vdev {pname}/{vdev.get('name')}: {_counts(vdev)}")
        if (p.get("scan") or {}).get("errors"):
            add(CRIT, f"Pool {pname}: last scrub found {p['scan']['errors']} error(s).")
        cap = p.get("capacity_percent", 0)
        if cap >= t.capacity_critical_percent:
            add(CRIT, f"Pool {pname} is {cap}% full.")
        elif cap >= t.capacity_warning_percent:
            add(WARN, f"Pool {pname} is {cap}% full.")
        days = (p.get("scan") or {}).get("days_since_last")
        if days is None:
            add(WARN, f"Pool {pname} has no completed scrub on record.")
        elif days > t.scrub_overdue_days:
            add(WARN, f"Pool {pname}: last scrub was {days} days ago.")
        if p.get("fragmentation_percent", 0) >= t.fragmentation_warning_percent:
            add(WARN, f"Pool {pname} is {p['fragmentation_percent']}% fragmented.")
        if (p.get("trim") or {}).get("errors"):
            add(WARN, f"Pool {pname}: {p['trim']['errors']} TRIM error(s).")

    for d in doc.get("smart", []):
        # One line per drive, at its worst level.
        issues: list[tuple[str, str]] = []
        if d.get("smart_readable") is False:
            issues.append((WARN, "SMART data unreadable"))
        if d.get("smart_passed") is False:
            issues.append((CRIT, "SMART health check FAILED"))
        if d.get("smart_prefail"):
            attrs = ", ".join(map(str, d.get("failing_attributes") or []))
            issues.append((CRIT, "pre-failure attribute tripped" + (f" ({attrs})" if attrs else "")))
        if d.get("smart_selftest_failed"):
            issues.append((CRIT, "last self-test failed"))
        if d.get("critical_warning"):
            issues.append((CRIT, f"NVMe critical warning {d['critical_warning']}"))
        spare, floor = d.get("available_spare"), d.get("available_spare_threshold")
        if spare is not None and floor is not None and spare <= floor:
            issues.append((CRIT, f"available spare {spare}% (threshold {floor}%)"))
        for key, label in (("media_errors", "media errors"),
                           ("reallocated_sectors", "reallocated sectors"),
                           ("pending_sectors", "pending sectors"),
                           ("offline_uncorrectable", "uncorrectable sectors")):
            if d.get(key):
                issues.append((WARN, f"{d[key]} {label}"))
        temp = d.get("temperature_celsius")
        if temp is not None and temp >= t.hot_drive_celsius:
            issues.append((WARN, f"{temp}°C"))
        if issues:
            name = d.get("device_path", "?") + (f" ({d['model']})" if d.get("model") else "")
            level = CRIT if any(lvl == CRIT for lvl, _ in issues) else WARN
            add(level, f"Drive {name}: " + ", ".join(msg for _, msg in issues))

    for g in doc.get("snapshot_groups", []):
        if g.get("newest_age_seconds", 0) > t.snapshot_stale_seconds:
            add(WARN, f"Dataset {g.get('dataset')}: newest snapshot is "
                      f"{fmt_age(g['newest_age_seconds'])} old.")

    zed_errors = [e for e in doc.get("zed_events", [])
                  if str(e.get("class", "")).startswith("ereport.")]
    if zed_errors:
        add(WARN, f"{len(zed_errors)} ZFS error event(s) recently, see ZED events below.")
    if doc.get("zed_running") is False:
        add(WARN, "ZED (ZFS event daemon) is not running, so fault events go unhandled.")
    if doc.get("zfs_version_mismatch"):
        add(WARN, f"ZFS userland {doc.get('zfs_version')} does not match kernel module "
                  f"{doc.get('zfs_kmod_version')}.")
    ratio = (doc.get("arc") or {}).get("lifetime_hit_ratio")
    if ratio and ratio < t.arc_hit_ratio_min:
        add(WARN, f"ARC hit ratio is {ratio:.1%} (below {t.arc_hit_ratio_min:.0%}).")
    for n in doc.get("notices", []):
        add(WARN, f"Collector: {n}")

    # In-progress work: worth knowing, not a problem.
    for pname, p in pools.items():
        rs = p.get("resilver") or {}
        if rs.get("in_progress"):
            pct = rs.get("progress_percent")
            add(INFO, f"Pool {pname} is resilvering" + (f", {pct}% done." if pct is not None else "."))
        if (p.get("scan") or {}).get("state") == "SCANNING":
            add(INFO, f"Pool {pname} is being scrubbed.")
        if (p.get("removal") or {}).get("in_progress"):
            add(INFO, f"Pool {pname}: vdev removal in progress.")
        if (p.get("raidz_expand") or {}).get("in_progress"):
            add(INFO, f"Pool {pname}: RAIDZ expansion in progress.")
        if p.get("checkpoint"):
            add(INFO, f"Pool {pname} has a checkpoint (blocks freeing space until discarded).")
        if p.get("features_pending"):
            add(INFO, f"Pool {pname} has features not yet enabled (zpool upgrade).")

    order = {CRIT: 0, WARN: 1, INFO: 2}
    return sorted(out, key=lambda f: order[f[0]])  # stable: keeps report order within a level


def _cell(value: Any, level: str | None = None) -> str:
    text = "—" if value is None else escape(str(value))
    return f'<td class="{level}">{text}</td>' if level else f"<td>{text}</td>"


def _table(head: list[str], rows: list[str]) -> str:
    if not rows:
        return ""
    th = "".join(f"<th>{escape(h)}</th>" for h in head)
    return f"<table><tr>{th}</tr>{''.join(rows)}</table>"


def render(doc: dict, t: Thresholds) -> str:
    found = findings(doc, t)
    crit = sum(1 for lvl, _ in found if lvl == CRIT)
    warn = sum(1 for lvl, _ in found if lvl == WARN)
    if crit:
        verdict, vclass = f"{crit} problem(s) need attention", CRIT
    elif warn:
        verdict, vclass = f"OK, {warn} warning(s)", WARN
    else:
        verdict, vclass = "All OK", "ok"
    if warn and crit:
        verdict += f", {warn} warning(s)"

    meta = doc.get("_meta", {})
    host = escape(str(doc.get("hostname", "zfs")))
    sub = (f"{host} · collected {escape(str(doc.get('collection_timestamp', '?')))} "
           f"({fmt_age(meta.get('file_age_seconds'))} ago) · {escape(str(doc.get('zfs_version', '')))}")

    items = "".join(f'<li class="{lvl}">{escape(msg)}</li>' for lvl, msg in found)

    pool_rows = []
    for name, p in doc.get("pools", {}).items():
        cap = p.get("capacity_percent", 0)
        days = (p.get("scan") or {}).get("days_since_last")
        frag = p.get("fragmentation_percent", 0)
        pool_rows.append(
            "<tr>" + _cell(name)
            + _cell(p.get("health"), None if p.get("health") == "ONLINE" else CRIT)
            + _cell(f"{cap}%", CRIT if cap >= t.capacity_critical_percent
                    else WARN if cap >= t.capacity_warning_percent else None)
            + _cell(f"{frag}%", WARN if frag >= t.fragmentation_warning_percent else None)
            + _cell("never" if days is None else f"{days}d ago",
                    WARN if days is None or days > t.scrub_overdue_days else None)
            + _cell(p.get("error_count", 0), CRIT if p.get("error_count") else None)
            + "</tr>")
    for name in doc.get("missing_pools", []):
        pool_rows.append("<tr>" + _cell(name) + _cell("NOT IMPORTED", CRIT) + "<td></td>" * 4 + "</tr>")

    drive_rows = []
    for d in doc.get("smart", []):
        temp = d.get("temperature_celsius")
        passed = d.get("smart_passed")
        drive_rows.append(
            "<tr>" + _cell(d.get("device_path")) + _cell(d.get("model"))
            + _cell("PASSED" if passed else "FAILED" if passed is False else None,
                    CRIT if passed is False or d.get("smart_prefail") else None)
            + _cell(None if temp is None else f"{temp}°C",
                    WARN if temp is not None and temp >= t.hot_drive_celsius else None)
            + _cell(d.get("power_on_hours")) + "</tr>")

    snap_rows = []
    for g in doc.get("snapshot_groups", []):
        newest = g.get("newest_age_seconds")
        snap_rows.append(
            "<tr>" + _cell(g.get("dataset")) + _cell(g.get("count"))
            + _cell(fmt_age(newest), WARN if newest is not None
                    and newest > t.snapshot_stale_seconds else None)
            + _cell(fmt_age(g.get("age_seconds"))) + "</tr>")

    zed_rows = ["<tr>" + _cell(e.get("timestamp")) + _cell(e.get("class"),
                WARN if str(e.get("class", "")).startswith("ereport.") else None)
                + _cell(e.get("message")) + "</tr>"
                for e in doc.get("zed_events", []) if e.get("class") != ZED_NOISE_CLASS]

    sections = [
        ("Pools", _table(["Pool", "Health", "Used", "Fragmentation", "Last scrub", "Data errors"],
                         pool_rows)),
        ("Drives", _table(["Device", "Model", "SMART", "Temp", "Power-on h"], drive_rows)),
        ("Snapshots", _table(["Dataset", "Count", "Newest", "Oldest"], snap_rows)),
        ("ZED events", _table(["Time", "Class", "Detail"], zed_rows)),
    ]
    body = "".join(f"<h2>{h}</h2>{tbl}" for h, tbl in sections if tbl)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>ZFS · {host}</title>
<style>{CSS}</style></head><body>
<header class="{vclass}"><h1>{escape(verdict)}</h1><p>{sub}</p></header>
{f'<ul>{items}</ul>' if items else ''}
{body}
<footer><a href="/status">raw JSON</a> · <a href="/metrics">metrics</a> · refreshes every 60s</footer>
</body></html>
"""


def render_error(message: str, detail: str | None = None) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60"><title>ZFS · error</title>
<style>{CSS}</style></head><body>
<header class="crit"><h1>{escape(message)}</h1><p>{escape(detail or '')}</p></header>
</body></html>
"""


CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--mute:#6e6e73;--line:#e5e5ea;
--ok:#1f7a3a;--ok-bg:#e6f4ea;--warn:#8a5a00;--warn-bg:#fff4d6;
--crit:#b3261e;--crit-bg:#fde7e7;--info:#1a5fb4;--info-bg:#e8f0fb}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececf0;--mute:#9a9aa2;
--line:#2c2c30;--ok:#7bd88f;--ok-bg:#16301d;--warn:#f2c14e;--warn-bg:#352a0e;
--crit:#ff8a80;--crit-bg:#3b1715;--info:#8ab4f8;--info-bg:#15243b}}
body{margin:0 auto;max-width:960px;padding:16px;background:var(--bg);color:var(--fg);
font:15px/1.45 system-ui,sans-serif}
header{border-radius:10px;padding:14px 18px;margin-bottom:12px}
header h1{margin:0;font-size:1.5rem}header p{margin:4px 0 0;color:var(--mute);font-size:.85rem}
header.ok{background:var(--ok-bg);color:var(--ok)}
header.warn{background:var(--warn-bg);color:var(--warn)}
header.crit{background:var(--crit-bg);color:var(--crit)}
ul{list-style:none;padding:0;margin:0 0 8px}
li{padding:7px 12px;margin:4px 0;border-left:4px solid;border-radius:4px}
li.crit{border-color:var(--crit);background:var(--crit-bg)}
li.warn{border-color:var(--warn);background:var(--warn-bg)}
li.info{border-color:var(--info);background:var(--info-bg)}
h2{font-size:1rem;margin:20px 0 6px;color:var(--mute)}
table{border-collapse:collapse;width:100%;display:block;overflow-x:auto}
th,td{text-align:left;padding:5px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-weight:600;color:var(--mute);font-size:.85rem}
td.crit{color:var(--crit);font-weight:600}td.warn{color:var(--warn);font-weight:600}
footer{margin-top:24px;color:var(--mute);font-size:.85rem}a{color:inherit}
"""
