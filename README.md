# codex-accounts

Small CLI tool to manage multiple Codex accounts and inspect their usage limits while keeping one shared Codex home and its sessions.

It can:

- List registered accounts and show which one is active
- Switch the active account without moving chats or other Codex data
- Show remaining 5-hour and weekly usage, reset times, and available usage resets
- Start an unstarted usage window with a short greeting

Account credentials are stored locally in `~/.codex/accounts/<name>.json`. Codex continues to use `~/.codex/auth.json` for the active account. The tool uses only the Python standard library and the Codex CLI; it does not send credentials to a separate service.

## Installation

Clone the repository:

```bash
gh repo clone endikallanomatxin/codex-accounts
cd codex-accounts
```

Install it into `~/.local/bin`:

```bash
install -Dm755 codex-accounts.py ~/.local/bin/codex-accounts
```

Make sure `~/.local/bin` is in your `PATH`. For Bash:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

For Zsh:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

## Set up accounts

After signing in to Codex, save the current credentials under a name:

```bash
mkdir -p ~/.codex/accounts
install -m600 ~/.codex/auth.json ~/.codex/accounts/personal.json
```

To add another account, sign out and sign in to Codex with that account, then save its `auth.json` under a different name:

```bash
codex logout
codex login
install -m600 ~/.codex/auth.json ~/.codex/accounts/work.json
```

Keep the files in `~/.codex/accounts` private. They contain login credentials; never commit or share them. Your Codex sessions stay in the same `~/.codex` directory.

If you previously used `codex-usage` with `~/.codex/auth-<name>.json`, the first `codex-accounts` command copies those files into `~/.codex/accounts/<name>.json`. Existing files are left in place, and names already present in `accounts/` are not overwritten.

## Usage

```bash
codex-accounts list
codex-accounts switch personal
codex-accounts usage
codex-accounts kick
codex-accounts kick personal
```

Useful usage options:

```bash
codex-accounts usage personal work
codex-accounts usage --all-buckets
codex-accounts usage --json
codex-accounts usage --verbose-errors
```

Switching saves the current `auth.json` into its registered account file before activating the target. If the current login is not registered, the tool first backs it up under `~/.codex/accounts/.untracked-auth-<timestamp>.json`.

Current Codex versions use a shared app-server daemon that keeps authentication in memory after the TUI exits. `codex-accounts switch NAME` therefore stops that daemon before replacing `auth.json`. The next `codex` or `codex resume` launch starts a fresh daemon and loads the selected account. Stopping the daemon disconnects any Codex clients that are still attached, so switch accounts between active turns. If the daemon cannot be stopped, the switch aborts before changing credentials rather than leaving disk and in-memory authentication out of sync.

`kick` checks the 5-hour and weekly windows for each account, or only the named account. If either window reports 0% used and no reset time, it sends Codex `Hi! Reply with only 'Hi'.` through a non-interactive, read-only, ephemeral session. Each greeting runs in an isolated temporary `CODEX_HOME`, so it does not replace the shared `auth.json` or interfere with a running daemon. Refreshed credentials are saved back to that account's file. Missing or ambiguous window data is skipped. Each greeting consumes a small amount of that account's usage.

Run `codex-accounts --help` or `codex-accounts usage --help` for all options. You can choose a different Codex home with `--codex-home` or a different Codex executable with `--codex-bin`.

## Requirements

- Python 3
- Codex CLI available in `PATH`
- At least one Codex login saved in `~/.codex/accounts/`
