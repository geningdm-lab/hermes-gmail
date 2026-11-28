# The Python environment from uv.lock, the `hermes-gmail` command built from
# it, and the unit tests run against that environment.
{
  pkgs,
  lib,
  pyproject-nix,
  uv2nix,
  pyproject-build-systems,
}:
let
  # Only what the build reads, so docs and Nix edits do not rebuild it.
  # README.md is the project's readme, LICENSE its license file.
  workspaceRoot = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../uv.lock
      ../README.md
      ../LICENSE
      ../src
    ];
  };
  workspace = uv2nix.lib.workspace.loadWorkspace { inherit workspaceRoot; };

  pythonSet =
    (pkgs.callPackage pyproject-nix.build.packages {
      python = pkgs.python314;
    }).overrideScope
      (
        lib.composeManyExtensions [
          pyproject-build-systems.overlays.wheel
          (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
        ]
      );

  # The service needs only the standard library; the mcp extra is the agent
  # adapter (`hermes-gmail mcp`). One command serves both, so one environment.
  venv = pythonSet.mkVirtualEnv "hermes-gmail-env" { hermes-gmail = [ "mcp" ]; };

  util = pkgs.callPackage pyproject-nix.build.util { };

  # Only bin/hermes-gmail: the venv's own python and the mcp dependencies'
  # commands must not land on a host PATH next to its python3.
  app =
    (util.mkApplication {
      inherit venv;
      package = pythonSet.hermes-gmail;
    }).overrideAttrs
      (old: {
        meta = (old.meta or { }) // {
          description = "A small Gmail gate for a Hermes agent host";
          homepage = "https://github.com/geningdm-lab/hermes-gmail";
          license = lib.licenses.mit;
          mainProgram = "hermes-gmail";
          platforms = lib.platforms.linux ++ lib.platforms.darwin;
        };
      });

  testSource = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../tests
      # What the tests check besides the code: the example config and the
      # plugin package.
      ../docs/config.example.toml
      ../plugin.json
      ../mcp.json
      ../skills
    ];
  };

  # The same command as CI's `uv run python -m unittest discover -s tests -t .`,
  # against the installed package rather than the source tree.
  tests =
    pkgs.runCommand "hermes-gmail-tests"
      {
        nativeBuildInputs = [ venv ];
        # The tests talk to a stand-in for Google on 127.0.0.1.
        __darwinAllowLocalNetworking = true;
      }
      ''
        cp -r ${testSource} source
        chmod -R u+w source
        cd source
        export HOME="$TMPDIR"
        python -B -m unittest discover -s tests -t .
        touch "$out"
      '';
in
{
  inherit app venv tests;
}
