# Design: hermes-gmail

Status: draft for owner review. Date: 2026-11-27.

## Why

Several programs on the Hermes host talk to the owner's Gmail, and each builds its own client: the daily label classifier, the proactive-attention reader (twice: once for polling, once for enrichment), the OAuth keepalive watchdog, and the agent itself when it works the mailbox interactively (labels, filters, drafts, send). Every one of them reads the same OAuth document from 1Password and refreshes its own access token.

That has already cost real failures and leaves real gaps:

- A classifier run failed as `OAuth document lookup failed` while 1Password was answering `Too many requests`. To the caller, a vault throttle, a revoked refresh token (`invalid_grant`) and a Gmail quota error look alike, so the wrong fix (reauthorize Google) is the obvious one.
- The classifier's helper checks a label plan in `apply()`, but its public `request()` accepts any `POST messages/batchModify` body (`lumi@9e55b53:automations/gmail_classifier/gmail_io.py:175`).
- That helper retries `401`, `429` and `5xx` the same way for reads and writes (`gmail_io.py:181-205`).
- Pacing and concurrency limits exist per client instance, not per account, so a large catch-up competes with interactive reads.
- Nothing stops any caller from reading the OAuth document itself and calling Gmail directly. The mailbox skill tells the agent to do exactly that.

This repository builds one small service that holds the credential, serves a fixed set of Gmail operations with explicit bounds, and reports every outcome in a category a caller can act on. It is not a framework. Classification rules, taxonomy and consumer checkpoints stay with their owners.

## What exists today

Consumer code lives in the private Lumi repository; citations are `lumi@9e55b53:path:line`.

| Consumer | Gmail use | Credential lifecycle |
|---|---|---|
| Daily classifier (`automations/gmail_classifier/`) | list, metadata reads, labels, filters, `batchModify` of labels and `INBOX` | own vault read (`gmail_io.py:35`), own refresh |
| Proactive reader (`automations/proactive-assistance/sources.py`) | `profile`, `history`, message and thread reads | own vault read per client (`sources.py:278`), a second client for enrichment (`runtime.py:179`) |
| OAuth watchdog (`automations/google-workspace/refresh_watchdog.py`) | none; refreshes once a day to surface `invalid_grant` | own vault read (`refresh_watchdog.py:17`) |
| Agent, interactive | labels, filters, approved batch changes, threaded drafts, send | reads the document per task, following the mailbox skill |

An unreleased attempt at a shared client exists on Lumi branches (`feature/2-mail-worker-...`, `task/8-...`): a credential broker, search and message/thread reads, and a MIME text extractor, 44 passing tests. It has no real I/O (Gmail and the vault are injected callables), and once its bounded vault-read attempts fail it never reads the vault again in that process. This repository reuses its bounds, error categories and tests, not its plan of three sockets.

## Decisions

Owner answers of 2026-11-27.

| # | Decision |
|---|---|
| D1 | Public repository in `geningdm-lab`. It holds the gate only. Classification rules, host paths, vault item names, account addresses and any mail data stay out: in deployment config or in Lumi. |
| D2 | Ordinary GitHub pull requests with owner review. Prism is not used. |
| D3 | One service process, one Unix socket. The service identifies the caller by the socket's peer credentials (`SO_PEERCRED`), never by a request field, and each caller has a fixed table of operations. |
| D4 | First an operational facade: while the service and the agent run as the same user, this is not a security boundary. It becomes one when the service runs as its own user and the agent loses read access to the OAuth document. That is a separate homelab change and a separate decision. |
| D5 | One read budget per account, as today: requests paced at least 0.13 s apart, at most 3 in flight, shared by every caller. |
| D6 | Drafts, send and filters are in scope: the owner uses them today. |
| D7 | The Lumi mail-worker branches are closed as superseded once the tests worth keeping are carried over. |

## Architecture

### Process

`hermes-gmail serve` is a long-lived process supervised by systemd (deployed by homelab, not by this repository). It holds one credential broker, one HTTP transport, one budget, one journal for write operations, and one socket listener. `hermes_gmail.client` is a thin JSON-over-socket client; `hermes-gmail <operation>` is a CLI over that client, which replaces direct `op document get` calls in the agent's skills.

One account is configured. A request for any other account is refused.

### Credential broker

- Reads the OAuth document with `op document get` into memory only. Item and vault names come from config. No token reaches disk, stdout, logs or a response.
- One refresh at a time: concurrent callers that need a fresh token wait for the same refresh.
- A failed vault read is retried a bounded number of times with backoff, then the broker enters a cooldown and answers `vault_unavailable` without touching the vault until the cooldown ends. The cooldown start is kept in the state directory (a timestamp, no secret), so a restart loop cannot turn into a vault-read storm.
- The token endpoint must be one of Google's two token URIs; redirects are refused.

### Transport

Stdlib `urllib`, carried over from the classifier helper: fixed Gmail base URL (tests point it at a local server through config), no redirects, every response bounded by an operation deadline.

- Reads retry `429`, `5xx` and `403 rateLimitExceeded`/`userRateLimitExceeded` with exponential backoff and jitter, honour `Retry-After`, and stop at the operation deadline.
- Writes are never retried blindly. A label change that times out or fails is followed by a fresh read of the targets. A write that is not idempotent (send, draft create, filter create) is recorded `uncertain` in the journal, and only an explicit reconcile resolves it.
- One `401` triggers one refresh and one repeat of the request: Gmail did not act on a request it answered with `401`.

### Errors

Every error a caller sees carries exactly one category from a closed set, readable without parsing a message, and never a provider response body or a secret.

| Category | Meaning | What fixes it |
|---|---|---|
| `vault_unavailable` | 1Password read failed or is in cooldown | wait, check 1Password service account |
| `oauth_invalid_grant` | Google rejected the refresh token | owner reauthorizes |
| `oauth_invalid_client` | Google rejected the OAuth client | fix the client in the Cloud project |
| `oauth_failed` | any other token-endpoint failure or malformed token response | investigate |
| `gmail_quota` | `429` or a rate-limit `403` after retries | wait; the budget will slow down |
| `gmail_scope` | `403 insufficientPermissions` | grant the scope; not a reauthorization by itself |
| `gmail_not_found` | message, thread, draft or filter does not exist | caller decides |
| `gmail_failed` | any other Gmail HTTP or transport failure | investigate |
| `refused` | the gate refused before any vault or provider call: unknown operation, caller not allowed, wrong account, bound exceeded, malformed request, stale preview | fix the caller |
| `uncertain` | a write may or may not have happened | reconcile before anything else |

`health()` returns per-category counters, the time of the last success per operation class, cooldown state and budget use. No mail content.

## Operations

Each caller (peer user, plus a per-caller config entry) gets a fixed set: the classifier gets read and classify; the proactive reader gets read only; the agent gets read and interactive. An operation outside the caller's set is `refused` before any vault or provider call.

### Read

| Operation | Returns | Bounds |
|---|---|---|
| `profile()` | account address, current `historyId` | |
| `search(query, page_bound)` | IDs, `complete`, `next_page_token` when cut | query at most 8 KiB, at most 100 pages |
| `read_messages(ids, format, cap)` | per message: headers, labels, and for `full` the plain text with `truncated`/`decode_failed` flags | at most 100 IDs, ID at most 512 bytes |
| `read_thread(thread_id, bound)` | messages with direction (sent/received) and reply headers | at most 100 messages |
| `history(start_history_id, page_bound)` | added message IDs and label changes, new `historyId` | at most 100 pages |
| `labels()`, `filters()`, `drafts(thread_id)` | current provider state | |

A count is what was fetched, never `resultSizeEstimate`. HTML-only bodies become plain text without fetching anything remote.

### Classify

`classify_apply(plan, rule_version)` is today's `GmailAPI.apply` (`gmail_io.py:111`) with a per-message outcome. A plan entry may only add existing user labels and remove `INBOX`; every target is preflighted with a fresh read; a message carrying `SPAM`, `TRASH`, `SENT` or `DRAFT` refuses the plan; the result must carry exactly one label with the configured exclusive prefix (`Area/` today); `UNREAD` is never changed. Writes go out per label-delta group in batches of at most 1000, and every written message is read back. The result lists verified IDs and mismatched IDs separately, so the consumer can advance its checkpoint for the verified ones and set the rest aside instead of losing the whole run.

The standing authority for unattended classification is the classifier's own rule version, recorded in the journal. It does not cover any other change.

### Interactive

Every interactive write is two steps. `prepare` returns an immutable preview: exact targets, exact changes, the provider state it was computed from, a hash and an expiry. The owner approves that preview. `apply(preview_ref, approval_ref)` re-reads the targets, stops with `refused` if anything differs from the preview, writes, and reads back. A changed scope needs a new preview.

| Operation | Notes |
|---|---|
| `label_create(name)` | idempotent by name; creating a label authorizes nothing else |
| `modify_prepare(ids or query, add, remove)` / `modify_apply` | may change `INBOX` and `UNREAD`; owner actions since the preview stop the apply rather than being overwritten |
| `filter_prepare(criteria, action)` / `filter_apply`, `filter_delete_prepare` / `filter_delete_apply` | at most one user label per filter (Gmail's limit); read-back compares label arrays as sets; needs `gmail.settings.basic` |
| `draft_put(thread_id, to, cc, subject, body)` | updates the draft already attached to the thread instead of creating a second one; the service builds the MIME with `In-Reply-To`, `References` and `threadId`; read-back decodes the MIME and compares recipients, subject and body |
| `send_prepare(draft_id)` / `send_apply` | sends an existing draft only; the preview holds recipients, subject and a body hash. The journal records `in_flight` durably before the call. A lost response or timeout leaves `uncertain`, never a second send; `send_reconcile` looks for the message in Sent by its `Message-ID` |

Not offered: delete, trash, spam, sending without a draft, forwarding and other settings, attachment download.

Approval in this first version is procedural. The agent passes a reference to the owner's approving message, and the service records it in the journal and refuses an apply without one. Running as the same user as the agent, the service cannot tell a real reference from a forged one. See Q1.

## State

The service keeps a state directory (path from config, not in Git): the journal (SQLite) and the vault cooldown timestamp. The journal records operation, caller, preview hash, approval reference, status and Gmail IDs. It never records bodies, subjects, snippets, codes or tokens.

Consumers keep their own state. The classifier's checkpoint, withheld set and lock and the reader's cursors stay where they are; the service never holds a consumer checkpoint, and moving code never creates a fresh one.

## Testing

Offline only, with synthetic fixtures:

- A local HTTP server stands in for Gmail and the OAuth token endpoint, so tests cross the real transport and a real socket, not a mocked method.
- A fake `op` executable on `PATH` stands in for 1Password: it can succeed, throttle, fail or hang.
- Fixtures are written for these tests, never derived from real mail.
- No test touches real Gmail, Google OAuth or 1Password.

Carried over: the 13 transport and apply tests of `gmail_io.py`, and the mail-worker tests that still apply (broker categories, bounds, MIME).

## Acceptance

| Row | Behaviour | Slice |
|---|---|---|
| 1 | Cold start with several concurrent callers: one vault read, one refresh | 1 |
| 2 | Vault throttle or outage, `invalid_grant`, `invalid_client`, quota and missing scope each end in their own category; no error, log or response carries a secret or a provider body | 1 |
| 3 | A persistent vault outage leads to cooldown, not repeated reads; a restart does not reset it | 1 |
| 4 | Search: zero results, several pages, page bound reached, repeated continuation token, a failed read inside a batch; the result states exact fetched count and coverage | 2 |
| 5 | MIME: HTML-only, image-only, malformed base64, unknown charset, truncation; nothing remote is fetched | 2 |
| 6 | Classify reproduces today's plans and outcomes on the carried-over cases; `UNREAD` never changes; a partial failure returns verified and mismatched IDs separately | 3 |
| 7 | Unknown caller, operation outside the caller's set, wrong account, raw URL or endpoint: refused before any vault or provider call; the reader cannot change the mailbox by any request it can build | 4 |
| 8 | Interactive apply stops on a stale or expired preview, changed labels or an owner action since the preview; every written target is read back | 5 |
| 9 | Drafts: one draft per thread, threading headers set, semantic read-back | 5 |
| 10 | Filters: more than one user label refused; read-back compares as sets | 5 |
| 11 | Send: lost response, timeout after acceptance, restart with a send in flight, a repeated approval: never a second send; the journal shows `uncertain` until reconciled | 6 |
| 12 | Live, after deploy and separately approved: one bounded read, then one approved reversible label batch with read-back. No test send | cutover |

## Slices

One slice per pull request, in order.

1. Credential broker, transport, error categories, `health()`, and the synthetic Gmail/OAuth/vault harness. Rows 1-3.
2. Read operations and MIME text. Rows 4-5.
3. `classify_apply` with per-message outcome. Row 6.
4. Service: socket, peer credentials, per-caller operation sets, client, CLI. Row 7.
5. Interactive writes: labels, filters, batch changes, drafts. Rows 8-10.
6. Send with journal and reconcile. Row 11.

Outside this repository, each approved separately:

- Homelab: systemd unit, user, socket and state directory permissions, config, rollback.
- Lumi: the classifier calls the service; a missing checkpoint fails closed instead of starting from an empty one (today `run_daily.py:20` starts empty, which would replay archiving since the baseline); verified IDs advance the checkpoint and mismatched ones are set aside like withheld ones.
- Lumi: the proactive reader calls the service; its cursors stay as they are.
- Lumi: the mailbox and OAuth skills use the CLI instead of reading the OAuth document.
- Lumi: the watchdog job calls `health()` with a forced refresh instead of reading the vault.
- Lumi: close the mail-worker PRs as superseded (D7).

Rollback restores the consumer's previous runtime binding, never an older checkpoint.

## Open questions

| # | Question | Default until answered |
|---|---|---|
| Q1 | How does send approval become something the agent cannot forge? One option: the owner presses a button in Telegram (hermes-telegram) and the gateway, under its own user, hands the approval to the service. Depends on D4 | procedural approval reference, recorded in the journal |
| Q2 | When does the service get its own user and the agent lose read access to the OAuth document? | not before all consumers are cut over |
| Q3 | Who confirms the first live cutover and the rollback? | the owner, by explicit message, after the homelab PR |
| Q4 | Journal retention | 90 days |
| Q5 | Attachment reads (used for travel-history work): in or out? | out of the first version |
| Q6 | Vault cooldown length after a failed bounded retry | 15 minutes |

## Out of scope

- Classification rules, taxonomy, and which mail may be archived or marked read unattended.
- A thread-aware audit of missed replies.
- Delete, trash, spam, forwarding and settings other than filters.
- A second account or family profiles.
- OCR, PDF extraction, attachment execution.
- Deployment, which belongs to homelab.
