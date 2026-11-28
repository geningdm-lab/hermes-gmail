{
  description = "hermes-gmail: a small Gmail gate for a Hermes agent host";

  inputs = {
    # The fleet's release; a host flake sets `inputs.nixpkgs.follows`.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    # The agent adapter needs mcp 2.3 and nixpkgs has 1.26, so the Python
    # environment comes from uv.lock through uv2nix, as Hermes builds its own.
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
    }:
    let
      inherit (nixpkgs) lib;
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];
      forAllSystems = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      build =
        pkgs:
        import ./nix/package.nix {
          inherit
            pkgs
            lib
            pyproject-nix
            uv2nix
            pyproject-build-systems
            ;
        };
    in
    {
      packages = forAllSystems (pkgs: {
        default = (build pkgs).app;
      });

      # The service definition. A host flake imports it and sets the values:
      # account, vault item, callers, the environment file with the token.
      nixosModules.default = import ./nix/module.nix { inherit self; };

      checks = forAllSystems (
        pkgs:
        let
          built = build pkgs;
        in
        {
          inherit (built) tests;
          package = built.app;
        }
        // lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
          module = pkgs.testers.runNixOSTest (import ./nix/module-test.nix { inherit self; });
        }
      );
    };
}
