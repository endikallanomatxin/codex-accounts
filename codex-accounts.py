#!/usr/bin/env python3
"""
codex-accounts

Manage multiple Codex ChatGPT accounts while keeping one shared CODEX_HOME
(and therefore shared sessions/chats).

Layout:
    ~/.codex/
      auth.json                 # currently active account, used by Codex
      accounts/
        ganora.json             # canonical state for each account
        osaba.json
        ...

Commands:
    codex-accounts list
    codex-accounts switch NAME
    codex-accounts usage
    codex-accounts kick [NAME]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

FIVE_HOURS_MIN = 5 * 60
ONE_WEEK_MIN = 7 * 24 * 60


@dataclass(frozen=True)
class Account:
    name: str
    path: Path


@dataclass
class QueryResult:
    account: Account
    result: dict[str, Any] | None = None
    error: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-accounts",
        description="Manage multiple Codex accounts with shared chats/sessions.",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser(),
        help="Shared Codex home (default: CODEX_HOME or ~/.codex).",
    )
    parser.add_argument(
        "--codex-bin",
        default="codex",
        help="Codex executable (default: codex).",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List registered accounts and show the active one.")

    p_switch = sub.add_parser(
        "switch",
        help="Save the current account state and activate another account.",
    )
    p_switch.add_argument("name", help="Account name from ~/.codex/accounts/<name>.json")

    p_usage = sub.add_parser(
        "usage",
        help="Show remaining 5-hour/weekly quota and reset credits.",
    )
    p_usage.add_argument(
        "names",
        nargs="*",
        help="Optional account names. If omitted, query all accounts.",
    )
    p_usage.add_argument(
        "--jobs",
        type=int,
        default=4,
        help="Maximum parallel account queries (default: 4).",
    )
    p_usage.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Timeout per app-server request in seconds (default: 20).",
    )
    p_usage.add_argument(
        "--all-buckets",
        action="store_true",
        help="Also show extra quota buckets returned by Codex.",
    )
    p_usage.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable JSON output.",
    )
    p_usage.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI formatting.",
    )
    p_usage.add_argument(
        "--verbose-errors",
        action="store_true",
        help="Show full app-server stderr for failed accounts.",
    )
    p_kick = sub.add_parser(
        "kick",
        help="Start idle usage windows with a brief Codex greeting.",
    )
    p_kick.add_argument("name", nargs="?", help="Account to check (default: all accounts).")
    return parser.parse_args()


# ---------- account storage ----------

def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _find_account_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("account_id", "accountId", "chatgpt_account_id", "chatgptAccountId"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for child in value.values():
            found = _find_account_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_account_id(child)
            if found:
                return found
    return None


def auth_account_id(path: Path) -> str | None:
    try:
        return _find_account_id(load_json(path))
    except Exception:
        return None


def file_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def same_account(a: Path, b: Path) -> bool:
    a_id = auth_account_id(a)
    b_id = auth_account_id(b)
    if a_id and b_id:
        return a_id == b_id
    a_hash = file_digest(a)
    b_hash = file_digest(b)
    return bool(a_hash and b_hash and a_hash == b_hash)


def accounts_dir(home: Path) -> Path:
    return home / "accounts"


def discover_accounts(home: Path) -> list[Account]:
    root = accounts_dir(home)
    if not root.is_dir():
        return []
    accounts: list[Account] = []
    for path in sorted(root.glob("*.json")):
        if path.is_file() and not path.name.startswith("."):
            accounts.append(Account(path.stem, path))
    return accounts


def atomic_copy(src: Path, dst: Path) -> None:
    """Copy an auth file atomically and keep it private (0600)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{dst.name}.", dir=str(dst.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, dst)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def migrate_legacy_accounts(home: Path) -> list[str]:
    """
    Soft migration from ~/.codex/auth-foo.json to ~/.codex/accounts/foo.json.
    Old files are deliberately left untouched.
    """
    root = accounts_dir(home)
    root.mkdir(parents=True, exist_ok=True)
    migrated: list[str] = []
    for legacy in sorted(home.glob("auth-*.json")):
        if not legacy.is_file():
            continue
        name = legacy.name[len("auth-") : -len(".json")]
        if not name:
            continue
        dest = root / f"{name}.json"
        if dest.exists():
            continue
        atomic_copy(legacy, dest)
        migrated.append(name)
    return migrated


def find_active_account(home: Path, accounts: list[Account]) -> Account | None:
    active_auth = home / "auth.json"
    if not active_auth.is_file():
        return None
    active_id = auth_account_id(active_auth)
    if active_id:
        matches = [a for a in accounts if auth_account_id(a.path) == active_id]
        if len(matches) == 1:
            return matches[0]
    for account in accounts:
        if same_account(active_auth, account.path):
            return account
    return None


def backup_untracked_auth(home: Path) -> Path:
    root = accounts_dir(home)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = root / f".untracked-auth-{stamp}.json"
    atomic_copy(home / "auth.json", path)
    return path


def sync_active_to_canonical(home: Path, accounts: list[Account]) -> Account | None:
    """Persist the freshest ~/.codex/auth.json into its canonical account file."""
    live = home / "auth.json"
    if not live.is_file():
        return None
    active = find_active_account(home, accounts)
    if active is not None:
        atomic_copy(live, active.path)
    return active


def stop_codex_daemon(home: Path, codex_bin: str) -> str | None:
    """
    Stop Codex's shared app-server before replacing auth.json.

    Current Codex versions cache authentication in the long-lived daemon, so
    changing auth.json alone does not switch the account used by new TUI clients.
    """
    executable = shutil.which(codex_bin)
    if executable is None:
        raise RuntimeError(f"{codex_bin!r} not found in PATH; cannot safely switch accounts")

    env = os.environ.copy()
    env["CODEX_HOME"] = str(home)
    try:
        result = subprocess.run(
            [executable, "app-server", "daemon", "stop"],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("timed out stopping the Codex background server") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail.splitlines()[-1]}" if detail else ""
        raise RuntimeError(f"failed to stop the Codex background server{suffix}")

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    status = payload.get("status") if isinstance(payload, dict) else None
    return status if isinstance(status, str) else None


# ---------- app-server usage query ----------

def write_jsonl(proc: subprocess.Popen[str], obj: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(obj, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def read_response(proc: subprocess.Popen[str], request_id: int, timeout: float) -> dict[str, Any]:
    assert proc.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for response id={request_id}")
            if not selector.select(remaining):
                raise TimeoutError(f"timed out waiting for response id={request_id}")
            line = proc.stdout.readline()
            if line == "":
                rc = proc.poll()
                raise RuntimeError(f"codex app-server closed stdout" + (f" (exit {rc})" if rc is not None else ""))
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") != request_id:
                continue
            if "error" in msg:
                err = msg["error"]
                detail = err.get("message") if isinstance(err, dict) else str(err)
                raise RuntimeError(str(detail or err))
            result = msg.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"unexpected response: {result!r}")
            return result
    finally:
        selector.close()


def tail_text(path: Path, max_chars: int = 5000) -> str:
    try:
        return path.read_text(errors="replace")[-max_chars:].strip()
    except OSError:
        return ""


def persist_temp_auth_if_replaced(temp_auth: Path, source_auth: Path) -> None:
    """
    Normally temp_home/auth.json is a symlink to source_auth. If Codex replaces
    auth.json atomically, the symlink can become a regular file. Copy that refreshed
    file back so token rotation is not lost.
    """
    try:
        if temp_auth.is_symlink():
            return
        if temp_auth.is_file():
            atomic_copy(temp_auth, source_auth)
    except OSError:
        pass


def compact_error(message: str) -> str:
    lower = message.lower()
    if "refresh token was already used" in lower:
        return "saved credentials are stale; switch/login to this account once"
    if "token_expired" in lower or "authentication token is expired" in lower or "access token is expired" in lower:
        return "access token expired; refresh/login required"
    if "401 unauthorized" in lower:
        return "authentication rejected"
    first = message.strip().splitlines()[0] if message.strip() else "unknown error"
    return first[:240]


def query_account(account: Account, source_auth: Path, codex_bin: str, timeout: float) -> QueryResult:
    proc: subprocess.Popen[str] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix=f"codex-accounts-{account.name}-") as td:
            temp_home = Path(td)
            temp_auth = temp_home / "auth.json"
            stderr_path = temp_home / "app-server.stderr"
            os.symlink(source_auth.resolve(), temp_auth)
            (temp_home / "config.toml").write_text('cli_auth_credentials_store = "file"\n', encoding="utf-8")
            env = os.environ.copy()
            env["CODEX_HOME"] = str(temp_home)
            env.setdefault("RUST_LOG", "warn")

            with stderr_path.open("w+", encoding="utf-8") as stderr_file:
                try:
                    proc = subprocess.Popen(
                        [codex_bin, "app-server", "--stdio"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=stderr_file,
                        text=True,
                        bufsize=1,
                        env=env,
                    )
                    write_jsonl(proc, {
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "clientInfo": {"name": "codex-accounts", "title": "Codex Accounts", "version": "2.1.0"},
                            "capabilities": {"experimentalApi": False},
                        },
                    })
                    read_response(proc, 1, timeout)
                    write_jsonl(proc, {"method": "initialized", "params": {}})
                    write_jsonl(proc, {"id": 2, "method": "account/rateLimits/read", "params": {}})
                    return QueryResult(account=account, result=read_response(proc, 2, timeout))
                except Exception as exc:
                    stderr_file.flush()
                    details = tail_text(stderr_path)
                    message = str(exc)
                    if details:
                        message += "\n---DETAILS---\n" + details
                    return QueryResult(account=account, error=message)
                finally:
                    if proc is not None:
                        try:
                            if proc.stdin:
                                proc.stdin.close()
                        except Exception:
                            pass
                        if proc.poll() is None:
                            proc.terminate()
                            try:
                                proc.wait(timeout=1.5)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                    persist_temp_auth_if_replaced(temp_auth, source_auth)
    except Exception as exc:
        return QueryResult(account=account, error=str(exc))


def run_account_greeting(account: Account, codex_bin: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    """
    Run the kick greeting in an isolated CODEX_HOME.

    This avoids temporarily replacing the shared auth.json while a Codex daemon
    may still be running. Any refreshed credentials are copied back afterward.
    """
    with tempfile.TemporaryDirectory(prefix=f"codex-accounts-kick-{account.name}-") as td:
        temp_home = Path(td)
        temp_auth = temp_home / "auth.json"
        atomic_copy(account.path, temp_auth)
        (temp_home / "config.toml").write_text(
            'cli_auth_credentials_store = "file"\n',
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["CODEX_HOME"] = str(temp_home)
        try:
            return subprocess.run(
                [
                    codex_bin,
                    "exec",
                    "--ephemeral",
                    "--sandbox",
                    "read-only",
                    "--skip-git-repo-check",
                    "-C",
                    str(temp_home),
                    "Hi! Reply with only 'Hi'.",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        finally:
            if temp_auth.is_file():
                atomic_copy(temp_auth, account.path)


# ---------- formatting ----------

def to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).astimezone()
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone()
        except ValueError:
            return None
    return None


def human_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours and len(parts) < 2:
        parts.append(f"{hours}h")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "<1m"


def human_delta(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "unknown"
    seconds = int((dt - now).total_seconds())
    if seconds <= 0:
        age = abs(seconds)
        return "now" if age < 60 else f"{human_duration(age)} ago"
    return f"in {human_duration(seconds)}"


def format_dt(dt: datetime | None) -> str:
    if dt is None:
        return "unknown"
    now = datetime.now().astimezone()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if dt.year == now.year:
        return dt.strftime("%d %b %H:%M")
    return dt.strftime("%Y-%m-%d %H:%M")


def progress_bar(remaining: float, width: int = 14) -> str:
    remaining = min(100.0, max(0.0, remaining))
    filled = round(width * remaining / 100.0)
    return "█" * filled + "░" * (width - filled)


def extract_main_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        codex = by_id.get("codex")
        if isinstance(codex, dict):
            return codex
    snapshot = result.get("rateLimits")
    return snapshot if isinstance(snapshot, dict) else {}


def iter_windows(snapshot: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for key in ("primary", "secondary"):
        value = snapshot.get(key)
        if isinstance(value, dict):
            yield value


def choose_windows(snapshot: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    five = None
    week = None
    for window in iter_windows(snapshot):
        minutes = window.get("windowDurationMins")
        if minutes in (FIVE_HOURS_MIN - 1, FIVE_HOURS_MIN):
            five = window
        elif minutes in (ONE_WEEK_MIN - 1, ONE_WEEK_MIN):
            week = window
    return five, week


def window_not_started(window: dict[str, Any] | None) -> bool:
    if not window or window.get("resetsAt") is not None:
        return False
    try:
        return float(window["usedPercent"]) == 0
    except (KeyError, TypeError, ValueError):
        return False


def window_label(minutes: Any) -> str:
    if minutes in (FIVE_HOURS_MIN - 1, FIVE_HOURS_MIN):
        return "5 h"
    if minutes in (ONE_WEEK_MIN - 1, ONE_WEEK_MIN):
        return "week"
    if isinstance(minutes, (int, float)):
        mins = int(minutes)
        if mins % (7 * 24 * 60) == 0:
            return f"{mins // (7 * 24 * 60)}w"
        if mins % (24 * 60) == 0:
            return f"{mins // (24 * 60)}d"
        if mins % 60 == 0:
            return f"{mins // 60}h"
        return f"{mins}m"
    return "quota"


class Colors:
    def __init__(self, enabled: bool):
        self.enabled = enabled
    def wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text
    def bold(self, text: str) -> str:
        return self.wrap("1", text)
    def dim(self, text: str) -> str:
        return self.wrap("2", text)
    def red(self, text: str) -> str:
        return self.wrap("31", text)


def ansi_enabled(no_color: bool) -> bool:
    return not no_color and "NO_COLOR" not in os.environ and sys.stdout.isatty()


def print_window(label: str, window: dict[str, Any] | None, colors: Colors, now: datetime) -> None:
    if not window:
        print(f"  {label:<5} {colors.dim('not reported')}")
        return
    try:
        used = float(window.get("usedPercent"))
    except (TypeError, ValueError):
        print(f"  {label:<5} {colors.dim('invalid usage data')}")
        return
    remaining = min(100.0, max(0.0, 100.0 - used))
    reset = to_datetime(window.get("resetsAt"))
    reset_text = f"reset {human_delta(reset, now)} · {format_dt(reset)}" if reset else "reset unknown"
    print(f"  {label:<5} {progress_bar(remaining)}  {remaining:g}% left  {colors.dim(reset_text)}")


def print_reset_credits(result: dict[str, Any], colors: Colors) -> None:
    reset_info = result.get("rateLimitResetCredits")
    if not isinstance(reset_info, dict):
        print(f"  resets {colors.dim('not reported')}")
        return
    try:
        count = int(reset_info.get("availableCount", 0))
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        print("  resets 0")
        return
    expiries: list[datetime] = []
    credits = reset_info.get("credits")
    if isinstance(credits, list):
        for credit in credits:
            if not isinstance(credit, dict) or str(credit.get("status", "")) not in ("available", "redeeming"):
                continue
            dt = to_datetime(credit.get("expiresAt"))
            if dt:
                expiries.append(dt)
    if expiries:
        print(f"  resets {count}  {colors.dim('expire: ' + ' · '.join(format_dt(dt) for dt in expiries))}")
    else:
        print(f"  resets {count}")


def print_extra_buckets(result: dict[str, Any], colors: Colors, now: datetime) -> None:
    by_id = result.get("rateLimitsByLimitId")
    if not isinstance(by_id, dict):
        return
    extras = [(k, v) for k, v in by_id.items() if k != "codex" and isinstance(v, dict)]
    for bucket_id, snapshot in extras:
        print(f"  {colors.dim(str(snapshot.get('limitName') or bucket_id))}")
        for window in iter_windows(snapshot):
            try:
                remaining = max(0.0, 100.0 - float(window.get("usedPercent")))
            except (TypeError, ValueError):
                continue
            reset = to_datetime(window.get("resetsAt"))
            print(f"    {window_label(window.get('windowDurationMins')):<5} {remaining:g}% left · {human_delta(reset, now)}")


def print_usage_account(item: QueryResult, *, active_name: str | None, colors: Colors, all_buckets: bool, verbose_errors: bool) -> None:
    active = item.account.name == active_name
    active_tag = " [ACTIVE]" if active else ""
    print()
    if item.error:
        compact, sep, details = item.error.partition("\n---DETAILS---\n")
        print(colors.bold(item.account.name + active_tag))
        print(f"  {colors.red('ERROR')} {compact_error(compact)}")
        if verbose_errors and sep:
            for line in details.splitlines():
                print("    " + colors.dim(line))
        return
    assert item.result is not None
    snapshot = extract_main_snapshot(item.result)
    plan = snapshot.get("planType")
    plan_tag = f" [{str(plan).upper()}]" if plan else ""
    blocked = " blocked" if item.result.get("ordinaryUsageAllowed") is False else ""
    print(colors.bold(f"{item.account.name}{plan_tag}{active_tag}{blocked}"))
    now = datetime.now().astimezone()
    five, week = choose_windows(snapshot)
    print_window("5 h", five, colors, now)
    print_window("week", week, colors, now)
    known = {id(x) for x in (five, week) if x is not None}
    for window in iter_windows(snapshot):
        if id(window) not in known:
            print_window(window_label(window.get("windowDurationMins")), window, colors, now)
    print_reset_credits(item.result, colors)
    if all_buckets:
        print_extra_buckets(item.result, colors, now)


# ---------- commands ----------

def ensure_home(args: argparse.Namespace) -> Path:
    home = args.codex_home.expanduser().resolve()
    if not home.is_dir():
        raise RuntimeError(f"Codex home does not exist: {home}")
    migrated = migrate_legacy_accounts(home)
    if migrated and sys.stderr.isatty():
        print("Imported legacy accounts: " + ", ".join(migrated), file=sys.stderr)
    return home


def command_list(args: argparse.Namespace) -> int:
    home = ensure_home(args)
    accounts = discover_accounts(home)
    if not accounts:
        print("No accounts registered.")
        print(f"Add account files under {accounts_dir(home)}/<name>.json")
        return 1
    active = find_active_account(home, accounts)
    active_name = active.name if active else None
    for account in accounts:
        print(f"{account.name}{'  [ACTIVE]' if account.name == active_name else ''}")
    if active is None and (home / "auth.json").is_file():
        print("\nCurrent auth.json is not registered in accounts/.", file=sys.stderr)
    return 0


def command_switch(args: argparse.Namespace) -> int:
    home = ensure_home(args)
    accounts = discover_accounts(home)
    by_name = {a.name: a for a in accounts}
    target = by_name.get(args.name)
    if target is None:
        print(f"error: unknown account: {args.name}", file=sys.stderr)
        if accounts:
            print("available: " + ", ".join(a.name for a in accounts), file=sys.stderr)
        return 2

    # Codex now keeps auth in a shared background app-server. Stop it before
    # touching auth.json so it cannot keep serving or refresh the previous login.
    daemon_status = stop_codex_daemon(home, args.codex_bin)

    live = home / "auth.json"
    active = find_active_account(home, accounts)
    same_account_on_disk = active is not None and active.name == target.name
    if live.is_file():
        if active is not None:
            atomic_copy(live, active.path)
        else:
            backup = backup_untracked_auth(home)
            print(f"warning: current auth.json was unregistered; backed up to {backup}", file=sys.stderr)

    atomic_copy(target.path, live)
    if same_account_on_disk:
        print(f"Already using {target.name} on disk.")
    else:
        print(f"Switched to {target.name}.")
    if daemon_status == "stopped":
        print("Stopped the shared Codex background server; the next Codex launch will reload this account.")
    return 0


def usage_json(results: list[QueryResult], active_name: str | None) -> str:
    data: dict[str, Any] = {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "activeAccount": active_name,
        "accounts": {},
    }
    for item in results:
        entry: dict[str, Any] = {"active": item.account.name == active_name, "ok": item.error is None}
        if item.error:
            entry["error"] = compact_error(item.error)
        else:
            entry["rateLimits"] = item.result
        data["accounts"][item.account.name] = entry
    return json.dumps(data, indent=2, ensure_ascii=False)


def command_usage(args: argparse.Namespace) -> int:
    home = ensure_home(args)
    codex_bin = shutil.which(args.codex_bin)
    if codex_bin is None:
        print(f"error: {args.codex_bin!r} not found in PATH", file=sys.stderr)
        return 127
    if args.jobs < 1 or args.timeout <= 0:
        print("error: --jobs must be >= 1 and --timeout > 0", file=sys.stderr)
        return 2

    accounts = discover_accounts(home)
    if not accounts:
        print("error: no accounts registered", file=sys.stderr)
        return 2

    # Save any token rotation from the active Codex account before querying.
    active_before = sync_active_to_canonical(home, accounts)
    active_name = active_before.name if active_before else None

    if args.names:
        wanted = set(args.names)
        known = {a.name for a in accounts}
        missing = sorted(wanted - known)
        if missing:
            print("error: unknown account(s): " + ", ".join(missing), file=sys.stderr)
            return 2
        accounts = [a for a in accounts if a.name in wanted]

    live_auth = home / "auth.json"
    def source_for(account: Account) -> Path:
        if account.name == active_name and live_auth.is_file():
            return live_auth
        return account.path

    workers = min(args.jobs, len(accounts))
    results_by_name: dict[str, QueryResult] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(query_account, account, source_for(account), codex_bin, args.timeout): account
            for account in accounts
        }
        for future in concurrent.futures.as_completed(futures):
            account = futures[future]
            try:
                results_by_name[account.name] = future.result()
            except Exception as exc:
                results_by_name[account.name] = QueryResult(account=account, error=str(exc))

    results = [results_by_name[a.name] for a in accounts]

    # If the active usage query refreshed auth.json, persist it canonically too.
    sync_active_to_canonical(home, discover_accounts(home))

    if args.json:
        print(usage_json(results, active_name))
    else:
        colors = Colors(ansi_enabled(args.no_color))
        print(colors.bold("Codex accounts usage"))
        for item in results:
            print_usage_account(
                item,
                active_name=active_name,
                colors=colors,
                all_buckets=args.all_buckets,
                verbose_errors=args.verbose_errors,
            )
    return 1 if any(item.error for item in results) else 0


def command_kick(args: argparse.Namespace) -> int:
    home = ensure_home(args)
    codex_bin = shutil.which(args.codex_bin)
    if codex_bin is None:
        print(f"error: {args.codex_bin!r} not found in PATH", file=sys.stderr)
        return 127
    accounts = discover_accounts(home)
    if args.name:
        accounts = [account for account in accounts if account.name == args.name]
        if not accounts:
            print(f"error: unknown account: {args.name}", file=sys.stderr)
            return 2
    elif not accounts:
        print("error: no accounts registered", file=sys.stderr)
        return 2

    # Persist any token rotation for the active account once. From here on each
    # account is queried and greeted through an isolated CODEX_HOME.
    sync_active_to_canonical(home, discover_accounts(home))

    failures = 0
    for account in accounts:
        item = query_account(account, account.path, codex_bin, 20.0)
        if item.error:
            print(f"{account.name}: usage check failed: {compact_error(item.error)}", file=sys.stderr)
            failures += 1
            continue
        assert item.result is not None
        five, week = choose_windows(extract_main_snapshot(item.result))
        idle = [label for label, window in (("5 h", five), ("week", week)) if window_not_started(window)]
        if not idle:
            print(f"{account.name}: no unstarted window reported")
            continue
        try:
            result = run_account_greeting(account, codex_bin)
            if result.returncode:
                detail = (result.stderr or result.stdout).strip().splitlines()
                print(f"{account.name}: greeting failed: {detail[-1] if detail else result.returncode}", file=sys.stderr)
                failures += 1
            else:
                print(f"{account.name}: greeted (unstarted: {', '.join(idle)})")
        except subprocess.TimeoutExpired:
            print(f"{account.name}: greeting timed out", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


def main() -> int:
    args = parse_args()
    try:
        if args.command == "list":
            return command_list(args)
        if args.command == "switch":
            return command_switch(args)
        if args.command == "usage":
            return command_usage(args)
        if args.command == "kick":
            return command_kick(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
