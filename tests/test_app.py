import importlib
import json
import os
import sys
import tempfile
import unittest


class TestAppConfig(unittest.TestCase):
    def test_prowlarr_url_uses_environment_override(self):
        os.environ["PROWLARR_URL"] = "http://example.test:9696"
        sys.modules.pop("app", None)

        module = importlib.import_module("app")

        self.assertEqual(module.PROWLARR_URL, "http://example.test:9696")

        sys.modules.pop("app", None)
        os.environ.pop("PROWLARR_URL", None)

    def test_load_rules_creates_config_file_from_default_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config", "rules.json")
            default_path = os.path.join(tmpdir, "defaults", "rules.json")
            os.makedirs(os.path.dirname(default_path), exist_ok=True)
            with open(default_path, "w", encoding="utf-8") as f:
                json.dump({"settings": {"rewrite_enabled": False}}, f)

            sys.modules.pop("app", None)
            module = importlib.import_module("app")
            module.RULES_FILE = config_path
            module.DEFAULT_RULES_SOURCE = default_path

            rules = module.load_rules()

            self.assertFalse(rules["settings"]["rewrite_enabled"])
            self.assertTrue(os.path.exists(config_path))

            with open(config_path, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f)["settings"]["rewrite_enabled"], False)

            sys.modules.pop("app", None)


if __name__ == "__main__":
    unittest.main()
