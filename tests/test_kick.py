import argparse
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "codex-accounts.py"
spec = importlib.util.spec_from_file_location("codex_accounts", SCRIPT)
accounts = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = accounts
spec.loader.exec_module(accounts)


class KickTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name)
        (self.home / "accounts").mkdir()
        self.auth = self.home / "auth.json"
        self.auth.write_text(json.dumps({"tokens": {"account_id": "original"}}))
        for name in ("original", "other"):
            (self.home / "accounts" / f"{name}.json").write_text(
                json.dumps({"tokens": {"account_id": name}})
            )

    def run_kick(self, name, snapshots):
        args = argparse.Namespace(codex_home=self.home, codex_bin="codex", name=name)
        seen = []

        def query(account, *_):
            return accounts.QueryResult(account, result={"rateLimits": snapshots[account.name]})

        def greet(command, **kwargs):
            isolated_home = pathlib.Path(kwargs["env"]["CODEX_HOME"])
            account_id = json.loads((isolated_home / "auth.json").read_text())["tokens"]["account_id"]
            seen.append((command, account_id, isolated_home != self.home))
            return subprocess.CompletedProcess(command, 0, "Hi", "")

        with mock.patch.object(accounts, "query_account", side_effect=query), \
                mock.patch.object(accounts.shutil, "which", return_value="codex"), \
                mock.patch.object(accounts.subprocess, "run", side_effect=greet):
            status = accounts.command_kick(args)
        return status, seen

    def test_greets_only_unstarted_account_and_restores_login(self):
        started = {"usedPercent": 1, "windowDurationMins": 300, "resetsAt": 12345}
        unstarted = {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": None}
        snapshots = {
            "original": {"primary": started, "secondary": started | {"windowDurationMins": 10080}},
            "other": {"primary": started, "secondary": unstarted},
        }
        status, seen = self.run_kick(None, snapshots)
        self.assertEqual(status, 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], "other")
        self.assertTrue(seen[0][2])
        self.assertIn("exec", seen[0][0])
        self.assertEqual(json.loads(self.auth.read_text())["tokens"]["account_id"], "original")

    def test_named_account_limits_queries_and_missing_windows_are_skipped(self):
        snapshots = {"other": {"primary": None, "secondary": None}}
        status, seen = self.run_kick("other", snapshots)
        self.assertEqual(status, 0)
        self.assertEqual(seen, [])

    def test_greets_windows_one_minute_short_of_nominal_duration(self):
        unstarted = {"usedPercent": 0, "resetsAt": None}
        snapshots = {
            "other": {
                "primary": unstarted | {"windowDurationMins": 299},
                "secondary": unstarted | {"windowDurationMins": 10079},
            }
        }
        status, seen = self.run_kick("other", snapshots)
        self.assertEqual(status, 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(accounts.choose_windows(snapshots["other"]),
                         (snapshots["other"]["primary"], snapshots["other"]["secondary"]))

    def test_greets_zero_usage_with_synthetic_reset_times(self):
        snapshots = {
            "other": {
                "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1790993973},
                "secondary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 1791580773},
            }
        }
        status, seen = self.run_kick("other", snapshots)
        self.assertEqual(status, 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], "other")

    def test_failed_greeting_restores_login(self):
        window = {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": None}
        args = argparse.Namespace(codex_home=self.home, codex_bin="codex", name="other")
        account = accounts.Account("other", self.home / "accounts" / "other.json")
        item = accounts.QueryResult(account, result={"rateLimits": {"primary": window}})
        with mock.patch.object(accounts, "query_account", return_value=item), \
                mock.patch.object(accounts.shutil, "which", return_value="codex"), \
                mock.patch.object(accounts.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 120)):
            self.assertEqual(accounts.command_kick(args), 1)
        self.assertEqual(json.loads(self.auth.read_text())["tokens"]["account_id"], "original")


if __name__ == "__main__":
    unittest.main()
