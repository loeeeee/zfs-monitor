#!/bin/bash
# ZFS Status Collector — collects comprehensive ZFS monitoring data and
# writes it as structured JSON to /var/lib/zfs-monitor/status.json.
#
# Install on the ZFS host:
#   cp zfs-status-collector.sh /usr/local/bin/
#   chmod 755 /usr/local/bin/zfs-status-collector.sh
#   cp zfs-status-collector.service /etc/systemd/system/
#   cp zfs-status-collector.timer /etc/systemd/system/
#   systemctl daemon-reload
#   systemctl enable --now zfs-status-collector.timer
#
# Requires: bash, jq, flock, zpool/zfs (OpenZFS >= 2.3 for JSON output);
# smartctl optional.
#
# Environment overrides (e.g. via a systemd drop-in):
#   ZFS_MONITOR_OUTPUT_DIR  output directory   (default /var/lib/zfs-monitor)
#   ZFS_MONITOR_UID         owner of the files (default 101000; empty: leave as is)
#   ZFS_MONITOR_GID         group of the files (default 101000; empty: leave as is)
#   ZFS_MONITOR_FILE_MODE   mode of the files  (default 0644)
# 101000 is uid/gid 1000 inside an unprivileged Proxmox LXC, so a server
# running there as a normal user can read a bind-mounted output dir.

set -euo pipefail

readonly OUTPUT_DIR="${ZFS_MONITOR_OUTPUT_DIR:-/var/lib/zfs-monitor}"
readonly OUTPUT_FILE="${OUTPUT_DIR}/status.json"
readonly EVENTS_FILE="${OUTPUT_DIR}/events.jsonl"
readonly LOCK_FILE="${OUTPUT_DIR}/.collector.lock"
readonly LOCK_FD=9
readonly ZFS_USER="${ZFS_MONITOR_UID-101000}"
readonly ZFS_GROUP="${ZFS_MONITOR_GID-101000}"
readonly FILE_MODE="${ZFS_MONITOR_FILE_MODE:-0644}"

SCRIPT_START_EPOCH=$(date +%s%3N)
NOTICES=()
HAS_ZPOOL_STATUS_JSON=false
HAS_SMARTCTL=false

_init_ts() {
    local now
    now=$(date +%s%3N)
    echo $((now - SCRIPT_START_EPOCH))
}

# Hand an output file to the reader: mode, then owner (empty uid/gid = keep).
_publish_perms() {
    chmod "$FILE_MODE" "$1"
    if [ -n "$ZFS_USER" ] || [ -n "$ZFS_GROUP" ]; then
        chown "${ZFS_USER}:${ZFS_GROUP}" "$1"
    fi
}

_notice() {
    NOTICES+=("$1")
    echo "[notice] $1" >&2
}

# --- Init ---

init() {
    mkdir -p "$OUTPUT_DIR"

    if ! command -v jq &>/dev/null; then
        echo "FATAL: jq is required but not installed" >&2
        exit 1
    fi

    if zpool status -j &>/dev/null; then
        HAS_ZPOOL_STATUS_JSON=true
    fi

    if command -v smartctl &>/dev/null; then
        HAS_SMARTCTL=true
    fi
}

# --- Pool basic info ---

collect_pool_list_json() {
    local raw
    if ! raw=$(zpool list -j -p --json-int -o name,size,alloc,free,frag,cap,health 2>/dev/null); then
        _notice "zpool list -j failed"
        echo '{}'
        return
    fi
    echo "$raw" | jq '
        .pools | with_entries(
            .value.properties as $p |
            .value = {
                health: ($p.health.value // "UNKNOWN"),
                state: null,
                status_message: null,
                size_bytes: ($p.size.value // 0),
                allocated_bytes: ($p.allocated.value // 0),
                free_bytes: ($p.free.value // 0),
                fragmentation_percent: ($p.fragmentation.value // 0),
                capacity_percent: ($p.capacity.value // 0),
                scan: null,
                resilver: {in_progress: false, progress_percent: null, eta_hours: null},
                devices: [],
                l2arc: [],
                slog: [],
                spare: [],
                trim: {autotrim_enabled: false, last_done_timestamp: null},
                features_pending: false,
                io: {read_ops_per_sec: 0, write_ops_per_sec: 0, read_bytes_per_sec: 0, write_bytes_per_sec: 0}
            }
        )
    '
}

# --- Pool status, scan, resilver, devices ---

collect_pool_status_json() {
    if [ "$HAS_ZPOOL_STATUS_JSON" != true ]; then
        echo '{}'
        return
    fi

    local raw="${1:-}"
    if [ -z "$raw" ]; then
        if ! raw=$(zpool status -j --json-int 2>/dev/null); then
            _notice "zpool status -j failed"
            echo '{}'
            return
        fi
    fi

    echo "$raw" | jq '
        def walk_device:
            { name: ((.name // .path // .devid // "unknown") | tostring),
              type: (.vdev_type // "unknown"),
              state: (.state // "UNKNOWN"),
              read_errors: (.read_errors // 0),
              write_errors: (.write_errors // 0),
              checksum_errors: (.checksum_errors // 0),
              children: ([(.vdevs // {}) | to_entries[] | .value | walk_device])
            };

        def is_data_vdev($t):
            $t == "disk" or $t == "file" or $t == "mirror" or
            $t == "raidz" or $t == "raidz1" or $t == "raidz2" or $t == "raidz3" or
            $t == "draid" or $t == "replacing";

        def child_vdevs($parent):
            ($parent.vdevs // {}) | to_entries[] | .value;

        [.pools | to_entries[] | .value as $pool |
            ($pool.vdevs[$pool.name] // {}) as $root_vdev |
            {
                key: $pool.name,
                value: {
                    state: ($pool.state // "UNKNOWN"),
                    status_message: ($pool.comment // null),
                    scan: (if $pool.scan then {
                        state: ($pool.scan.state // null),
                        errors: (($pool.scan.scan_stats // {}).errors // 0),
                        repaired_bytes: (($pool.scan.scan_stats // {}).bytes_repaired // 0),
                        days_since_last: (if $pool.scan.end_time then
                            (if ($pool.scan.end_time | type) == "number" then
                                ((now - $pool.scan.end_time) / 86400 | floor)
                            else
                                (((now - ($pool.scan.end_time | sub("  "; " "; "g") | strptime("%a %b %d %H:%M:%S %Y") | mktime)) / 86400) | floor)
                            end)
                        else null end)
                    } else null end),
                    resilver: (if $pool.resilver then {
                        in_progress: (($pool.resilver.state // "none") == "active" or ($pool.resilver.state // "none") == "in_progress"),
                        progress_percent: ($pool.resilver.percent // null),
                        eta_hours: (if $pool.resilver.eta then ($pool.resilver.eta / 3600) | floor else null end)
                    } else {in_progress: false, progress_percent: null, eta_hours: null} end),
                    devices: (
                        [child_vdevs($root_vdev) | select(is_data_vdev(.vdev_type // "")) | walk_device]
                    ),
                    l2arc: (
                        [child_vdevs($root_vdev) | select((.vdev_type // "") == "l2cache") |
                            (child_vdevs(.) | walk_device) // []]
                    ),
                    slog: (
                        [child_vdevs($root_vdev) | select((.vdev_type // "") == "log") |
                            (child_vdevs(.) | walk_device) // []]
                    ),
                    spare: (
                        [child_vdevs($root_vdev) | select((.vdev_type // "") == "spare") |
                            (child_vdevs(.) | walk_device) // []]
                    )
                }
            }
        ] | from_entries
    '
}

# --- TRIM ---

collect_trim_json() {
    local pool_names
    pool_names=$(zpool list -H -o name 2>/dev/null || echo "")

    local result="{}"
    for pool in $pool_names; do
        [ -z "$pool" ] && continue
        local autotrim autotrim_enabled last_done_ts
        autotrim=$(zpool get -Hp -o value autotrim "$pool" 2>/dev/null || echo "off")
        if [ "$autotrim" = "on" ]; then
            autotrim_enabled=true
        else
            autotrim_enabled=false
        fi

        last_done_ts=null
        if zpool status -t "$pool" &>/dev/null 2>&1; then
            local ts
            ts=$(zpool status -t "$pool" 2>/dev/null \
                | grep -oP 'trimmed at \K\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}' \
                | sort -u | tail -1 || true)
            if [ -n "$ts" ]; then
                last_done_ts=$(printf '"%s"' "$ts")
            fi
        fi

        result=$(echo "$result" | jq --arg pool "$pool" --argjson enabled "$autotrim_enabled" --argjson ts "$last_done_ts" \
            '.[$pool].trim = {autotrim_enabled: $enabled, last_done_timestamp: $ts}')
    done
    echo "$result"
}

# --- IO stats (zpool iostat) ---

collect_iostat_json() {
    local raw
    if ! raw=$(zpool iostat -Hp 1 2 2>/dev/null); then
        _notice "zpool iostat failed"
        echo '{}'
        return
    fi

    local n_pools
    n_pools=$(zpool list -H -o name 2>/dev/null | wc -l)
    [ "$n_pools" -gt 0 ] || { echo '{}'; return; }

    # Take only the second sample (delta over 1s)
    local sample
    sample=$(echo "$raw" | tail -n "$n_pools")

    # Parse: capacity_alloc capacity_free read_ops write_ops read_bytes write_bytes
    local result="{}"
    while IFS=$'\t' read -r pool_name _ _ read_ops write_ops read_bytes write_bytes; do
        [ -z "$pool_name" ] && continue
        result=$(echo "$result" | jq \
            --arg pool "$pool_name" \
            --argjson r_ops "${read_ops:-0}" \
            --argjson w_ops "${write_ops:-0}" \
            --argjson r_bytes "${read_bytes:-0}" \
            --argjson w_bytes "${write_bytes:-0}" \
            '.[$pool].io = {
                read_ops_per_sec: $r_ops,
                write_ops_per_sec: $w_ops,
                read_bytes_per_sec: $r_bytes,
                write_bytes_per_sec: $w_bytes
            }')
    done <<< "$sample"

    echo "$result"
}

# --- Datasets ---

collect_datasets_json() {
    local raw
    if ! raw=$(zfs list -j -p --json-int -o name,used,available,referenced,quota,reservation,compressratio,usedbydataset,usedbysnapshots 2>/dev/null); then
        _notice "zfs list -j failed"
        echo '[]'
        return
    fi
    echo "$raw" | jq '
        [.datasets | to_entries[] |
            .value.properties as $p |
            (.value.name) as $n |
            {
                name: $n,
                pool: ($n | split("/")[0]),
                used_bytes: ($p.used.value // 0),
                available_bytes: ($p.available.value // 0),
                referenced_bytes: ($p.referenced.value // 0),
                quota_bytes: (if ($p.quota.value // 0) == 0 then null else $p.quota.value end),
                reservation_bytes: ($p.reservation.value // 0),
                compressratio: ($p.compressratio.value | tonumber? // 1),
                capacity_percent:
                    (if ($p.used.value // 0) + ($p.available.value // 0) > 0
                     then (($p.used.value / ($p.used.value + $p.available.value) * 100) | floor)
                     else null end),
                snapshot_ratio_percent:
                    (if ($p.used.value // 0) > 0 and ($p.usedbysnapshots.value // null) != null
                     then (($p.usedbysnapshots.value / $p.used.value * 100) | floor)
                     else null end)
            }
        ]
    '
}

# --- Snapshots, grouped by dataset ---

collect_snapshots_json() {
    local raw
    if ! raw=$(zfs list -j -t snapshot -p --json-int -o name,creation,used 2>/dev/null); then
        _notice "zfs list -j -t snapshot failed"
        echo '{ "groups": [], "stats": { "total_count": 0, "total_used_bytes": 0 } }'
        return
    fi

    echo "$raw" | jq '
        def dataset_of($name): $name | sub("@[^@]+$"; "");

        [.datasets | to_entries[] |
            .value as $v | .value.properties as $p |
            {
                dataset: dataset_of($v.name),
                name: $v.name,
                creation: ($p.creation.value | tonumber? // 0),
                used: ($p.used.value | tonumber? // 0)
            }
        ] as $flat
        | {
            groups: ($flat | group_by(.dataset) | map({
                dataset: .[0].dataset,
                count: length,
                newest_epoch: (max_by(.creation).creation),
                oldest_epoch: (min_by(.creation).creation),
                age_seconds: (now - (min_by(.creation).creation) | floor)
            })),
            stats: {
                total_count: ($flat | length),
                total_used_bytes: ($flat | map(.used) | add // 0)
            }
        }
    ' 2>/dev/null || echo '{ "groups": [], "stats": { "total_count": 0, "total_used_bytes": 0 } }'
}

# --- ARC stats ---

collect_arc_json() {
    local arcfile="/proc/spl/kstat/zfs/arcstats"
    if [ ! -f "$arcfile" ]; then
        _notice "$arcfile not found; ARC stats unavailable"
        echo '{ "size_bytes": 0, "c_max_bytes": 0, "hits": 0, "misses": 0, "lifetime_hit_ratio": 0, "l2_hits": 0, "l2_misses": 0, "l2_size_bytes": 0, "l2_hit_ratio": null }'
        return
    fi

    local size c_max hits misses
    size=$(awk '$1=="size"{print $3}' "$arcfile" 2>/dev/null || echo 0)
    c_max=$(awk '$1=="c_max"{print $3}' "$arcfile" 2>/dev/null || echo 0)
    hits=$(awk '$1=="hits"{print $3}' "$arcfile" 2>/dev/null || echo 0)
    misses=$(awk '$1=="misses"{print $3}' "$arcfile" 2>/dev/null || echo 0)

    local total_requests lifetime_hit_ratio
    total_requests=$((hits + misses))
    if [ "$total_requests" -gt 0 ]; then
        lifetime_hit_ratio=$(awk "BEGIN { printf \"%.4f\", $hits / $total_requests }")
    else
        lifetime_hit_ratio=0
    fi

    local l2_hits l2_misses l2_size l2_hit_ratio
    l2_hits=$(awk '$1=="l2_hits"{print $3}' "$arcfile" 2>/dev/null || echo 0)
    l2_misses=$(awk '$1=="l2_misses"{print $3}' "$arcfile" 2>/dev/null || echo 0)
    l2_size=$(awk '$1=="l2_size"{print $3}' "$arcfile" 2>/dev/null || echo 0)

    if [ "$((l2_hits + l2_misses))" -gt 0 ]; then
        l2_hit_ratio=$(awk "BEGIN { printf \"%.4f\", $l2_hits / ($l2_hits + $l2_misses) }")
    else
        l2_hit_ratio=null  # bareword null passed via --argjson; do not quote
    fi

    jq -n \
        --argjson size "$size" \
        --argjson c_max "$c_max" \
        --argjson hits "$hits" \
        --argjson misses "$misses" \
        --argjson lifetime_hit_ratio "$lifetime_hit_ratio" \
        --argjson l2_hits "$l2_hits" \
        --argjson l2_misses "$l2_misses" \
        --argjson l2_size "$l2_size" \
        --argjson l2_hit_ratio "$l2_hit_ratio" \
        '{size_bytes: $size, c_max_bytes: $c_max, hits: $hits, misses: $misses, lifetime_hit_ratio: $lifetime_hit_ratio, l2_hits: $l2_hits, l2_misses: $l2_misses, l2_size_bytes: $l2_size, l2_hit_ratio: $l2_hit_ratio}'
}

# --- SMART data ---

get_device_paths() {
    zpool status -j --json-int 2>/dev/null | jq -r '
        def all_disks:
            if .vdev_type == "disk" then [.]
            elif .vdevs then [.vdevs | to_entries[] | .value | all_disks] | flatten
            else [] end;
        [.pools | to_entries[] | .value | all_disks | .[].path? ]
        | map(select(. != null)) | unique | .[]
    ' 2>/dev/null || zpool status 2>/dev/null | grep -oP '/dev/[^ ]+' | sort -u
}

collect_smart_json() {
    if [ "$HAS_SMARTCTL" != true ]; then
        _notice "smartctl not available; skipping SMART collection"
        echo '[]'
        return
    fi

    local devices result="[]"
    devices=$(get_device_paths)

    if [ -z "$devices" ]; then
        echo '[]'
        return
    fi

    while IFS= read -r dev; do
        [ -z "$dev" ] && continue
        local dev_info
        dev_info=$(smartctl -iA --json "$dev" 2>/dev/null) || {
            _notice "smartctl failed for $dev"
            continue
        }

        local entry
        entry=$(echo "$dev_info" | jq --arg dev "$dev" '
            if .nvme_smart_health_information_log then
                {
                    device_path: $dev,
                    protocol: "NVMe",
                    model: (.model_name // "unknown"),
                    temperature_celsius: (.temperature.current // null),
                    power_on_hours: (.power_on_time.hours // null),
                    percentage_used: (.nvme_smart_health_information_log.percentage_used // null),
                    available_spare: (.nvme_smart_health_information_log.available_spare // null),
                    available_spare_threshold: (.nvme_smart_health_information_log.available_spare_threshold // null),
                    media_errors: (.nvme_smart_health_information_log.media_errors // 0),
                    critical_warning: (.nvme_smart_health_information_log.critical_warning // 0),
                    reallocated_sectors: 0,
                    pending_sectors: 0,
                    offline_uncorrectable: 0,
                    udma_crc_errors: 0,
                    ssd_life_left: null,
                    media_wearout: null
                }
            else
                {
                    device_path: $dev,
                    protocol: "ATA",
                    model: (.model_name // .model_family // "unknown"),
                    temperature_celsius: (
                        .temperature.current //
                        [.ata_smart_attributes.table[]?
                            | select(.id == 194 or .id == 190)
                            | .raw.value
                        ] | first // null
                    ),
                    reallocated_sectors: (
                        [.ata_smart_attributes.table[]?
                            | select(.id == 5)
                            | .raw.value
                        ] | first // 0
                    ) | tonumber,
                    pending_sectors: (
                        [.ata_smart_attributes.table[]?
                            | select(.id == 197)
                            | .raw.value
                        ] | first // 0
                    ) | tonumber,
                    offline_uncorrectable: (
                        [.ata_smart_attributes.table[]?
                            | select(.id == 198)
                            | .raw.value
                        ] | first // 0
                    ) | tonumber,
                    udma_crc_errors: (
                        [.ata_smart_attributes.table[]?
                            | select(.id == 199)
                            | .raw.value
                        ] | first // 0
                    ) | tonumber,
                    power_on_hours: (
                        [.ata_smart_attributes.table[]?
                            | select(.id == 9)
                            | .raw.value
                        ] | first // null
                    ),
                    ssd_life_left: (
                        [.ata_smart_attributes.table[]?
                            | select(.id == 231)
                            | .value
                        ] | first // null
                    ),
                    media_wearout: (
                        [.ata_smart_attributes.table[]?
                            | select(.id == 233)
                            | .value
                        ] | first // null
                    ),
                    percentage_used: null,
                    available_spare: null,
                    available_spare_threshold: null,
                    media_errors: 0,
                    critical_warning: 0
                }
            end
        ' 2>/dev/null) || continue

        result=$(echo "$result" | jq --argjson entry "$entry" '. + [$entry]')
    done <<< "$devices"

    echo "$result"
}

# --- ZED events ---

collect_zed_json() {
    local raw

    # Try zpool events -j first (OpenZFS 2.2+, structured JSON)
    if raw=$(zpool events -j 2>/dev/null) && [ -n "$raw" ]; then
        echo "$raw" | jq -r '
            .events
            | map(select(.class | test("^ereport\\.fs\\.zfs\\.|^sysevent\\.fs\\.zfs\\.(scrub|resilver|vdev|pool)_")))
            | .[-50:][]
            |
            [.time[0],
             (.class // "unknown"),
             (.eid // 0),
             ((.pool? // "" | tostring) + " " + (.vdev_path? // "" | tostring))]
            | @tsv' 2>/dev/null | while IFS=$'\t' read -r epoch_sec class eid message; do
            [ -z "$class" ] && continue
            local ts
            ts=$(date -d "@${epoch_sec}" -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "epoch:${epoch_sec}")
            jq -n \
                --arg ts "$ts" \
                --argjson eid "$eid" \
                --arg class "$class" \
                --arg message "$message" \
                '{timestamp: $ts, eid: $eid, class: $class, message: $message}'
        done | jq -s '.' 2>/dev/null || echo '[]'
        return
    fi

    # Try zpool events -H (tab-separated, no -v so no continuation lines)
    if raw=$(zpool events -H 2>/dev/null | tail -50) && [ -n "$raw" ]; then
        echo "$raw" | while IFS=$'\t' read -r ts_raw class rest; do
            [ -z "$class" ] && continue
            local ts
            ts=$(date -d "${ts_raw}" -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "$ts_raw")
            jq -n \
                --arg ts "$ts" \
                --arg class "$class" \
                --arg message "${rest:-}" \
                '{timestamp: $ts, class: $class, message: $message}'
        done | jq -s '.' 2>/dev/null || echo '[]'
        return
    fi

    # Fallback: parse free-form zed.log
    local zedfile="/var/log/zfs/zed.log"
    if [ ! -f "$zedfile" ]; then
        echo '[]'
        return
    fi

    raw=$(tail -50 "$zedfile" 2>/dev/null) || { echo '[]'; return; }

    echo "$raw" | while IFS= read -r line; do
        [ -z "$line" ] && continue
        local ts class rest
        ts=$(echo "$line" | awk '{print $1, $2, $3}')
        class=$(echo "$line" | awk '{print $5}')
        rest=$(echo "$line" | awk '{for(i=6;i<=NF;i++) printf "%s ", $i; print ""}' | sed 's/ $//')
        [ -z "$class" ] && continue
        jq -n \
            --arg ts "$ts" \
            --arg class "$class" \
            --arg message "$rest" \
            '{timestamp: $ts, class: $class, message: $message}'
    done | jq -s '.' 2>/dev/null || echo '[]'
}

# --- Pool feature flags ---

collect_pool_features_json() {
    local pool_names
    pool_names=$(zpool list -H -o name 2>/dev/null || echo "")
    local result="{}"
    for pool in $pool_names; do
        [ -z "$pool" ] && continue
        local pending
        if zpool get -H -o property,value all "$pool" 2>/dev/null | awk -F'\t' '/^feature@/ && $2 == "disabled" {c++} END {exit !c}'; then
            pending=true
        else
            pending=false
        fi
        result=$(echo "$result" | jq --arg pool "$pool" --argjson pending "$pending" \
            '.[$pool].features_pending = $pending')
    done
    echo "$result"
}

# --- Events journal ---

build_events_json() {
    local status_raw="$1"
    local scan_json="[]"
    local error_json="[]"

    if [ "$HAS_ZPOOL_STATUS_JSON" = true ] && [ -n "$status_raw" ]; then
        scan_json=$(echo "$status_raw" | jq '
            [.pools | to_entries[] | .value |
             select(.scan.state == "finished" or .scan.state == "completed") |
             { pool: .name,
               event: "scrub_completed",
               timestamp: (now | todateiso8601),
               errors: ((.scan.scan_stats // {}).errors // 0),
               repaired_bytes: ((.scan.scan_stats // {}).bytes_repaired // 0)
             }]
        ' 2>/dev/null || echo '[]')

        error_json=$(echo "$status_raw" | jq '
            def find_disk_errors:
                if .vdev_type == "disk" and ((.read_errors? // 0) > 0 or (.write_errors? // 0) > 0 or (.checksum_errors? // 0) > 0) then
                    [{ device: (.path // "unknown"),
                       event: "device_errors",
                       timestamp: (now | todateiso8601),
                       read_errors: (.read_errors // 0),
                       write_errors: (.write_errors // 0),
                       checksum_errors: (.checksum_errors // 0) }]
                elif .vdevs then
                    [.vdevs | to_entries[] | .value | find_disk_errors] | flatten
                else [] end;
            [.pools | to_entries[] | .value | find_disk_errors] | flatten
        ' 2>/dev/null || echo '[]')
    fi

    jq -n --argjson a "$scan_json" --argjson b "$error_json" '$a + $b'
}

# --- Assembly ---

assemble_and_write() {
    local status_raw="$1"

    local pools_json datasets_json snap_json arc_json smart_json zed_json trim_json io_json features_json
    local collection_timestamp collection_duration_ms hostname zfs_version

    collection_timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    hostname=$(hostname 2>/dev/null || echo "")
    hostname="${hostname:-unknown}"
    zfs_version=$(zfs version 2>/dev/null | head -1 || echo "")
    zfs_version="${zfs_version:-unknown}"

    pools_json=$(collect_pool_list_json)
    local status_json
    status_json=$(collect_pool_status_json "$status_raw")

    # Merge status into pools
    local merged_pools
    merged_pools=$(jq -n --argjson base "$pools_json" --argjson status "$status_json" \
        '$base * $status')

    # Merge TRIM into pools
    trim_json=$(collect_trim_json)
    merged_pools=$(jq -n --argjson base "$merged_pools" --argjson trim "$trim_json" \
        'reduce ($trim | keys[]) as $k ($base; .[$k].trim = $trim[$k].trim)')

    # Merge IO stats into pools
    io_json=$(collect_iostat_json)
    merged_pools=$(jq -n --argjson base "$merged_pools" --argjson io "$io_json" \
        'reduce ($io | keys[]) as $k ($base; .[$k].io = $io[$k].io)')

    # Merge feature flags into pools
    features_json=$(collect_pool_features_json)
    merged_pools=$(jq -n --argjson base "$merged_pools" --argjson features "$features_json" \
        'reduce ($features | keys[]) as $k ($base; .[$k].features_pending = $features[$k].features_pending)')

    # Collect remaining sections
    datasets_json=$(collect_datasets_json)
    snap_json=$(collect_snapshots_json)
    arc_json=$(collect_arc_json)
    smart_json=$(collect_smart_json)
    zed_json=$(collect_zed_json)

    # Build final JSON
    local final_json
    collection_duration_ms=$(_init_ts)
    final_json=$(jq -n \
        --arg ts "$collection_timestamp" \
        --argjson duration "$collection_duration_ms" \
        --arg hostname "$hostname" \
        --arg zfs_version "$zfs_version" \
        --argjson pools "$merged_pools" \
        --argjson datasets "$datasets_json" \
        --argjson snapshot_groups "$(echo "$snap_json" | jq '.groups')" \
        --argjson snapshot_stats "$(echo "$snap_json" | jq '.stats')" \
        --argjson arc "$arc_json" \
        --argjson smart "$smart_json" \
        --argjson zed "$zed_json" \
        --argjson notices "$(printf '%s\n' "${NOTICES[@]:-}" | jq -R -s 'split("\n") | map(select(length > 0))')" \
        '{
            hostname: $hostname,
            zfs_version: $zfs_version,
            collection_timestamp: $ts,
            collection_duration_ms: $duration,
            pools: $pools,
            datasets: $datasets,
            snapshot_groups: $snapshot_groups,
            snapshot_stats: $snapshot_stats,
            arc: $arc,
            smart: $smart,
            zed_events: $zed,
            notices: $notices
        }')

    # Atomic write
    local tmpfile
    tmpfile=$(mktemp -p "$OUTPUT_DIR" ".status.json.XXXXXX")
    echo "$final_json" > "$tmpfile"
    _publish_perms "$tmpfile"
    mv "$tmpfile" "$OUTPUT_FILE"
}

# --- Events journal append ---

append_events() {
    local events_json="$1"
    local count
    count=$(echo "$events_json" | jq 'length' 2>/dev/null || echo 0)
    if [ "$count" -gt 0 ]; then
        echo "$events_json" | jq -c '.[]' >> "$EVENTS_FILE"
        _publish_perms "$EVENTS_FILE" 2>/dev/null || true
    fi
}

# --- Summary report ---

print_summary() {
    local duration_ms pools_count ds_count snap_count smart_count
    duration_ms=$(_init_ts)

    pools_count=$(jq '.pools | keys | length' "$OUTPUT_FILE" 2>/dev/null || echo 0)
    ds_count=$(jq '.datasets | length' "$OUTPUT_FILE" 2>/dev/null || echo 0)
    snap_count=$(jq '.snapshot_stats.total_count' "$OUTPUT_FILE" 2>/dev/null || echo 0)
    smart_count=$(jq '.smart | length' "$OUTPUT_FILE" 2>/dev/null || echo 0)

    echo "ZFS Status Collector — ${duration_ms}ms"
    echo "  pools: ${pools_count}  datasets: ${ds_count}  snapshots: ${snap_count}  smart devices: ${smart_count}"
    if [ "${#NOTICES[@]}" -gt 0 ]; then
        echo "  notices (${#NOTICES[@]}):"
        for n in "${NOTICES[@]}"; do
            echo "    - $n"
        done
    fi
}

# --- Main ---

main() {
    # Acquire lock (the output dir holds the lock file, so it must exist first)
    mkdir -p "$OUTPUT_DIR"
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        echo "[warn] Another collector instance is running (lock held on $LOCK_FILE). Exiting." >&2
        exit 0
    fi

    init

    local status_raw
    if [ "$HAS_ZPOOL_STATUS_JSON" = true ]; then
        status_raw=$(zpool status -j --json-int 2>/dev/null) || status_raw="{}"
    else
        status_raw="{}"
    fi

    local events_json
    events_json=$(build_events_json "$status_raw")

    assemble_and_write "$status_raw"

    append_events "$events_json"
    print_summary
}

main "$@"
