# services.hermes-gmail: the gate as a systemd service. The host's own config
# (homelab) imports this module and sets the values: the account, the vault
# item, the callers and the environment file with the service-account token.
{ self }:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib)
    literalExpression
    mkEnableOption
    mkIf
    mkOption
    types
    ;
  cfg = config.services.hermes-gmail;
  toml = pkgs.formats.toml { };

  stateDir = "/var/lib/hermes-gmail";
  runtimeDir = "/run/hermes-gmail";

  configFile = toml.generate "hermes-gmail.toml" (
    cfg.settings
    // {
      callers = lib.mapAttrsToList (name: caller: {
        inherit name;
        inherit (caller) user roles;
      }) cfg.callers;
    }
  );
in
{
  options.services.hermes-gmail = {
    enable = mkEnableOption "hermes-gmail, the Gmail gate";

    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = literalExpression "hermes-gmail.packages.\${system}.default";
      description = "The hermes-gmail package.";
    };

    user = mkOption {
      type = types.str;
      default = "hermes-gmail";
      description = ''
        User the service runs as. This module creates the default user. While
        the service and the agent share a user (design D4), set the agent's
        user here: the gate is then an operational facade, not a security
        boundary.
      '';
    };

    group = mkOption {
      type = types.str;
      default = "hermes-gmail";
      description = ''
        Group of the service and of its socket. A caller that runs as another
        user than the service must be a member of it to reach the socket.
      '';
    };

    opPackage = mkOption {
      type = types.package;
      default = pkgs._1password-cli;
      defaultText = literalExpression "pkgs._1password-cli";
      description = "Package with the `op` command. It is unfree: the host must allow it.";
    };

    environmentFile = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "/run/secrets/hermes-gmail-env";
      description = ''
        systemd EnvironmentFile with `OP_SERVICE_ACCOUNT_TOKEN=...`, for
        example a sops-nix secret owned by root. systemd reads it before it
        drops privileges, and it never enters the Nix store.
      '';
    };

    callers = mkOption {
      type = types.attrsOf (
        types.submodule {
          options = {
            user = mkOption {
              type = types.str;
              description = "User the caller runs as. The gate knows it by the socket peer's user ID.";
            };
            roles = mkOption {
              type = types.nonEmptyListOf (
                types.enum [
                  "read"
                  "classify"
                  "interactive"
                ]
              );
              description = "What this caller may do.";
            };
          };
        }
      );
      default = { };
      example = literalExpression ''
        {
          classifier = { user = "hermes"; roles = [ "read" "classify" ]; };
          agent = { user = "hermes"; roles = [ "read" "interactive" ]; };
        }
      '';
      description = "The config's `[[callers]]` entries, by caller name.";
    };

    settings = mkOption {
      description = ''
        The rest of the service config (docs/config.example.toml lists every
        key). The state directory and the socket belong to the unit and are
        fixed here; the callers come from `callers`.
      '';
      default = { };
      type = types.submodule {
        freeformType = toml.type;
        options = {
          account = mkOption {
            type = types.str;
            description = "The one Gmail account the gate serves.";
          };
          vault = mkOption {
            description = "Where `op` finds the OAuth document.";
            type = types.submodule {
              freeformType = toml.type;
              options = {
                item = mkOption {
                  type = types.str;
                  description = "1Password item with the OAuth document.";
                };
                vault = mkOption {
                  type = types.str;
                  description = "1Password vault of that item.";
                };
                op = mkOption {
                  type = types.str;
                  default = lib.getExe' cfg.opPackage "op";
                  defaultText = literalExpression ''lib.getExe' config.services.hermes-gmail.opPackage "op"'';
                  description = "The `op` command.";
                };
              };
            };
          };
          state_dir = mkOption {
            type = types.str;
            default = stateDir;
            readOnly = true;
            description = "The unit's StateDirectory: journal and vault cooldown.";
          };
          socket = mkOption {
            type = types.str;
            default = "${runtimeDir}/gate.sock";
            readOnly = true;
            description = "The socket path, in the unit's RuntimeDirectory. Callers point HERMES_GMAIL_SOCKET at it.";
          };
          socket_mode = mkOption {
            type = types.ints.unsigned;
            default = 432; # 0o660: the service's group reaches it, the peer user ID decides the rest
            description = "Socket permission bits, as a decimal number.";
          };
        };
      };
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = !(cfg.settings ? callers);
        message = "services.hermes-gmail: set callers in services.hermes-gmail.callers, not in settings.";
      }
    ];

    users.users = mkIf (cfg.user == "hermes-gmail") {
      hermes-gmail = {
        isSystemUser = true;
        inherit (cfg) group;
        description = "hermes-gmail Gmail gate";
      };
    };
    users.groups = mkIf (cfg.group == "hermes-gmail") { hermes-gmail = { }; };

    systemd.services.hermes-gmail = {
      description = "hermes-gmail Gmail gate";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];

      serviceConfig = {
        ExecStart = "${lib.getExe cfg.package} serve --config ${configFile}";
        User = cfg.user;
        Group = cfg.group;
        EnvironmentFile = lib.optional (cfg.environmentFile != null) cfg.environmentFile;
        # op keeps its own config below HOME; the state directory is the one
        # place the unit may write.
        Environment = [ "HOME=${stateDir}" ];
        StateDirectory = "hermes-gmail";
        StateDirectoryMode = "0700";
        RuntimeDirectory = "hermes-gmail";
        RuntimeDirectoryMode = "0750";
        UMask = "0077";
        Restart = "on-failure";
        RestartSec = "5s";

        # Hardening. Not PrivateUsers: it would map every caller to nobody, and
        # the gate tells callers apart by the socket peer's user ID. Not
        # RemoveIPC: under D4 the user is the agent's, and its IPC is not ours.
        NoNewPrivileges = true;
        CapabilityBoundingSet = "";
        AmbientCapabilities = "";
        PrivateTmp = true;
        PrivateDevices = true;
        DevicePolicy = "closed";
        ProtectSystem = "strict";
        ProtectHome = true;
        ProtectProc = "invisible";
        ProtectHostname = true;
        ProtectClock = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        RestrictAddressFamilies = [
          "AF_UNIX"
          "AF_INET"
          "AF_INET6"
        ];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        SystemCallArchitectures = "native";
        SystemCallFilter = [
          "@system-service"
          "~@privileged"
        ];
        # A filtered call fails with EPERM instead of killing op or Python.
        SystemCallErrorNumber = "EPERM";
      };
    };
  };
}
