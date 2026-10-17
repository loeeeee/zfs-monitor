{
  lib,
  python3Packages,
  zfs,
  smartmontools,
  # Put zfs + smartctl on the wrapped PATH. Turn off for a server-only host
  # that never runs the collector (avoids pulling the zfs closure), or when
  # the host's own kernel-matched zfs userland must be used instead.
  withTools ? true,
}:

python3Packages.buildPythonApplication {
  pname = "zfs-monitor";
  version = "0.1.0";
  pyproject = true;

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../README.md
      ../src
      ../tests
    ];
  };

  build-system = [ python3Packages.setuptools ];

  nativeCheckInputs = [ python3Packages.pytestCheckHook ];

  makeWrapperArgs = lib.optionals withTools [
    "--prefix"
    "PATH"
    ":"
    (lib.makeBinPath [
      zfs
      smartmontools
    ])
  ];

  meta = {
    description = "ZFS health collector (JSON report) and report server";
    mainProgram = "zfs-monitor";
    platforms = lib.platforms.linux;
  };
}
