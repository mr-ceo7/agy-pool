# agy-pool

Multi-account rotation, quota failover, and unified conversation management wrapper for Google's Antigravity CLI (`agy`), Anthropic's Claude Code (`claude`), and GitHub Copilot (`copilot`).

```
  ┌────────────────────────────────────────────────────────┐
  │                        agy-pool                        │
  └───────┬────────────────────────┬───────────────────────┘
          │ (Active Account)       │ (Failover on Quota)
          ▼                        ▼
    ┌───────────┐            ┌───────────┐
    │ Account A │            │ Account B │
    └─────┬─────┘            └─────┬─────┘
          │                        │
          └───────────┬────────────┘
                      ▼
     Shared Workspace & History Storage
```

---

## The Problem

When running complex development tasks, agent loops, or extensive refactors with modern AI developer CLIs, you quickly encounter hard account quota limits (`RESOURCE_EXHAUSTED` / `HTTP 429` / 5-hour & weekly limits).

Managing multiple accounts manually is frustrating because:
1. **Desktop Keyring Collisions:** CLIs hardcode desktop secret-service keyrings. Running multiple accounts on the same desktop causes the system keyring to overwrite tokens or silently hijack sessions.
2. **Broken Terminal Image Uploads:** Naive headless workarounds (such as injecting fake SSH environment variables) trick tools into believing the session is remote, disabling local clipboard image pasting (`wl-paste` / `xclip`) with `Image upload is not supported in this terminal`.
3. **Fragmented History:** Isolating configuration directories isolates conversation databases and project state, breaking session resumption across account switches.

`agy-pool` resolves all three issues.

---

## Features

* **Multi-CLI Tool Rotation:** Seamlessly manages account pools and quota failovers for Google Antigravity (`agy`), Anthropic Claude Code (`claude` / `claude2`), and GitHub Copilot (`copilot`).
* **Automatic Quota Failover:** Runs non-interactive prompts with live output streaming. If an account hits a quota limit, it enters cooldown, rolls over to the next healthy account, and retries automatically.
* **Unified Conversation History:** Transcripts, SQLite databases, `/resume` indexes, and prompt history are automatically symlinked to a central store. Switching accounts never breaks session continuity.
* **Zero Keyring Collisions:** Neutralizes desktop SecretService D-Bus hijacking so each account strictly loads its own token from its isolated sandbox.
* **Native Clipboard & Image Paste:** Avoids fake SSH environments, keeping Wayland (`wl-paste`) and X11 (`xclip`) active for pasting images directly into the terminal.
* **Zero Dependencies:** Written in 100% standard-library Python 3. No external pip packages required.

---

## Quick Install

### One-Line Shell Installer (Recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/mr-ceo7/agy-pool/main/install.sh | bash
```

### Via Pip / Source

```bash
git clone https://github.com/mr-ceo7/agy-pool.git
cd agy-pool
pip install -e .
```

---

## Quickstart

### 1. Import Your Current Account
If you already logged into `agy` on your machine, import your active token into the pool:

```bash
agy-pool import primary
```

### 2. Add Additional Accounts
Add second or third accounts with an isolated browser login:

```bash
agy-pool add backup_account
```
Follow the OAuth URL printed in the terminal, log into your other Google account, and paste the code back.

### 3. Check Pool Status

```bash
agy-pool status
```

Output:
```text
====================================================================
  AGY ACCOUNT ROTATION POOL
====================================================================

★ [ACTIVE] #1: primary
   Email:    dev@example.com
   Status:   READY
   Requests: 14 succeeded, 0 failed
   Home Dir: ~/.gemini_accounts/accounts/primary

  [READY]  #2: backup_account
   Email:    backup@example.com
   Status:   READY
   Requests: 0 succeeded, 0 failed
   Home Dir: ~/.gemini_accounts/accounts/backup_account
====================================================================
```

### 4. Run Antigravity

Launch interactive session:
```bash
agy-pool
```

Or run non-interactive prompt with automatic quota retry:
```bash
agy-pool -p "Audit and refactor auth routes"
```

All standard `agy` arguments (`-c`, `--conversation <id>`, `--model`, etc.) pass through transparently.

---

## Command Reference

| Command | Description |
| :--- | :--- |
| `agy-pool` | Launch interactive session under the active account |
| `agy-pool -p "<prompt>"` | Run prompt with automatic quota detection & failover |
| `agy-pool <flags>` | Pass any standard `agy` flags through directly |
| `agy-pool add <name>` | Authenticate and add a new Google account |
| `agy-pool import [name]` | Import existing local `~/.gemini/antigravity-cli` token |
| `agy-pool import-keyring [name]` | Import token from system desktop keyring (`secret-tool`) |
| `agy-pool status` | View pool accounts, active marker, and cooldown timers |
| `agy-pool switch [name]` | Switch active account manually |
| `agy-pool test [name]` | Probe registered accounts with a test prompt |
| `agy-pool update [--check]` | Check for updates or upgrade to the latest version |
| `agy-pool remove <name>` | Remove an account from the pool and purge its sandbox |

---

## Multi-CLI Tool Support

`agy-pool` extends account rotation and quota failover across other developer AI CLIs:

### Claude Code Support (`claude-pool` or `agy-pool claude`)
Isolates Claude accounts under `~/.claude_accounts/` while automatically symlinking shared project states, file history, and session context from `~/.claude`:

```bash
# View Claude pool status (auto-discovers ~/.claude and ~/.claude2 if present)
claude-pool status
# or
agy-pool claude status

# Switch between Claude accounts
claude-pool switch claude2

# Run non-interactive prompt with auto-failover on 5-hour/weekly limit
claude-pool -p "Run code review on src/"

# Launch interactive session under the active Claude account
claude-pool
```

### GitHub Copilot Support (`copilot-pool` or `agy-pool copilot`)
Isolates GitHub credentials and tokens under `~/.copilot_accounts/`:

```bash
copilot-pool status
copilot-pool switch backup
copilot-pool -p "Explain this function"
```

---

## Architecture & Storage

Accounts and tokens are stored in `~/.gemini_accounts/`:
```text
~/.gemini_accounts/
├── accounts.json          # Pool state, active index, cooldown timestamps
├── accounts.json.lock     # Process concurrency lock (fcntl)
└── accounts/
    ├── primary/
    │   └── .gemini/antigravity-cli/
    │       ├── antigravity-oauth-token   (Isolated OAuth token)
    │       ├── brain -> ~/.gemini/antigravity-cli/brain
    │       ├── conversations -> ~/.gemini/antigravity-cli/conversations
    │       └── conversation_summaries.db -> ~/.gemini/antigravity-cli/conversation_summaries.db
    └── backup_account/
        └── .gemini/antigravity-cli/
            └── ...
```

---

## License

MIT © [mr-ceo7](https://github.com/mr-ceo7)
