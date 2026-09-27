import argparse
import contextlib
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "codex-accounts.py"
spec = importlib.util.spec_from_file_location("codex_accounts_switch", SCRIPT)
accounts = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = accounts
spec.loader.exec_module(accounts)


class SwitchTests(unittest.TestCase):
    def test_switch_changes_file_and_explains_running_sessions(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            root = home / "accounts"
            root.mkdir()
            for name in ("one", "two"):
                (root / f"{name}.json").write_text(json.dumps({"tokens": {"account_id": name}}))
            (home / "auth.json").write_bytes((root / "one.json").read_bytes())
            output = io.StringIO()
            with mock.patch.object(accounts, "stop_codex_daemon", return_value="stopped") as stop, \
                    contextlib.redirect_stdout(output):
                status = accounts.command_switch(
                    argparse.Namespace(codex_home=home, codex_bin="codex", name="two")
                )
            self.assertEqual(status, 0)
            stop.assert_called_once_with(home.resolve(), "codex")
            self.assertEqual(accounts.auth_account_id(home / "auth.json"), "two")
            self.assertIn("Stopped the shared Codex background server", output.getvalue())

    def test_stop_daemon_uses_codex_home_and_accepts_not_running(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            completed = subprocess.CompletedProcess(
                ["codex", "app-server", "daemon", "stop"],
                0,
                '{"status":"notRunning"}\n',
                "",
            )
            with mock.patch.object(accounts.shutil, "which", return_value="/usr/bin/codex"), \
                    mock.patch.object(accounts.subprocess, "run", return_value=completed) as run:
                status = accounts.stop_codex_daemon(home, "codex")

            self.assertEqual(status, "notRunning")
            command = run.call_args.args[0]
            kwargs = run.call_args.kwargs
            self.assertEqual(command, ["/usr/bin/codex", "app-server", "daemon", "stop"])
            self.assertEqual(kwargs["env"]["CODEX_HOME"], str(home))

    def test_switch_stops_daemon_even_when_target_is_already_on_disk(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            root = home / "accounts"
            root.mkdir()
            target = root / "one.json"
            target.write_text(json.dumps({"tokens": {"account_id": "one"}}))
            (home / "auth.json").write_bytes(target.read_bytes())

            with mock.patch.object(accounts, "stop_codex_daemon", return_value="stopped") as stop:
                status = accounts.command_switch(
                    argparse.Namespace(codex_home=home, codex_bin="codex", name="one")
                )

            self.assertEqual(status, 0)
            stop.assert_called_once_with(home.resolve(), "codex")
            self.assertEqual(accounts.auth_account_id(home / "auth.json"), "one")


if __name__ == "__main__":
    unittest.main()
