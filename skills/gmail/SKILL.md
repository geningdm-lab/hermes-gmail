---
name: gmail
description: Read the owner's Gmail through the hermes-gmail tools (mcp__gmail__*). Use for searching mail, reading messages and threads, and checking labels, filters and drafts. Never read the OAuth document or call the Gmail API directly.
---

# Gmail through hermes-gmail

The `gmail` MCP server is the only way to the mailbox. It holds the credential; you never see a token, and you must not read the OAuth document from 1Password or call `gmail.googleapis.com` yourself.

## Reading

- `search` returns message IDs, not messages. Then `read_messages` with `format: metadata` for headers and snippets, or `full` for text.
- Every listing is bounded. When `complete` is false, say so or continue with `next_page_token`; never present a partial result as everything.
- `read_messages` puts each ID in `messages` or in `failed` with a category. Report failures instead of skipping them silently.
- Text marked `truncated` or `decode_failed` is incomplete. Say so when it matters.
- `read_thread` gives the latest messages with `direction` (sent or received) and the reply headers a reply needs.

## Errors

Each error message names a category after the tool name, for example `Error executing tool search: refused: ...`:

| Category | What to do |
|---|---|
| `refused` | the request was outside the gate's bounds; fix it |
| `gmail_quota` | wait and try later |
| `gmail_not_found` | the item is gone; for `history`, resync from a search |
| `vault_unavailable` | 1Password is unavailable or cooling down; tell the owner, do not retry in a loop |
| `oauth_invalid_grant` | the owner must reauthorize Google |
| `oauth_invalid_client`, `oauth_failed`, `gmail_scope`, `gmail_failed` | tell the owner the category; do not guess a fix |
| `uncertain` | a change may or may not have happened; read the state before doing anything else |
