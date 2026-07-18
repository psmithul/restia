# Read-only Slack and Twilio polling

Restia can project Slack conversations plus Twilio SMS and call logs into the
owner-scoped Communications Hub. These pollers are independent database-leased
runtime workers; disabling user Tasks does not stop them.

## Slack

In **Settings -> Integrations**, add the **Slack (read-only)** preset and store
a least-privilege Slack OAuth token. The preset is locked to:

- `https://slack.com`
- HTTP `GET` only
- `/api` paths only

The worker reads the authenticated identity, visible conversations and bounded
conversation history. Per-channel timestamps are kept in an encrypted SQL
cursor. Restia projects the messages through the canonical Life ingestion
boundary and never exposes a message-posting operation.

## Twilio

Add the **Twilio SMS and calls (read-only)** preset. Enter the credential as:

```text
ACCOUNT_SID:AUTH_TOKEN
```

The account SID must use Twilio's `AC` format. The preset is locked to
`https://api.twilio.com`, HTTP `GET` and the account's 2010 API path. The worker
reads bounded SMS and call-log pages, records encrypted high-water timestamps,
and projects them as `sms` and `call` communications. It cannot create an SMS,
place a call or reply.

## Runtime controls

```dotenv
RESTIA_INPROCESS_COMMUNICATION_POLLING=1
RESTIA_COMMUNICATION_POLL_SECONDS=60
RESTIA_COMMUNICATION_HTTP_TIMEOUT_SECONDS=30
```

Each integration is bound to its immutable Restia account. Retries converge on
provider message IDs, cursor state is encrypted, response sizes and provider
counts are bounded, and safe health codes appear in Security Posture. Provider
credentials and response bodies are never written to worker logs.
