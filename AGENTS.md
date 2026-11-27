# Working in hermes-gmail

Read README.md and docs/design.md before changing anything. The design's decisions and acceptance rows are the requirements; do not narrow or widen them silently. A change that departs from the design updates the design in the same pull request.

## Rules

- This repository is public. No tokens, OAuth documents, vault item names, account addresses, host paths, classification rules, mail IDs, subjects, bodies or runtime databases in Git, including tests, fixtures, logs and commit messages. Those come from deployment config at run time.
- Tests are offline. Gmail and the Google token endpoint are a local HTTP server; 1Password is a fake `op` on `PATH`. Fixtures are synthetic, never derived from real mail. Never run anything against the real account, OAuth or vault from this repository.
- Cite consumer code in the private Lumi repository as `lumi@<commit>:path:line` and Hermes as `hermes-agent@<commit>:path:line`. Cite Gmail and OAuth behaviour from developers.google.com with the page. A passing test is not live behaviour.
- No secret reaches disk, stdout, logs, errors or responses. Errors carry a category from docs/design.md, never a provider response body.
- Writes are never retried blindly. A write that is not idempotent is journaled before the call and becomes `uncertain` when its outcome is unknown.
- Deployment (systemd unit, user, socket, config) belongs to homelab, not here.
- Owner review happens on GitHub pull requests. Never merge without the owner's approval.

## Per slice

- One slice of docs/design.md per pull request, in order.
- Product questions (what the owner sees or approves, not how the code does it) go to the owner, not into code as a guess.
