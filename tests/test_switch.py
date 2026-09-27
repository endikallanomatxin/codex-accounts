import argparse
import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest


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
            with contextlib.redirect_stdout(output):
                status = accounts.command_switch(argparse.Namespace(codex_home=home, name="two"))
            self.assertEqual(status, 0)
            self.assertEqual(accounts.auth_account_id(home / "auth.json"), "two")
            self.assertIn("Quit them, switch again", output.getvalue())


if __name__ == "__main__":
    unittest.main()
