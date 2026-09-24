import copy
import json
import sys
import unittest
from unittest import mock

import editor_tui


class ModelTests(unittest.TestCase):
    def test_malformed_config_is_normalized(self):
        cfg = editor_tui.ensure_cfg({"bar": {"layout": {"left": "bad"}}})
        self.assertEqual(cfg["bar"]["layout"]["left"], [])
        self.assertEqual(cfg["bar"]["layout"]["center"], [])

    def test_unknown_plugin_state_is_not_enabled(self):
        model = editor_tui.Model()
        model.catalog = {
            "x": {
                "id": "x",
                "kinds": ["bar-widget"],
                "barWidget": {"displayName": "X"},
            }
        }
        model.states = {"x": None}
        self.assertIsNone(model.plugins()[0]["enabled"])
        self.assertEqual(model.add_options()[0]["state"], None)
        with mock.patch.object(editor_tui, "run", return_value=(0, "", "")) as command:
            self.assertFalse(model.plugin_set("x", True))
            command.assert_not_called()

    def test_reload_requires_force_when_dirty(self):
        model = editor_tui.Model()
        model.layout["left"].append({"id": "x"})
        model.dirty = True
        with mock.patch.object(model, "load") as load:
            self.assertFalse(model.reload())
            load.assert_not_called()
            self.assertTrue(model.reload(force=True))
            load.assert_called_once()

    def test_widget_options_reject_unknown_keys_and_accept_bounded_values(self):
        model = editor_tui.Model()
        model.catalog = {
            "x": {
                "id": "x",
                "barWidget": {
                    "schema": [{
                        "key": "count",
                        "type": "integer",
                        "min": 1,
                        "max": 3,
                    }]
                },
            }
        }
        model.layout["left"].append({"id": "x"})
        self.assertFalse(model.apply_widget_option("left", 0, "other", 2))
        self.assertTrue(model.apply_widget_option("left", 0, "count", 99))
        self.assertEqual(model.layout["left"][0]["count"], 3)
        self.assertTrue(model.undo_step())
        self.assertNotIn("count", model.layout["left"][0])

    def test_profile_load_requires_explicit_force_when_dirty(self):
        model = editor_tui.Model()
        model.dirty = True
        with mock.patch.object(editor_tui, "_invoke_shell_io", return_value=(0, "{}", "")):
            with self.assertRaises(RuntimeError):
                model.load_profile("work")

    def test_profile_load_clears_history_and_preserves_disk_merge_base(self):
        model = editor_tui.Model()
        model.disk_cfg = {"version": 1, "external": "disk"}
        model.push_history()
        profile = {
            "shell": {"version": 1, "bar": {"position": "bottom"}},
            "toml": {"values": {}},
        }
        with mock.patch.object(
                editor_tui, "_invoke_shell_io",
                return_value=(0, json.dumps(profile), "")):
            model.load_profile("work")
        self.assertEqual(len(model.undo), 0)
        self.assertEqual(len(model.redo), 0)
        self.assertEqual(model.disk_cfg, {"version": 1, "external": "disk"})
        self.assertTrue(model.dirty)

    def test_save_advances_the_disk_merge_base_to_the_written_snapshot(self):
        model = editor_tui.Model()
        base = {"version": 1, "external": "old"}
        model.cfg = editor_tui.ensure_cfg(copy.deepcopy(base))
        model.disk_cfg = copy.deepcopy(base)
        model.position = "bottom"
        merged = editor_tui.ensure_cfg({"version": 1, "external": "new", "bar": {"position": "bottom"}})
        with mock.patch.object(editor_tui, "io_write", return_value=merged) as write:
            model.save()
        self.assertEqual(write.call_args.args[2], base)
        self.assertEqual(model.disk_cfg, merged)
        self.assertEqual(model.cfg["external"], "new")
        self.assertFalse(model.dirty)

    def test_command_with_unread_stdin_is_bounded(self):
        code, output, error = editor_tui.run([
            sys.executable,
            "-c",
            "import time; time.sleep(2)",
        ], payload={"value": "x"}, timeout=1)
        self.assertEqual(code, 124)
        self.assertLessEqual(len(output), editor_tui.MAX_COMMAND_OUTPUT)
        self.assertLessEqual(len(error), editor_tui.MAX_ERROR_OUTPUT)

    def test_command_output_is_bounded(self):
        code, output, error = editor_tui.run([
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * 3000000)",
        ])
        self.assertNotEqual(code, 0)
        self.assertLessEqual(len(output), editor_tui.MAX_COMMAND_OUTPUT)
        self.assertLessEqual(len(error), editor_tui.MAX_ERROR_OUTPUT)

    def test_category_color_and_history_are_deterministic_and_bounded(self):
        category = "custom"
        expected = editor_tui.CAT_STD[
            sum(category.encode("utf-8")) % len(editor_tui.CAT_STD)
        ]
        self.assertEqual(editor_tui.cat_pair(category), editor_tui.PAIR.get(expected, 0))
        model = editor_tui.Model()
        for index in range(105):
            model.position = str(index)
            model.push_history()
        self.assertEqual(len(model.undo), 100)
        self.assertEqual(len(model.redo), 0)

    def test_reload_resets_history_and_cached_metadata(self):
        model = editor_tui.Model()
        model.catalog = {"x": {"id": "x", "barWidget": {"displayName": "Old"}}}
        model._meta["x"] = ("Old", "System")
        model.push_history()
        data = {
            "cfg": {"version": 1, "bar": {"layout": {}}},
            "toml": {"values": {}},
            "profiles": [],
        }
        with mock.patch.object(editor_tui, "io_read", return_value=data), \
             mock.patch.object(editor_tui, "load_catalog", return_value=model.catalog), \
             mock.patch.object(editor_tui, "load_plugin_states", return_value=None):
            model.load()
        self.assertEqual(len(model.undo), 0)
        self.assertEqual(len(model.redo), 0)
        self.assertEqual(model._meta, {})

    def test_failed_reload_restores_the_original_snapshot(self):
        model = editor_tui.Model()
        model.cfg = editor_tui.ensure_cfg({"version": 1, "bar": {"position": "top"}})
        model.disk_cfg = copy.deepcopy(model.cfg)
        model.catalog = {"old": {"id": "old"}}
        model.states = {"old": True}
        model._meta["old"] = ("Old", "System")
        before = model.snapshot()
        data = {
            "cfg": {"version": 1, "bar": {"position": "bottom"}},
            "toml": {"values": {"background": "#123456"}},
            "profiles": ["new"],
        }

        def fail_apply():
            model.position = "left"
            raise RuntimeError("injected")

        with mock.patch.object(editor_tui, "io_read", return_value=data), \
             mock.patch.object(editor_tui, "load_catalog", return_value={"new": {"id": "new"}}), \
             mock.patch.object(editor_tui, "load_plugin_states", return_value={"new": True}), \
             mock.patch.object(model, "apply_from_cfg", side_effect=fail_apply):
            with self.assertRaises(RuntimeError):
                model.load()
        self.assertEqual(model.snapshot(), before)
        self.assertEqual(model.catalog, {"old": {"id": "old"}})
        self.assertEqual(model.states, {"old": True})
        self.assertEqual(model._meta, {"old": ("Old", "System")})

    def test_layout_operations_reject_invalid_indices_and_sections(self):
        model = editor_tui.Model()
        self.assertFalse(model.add_widget("../left", "x"))
        self.assertFalse(model.remove_widget("left", 0))
        self.assertTrue(model.add_widget("left", "x"))
        self.assertTrue(model.add_widget("left", "y"))
        self.assertTrue(model.move_widget("left", 0, 1))
        self.assertFalse(model.move_section("left", 0, "outside"))


if __name__ == "__main__":
    unittest.main()
