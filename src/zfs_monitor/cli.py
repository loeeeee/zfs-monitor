"""zfs-monitor command line: collect | serve | summary | print-config."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys

from . import __version__, config as cfgmod
from .collector import CollectionError, run_once
from .server import Report, serve
from .summary import compute_summary


def _collect(cfg: cfgmod.Config, args: argparse.Namespace) -> int:
    try:
        report, _ = run_once(cfg, write=not args.stdout)
    except BlockingIOError:
        print("[warn] another collector is running; exiting", file=sys.stderr)
        return 0
    except CollectionError as e:
        print(f"collection failed: {e}", file=sys.stderr)
        return 1
    if args.stdout:
        json.dump(report, sys.stdout, indent=2)
        print()
        return 0
    print(f"ZFS collector — {report['collection_duration_ms']}ms  "
          f"pools: {len(report['pools'])}  datasets: {len(report['datasets'])}  "
          f"snapshots: {report['snapshot_stats']['total_count']}  "
          f"smart devices: {len(report['smart'])}")
    for n in report["notices"]:
        print(f"  - {n}")
    return 0


def _summary(cfg: cfgmod.Config, _: argparse.Namespace) -> int:
    """Print the summary of the current report; exit 1 if stale or unhealthy."""
    doc, _, stale = Report(cfg).load()
    json.dump(doc["summary"], sys.stdout, indent=2)
    print()
    s = doc["summary"]
    return 0 if not stale and s["all_pools_healthy"] and not s["any_device_errors"] else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="zfs-monitor", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-c", "--config", help="JSON config file (default: built-in defaults)")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="write one report (needs zpool/zfs access)")
    c.add_argument("--stdout", action="store_true", help="print the report, write nothing")
    sub.add_parser("serve", help="serve the report over HTTP")
    sub.add_parser("summary", help="print the summary; non-zero exit if unhealthy/stale")
    sub.add_parser("print-config", help="print the effective configuration")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = cfgmod.load(args.config)
    except (OSError, ValueError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if args.cmd == "collect":
        return _collect(cfg, args)
    if args.cmd == "serve":
        serve(cfg)
        return 0
    if args.cmd == "summary":
        return _summary(cfg, args)
    json.dump(dataclasses.asdict(cfg), sys.stdout, indent=2)
    print()
    return 0
