# Design: hermes-gmail

Status: draft for owner review. Date: 2026-11-27, updated 2026-11-28.

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

Owner answers of 2026-11-27 (D1-D7) and 2026-11-28 (D8-D9).

| # | Decision |
|---|---|
| D1 | Public repository in `geningdm-lab`. It holds the gate only. Classification rules, host paths, vault item names, account addresses and any mail data stay out: in deployment config or in Lumi. |
| D2 | Ordinary GitHub pull requests with owner review. Prism is not used. |
| D3 | One service process, one Unix socket. The service identifies the caller by the socket's peer credentials (`SO_PEERCRED`), never by a request field, and each caller has a fixed table of operations. |
| D4 | First an operational facade: while the service and the agent run as the same user, this is not a security boundary. It becomes one when the service runs as its own user and the agent loses read access to the OAuth document. That is a separate homelab change and a separate decision. |
| D5 | One read budget per account, as today: requests paced at least 0.13 s apart, at most 3 in flight, shared by every caller. |
| D6 | Drafts, send and filters are in scope: the owner uses them today. They go through this gate, not through Google's Gmail MCP server: one transport, one OAuth grant, one place the token lives (see Considered and rejected). |
| D7 | The Lumi mail-worker branches are closed as superseded once the tests worth keeping are carried over. |
| D8 | The agent reaches the gate through a local stdio MCP server, `hermes-gmail mcp`, gated by Hermes with `trust: untrusted`, not through the CLI. Every write asks the owner in the agent's chat (see Agent adapter). |
| D9 | The service is a standalone process, not a Hermes plugin. The agent side (the MCP adapter, its `trust: untrusted` declaration and a generic usage skill) ships from this repository as a Hermes portable plugin package. |

## Architecture

### Process

`hermes-gmail serve` is a long-lived process supervised by systemd (deployed by homelab, not by this repository). It holds one credential broker, one HTTP transport, one budget, one journal for write operations, and one socket listener. Around it:

- `hermes_gmail.client`, a thin JSON-over-socket client for the cron consumers.
- `hermes-gmail mcp`, the agent adapter (D8): a stdio MCP server that Hermes starts. It holds no credential and turns each tool call into one socket request.
- `hermes-gmail <operation>`, a CLI over the client for reads and `health()`, for the owner and for diagnostics. It offers no writes, so the documented write path always passes the owner's approval.

The service is not a Hermes plugin (D9). A plugin runs inside the Hermes process with the agent's user and trust (`hermes-agent@28ef6e4:website/docs/developer-guide/plugins/index.md:117-126`), which would put the credential in the agent's memory and rule out a separate user later (D4, Q2). The cron consumers also run without Hermes.

One account is configured. A request for any other account is refused.

The socket speaks one JSON object per line each way: `{"op", "args", "caller", "account"}` in, `{"ok": true, "result"}` or `{"ok": false, "error": {"category", "detail"}}` out. Config lists the callers as `[[callers]]` entries of name, user ID and roles (`read`, `classify`, `interactive`). The peer's user ID comes from the kernel. While several callers run as one user (D4), a request names which of that user's entries it is; the name can only pick among them, never add to them, so this separation is operational, not a security boundary, until the callers get their own users. `docs/config.example.toml` shows every key.

### Credential broker

- Reads the OAuth document with `op document get` into memory only. Item and vault names come from config. No token reaches disk, stdout, logs or a response.
- One refresh at a time: concurrent callers that need a fresh token wait for the same refresh.
- A failed vault read is retried a bounded number of times with backoff, then the broker enters a cooldown and answers `vault_unavailable` without touching the vault until the cooldown ends. The cooldown start is kept in the state directory (a timestamp, no secret), so a restart loop cannot turn into a vault-read storm.
- When Google rejects the refresh token or the client (`oauth_invalid_grant`, `oauth_invalid_client`), the broker answers that category without calling Google or the vault for the cooldown length, then reads the document again: the owner may have reauthorized and updated it in 1Password. The hold is in memory only; a restart reads the vault once.
- The token endpoint must be one of Google's two token URIs; redirects are refused.

### Transport

Stdlib `urllib`, carried over from the classifier helper: fixed Gmail base URL, no redirects, every response bounded by an operation deadline. Config may move the Gmail base and the token endpoint only to a loopback URL; that is how tests reach the local stand-in.

- Reads retry `429`, `5xx` and `403 rateLimitExceeded`/`userRateLimitExceeded` with exponential backoff and jitter, honour `Retry-After`, and stop at the operation deadline.
- Writes are never retried blindly. A label change that times out or fails is followed by a fresh read of the targets. A write that is not idempotent (send, draft create, filter create) is recorded `uncertain` in the journal, and only an explicit reconcile resolves it.
- One `401` triggers one refresh and one repeat of the request: Gmail did not act on a request it answered with `401`.

### Agent adapter

`hermes-gmail mcp` offers one MCP tool per operation in the agent's set. Hermes citations below are to `hermes-agent@28ef6e4`, read in source, not observed live.

- Read tools and every `*_prepare` carry `readOnlyHint: true`: they do not change the mailbox. Every apply carries no hint. On a `trust: untrusted` server Hermes asks the owner before any tool without `readOnlyHint: true`, and shows the call's arguments (`tools/mcp_tool_handlers.py:190-306`). A missing or malformed hint counts as write-capable (`tools/mcp_tool_registration.py:52-89`).
- That prompt can be answered "once", "for this session" or "always" per tool, and YOLO or `approvals.mode: off` skip it (`tools/mcp_tool_handlers.py:68-74`, `website/docs/user-guide/features/mcp.md:1022`). So each apply also asks on its own: before the socket request, the adapter sends an MCP form elicitation with the preview summary (targets, changes; for send, recipients and subject). Hermes offers only yes or no for an elicitation and remembers no answer (`tools/approval_prompt.py:296-310`, `tools/mcp_tool_sampling.py:351-372`, `tools/mcp_elicitation_form.py:319-328`). Whether YOLO also skips the elicitation is not verified.
- Decline, cancel, a timeout or a client without elicitation leaves the preview unused and writes nothing; the result is `declined`. Hermes declines an elicitation where nobody can answer, such as cron (`tools/approval_context.py:229-233`), so the agent cannot make interactive writes unattended.
- The service journals each apply with the elicitation outcome and time, in place of an approval reference. Under D4 this is still a facade: the agent's user can start the adapter and answer its elicitation itself, or call the socket directly. See Q1.
- The adapter uses the official MCP Python SDK. Server-to-client elicitation is the part not worth writing by hand. The service and the cron client stay stdlib-only; how the adapter's environment gets the SDK is settled in slice 4.
- Packaging (D9): the repository root is a Hermes portable plugin package with `plugin.json`, `mcp.json` (the stdio command) and `skills/`. The package declares `trust: untrusted` for its server itself (`website/docs/developer-guide/plugins/index.md:128-150`). A `config.yaml` server with the same name replaces the package's entry, trust included, so deployment must not declare one.

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
| `declined` | the owner declined or cancelled an apply's approval, nobody answered in time, or nobody can answer; nothing was written | ask again only on the owner's direction |
| `uncertain` | a write may or may not have happened | reconcile before anything else |

`health()` returns per-category counters, the time of the last success per operation class, cooldown state and budget use. No mail content.

## Operations

Each caller (peer user, plus a per-caller config entry) gets a fixed set: the classifier gets read and classify; the proactive reader gets read only; the agent gets read and interactive. An operation outside the caller's set is `refused` before any vault or provider call.

### Read

| Operation | Returns | Bounds |
|---|---|---|
| `profile()` | account address, current `historyId` | |
| `search(query, page_bound, page_token)` | IDs, exact `fetched` count, `complete`, `next_page_token` when cut | query at most 8 KiB, at most 100 pages; a repeated page token fails |
| `read_messages(ids, format, headers, cap)` | per message: labels, and for `metadata`/`full` headers and snippet, for `full` the plain text with `truncated`/`decode_failed` flags; every ID ends in `messages` or in `failed` with its category | at most 100 distinct IDs, ID at most 512 bytes of Gmail's alphabet; `format` is `minimal`, `metadata` or `full`; `cap` is the text length per message, at most 200 000 characters |
| `read_thread(thread_id, bound, cap)` | the latest `bound` messages, oldest first, with direction (sent/received), reply headers and text; `complete` and `total` | at most 100 messages |
| `history(start_history_id, page_bound, page_token)` | added and deleted message IDs, label changes, the mailbox's `history_id`; a cursor advances to it only when `complete`; `gmail_not_found` means the start is too old and the consumer resyncs | at most 100 pages |
| `labels()`, `filters()`, `drafts(thread_id, page_bound)` | current provider state | at most 100 pages of drafts |

A count is what was fetched, never `resultSizeEstimate`. HTML-only bodies become plain text without fetching anything remote.

### Classify

`classify_apply(plan, rule_version)` is today's `GmailAPI.apply` (`gmail_io.py:111`) with a per-message outcome. A plan has at most 2000 entries. A plan entry may only add existing user labels and remove `INBOX`; every target is preflighted with a fresh read; a message carrying `SPAM`, `TRASH`, `SENT` or `DRAFT` refuses the plan; the result must carry exactly one label with the configured exclusive prefix (`Area/` today, from config, since the taxonomy is the classifier's); `UNREAD` is never changed. Writes go out per label-delta group in batches of at most 1000, and every written message is read back. A failed or uncertain write is read back too, and later batches are not sent.

Every planned message ends in exactly one list: `missing` (gone before the preflight), `unchanged` (already as planned), `verified`, `mismatched` (read back different: a race, never rolled back), `unverified` (the read-back failed) or `not_attempted` (a failed batch left it as it was, or it came after a failed batch); `stopped` names the write failure's category. The consumer advances its checkpoint for `verified`, `unchanged` and `missing`, sets `mismatched` and `unverified` aside like withheld messages, and retries `not_attempted` on the next run.

The standing authority for unattended classification is the classifier's own rule version, recorded in the journal. It does not cover any other change.

### Interactive

Label changes on messages and filters are two steps. `prepare` returns an immutable preview: exact targets, exact changes, the provider state it was computed from, a hash and a 15-minute expiry. The owner approves that preview in the agent's chat (see Agent adapter). `apply(preview_id, approval)` takes the preview once (a second apply, even a concurrent one, is `refused`), re-reads the targets, stops with `refused` if anything differs from the preview, writes, and reads back. A changed scope needs a new preview. A declined approval leaves the preview open; a stale or expired one cannot be applied. Previews live in the service's memory, because they hold label names, filter criteria and addresses that the journal must not record; a restart drops them, and the agent prepares again. A summary leads with the action and the targets; every name or value taken from the agent or the mailbox is shown in quotes, on one line, without control characters and cut at 80 characters, so it cannot pose as part of the action.

`label_create` and `draft_put` are one step. A label is idempotent by name and authorizes nothing else; a draft is idempotent per thread and changes nothing anyone else sees until it is sent. Hermes' trust prompt is their approval.

| Operation | Notes |
|---|---|
| `label_create(name)` | idempotent by name: an existing label is returned, not created again; after an uncertain create the label list decides |
| `modify_prepare(ids or query, add, remove)` / `modify_apply` | at most 1000 targets; `add` and `remove` are existing user labels or `INBOX`, `UNREAD`, `STARRED`, `IMPORTANT`; spam and trash are never targets; owner actions since the preview stop the apply rather than being overwritten; each written target is `verified`, `mismatched` or `unverified` |
| `filter_prepare(criteria, add, remove)` / `filter_apply`, `filter_delete_prepare` / `filter_delete_apply` | criteria are Gmail's filter fields; `add` is at most one user label (Gmail's limit) plus `STARRED` or `IMPORTANT`; `remove` is `INBOX`, `UNREAD`, `IMPORTANT` or `SPAM`; no forwarding; an identical filter refuses the preview; read-back compares label arrays as sets; a create whose answer was lost is reconciled by listing filters; needs `gmail.settings.basic` |
| `draft_put(to, subject, body, cc, thread_id, draft_id)` | plain text; updates the draft already attached to the thread instead of creating a second one (several drafts: name one); the service builds the MIME with `In-Reply-To`, `References` and `threadId` from the thread's latest message; read-back decodes the MIME and compares recipients, subject, threading and body; a lost create is `uncertain`, and the next `draft_put` in that thread finds and updates it |
| `send_prepare(draft_id)` / `send_apply` | sends an existing draft only; the preview holds recipients, subject and a body hash. The journal records `in_flight` durably before the call. A lost response or timeout leaves `uncertain`, never a second send; `send_reconcile` looks for the message in Sent by its `Message-ID` |

Not offered: delete, trash, spam, sending without a draft, forwarding and other settings, attachment download.

An apply without an accepted elicitation writes nothing. The adapter asks through the MCP SDK's elicitation resolver, which uses a mid-call request on the initialize-handshake protocol and an input-required round trip on protocol 2026-07-28; on the latter the answer rides the client's retry sealed with a per-process key, so a client cannot present an answer that was never given. Only an accepted answer whose data is the approval form counts: the SDK reports any value a resolver returns itself as accepted, so a client without elicitation is answered with a placeholder that is never an approval, and the apply is `declined`. Running as the same user as the agent, the service still cannot tell an elicitation the owner answered from one the agent answered itself. See Q1.

## State

The service keeps a state directory (path from config, not in Git): the journal (SQLite) and the vault cooldown timestamp. Previews are in memory only. The journal records operation, caller, preview hash, approval outcome and time, status and Gmail IDs. It never records bodies, subjects, snippets, codes or tokens.

Consumers keep their own state. The classifier's checkpoint, withheld set and lock and the reader's cursors stay where they are; the service never holds a consumer checkpoint, and moving code never creates a fresh one.

## Testing

Offline only, with synthetic fixtures:

- A local HTTP server stands in for Gmail and the OAuth token endpoint, so tests cross the real transport and a real socket, not a mocked method.
- A fake `op` executable on `PATH` stands in for 1Password: it can succeed, throttle, fail or hang.
- The adapter is tested over stdio with the MCP SDK's client against a real service on a temporary socket. The test client answers the elicitations: accept, decline, cancel, timeout, or no elicitation support.
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
| 8 | Adapter: the tool list is the agent's operation set; every read and prepare tool is annotated `readOnlyHint: true`, every apply carries no hint; the package declares `trust: untrusted` for its server | 4 |
| 9 | Interactive apply stops on a stale or expired preview, changed labels or an owner action since the preview; every written target is read back | 5 |
| 10 | Apply approval: the elicitation shows the preview summary; decline, cancel, timeout or a client without elicitation ends in `declined` with nothing written and the preview unused | 5 |
| 11 | Drafts: one draft per thread, threading headers set, semantic read-back | 5 |
| 12 | Filters: more than one user label refused; read-back compares as sets | 5 |
| 13 | Send: lost response, timeout after acceptance, restart with a send in flight, a repeated approval: never a second send; the journal shows `uncertain` until reconciled | 6 |
| 14 | Live, after deploy and separately approved: one bounded read, then one reversible label batch approved by the owner in Telegram, with read-back. No test send | cutover |

## Slices

One slice per pull request, in order.

1. Credential broker, transport, error categories, `health()`, and the synthetic Gmail/OAuth/vault harness. Rows 1-3.
2. Read operations and MIME text. Rows 4-5.
3. `classify_apply` with per-message outcome. Row 6.
4. Service: socket, peer credentials, per-caller operation sets, client, CLI; the MCP adapter with read tools and the plugin package. Rows 7-8.
5. Interactive writes as adapter tools with elicitation: labels, filters, batch changes, drafts. Rows 9-12.
6. Send with journal and reconcile. Row 13.

Outside this repository, each approved separately:

- Homelab: systemd unit, user, socket and state directory permissions, config, rollback; install and enable the plugin package for the agent's profile, with no same-name `mcp_servers` entry in `config.yaml`.
- Lumi: the classifier calls the service; a missing checkpoint fails closed instead of starting from an empty one (today `run_daily.py:20` starts empty, which would replay archiving since the baseline); the outcome lists drive the checkpoint as described in Classify.
- Lumi: the proactive reader calls the service; its cursors stay as they are.
- Lumi: the mailbox and OAuth skills stop reading the OAuth document; the agent uses the adapter's tools, and the private mailbox rules stay in Lumi's skill.
- Lumi: the watchdog job calls `health()` with a forced refresh instead of reading the vault.
- Lumi: close the mail-worker PRs as superseded (D7).

Rollback restores the consumer's previous runtime binding, never an older checkpoint.

## Open questions

| # | Question | Default until answered |
|---|---|---|
| Q1 | How does approval become something the agent cannot forge? The owner answers in Telegram, but the adapter runs as the agent's user, so the agent can start it and answer its elicitation itself. A forge-proof approval needs the approving side to run as a user the agent cannot become, for example the gateway handing the owner's button press to the service. Depends on D4 | elicitation through the adapter, recorded in the journal |
| Q2 | When does the service get its own user and the agent lose read access to the OAuth document? | not before all consumers are cut over |
| Q3 | Who confirms the first live cutover and the rollback? | the owner, by explicit message, after the homelab PR |
| Q4 | Journal retention | 90 days |
| Q5 | Attachment reads (used for travel-history work): in or out? | out of the first version |
| Q6 | Vault cooldown length after a failed bounded retry | 15 minutes |

## Considered and rejected

### Google's Gmail MCP server

Google offers a remote Gmail MCP server (`gmailmcp.googleapis.com`), and Hermes can connect to it. It was considered as the agent's path for drafts, send and filters, next to this gate for the cron consumers. It was rejected on 2026-11-28 because it would add a second transport without removing the first:

- It cannot serve the cron consumers. There is no `history`, which the reader's incremental sync needs, and no batch label change: `update_message_labels` takes one message ID per call. Search returns threads, at most 50 a page. So the gate is needed in any case.
- It lacks operations the owner uses: there is no filter, draft or label delete.
- It needs a second OAuth grant, from a client flagged for AI agents. Hermes would keep that grant's refresh token on disk under `HERMES_HOME/mcp-tokens/` with mode 0600 (`hermes-agent@28ef6e4:tools/mcp_oauth.py:392-408`), against the rule that the token lives only in 1Password and the service's memory.
- Its main advantage, approval of writes in the owner's chat, comes from Hermes and not from Google. Hermes gives the same to this gate's adapter (D8).
- Google documents no quotas for it, and its tool list is still changing.

Sources: [Gmail MCP reference](https://developers.google.com/workspace/gmail/api/reference/mcp), [Configure the Gmail MCP server](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server), [Configure the Google Workspace MCP servers](https://developers.google.com/workspace/guides/configure-mcp-servers), [update_message_labels](https://developers.google.com/workspace/gmail/api/reference/mcp/tools_list/update_message_labels).

## Out of scope

- Classification rules, taxonomy, and which mail may be archived or marked read unattended.
- A thread-aware audit of missed replies.
- Delete, trash, spam, forwarding and settings other than filters.
- A second account or family profiles.
- OCR, PDF extraction, attachment execution.
- Deployment, which belongs to homelab.
