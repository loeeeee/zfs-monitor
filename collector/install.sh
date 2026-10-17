#!/bin/bash
# Install (or remove) the standalone bash collector and its systemd timer.
#
#   ./install.sh [--output-dir DIR] [--uid UID] [--gid GID] [--mode MODE]
#                [--no-chown] [--no-enable] [--dry-run]
#   ./install.sh --uninstall [--purge]
#
# Run as root on the ZFS host, from a checkout (it copies the files next to
# it). Re-running upgrades the script and units in place. The env file is
# only rewritten when a setting flag is given, so local edits survive.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SRC_DIR
readonly BIN=/usr/local/bin/zfs-status-collector.sh
readonly UNIT_DIR=/etc/systemd/system
readonly UNITS=(zfs-status-collector.service zfs-status-collector.timer)
readonly ENV_FILE=/etc/zfs-monitor/collector.env

OUTPUT_DIR=/var/lib/zfs-monitor
OWNER_UID=101000
OWNER_GID=101000
FILE_MODE=0644
WRITE_ENV=false
ENABLE=true
DRY_RUN=false
UNINSTALL=false
PURGE=false

usage() {
    sed -n '2,10s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"
    exit "${1:-0}"
}

die() {
    echo "error: $*" >&2
    exit 1
}

# Print, and run unless --dry-run.
run() {
    echo "+ $*"
    if [ "$DRY_RUN" = false ]; then
        "$@"
    fi
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --output-dir) OUTPUT_DIR="${2:?}"; WRITE_ENV=true; shift ;;
            --uid)        OWNER_UID="${2:?}"; WRITE_ENV=true; shift ;;
            --gid)        OWNER_GID="${2:?}"; WRITE_ENV=true; shift ;;
            --mode)       FILE_MODE="${2:?}"; WRITE_ENV=true; shift ;;
            --no-chown)   OWNER_UID=""; OWNER_GID=""; WRITE_ENV=true ;;
            --no-enable)  ENABLE=false ;;
            --dry-run)    DRY_RUN=true ;;
            --uninstall)  UNINSTALL=true ;;
            --purge)      PURGE=true ;;
            -h|--help)    usage ;;
            *)            echo "unknown option: $1" >&2; usage 2 ;;
        esac
        shift
    done
    if [ -n "$OWNER_UID" ] && ! [[ "$OWNER_UID" =~ ^[0-9]+$ ]]; then
        die "--uid must be numeric"
    fi
    if [ -n "$OWNER_GID" ] && ! [[ "$OWNER_GID" =~ ^[0-9]+$ ]]; then
        die "--gid must be numeric"
    fi
    [[ "$FILE_MODE" =~ ^0?[0-7]{3}$ ]] || die "--mode must be octal, e.g. 0644"
    [[ "$OUTPUT_DIR" = /* ]] || die "--output-dir must be an absolute path"
}

# Missing dependencies are fatal, except in a dry run.
dep_error() {
    if [ "$DRY_RUN" = true ]; then
        echo "warning: $*" >&2
    else
        die "$*"
    fi
}

check_deps() {
    local missing=() cmd
    for cmd in jq flock zpool zfs systemctl; do
        command -v "$cmd" &>/dev/null || missing+=("$cmd")
    done
    [ ${#missing[@]} -eq 0 ] || dep_error "missing required commands: ${missing[*]}"
    if ! zpool status -j &>/dev/null; then
        dep_error "zpool has no JSON output (-j); OpenZFS >= 2.3 is required"
    fi
    command -v smartctl &>/dev/null \
        || echo "note: smartctl not found; SMART data will be skipped" >&2
}

write_env() {
    if [ "$WRITE_ENV" = false ] && [ -e "$ENV_FILE" ]; then
        echo "keeping existing $ENV_FILE"
        return
    fi
    echo "+ write $ENV_FILE"
    [ "$DRY_RUN" = true ] && return
    mkdir -p "$(dirname "$ENV_FILE")"
    cat > "$ENV_FILE" <<EOF
# zfs-status-collector settings (written by install.sh).
# Empty UID/GID leave the files owned by root.
ZFS_MONITOR_OUTPUT_DIR=$OUTPUT_DIR
ZFS_MONITOR_UID=$OWNER_UID
ZFS_MONITOR_GID=$OWNER_GID
ZFS_MONITOR_FILE_MODE=$FILE_MODE
EOF
}

install_collector() {
    check_deps
    run install -m 0755 "$SRC_DIR/zfs-status-collector.sh" "$BIN"
    local u
    for u in "${UNITS[@]}"; do
        run install -m 0644 "$SRC_DIR/$u" "$UNIT_DIR/$u"
    done
    write_env
    run install -d -m 0755 "$OUTPUT_DIR"
    run systemctl daemon-reload
    if [ "$ENABLE" = true ]; then
        run systemctl enable --now zfs-status-collector.timer
        # One run now so a failure shows up here rather than in two minutes.
        run systemctl start zfs-status-collector.service
        if [ "$DRY_RUN" = false ]; then
            echo
            ls -l "$OUTPUT_DIR"
        fi
    fi
    echo
    if [ "$DRY_RUN" = true ]; then
        echo "Dry run: nothing was changed."
    else
        echo "Installed."
    fi
    echo "Logs: journalctl -u zfs-status-collector.service"
    echo "Share $OUTPUT_DIR with the server, e.g. for an LXC:"
    echo "  pct set <vmid> -mp0 $OUTPUT_DIR,mp=/var/lib/zfs-monitor,ro=1"
}

uninstall_collector() {
    run systemctl disable --now zfs-status-collector.timer || true
    local u
    for u in "${UNITS[@]}"; do
        run rm -f "$UNIT_DIR/$u"
    done
    run rm -f "$BIN"
    run systemctl daemon-reload
    if [ "$PURGE" = true ]; then
        # Read the output dir before the env file goes.
        if [ -r "$ENV_FILE" ]; then
            OUTPUT_DIR=$(sed -n 's/^ZFS_MONITOR_OUTPUT_DIR=//p' "$ENV_FILE")
            OUTPUT_DIR=${OUTPUT_DIR:-/var/lib/zfs-monitor}
        fi
        case "$OUTPUT_DIR" in
            /*/*) ;;
            *) die "refusing to remove suspicious output dir '$OUTPUT_DIR'" ;;
        esac
        run rm -f "$ENV_FILE"
        run rm -rf "$OUTPUT_DIR"
    else
        echo "kept $ENV_FILE and the output directory (use --purge to remove)"
    fi
}

main() {
    parse_args "$@"
    if [ "$DRY_RUN" = false ] && [ "$(id -u)" -ne 0 ]; then
        die "must run as root (or pass --dry-run)"
    fi
    if [ "$UNINSTALL" = true ]; then
        uninstall_collector
    else
        install_collector
    fi
}

main "$@"
