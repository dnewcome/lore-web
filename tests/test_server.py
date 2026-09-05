"""Server-side tests that do not need a Lore remote."""
import importlib.util
import os
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "loreweb_server", os.path.join(HERE, os.pardir, "server.py"))
srv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srv)


def fake_plugin(name, **attrs):
    mod = types.SimpleNamespace(**attrs)
    mod._name = name
    return mod


class PluginHandlerTest(unittest.TestCase):
    """The cache key that decides whether preview art is reused."""

    def test_plain_plugin_is_just_its_name(self):
        self.assertEqual(srv.plugin_handler(fake_plugin("pd")), "pd")

    def test_no_plugin_is_the_builtin_handler(self):
        self.assertEqual(srv.plugin_handler(None), "builtin")

    def test_empty_salt_does_not_change_the_key(self):
        """Existing caches must survive a plugin gaining the feature."""
        self.assertEqual(srv.plugin_handler(fake_plugin("pd", CACHE_SALT="")),
                         "pd")

    def test_salt_changes_the_key(self):
        a = srv.plugin_handler(fake_plugin("pd", CACHE_SALT="w400-0-0"))
        b = srv.plugin_handler(fake_plugin("pd", CACHE_SALT="w1200-0-3"))
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, "pd")

    def test_real_plugins_load_and_have_handlers(self):
        self.assertTrue(srv.PLUGINS, "no plugins loaded")
        names = {p._name for p in srv.PLUGINS}
        self.assertLessEqual({"pd", "tif", "midi", "ableton"}, names)
        for plugin in srv.PLUGINS:
            with self.subTest(plugin=plugin._name):
                self.assertTrue(srv.plugin_handler(plugin))
                self.assertTrue(hasattr(plugin, "MATCH"))
                self.assertTrue(callable(plugin.inspect))

    def test_extensions_are_claimed_by_one_plugin_each(self):
        seen = {}
        for plugin in srv.PLUGINS:
            for ext in plugin.MATCH:
                self.assertNotIn(ext, seen,
                                 f"{ext} claimed by {seen.get(ext)} and "
                                 f"{plugin._name}")
                seen[ext] = plugin._name
        self.assertEqual(srv.plugin_for("a/b/c.pd")._name, "pd")
        self.assertEqual(srv.plugin_for("X.TIF")._name, "tif")
        self.assertIsNone(srv.plugin_for("notes.txt"))

    def test_tif_stays_out_of_the_builtin_image_path(self):
        """The builtin ffmpeg fallback would undo the plugin's refusals."""
        for ext in (".tif", ".tiff"):
            self.assertNotIn(ext, srv.IMAGE_EXT)
        self.assertEqual(srv.file_kind("scan.tif"), "other")


if __name__ == "__main__":
    unittest.main()
