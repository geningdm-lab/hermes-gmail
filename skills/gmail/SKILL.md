---
name: gmail
description: Work the owner's Gmail through the hermes-gmail tools (mcp__gmail__*). Use for searching mail, reading messages and threads, changing labels, managing filters, writing drafts and sending them. Never read the OAuth document or call the Gmail API directly.
---

# Gmail through hermes-gmail

The `gmail` MCP server is the only way to the mailbox. It holds the credential; you never see a token, and you must not read the OAuth document from 1Password or call `gmail.googleapis.com` yourself.

## Reading

- `search` returns message IDs, not messages. Then `read_messages` with `format: metadata` for headers and snippets, or `full` for text.
- Every listing is bounded. When `complete` is false, say so or continue with `next_page_token`; never present a partial result as everything.
- `read_messages` puts each ID in `messages` or in `failed` with a category. Report failures instead of skipping them silently.
- Text marked `truncated` or `decode_failed` is incomplete. Say so when it matters.
- `read_thread` gives the latest messages with `direction` (sent or received) and the reply headers a reply needs.

## Changing the mailbox

- Label changes and filters are two steps. `modify_prepare`, `filter_prepare` or `filter_delete_prepare` return a `preview_id` and a `summary`; show the owner the summary, then call the matching `*_apply` with the `preview_id`. The owner is asked to approve the gate's own summary; if they decline, nothing is written and you may ask again only when they say so.
- An apply stops with `refused` when the messages or the filter changed since the preview, or when the preview expired (15 minutes) or was already used. Prepare again; never work around it.
- `label_create` returns an existing label of that name instead of creating a second one.
- `draft_put` with `thread_id` updates the thread's draft and sets the reply headers; it never sends.
- To send, write the draft first, then `send_prepare` and `send_apply`; the owner approves the recipients and subject. If `send_apply` ends `uncertain`, never send again: run `send_reconcile`, which answers `sent`, `not_sent` (prepare again if the owner still wants it) or `uncertain` (tell the owner).
- After `uncertain`, read the current state (labels, filters, the thread's drafts) before trying again.

## Errors

Each error message names a category after the tool name, for example `Error executing tool search: refused: ...`:

| Category | What to do |
|---|---|
| `refused` | the request was outside the gate's bounds, or the preview is stale; fix it or prepare again |
| `declined` | the owner did not approve; nothing was written |
| `gmail_quota` | wait and try later |
| `gmail_not_found` | the item is gone; for `history`, resync from a search |
| `vault_unavailable` | 1Password is unavailable or cooling down; tell the owner, do not retry in a loop |
| `oauth_invalid_grant` | the owner must reauthorize Google |
| `oauth_invalid_client`, `oauth_failed`, `gmail_scope`, `gmail_failed` | tell the owner the category; do not guess a fix |
| `uncertain` | a change may or may not have happened; read the state before doing anything else |
