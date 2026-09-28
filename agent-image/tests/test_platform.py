import json
import os
import stat
import tempfile
import unittest

from bridge.platform import load_platform_file, render_hermes_config, write_hermes_config


class PlatformTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "platform.json")
        with open(self.path, "w") as fh:
            json.dump({
                "version": 1, "user_id": "u1", "mcp_token": "tok-123", "bridge_secret": "sec", "litellm_key": "sk-1",
                "timezone": "Europe/Berlin",
                "mcp_servers": [
                    {"id": "M-1", "name": "notes", "url": "https://notes.example.org/mcp",
                     "auth_header_name": "X-Api-Key", "auth_secret": "k1"},
                    {"id": "m2", "name": "open", "url": "https://open.example.org/mcp"},
                ],
            }, fh)

    def test_load_requires_secrets(self):
        with open(self.path, "w") as fh:
            json.dump({"user_id": "u1"}, fh)
        with self.assertRaises(ValueError):
            load_platform_file(self.path)

    def test_render(self):
        pf = load_platform_file(self.path)
        cfg = render_hermes_config(pf, {"LITELLM_BASE_URL": "http://litellm:4000/", "LITELLM_MODEL": "cheap",
                                        "PLATFORM_MCP_URL": "http://api:8080/mcp"})
        self.assertEqual(cfg["model"], {"provider": "custom", "base_url": "http://litellm:4000/v1",
                                        "api_key": "sk-1", "default": "cheap"})
        platform = cfg["mcp_servers"]["platform"]
        self.assertEqual(platform["url"], "http://api:8080/mcp")
        self.assertEqual(platform["headers"], {"Authorization": "Bearer tok-123"})
        self.assertIn("send_message", platform["tools"]["exclude"])
        self.assertEqual(cfg["mcp_servers"]["user_m_1"]["headers"], {"X-Api-Key": "k1"})
        self.assertNotIn("headers", cfg["mcp_servers"]["user_m2"])
        self.assertFalse(cfg["cron"]["enabled"])
        self.assertIn("cronjob", cfg["agent"]["disabled_toolsets"])
        self.assertIn("messaging", cfg["agent"]["disabled_toolsets"])
        self.assertEqual(cfg["gateway"]["api_server"]["host"], "127.0.0.1")
        self.assertEqual(cfg["terminal"]["cwd"], "/data/workspace")
        self.assertEqual(cfg["timezone"], "Europe/Berlin")

    def test_write_is_private_json(self):
        pf = load_platform_file(self.path)
        home = os.path.join(self.tmp, "hermes")
        path = write_hermes_config(render_hermes_config(pf, {}), home)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as fh:
            self.assertEqual(json.load(fh)["model"]["api_key"], "sk-1")


if __name__ == "__main__":
    unittest.main()
