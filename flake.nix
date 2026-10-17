{
  description = "zfs-monitor — ZFS health collector (JSON report) and report server";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (pkgs: rec {
        zfs-monitor = pkgs.callPackage ./nix/package.nix { };
        default = zfs-monitor;
      });

      overlays.default = final: _: {
        zfs-monitor = final.callPackage ./nix/package.nix { };
      };

      nixosModules.default = ./nix/module.nix;

      apps = forAll (pkgs: {
        default = {
          type = "app";
          program = "${self.packages.${pkgs.system}.default}/bin/zfs-monitor";
        };
      });

      # The package build runs the pytest suite; the module check evaluates a
      # full NixOS config with both halves enabled.
      checks = forAll (pkgs: {
        package = self.packages.${pkgs.system}.default;
        module =
          let
            eval = nixpkgs.lib.nixosSystem {
              inherit (pkgs) system;
              modules = [
                self.nixosModules.default
                {
                  boot.loader.grub.enable = false;
                  fileSystems."/" = { device = "none"; fsType = "tmpfs"; };
                  system.stateVersion = "25.05";
                  services.zfs-monitor = {
                    collector.enable = true;
                    server = { enable = true; openFirewall = true; };
                  };
                }
              ];
            };
          in
          pkgs.writeText "zfs-monitor-module-check" (
            builtins.toJSON [
              eval.config.systemd.services.zfs-monitor-collector.serviceConfig.ExecStart
              eval.config.systemd.services.zfs-monitor-server.serviceConfig.ExecStart
            ]
          );
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.pytest ]))
            pkgs.jq
          ];
          shellHook = "export PYTHONPATH=$PWD/src";
        };
      });
    };
}
