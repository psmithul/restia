<p align="center">
  <img src="docs/restia-wordmark.png" alt="Restia" width="238">
</p>

<p align="center">
  A self-hosted AI workspace for chat, agents, research, documents, email, notes, calendar, and local model workflows.
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="docs/setup.md">Setup Guide</a> ·
  <a href="#features">Features</a> ·
  <a href="CONTRIBUTING.md">Contributing</a> ·
  <a href="ROADMAP.md">Roadmap</a> ·
  <a href="SECURITY.md">Security</a>
</p>

<p align="center">
  <img src="docs/restia-browser.jpg" alt="Restia interface">
</p>

---

## Quick Start

> `dev` is the default branch and gets the newest changes first. Use [`main`](https://github.com/psmithul/restia/tree/main) if you want the more curated branch.

```bash
git clone https://github.com/psmithul/restia.git
cd restia
cp .env.example .env
docker compose pull
docker compose up -d --no-build
```

Open `http://localhost:7000` when the containers are healthy. The first admin
password is printed in the container logs — find it with `docker compose logs`.

Native installs, GPU notes, Windows/macOS instructions, HTTPS, and configuration live in the [setup guide](docs/setup.md).

### Updating

Updates preserve your `data/` and `logs/` directories: run `./update.sh` on
Linux/macOS or `update_windows.bat` on Windows. Each published GitHub Release
builds `ghcr.io/psmithul/restia:latest` plus a versioned tag, so you pull the
same verified multi-architecture image instead of rebuilding stale source.

## Features

- **Chat + Agents** — local/API models, tools, MCP, files, shell, skills, and memory.
- **Cookbook** — hardware-aware model recommendations, downloads, and serving.
- **Deep Research** — multi-step web research with source reading and report generation.
- **Compare** — blind side-by-side model testing and synthesis.
- **Documents** — writing-first editor with AI edits, suggestions, Markdown, HTML, CSV, and syntax highlighting.
- **Messages** — WhatsApp-style direct messages between accounts with real-time delivery (SSE), typing indicators, read receipts, emoji reactions, replies, and message editing/deletion — plus Home Link developer chat and Telegram.
- **Email** — IMAP/SMTP inbox with triage, tags, summaries, reminders, and reply drafts.
- **Notes, Tasks + Calendar** — reminders, todos, scheduled agent tasks, and CalDAV sync.
- **Extras** — gallery/image editor, themes, uploads, web search, presets, sessions, update checker, and 2FA.

## Chat with the developer

Every Restia install ships with **Home Link**: once you've built the app and
logged in, open **Messages** (the sidebar icon) → **✎ New message** → pick the
contact tagged `dev`. Choose a handle, send the request, and once the
developer approves it the thread opens — chat directly from your own
instance, with replies landing back in the same thread. You never get (or
need) an account on the developer's server: your instance registers with the
home server (`app.restia.dev`), stores its token locally encrypted, and that
token unlocks exactly one conversation — nothing else.

Privacy notes: nothing is sent anywhere until you pick a handle and hit
Connect, and only the messages you type in that one thread leave your
instance — your accounts, keys, and data stay on your device. Set
`RESTIA_HOME_SERVER=` (empty) in `.env` to remove the contact entirely, or
point it at a friend's instance that has `LINK_HUB_ENABLED=true` to chat with
them instead; as a hub you approve or block each request from the Messages
UI.

## Demo

A full hover-to-play tour lives on the landing page: [`docs/index.html`](docs/index.html).

## Contributing

Help is welcome. The best entry points are fresh-install testing, provider setup bugs, mobile/editor polish, docs, and small focused refactors. See [CONTRIBUTING.md](CONTRIBUTING.md) and [ROADMAP.md](ROADMAP.md).

## Security

Restia is a self-hosted workspace with powerful local tools. Keep auth enabled, keep private data out of Git, and do not expose raw model/service ports publicly. See [SECURITY.md](SECURITY.md) for the security policy and the [setup guide](docs/setup.md#security-notes) for deployment details.

## Star History

<a href="https://www.star-history.com/?repos=psmithul%2Frestia&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=psmithul/restia&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=psmithul/restia&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=psmithul/restia&type=date&legend=top-left" />
 </picture>
</a>

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE) and [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md).
