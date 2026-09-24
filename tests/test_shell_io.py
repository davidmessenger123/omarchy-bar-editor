import contextlib
import copy
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import shell_io


class ShellIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.temp.name, "omarchy")
        os.mkdir(self.root)
        self.old = {
            "CONFIG_ROOT": shell_io.CONFIG_ROOT,
            "SHELL_JSON": shell_io.SHELL_JSON,
            "SHELL_TOML": shell_io.SHELL_TOML,
            "PROFILES_DIR": shell_io.PROFILES_DIR,
        }
        shell_io.CONFIG_ROOT = self.root
        shell_io.SHELL_JSON = os.path.join(self.root, "shell.json")
        shell_io.SHELL_TOML = os.path.join(self.root, "shell.toml")
        shell_io.PROFILES_DIR = os.path.join(self.root, "bar-profiles")

    def tearDown(self):
        for key, value in self.old.items():
            setattr(shell_io, key, value)
        self.temp.cleanup()

    def test_profile_names_are_constrained(self):
        self.assertEqual(shell_io.safe_profile_name("work profile"), "work profile")
        for value in ("../escape", ".", "a/b", "a\nb", "x" * 65):
            with self.assertRaises(SystemExit):
                shell_io.safe_profile_name(value)

    def test_read_rejects_symlink_and_hardlink(self):
        target = os.path.join(self.root, "target")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("safe")
        link = os.path.join(self.root, "link")
        os.symlink(target, link)
        with self.assertRaises(SystemExit):
            shell_io.read_file_checked(link)
        hard = os.path.join(self.root, "hard")
        os.link(target, hard)
        with self.assertRaises(SystemExit):
            shell_io.read_file_checked(hard)
        fifo = os.path.join(self.root, "fifo")
        os.mkfifo(fifo)
        with self.assertRaises(SystemExit):
            shell_io.read_file_checked(fifo)

    def test_atomic_write_refuses_symlink_target(self):
        target = os.path.join(self.root, "target")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("unchanged")
        link = os.path.join(self.root, "link")
        os.symlink(target, link)
        with self.assertRaises(SystemExit):
            shell_io.write_bytes_atomic(link, b"replacement")
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "unchanged")

    def test_first_run_profile_save_creates_config_safely(self):
        fresh_root = os.path.join(self.temp.name, "fresh", ".config", "omarchy")
        fresh_profiles = os.path.join(fresh_root, "bar-profiles")
        payload = {"shell": {"bar": {"layout": {}}}, "toml": {"values": {}}}
        with mock.patch.multiple(
                shell_io,
                CONFIG_ROOT=fresh_root,
                SHELL_JSON=os.path.join(fresh_root, "shell.json"),
                SHELL_TOML=os.path.join(fresh_root, "shell.toml"),
                PROFILES_DIR=fresh_profiles), \
             mock.patch.object(shell_io.sys, "argv", ["shell_io.py", "profiles", "save", "first"]), \
             mock.patch.object(shell_io, "read_stdin_json", return_value=payload), \
             contextlib.redirect_stdout(io.StringIO()):
            shell_io.main()
        self.assertTrue(os.path.isdir(fresh_root))
        self.assertTrue(os.path.isfile(os.path.join(fresh_profiles, "first.json")))

    def test_toml_round_trip_preserves_other_sections_and_quotes(self):
        original = (
            '[bar]\ncustom-first = "keep" # keep comment\n'
            'text = "#111111" # managed comment\n'
            'custom-second = true\n[bar.child]\nx = 1\n[other]\ny = "z"\n'
        )
        values = {
            "background": "#123456",
            "background_alpha": 0.5,
            "text": "#abcdef",
            "active": "#fedcba",
            "scale_with_font": False,
            "size_horizontal": 30,
            "size_vertical": 40,
        }
        rendered = shell_io.format_toml(values, original)
        self.assertIn('[bar]\ncustom-first = "keep" # keep comment\n', rendered)
        self.assertIn('text             = "#abcdef" # managed comment\n', rendered)
        self.assertIn("custom-second = true\n", rendered)
        self.assertIn("[bar.child]\nx = 1\n[other]\ny = \"z\"\n", rendered)
        self.assertLess(rendered.index("custom-first"), rendered.index("text"))
        self.assertLess(rendered.index("text"), rendered.index("custom-second"))
        with open(shell_io.SHELL_TOML, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        parsed, exists = shell_io.load_toml_bar()
        self.assertTrue(exists)
        self.assertEqual(parsed["text"], "#abcdef")
        self.assertEqual(parsed["background_alpha"], 0.5)
        self.assertEqual(parsed["scale_with_font"], False)

    def test_write_is_locked_verified_and_rolled_back(self):
        with open(shell_io.SHELL_JSON, "w", encoding="utf-8") as handle:
            json.dump({"version": 1}, handle)
        payload = {
            "base": {"version": 1},
            "shell": {"version": 1, "bar": {"layout": {}}},
            "toml": {"values": {"background": "#123456", "text": "#abcdef", "active": "#fedcba"}},
        }
        with mock.patch.object(shell_io, "read_stdin_json", return_value=payload):
            shell_io.cmd_write()
        self.assertEqual(shell_io.load_json(shell_io.SHELL_JSON)[0]["version"], 1)
        self.assertTrue(os.path.exists(shell_io.SHELL_JSON + shell_io.BACKUP_SUFFIX))
        with open(shell_io.SHELL_JSON, "w", encoding="utf-8") as handle:
            json.dump({"version": 0}, handle)
        original_write = shell_io.write_bytes_atomic
        failed = []

        def fail_toml(path, data, mode=None):
            if path == shell_io.SHELL_TOML and not failed:
                failed.append(True)
                raise OSError("injected failure")
            return original_write(path, data, mode)

        with mock.patch.object(shell_io, "read_stdin_json", return_value=payload), \
             mock.patch.object(shell_io, "write_bytes_atomic", side_effect=fail_toml):
            with self.assertRaises(OSError):
                shell_io.cmd_write()
        self.assertEqual(shell_io.load_json(shell_io.SHELL_JSON)[0]["version"], 0)

    def test_profile_round_trip_is_bounded_and_validated(self):
        payload = {"shell": {"bar": {"layout": {}}}, "toml": {"values": {}}}
        with mock.patch.object(shell_io, "read_stdin_json", return_value=payload):
            shell_io.cmd_profiles(["save", "work"])
        self.assertEqual(shell_io.list_profiles(), ["work"])
        content, exists = shell_io.read_file_checked(os.path.join(shell_io.PROFILES_DIR, "work.json"))
        self.assertTrue(exists)
        self.assertIsInstance(json.loads(content)["shell"], dict)
        shell_io.cmd_profiles(["delete", "work"])
        self.assertEqual(shell_io.list_profiles(), [])

    def test_profile_commands_reject_traversal(self):
        with self.assertRaises(SystemExit):
            shell_io.cmd_profiles(["save", "../outside"])
        with self.assertRaises(SystemExit):
            shell_io.cmd_profiles(["load", "/tmp/x"])

    def test_pending_transaction_recovers_last_good_files(self):
        old = '{"version": 0}\n'
        new = '{"version": 1}\n'
        with open(shell_io.SHELL_JSON, "w", encoding="utf-8") as handle:
            handle.write(old)
        shell_io._make_journal([(shell_io.SHELL_JSON, new.encode("utf-8"))])
        shell_io.write_bytes_atomic(shell_io.SHELL_JSON, new.encode("utf-8"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            shell_io.cmd_read()
        self.assertEqual(json.loads(output.getvalue())["cfg"]["version"], 0)
        self.assertFalse(os.path.exists(shell_io._journal_path()))

    def test_legacy_pending_transaction_is_recovered(self):
        old = b'{"version": 0}\n'
        new = b'{"version": 1}\n'
        with open(shell_io.SHELL_JSON, "wb") as handle:
            handle.write(old)
        legacy = shell_io._journal_path(shell_io.LEGACY_JOURNAL_NAME)
        shell_io._make_journal([(shell_io.SHELL_JSON, new)], legacy)
        shell_io.write_bytes_atomic(shell_io.SHELL_JSON, new)
        shell_io.recover_pending_transaction()
        self.assertEqual(shell_io.load_json(shell_io.SHELL_JSON)[0]["version"], 0)
        self.assertFalse(os.path.exists(legacy))

    def test_stale_shell_merge_preserves_unrelated_updates_and_rejects_conflicts(self):
        base = {
            "version": 1,
            "plugins": ["one"],
            "bar": {
                "position": "top",
                "layout": {"left": [], "center": [], "right": [{"id": "davidjm.rain", "running": False}]},
            },
        }
        requested = copy.deepcopy(base)
        requested["bar"]["position"] = "bottom"
        current = copy.deepcopy(base)
        current["plugins"].append("two")
        current["bar"]["layout"]["right"][0]["running"] = True
        merged = shell_io.merge_shell_config(base, requested, current)
        self.assertEqual(merged["plugins"], ["one", "two"])
        self.assertTrue(merged["bar"]["layout"]["right"][0]["running"])
        self.assertEqual(merged["bar"]["position"], "bottom")
        current["bar"]["position"] = "left"
        with self.assertRaises(SystemExit):
            shell_io.merge_shell_config(base, requested, current)
        deletion_base = {"version": 1, "remove": True, "keep": 1}
        deletion_requested = {"version": 1, "keep": 1}
        deletion_merged = shell_io.merge_shell_config(deletion_base, deletion_requested, deletion_base)
        self.assertEqual(deletion_merged, deletion_requested)

    def test_stale_subprocess_save_waits_for_shared_lock_and_merges(self):
        script = Path(__file__).resolve().parents[1] / "shell_io.py"
        holder_script = r"""
import fcntl
import json
import os
import sys
import time
os.chdir(sys.argv[1])
lock_fd = os.open(".shell.json.lock", os.O_RDWR | os.O_CREAT | os.O_NOCTTY, 0o600)
fcntl.flock(lock_fd, fcntl.LOCK_EX)
open("ready", "w").close()
while not os.path.exists("gate"):
    time.sleep(0.01)
with open("shell.json", encoding="utf-8") as handle:
    data = json.load(handle)
data["external"] = "kept"
payload = (json.dumps(data, indent=2) + "\n").encode("utf-8")
temp_name = ".external.%d.tmp" % os.getpid()
temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
view = memoryview(payload)
while view:
    view = view[os.write(temp_fd, view):]
os.fchmod(temp_fd, 0o640)
os.fsync(temp_fd)
os.close(temp_fd)
os.replace(temp_name, "shell.json")
dir_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
os.fsync(dir_fd)
os.close(dir_fd)
"""
        with tempfile.TemporaryDirectory() as home:
            config_dir = Path(home) / ".config" / "omarchy"
            config_dir.mkdir(parents=True)
            base = {
                "version": 1,
                "external": "old",
                "bar": {"position": "top", "layout": {"left": [], "center": [], "right": []}},
            }
            config = config_dir / "shell.json"
            config.write_text(json.dumps(base), encoding="utf-8")
            config.chmod(0o640)
            ready = config_dir / "ready"
            gate = config_dir / "gate"
            holder = subprocess.Popen(
                [sys.executable, "-I", "-c", holder_script, str(config_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            editor = None
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    if holder.poll() is not None:
                        stdout, stderr = holder.communicate()
                        self.fail(stderr or stdout)
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                requested = copy.deepcopy(base)
                requested["bar"]["position"] = "bottom"
                payload = {"base": base, "shell": requested}
                env = os.environ.copy()
                env["HOME"] = home
                editor = subprocess.Popen(
                    [sys.executable, "-I", str(script), "write"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=config_dir,
                    env=env,
                )
                editor.stdin.write(json.dumps(payload))
                editor.stdin.close()
                time.sleep(0.1)
                self.assertIsNone(editor.poll())
                gate.touch()
                stdout, stderr = editor.communicate(timeout=10)
                self.assertEqual(editor.returncode, 0, stderr or stdout)
                stdout, stderr = holder.communicate(timeout=10)
                self.assertEqual(holder.returncode, 0, stderr or stdout)
            finally:
                gate.touch(exist_ok=True)
                if editor is not None and editor.poll() is None:
                    editor.kill()
                    editor.wait(timeout=5)
                if holder.poll() is None:
                    holder.kill()
                    holder.wait(timeout=5)
            result = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(result["external"], "kept")
            self.assertEqual(result["bar"]["position"], "bottom")
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o640)
            self.assertEqual(stat.S_IMODE((config_dir / ".shell.json.lock").stat().st_mode), 0o600)
            self.assertFalse((config_dir / ".shell.json.transaction.json").exists())

    def test_schema_rejects_invalid_shell_and_toml_values(self):
        with self.assertRaises(SystemExit):
            shell_io.validate_shell_config({"bar": {"position": "sideways"}})
        with self.assertRaises(SystemExit):
            shell_io.validate_shell_config({"bar": {"layout": {"left": [{"id": "../x"}]}}})
        with self.assertRaises(SystemExit):
            shell_io.validate_toml_values({"background": "red"})
        with self.assertRaises(SystemExit):
            shell_io.validate_toml_values({"background_alpha": float("nan")})
        with self.assertRaises(SystemExit):
            shell_io.format_toml({"size_horizontal": float("inf")}, "")

    def test_input_and_toml_limits_are_enforced(self):
        with mock.patch.object(shell_io.sys, "stdin", io.StringIO("{} trailing")):
            with self.assertRaises(SystemExit):
                shell_io.read_stdin_json()
        with mock.patch.object(shell_io.sys, "stdin", io.StringIO("x" * (shell_io.MAX_INPUT_BYTES + 1))):
            with self.assertRaises(SystemExit):
                shell_io.read_stdin_json()
        nested = "[" * (shell_io.MAX_INPUT_DEPTH + 1) + "0" + "]" * (shell_io.MAX_INPUT_DEPTH + 1)
        with mock.patch.object(shell_io.sys, "stdin", io.StringIO(nested)):
            with self.assertRaises(SystemExit):
                shell_io.read_stdin_json()
        with self.assertRaises((ValueError, SystemExit)):
            shell_io.format_toml({}, "x" * (shell_io.MAX_TOML_LINE_CHARS + 1))
        with self.assertRaises(ValueError):
            shell_io.format_toml({}, "[[bar]]\n")
        with self.assertRaises(ValueError):
            shell_io.format_toml({}, "[bar]\n[bar]\n")

    def test_instance_lock_rejects_duplicate_editor(self):
        with shell_io.instance_lock():
            with self.assertRaises(SystemExit):
                with shell_io.instance_lock():
                    pass


if __name__ == "__main__":
    unittest.main()
