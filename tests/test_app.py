import importlib
import os
import sys
import unittest


class TestAppConfig(unittest.TestCase):
    def test_prowlarr_url_uses_environment_override(self):
        os.environ["PROWLARR_URL"] = "http://example.test:9696"
        sys.modules.pop("app", None)

        module = importlib.import_module("app")

        self.assertEqual(module.PROWLARR_URL, "http://example.test:9696")

        sys.modules.pop("app", None)
        os.environ.pop("PROWLARR_URL", None)


if __name__ == "__main__":
    unittest.main()
