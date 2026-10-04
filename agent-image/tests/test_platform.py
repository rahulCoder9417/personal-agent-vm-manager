import http.server
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest

from bridge.platform import (git_gateway_url, load_platform_file, render_gitconfig, render_hermes_config,
                             write_gitconfig, write_hermes_config)


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
        # Only send_message is hidden. web_search and read_page stay: a Hermes child gets the lead's tools,
        # so hiding them from the lead would take them from Scout too. On Hermes the web rule is an instruction.
        self.assertEqual(platform["tools"]["exclude"], ["send_message"])
        self.assertEqual(cfg["mcp_servers"]["user_m_1"]["headers"], {"X-Api-Key": "k1"})
        self.assertNotIn("headers", cfg["mcp_servers"]["user_m2"])
        self.assertFalse(cfg["cron"]["enabled"])
        self.assertIn("cronjob", cfg["agent"]["disabled_toolsets"])
        self.assertIn("messaging", cfg["agent"]["disabled_toolsets"])
        self.assertEqual(cfg["gateway"]["api_server"]["host"], "127.0.0.1")
        self.assertEqual(cfg["terminal"]["cwd"], "/data/workspace")
        self.assertEqual(cfg["timezone"], "Europe/Berlin")

    def test_render_delegation_for_the_crew(self):
        pf = load_platform_file(self.path)
        cfg = render_hermes_config(pf, {})
        # One level deep (a child never gets delegate_task), at most 3 children at a time.
        self.assertEqual(cfg["delegation"], {"max_concurrent_children": 3, "max_spawn_depth": 1,
                                             "orchestrator_enabled": False, "subagent_auto_approve": False})
        cfg = render_hermes_config(pf, {"BRIDGE_CREW_MAX_PARALLEL": "2"})
        self.assertEqual(cfg["delegation"]["max_concurrent_children"], 2)
        # The lead keeps delegate_task: only scheduling and messaging are switched off.
        self.assertEqual(cfg["agent"]["disabled_toolsets"], ["cronjob", "messaging"])

    def test_write_is_private_json(self):
        pf = load_platform_file(self.path)
        home = os.path.join(self.tmp, "hermes")
        path = write_hermes_config(render_hermes_config(pf, {}), home)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as fh:
            self.assertEqual(json.load(fh)["model"]["api_key"], "sk-1")

    def test_git_gateway_url(self):
        self.assertEqual(git_gateway_url({}), "http://api:8080/git/")
        self.assertEqual(git_gateway_url({"PLATFORM_MCP_URL": "http://host.docker.internal:8080/mcp/"}),
                         "http://host.docker.internal:8080/git/")

    def test_gitconfig_written_private_and_included_once(self):
        pf = load_platform_file(self.path)
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        user_cfg = os.path.join(home, ".gitconfig")
        with open(user_cfg, "w") as fh:
            fh.write("[user]\n\tname = Mine\n")
        content = render_gitconfig(pf, {})
        self.assertIn("Authorization: Bearer tok-123", content)
        for _ in range(2):
            path = write_gitconfig(content, self.tmp, home)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(user_cfg) as fh:
            written = fh.read()
        self.assertEqual(written.count("[include]"), 1)
        self.assertTrue(written.startswith("[include]"))
        self.assertTrue(written.endswith("[user]\n\tname = Mine\n"))

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_real_git_goes_through_the_gateway_with_the_container_token(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(403)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        pf = load_platform_file(self.path)
        home = os.path.join(self.tmp, "home")
        env = {"PLATFORM_MCP_URL": f"http://127.0.0.1:{server.server_port}/mcp"}
        write_gitconfig(render_gitconfig(pf, env), self.tmp, home)
        git_env = {"HOME": home, "PATH": os.environ["PATH"], "GIT_TERMINAL_PROMPT": "0",
                   "GIT_CONFIG_NOSYSTEM": "1", "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}

        def git(*args):
            return subprocess.run(["git", *args], env=git_env, cwd=self.tmp, capture_output=True, text=True,
                                  timeout=30)

        self.assertEqual(git("config", "--get", "user.name").stdout.strip(), "Personal Agent")
        for url in ("https://github.com/acme/app", "git@github.com:acme/app.git", "ssh://git@github.com/acme/app"):
            seen.clear()
            self.assertNotEqual(git("ls-remote", url).returncode, 0)
            self.assertEqual(len(seen), 1, url)
            path, auth = seen[0]
            self.assertTrue(path.startswith("/git/acme/app"), path)
            self.assertTrue(path.endswith("/info/refs?service=git-upload-pack"), path)
            self.assertEqual(auth, "Bearer tok-123")


if __name__ == "__main__":
    unittest.main()
