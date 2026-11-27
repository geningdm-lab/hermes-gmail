# hermes-gmail

A small Gmail gate for a Hermes agent host. One long-lived service holds the OAuth credential in memory and serves a fixed set of bounded Gmail operations over a Unix socket: reads, label classification, and owner-approved labels, filters, drafts and send. Every failure comes back in one category a caller can act on, and no caller sees a token.

Cron jobs use the socket client. The agent reaches the service through a local MCP server that Hermes runs with `trust: untrusted`, so the owner approves every write in the agent's chat. The agent side ships from this repository as a Hermes plugin package; the service itself is a standalone process.

The design, its decisions and its acceptance rows are in [docs/design.md](docs/design.md). Nothing is implemented yet; work proceeds one slice of the design per pull request.

## Development

Tools come from mise (`mise.toml`): Python 3.14, uv, ruff.

Tests run offline only. Gmail, the Google token endpoint and 1Password are replaced by local stand-ins; no test reaches a real account.

## License

MIT, see [LICENSE](LICENSE).
