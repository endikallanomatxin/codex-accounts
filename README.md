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

The switch takes effect when Codex starts a new process. An already running Codex session keeps the account it started with, even if you continue that chat after changing `auth.json`. It can also refresh its old credentials and overwrite the new `auth.json`. Quit Codex before switching accounts, then resume the conversation under the new login. For the CLI, exit the old session, run `codex-accounts switch NAME` in your shell, then run `codex resume`. Check the active login on disk with `codex-accounts list`.

`kick` checks the 5-hour and weekly windows for each account, or only the named account. If either window reports 0% used and no reset time, it briefly switches to that account and sends Codex `Hi! Reply with only 'Hi'.` through a non-interactive, read-only, ephemeral session. It then restores the login that was active before the command. Missing or ambiguous window data is skipped. Each greeting consumes a small amount of that account's usage.

Run `codex-accounts --help` or `codex-accounts usage --help` for all options. You can choose a different Codex home with `--codex-home` or a different Codex executable with `--codex-bin`.

## Requirements

- Python 3
- Codex CLI available in `PATH`
- At least one Codex login saved in `~/.codex/accounts/`
