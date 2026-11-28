# hermes-gmail

A small Gmail gate for a Hermes agent host. One long-lived service holds the OAuth credential in memory and serves a fixed set of bounded Gmail operations over a Unix socket: reads, label classification, and owner-approved labels, filters, drafts and send. Every failure comes back in one category a caller can act on, and no caller sees a token.

Cron jobs use the socket client. The agent reaches the service through a local MCP server that Hermes runs with `trust: untrusted`, so the owner approves every write in the agent's chat. The agent side ships from this repository as a Hermes plugin package; the service itself is a standalone process.

The design, its decisions and its acceptance rows are in [docs/design.md](docs/design.md). Work proceeds one slice of the design per pull request.

## Running

- `hermes-gmail serve --config <file>` runs the service; [docs/config.example.toml](docs/config.example.toml) lists every key. On NixOS, the flake's module runs it as a systemd service (see Nix).
- Cron consumers use `hermes_gmail.client.GateClient`, or the read-only CLI: `hermes-gmail search --socket <path> --query ...`.
- The agent gets the gate as a Hermes plugin package: this repository's `plugin.json`, `mcp.json` and `skills/`. Install the package with `hermes-gmail[mcp]` (on Nix, `packages.default`) on the agent's `PATH`, and set `HERMES_GMAIL_SOCKET` and `HERMES_GMAIL_CALLER` in the agent's profile.

## Nix

`flake.nix` builds the `hermes-gmail` command from `uv.lock` (`packages.default`, with the adapter's `mcp` extra) and gives a NixOS module, `nixosModules.default`. A host flake pins the input and sets the values; every value below is an example:

```nix
# inputs
hermes-gmail = {
  url = "git+https://github.com/geningdm-lab/hermes-gmail.git?ref=main&rev=<commit>&shallow=1";
  inputs.nixpkgs.follows = "nixpkgs";
};

# host module, with hermes-gmail.nixosModules.default imported
services.hermes-gmail = {
  enable = true;
  environmentFile = "/run/secrets/hermes-gmail-env"; # OP_SERVICE_ACCOUNT_TOKEN=...
  settings = {
    account = "owner@example.test";
    vault = { item = "OAuth document item"; vault = "Vault name"; };
  };
  callers.agent = { user = "agent"; roles = [ "read" "interactive" ]; };
};
```

The unit runs as its own `hermes-gmail` user unless `user` and `group` say otherwise, keeps its state in `/var/lib/hermes-gmail` and listens on `/run/hermes-gmail/gate.sock`. A caller that runs as another user must be in the service's group. `op` comes from nixpkgs' unfree `_1password-cli`, which the host must allow.

`nix flake check` builds the package and runs the unit tests against it; on Linux it also boots the module on a VM with stand-ins for `op` and Google.

## Development

Tools come from mise (`mise.toml`): Python 3.14, uv, ruff. The service itself uses the standard library only.

```bash
mise install
uv sync --all-extras
uv run python -m unittest discover -s tests -t .
ruff check . && ruff format --check .
```

Tests run offline only. Gmail and the Google token endpoint are a local HTTP server, 1Password is a fake `op` executable, and the config names both by loopback URL or absolute path, so no test can reach a real account or vault.

## License

MIT, see [LICENSE](LICENSE).
