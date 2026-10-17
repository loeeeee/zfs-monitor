# NixOS module: services.zfs-monitor
#
# Two independent halves, enable either or both:
#   collector — root oneshot on a timer; writes status.json (+ events.jsonl)
#               to collector.outputDir. Runs where ZFS lives.
#   server    — unprivileged HTTP server over that directory. May run in
#               another guest that sees the directory (bind mount).
{
  config,
  lib,
  pkgs,
  ...
}:

let
  inherit (lib)
    mkEnableOption
    mkIf
    mkMerge
    mkOption
    types
    ;
  cfg = config.services.zfs-monitor;
  json = pkgs.formats.json { };

  settings = lib.recursiveUpdate (
    {
      collector = {
        output_dir = cfg.collector.outputDir;
        smart = cfg.collector.smart;
        smart_device_args = cfg.collector.smartDeviceArgs;
        expected_pools = cfg.collector.expectedPools;
        iostat_seconds = cfg.collector.iostatSeconds;
        zed_event_limit = cfg.collector.zedEventLimit;
        command_timeout_seconds = cfg.collector.commandTimeoutSeconds;
        owner_uid = cfg.collector.fileOwner.uid;
        owner_gid = cfg.collector.fileOwner.gid;
      };
      server = {
        listen_address = cfg.server.listenAddress;
        port = cfg.server.port;
      };
      thresholds = {
        max_stale_seconds = cfg.thresholds.maxStaleSeconds;
        snapshot_stale_seconds = cfg.thresholds.snapshotStaleSeconds;
        hot_drive_celsius = cfg.thresholds.hotDriveCelsius;
        capacity_warning_percent = cfg.thresholds.capacityWarningPercent;
        capacity_critical_percent = cfg.thresholds.capacityCriticalPercent;
        arc_hit_ratio_min = cfg.thresholds.arcHitRatioMin;
        scrub_overdue_days = cfg.thresholds.scrubOverdueDays;
        fragmentation_warning_percent = cfg.thresholds.fragmentationWarningPercent;
      };
    }
  ) cfg.extraSettings;

  configFile = json.generate "zfs-monitor.json" settings;
  exe = lib.getExe cfg.package;

  # Server-only hosts skip the zfs closure; collector hosts get the tools of
  # the zfs build this system's kernel module matches.
  defaultPackage = pkgs.callPackage ./package.nix {
    withTools = cfg.collector.enable;
    zfs = config.boot.zfs.package;
  };
in
{
  options.services.zfs-monitor = {
    package = mkOption {
      type = types.package;
      default = defaultPackage;
      defaultText = "zfs-monitor built against boot.zfs.package";
      description = "The zfs-monitor package.";
    };

    collector = {
      enable = mkEnableOption "the periodic ZFS status collector";
      outputDir = mkOption {
        type = types.path;
        default = "/var/lib/zfs-monitor";
        description = ''
          Directory receiving status.json and events.jsonl. The server reads
          from it too, so on a server-only host (e.g. an LXC) set it to where
          the collector's directory is bind-mounted.
        '';
      };
      interval = mkOption {
        type = types.str;
        default = "120s";
        description = "Time between collections (systemd time span).";
      };
      smart = mkOption {
        type = types.bool;
        default = true;
        description = "Collect SMART data with smartctl.";
      };
      smartDeviceArgs = mkOption {
        type = types.attrsOf (types.listOf types.str);
        default = { };
        example = {
          "/dev/sdc" = [ "-d" "sat" ];
        };
        description = "Extra smartctl arguments per whole-disk path (USB bridges, RAID controllers).";
      };
      expectedPools = mkOption {
        type = types.listOf types.str;
        default = [ ];
        example = [ "tank" ];
        description = ''
          Pools that must be imported. One that is not is reported in
          `missing_pools` and makes `all_pools_healthy` false.
        '';
      };
      iostatSeconds = mkOption {
        type = types.ints.unsigned;
        default = 1;
        description = "Seconds of zpool iostat sampling; 0 disables IO stats.";
      };
      zedEventLimit = mkOption {
        type = types.ints.positive;
        default = 50;
        description = "Maximum number of recent ZED events to include.";
      };
      commandTimeoutSeconds = mkOption {
        type = types.ints.positive;
        default = 30;
        description = "Per-command timeout for zpool/zfs/smartctl.";
      };
      fileOwner = {
        uid = mkOption {
          type = types.nullOr types.int;
          default = null;
          description = "chown the output files to this uid (null: leave as root).";
        };
        gid = mkOption {
          type = types.nullOr types.int;
          default = null;
          description = "chown the output files to this gid (null: leave as root).";
        };
      };
    };

    server = {
      enable = mkEnableOption "the HTTP server for the collected report";
      listenAddress = mkOption {
        type = types.str;
        default = "0.0.0.0";
        description = "Address to bind.";
      };
      port = mkOption {
        type = types.port;
        default = 8080;
        description = "TCP port to serve on.";
      };
      openFirewall = mkOption {
        type = types.bool;
        default = false;
        description = "Open the server port in the NixOS firewall.";
      };
      user = mkOption {
        type = types.str;
        default = "zfs-monitor";
        description = "User the server runs as; created when left at the default.";
      };
      group = mkOption {
        type = types.str;
        default = "zfs-monitor";
        description = "Group the server runs as; created when left at the default.";
      };
      uid = mkOption {
        type = types.nullOr types.int;
        default = null;
        description = "Fixed uid for the created user (null: allocate).";
      };
      gid = mkOption {
        type = types.nullOr types.int;
        default = null;
        description = "Fixed gid for the created group (null: allocate).";
      };
    };

    thresholds = {
      maxStaleSeconds = mkOption {
        type = types.ints.positive;
        default = 300;
        description = "Report older than this is stale (served as HTTP 503).";
      };
      snapshotStaleSeconds = mkOption {
        type = types.ints.positive;
        default = 172800;
        description = "Snapshot age counted as stale.";
      };
      hotDriveCelsius = mkOption {
        type = types.int;
        default = 65;
        description = "Drive temperature counted as hot.";
      };
      capacityWarningPercent = mkOption {
        type = types.int;
        default = 80;
        description = "Pool capacity warning level.";
      };
      capacityCriticalPercent = mkOption {
        type = types.int;
        default = 90;
        description = "Pool capacity critical level.";
      };
      arcHitRatioMin = mkOption {
        type = types.float;
        default = 0.95;
        description = "Minimum acceptable lifetime ARC hit ratio.";
      };
      scrubOverdueDays = mkOption {
        type = types.ints.positive;
        default = 45;
        description = "Days since the last completed scrub before it is overdue.";
      };
      fragmentationWarningPercent = mkOption {
        type = types.int;
        default = 50;
        description = "Pool fragmentation warning level.";
      };
    };

    extraSettings = mkOption {
      type = json.type;
      default = { };
      description = ''
        Raw settings merged over the generated configuration (same shape as
        the JSON config file). Unknown keys make the service fail at start.
      '';
    };
  };

  config = mkMerge [
    (mkIf (cfg.collector.enable || cfg.server.enable) {
      environment.etc."zfs-monitor/config.json".source = configFile;
      environment.systemPackages = [ cfg.package ];
    })

    (mkIf cfg.collector.enable {
      systemd.tmpfiles.rules = [ "d ${cfg.collector.outputDir} 0755 root root -" ];

      systemd.services.zfs-monitor-collector = {
        description = "Collect ZFS pool, SMART and snapshot status";
        restartTriggers = [ configFile ];
        serviceConfig = {
          Type = "oneshot";
          ExecStart = "${exe} --config ${configFile} collect";
          TimeoutStartSec = 90;
          # Root is needed for /dev/zfs and raw SMART access; everything else
          # is closed off.
          NoNewPrivileges = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          PrivateTmp = true;
          ReadWritePaths = [ cfg.collector.outputDir ];
          ProtectKernelModules = true;
          ProtectControlGroups = true;
          RestrictNamespaces = true;
          RestrictRealtime = true;
          LockPersonality = true;
          SystemCallArchitectures = "native";
        };
      };

      systemd.timers.zfs-monitor-collector = {
        wantedBy = [ "timers.target" ];
        timerConfig = {
          OnBootSec = "10s";
          OnUnitActiveSec = cfg.collector.interval;
          AccuracySec = "5s";
        };
      };
    })

    (mkIf cfg.server.enable {
      users.groups = mkIf (cfg.server.group == "zfs-monitor") {
        zfs-monitor.gid = cfg.server.gid;
      };
      users.users = mkIf (cfg.server.user == "zfs-monitor") {
        zfs-monitor = {
          uid = cfg.server.uid;
          group = cfg.server.group;
          isSystemUser = true;
          home = cfg.collector.outputDir;
        };
      };

      systemd.services.zfs-monitor-server = {
        description = "ZFS monitor HTTP server";
        after = [ "network.target" ];
        wantedBy = [ "multi-user.target" ];
        restartTriggers = [ configFile ];
        serviceConfig = {
          ExecStart = "${exe} --config ${configFile} serve";
          User = cfg.server.user;
          Group = cfg.server.group;
          Restart = "always";
          RestartSec = 5;
          NoNewPrivileges = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          PrivateTmp = true;
          PrivateDevices = true;
          # "-": the dir may be a bind mount that appears after unit setup.
          ReadOnlyPaths = [ "-${cfg.collector.outputDir}" ];
          ProtectKernelTunables = true;
          ProtectKernelModules = true;
          ProtectControlGroups = true;
          RestrictAddressFamilies = [ "AF_INET" "AF_INET6" ];
          RestrictNamespaces = true;
          CapabilityBoundingSet = "";
          LockPersonality = true;
          SystemCallArchitectures = "native";
        };
      };

      networking.firewall.allowedTCPPorts = mkIf cfg.server.openFirewall [ cfg.server.port ];
    })
  ];
}
