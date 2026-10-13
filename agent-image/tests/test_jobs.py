"""Crew jobs in the Bridge: a folder per job, and every process of the job stopped and the folder deleted after its
turn, with real processes (Linux /proc)."""

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from bridge import crew
from bridge.core import Bridge, DeliverRequest, DeliveryStore, build_instructions
from bridge.jobs import JOB_DIR_ENV, JOB_MARK, CrewJob, JobFolders

from tests.fakes import FakeRunner, FakeSender

SECRET = "s3cret-bridge"
SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]
# Ignores SIGTERM, like a server that doesn't stop when asked: only SIGKILL ends it.
STUBBORN = [sys.executable, "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); "
            "time.sleep(60)"]


def job_req(job_id="crj_1", member="patch", delivery_id="evt-job", **kw):
    body = {"delivery_id": delivery_id, "kind": "message", "session_key": "web", "channel": "web",
            "text": f"[Crew job {job_id}] Review acme/web#42.",
            "metadata": {"crew_job": {"id": job_id, "member": member}, "crew": member}}
    body.update(kw)
    return DeliverRequest.from_json(body)


@unittest.skipUnless(os.path.isdir("/proc/self"), "needs Linux /proc")
class JobFoldersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.workspace = os.path.join(self.tmp, "workspace")
        os.makedirs(self.workspace)
        self.protected: list[int] = []
        self.jobs = JobFolders(self.workspace, grace=0.5, protected=lambda: self.protected)
        self.procs: list[subprocess.Popen] = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(5)
            if p.stdout is not None:
                p.stdout.close()

    def start(self, argv=SLEEP, cwd=None, env=None, new_session=False):
        p = subprocess.Popen(argv, cwd=cwd or self.tmp, env={**os.environ, **(env or {})},
                             start_new_session=new_session, stdout=subprocess.PIPE if argv is STUBBORN else None)
        self.procs.append(p)
        if argv is STUBBORN:
            p.stdout.readline()  # its SIGTERM handler is in place
        return p

    def alive(self, p):
        """Running, and not a zombie waiting for us to reap it."""
        if p.poll() is not None:
            return False
        try:
            with open(f"/proc/{p.pid}/stat") as fh:
                return fh.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
        except OSError:
            return False

    def test_the_brief_in_the_metadata(self):
        self.assertEqual(CrewJob.from_metadata({"crew_job": {"id": "crj_01ab", "member": "byte"}}),
                         CrewJob("crj_01ab", "byte"))
        for bad in ({"crew_job": {"id": "../etc", "member": "patch"}}, {"crew_job": {"id": "a/b", "member": "patch"}},
                    {"crew_job": {"id": "x", "member": "scout"}}, {"crew_job": "crj_1"},
                    {"crew_job": {"id": "", "member": "patch"}}, {"crew_job": {"id": "x" * 65, "member": "patch"}}):
            self.assertIsNone(CrewJob.from_metadata(bad), bad)
        self.assertIsNone(CrewJob.from_metadata({}))
        self.assertIsNone(CrewJob.from_metadata(None))
        self.assertEqual(CrewJob("crj_1", "patch").env("/w/jobs/crj_1"), {JOB_MARK: "crj_1", JOB_DIR_ENV: "/w/jobs/crj_1"})

    def test_prepare_gives_an_empty_folder(self):
        job = CrewJob("crj_1", "patch")
        folder = self.jobs.prepare(job)
        self.assertEqual(folder, os.path.join(self.workspace, "jobs", "crj_1"))
        with open(os.path.join(folder, "left.txt"), "w") as fh:
            fh.write("from an earlier try")
        self.assertEqual(os.listdir(self.jobs.prepare(job)), [])
        self.assertEqual(oct(os.stat(folder).st_mode & 0o777), oct(0o700))

    def test_cleanup_stops_the_jobs_processes_and_only_those(self):
        job, other = CrewJob("crj_1", "patch"), CrewJob("crj_12", "byte")
        folder = self.jobs.prepare(job)
        other_folder = self.jobs.prepare(other)
        repo = os.path.join(folder, "repo")
        os.makedirs(repo)
        in_folder = self.start(cwd=repo)  # e.g. `cd repo && pnpm dev &`
        stubborn = self.start(STUBBORN, cwd=folder)  # needs SIGKILL
        # Went into the background in a session of its own, somewhere else: the mark still says it is the job's.
        marked = self.start(cwd=self.tmp, env={JOB_MARK: "crj_1"}, new_session=True)
        # Another job's mark that starts the same way, another job's folder, and a process of nobody's job.
        longer_id = self.start(env={JOB_MARK: "crj_12"})
        other_job = self.start(cwd=other_folder)
        unrelated = self.start()
        # A process the Bridge must never stop (Hermes), even inside the folder.
        hermes = self.start(cwd=folder)
        self.protected.append(hermes.pid)

        self.assertEqual(sorted(self.jobs.processes(job)), sorted([in_folder.pid, stubborn.pid, marked.pid]))
        done = self.jobs.cleanup(job)
        self.assertEqual(done, {"stopped": 3, "removed": True})
        for p in (in_folder, stubborn, marked):
            self.assertFalse(self.alive(p), p.args)
        for p in (longer_id, other_job, unrelated, hermes):
            self.assertTrue(self.alive(p), p.args)
        self.assertFalse(os.path.exists(folder))
        self.assertTrue(os.path.isdir(other_folder), "another job's folder stays")

    def test_read_only_folders_and_links_out(self):
        job = CrewJob("crj_2", "byte")
        folder = self.jobs.prepare(job)
        cache = os.path.join(folder, "repo", "node_modules", ".cache")
        os.makedirs(cache)
        with open(os.path.join(cache, "f"), "w") as fh:
            fh.write("x")
        os.chmod(cache, 0o500)
        os.chmod(os.path.dirname(cache), 0o500)
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "keep.txt"), "w") as fh:
            fh.write("keep")
        os.symlink(outside, os.path.join(folder, "link"))
        self.assertEqual(self.jobs.cleanup(job), {"stopped": 0, "removed": True})
        self.assertFalse(os.path.exists(folder))
        self.assertTrue(os.path.exists(os.path.join(outside, "keep.txt")), "a link is removed, not followed")

    def test_sweep_and_what_is_not_a_job_folder(self):
        for jid in ("crj_a", "crj_b"):
            folder = self.jobs.prepare(CrewJob(jid, "patch"))
            with open(os.path.join(folder, "x"), "w") as fh:
                fh.write("x")
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "keep.txt"), "w") as fh:
            fh.write("keep")
        os.symlink(outside, os.path.join(self.jobs.root, "crj_link"))
        self.assertEqual(self.jobs.sweep(), 3)
        self.assertEqual(os.listdir(self.jobs.root), [])
        self.assertTrue(os.path.isdir(outside))
        self.assertEqual(self.jobs.sweep(), 0)
        # Only a folder right under the jobs root is ever removed.
        self.assertFalse(self.jobs._remove(self.workspace))
        self.assertTrue(os.path.isdir(self.workspace))
        self.assertEqual(JobFolders(os.path.join(self.tmp, "nothing")).sweep(), 0)
        # The jobs root itself made a link to somewhere else: the link goes, nothing it points to is touched.
        os.rmdir(self.jobs.root)
        os.symlink(outside, self.jobs.root)
        self.assertEqual(self.jobs.sweep(), 0)
        self.assertTrue(os.path.exists(os.path.join(outside, "keep.txt")))
        self.assertFalse(os.path.islink(self.jobs.root))
        self.jobs.prepare(CrewJob("crj_c", "patch"))
        self.assertFalse(os.path.exists(os.path.join(outside, "crj_c")))


@unittest.skipUnless(os.path.isdir("/proc/self"), "needs Linux /proc")
class BridgeJobTest(unittest.TestCase):
    """The Bridge runs a crew job in its folder and cleans up after the turn, whatever happened."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.workspace = os.path.join(self.tmp, "workspace")
        self.jobs = JobFolders(self.workspace, grace=0.5)
        self.runner = FakeRunner(output="**Verdict:** ship")
        self.sender = FakeSender()
        self.bridge = Bridge(SECRET, self.runner, self.sender, DeliveryStore(os.path.join(self.tmp, "d.json")),
                             retry_delay=0, jobs=self.jobs)
        self.procs: list[subprocess.Popen] = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(5)

    def test_a_job_turn_gets_its_folder_and_leaves_nothing(self):
        seen = {}
        original = self.runner.run

        def run(**kw):
            folder = os.path.join(self.workspace, "jobs", "crj_1")
            seen["folder_during"] = os.path.isdir(folder)
            env = dict(kw["options"].job_env)
            seen["env"] = env
            # A dev server the job started in the background, from inside its folder, with the turn's mark.
            p = subprocess.Popen(SLEEP, cwd=folder, env={**os.environ, **env}, start_new_session=True)
            self.procs.append(p)
            return original(**kw)

        self.runner.run = run
        events = list(self.bridge.deliver(job_req()))
        self.assertEqual([e["type"] for e in events], ["accepted", "working", "working", "turn_done"])
        self.assertEqual(events[2]["message"], "Patch: stopped 1 program and deleted the job folder")
        self.assertTrue(seen["folder_during"])
        folder = os.path.join(self.workspace, "jobs", "crj_1")
        self.assertEqual(seen["env"], {JOB_MARK: "crj_1", JOB_DIR_ENV: folder})
        self.assertFalse(os.path.exists(folder))
        self.procs[0].wait(5)
        instructions = self.runner.calls[0]["instructions"]
        self.assertIn(f"This turn is a crew job for Patch: the message holds the user's brief. The job folder is "
                      f"{folder}.", instructions)
        self.assertIn("up to 3 Patch runs at the same time", instructions)
        self.assertEqual(self.sender.sent, [("**Verdict:** ship", "web")], "the report is the final answer")

    def test_cleanup_also_after_a_failure(self):
        self.runner.raise_exc = RuntimeError("engine crashed")
        events = list(self.bridge.deliver(job_req(job_id="crj_9", member="byte")))
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("Byte: deleted the job folder", [e["message"] for e in events])
        self.assertFalse(os.path.exists(os.path.join(self.workspace, "jobs", "crj_9")))

    def test_two_jobs_at_once_keep_out_of_each_others_way(self):
        started = {"crj_a": threading.Event(), "crj_b": threading.Event()}
        release = {"crj_a": threading.Event(), "crj_b": threading.Event()}
        procs = {}
        original = self.runner.run

        def run(**kw):
            env = dict(kw["options"].job_env)
            jid = env[JOB_MARK]
            p = subprocess.Popen(SLEEP, cwd=env[JOB_DIR_ENV], env={**os.environ, **env})
            self.procs.append(p)
            procs[jid] = p
            started[jid].set()
            release[jid].wait(5)
            return original(**kw)

        self.runner.run = run
        out = {}
        a = threading.Thread(target=lambda: out.setdefault("a", list(self.bridge.deliver(job_req("crj_a", delivery_id="e-a")))))
        b = threading.Thread(target=lambda: out.setdefault("b", list(self.bridge.deliver(job_req("crj_b", delivery_id="e-b")))))
        a.start()
        b.start()
        self.assertTrue(started["crj_a"].wait(5) and started["crj_b"].wait(5))
        release["crj_a"].set()
        a.join(10)
        # Job a is cleaned up; job b's server, in b's folder and with b's mark, still runs.
        self.assertFalse(os.path.exists(os.path.join(self.workspace, "jobs", "crj_a")))
        procs["crj_a"].wait(5)
        self.assertIsNone(procs["crj_b"].poll())
        self.assertTrue(os.path.isdir(os.path.join(self.workspace, "jobs", "crj_b")))
        release["crj_b"].set()
        b.join(10)
        procs["crj_b"].wait(5)
        self.assertFalse(os.path.exists(os.path.join(self.workspace, "jobs", "crj_b")))

    def test_an_ordinary_message_gets_no_folder(self):
        list(self.bridge.deliver(DeliverRequest.from_json(
            {"delivery_id": "m1", "kind": "message", "session_key": "web", "channel": "web", "text": "hi"})))
        self.assertEqual(self.runner.calls[0]["options"].job_env, ())
        self.assertNotIn("crew job", self.runner.calls[0]["instructions"])
        self.assertFalse(os.path.exists(os.path.join(self.workspace, "jobs")))

    def test_a_bad_crew_job_runs_as_a_message(self):
        req = job_req(job_id="../../etc")
        events = list(self.bridge.deliver(req))
        self.assertEqual(events[-1]["type"], "turn_done")
        self.assertEqual(self.runner.calls[0]["options"].job_env, ())

    def test_without_job_folders_a_job_runs_as_before(self):
        bridge = Bridge(SECRET, self.runner, self.sender, DeliveryStore(None), retry_delay=0)
        events = list(bridge.deliver(job_req()))
        self.assertEqual(events[-1]["type"], "turn_done")
        self.assertNotIn("job folder", self.runner.calls[0]["instructions"])
        self.assertEqual(build_instructions(job_req()), build_instructions(job_req(), ""))


class CrewJobTextTest(unittest.TestCase):
    def test_job_rules_for_the_lead_and_the_specialists(self):
        patch = crew.job_instructions("patch", "/data/workspace/jobs/crj_1")
        byte = crew.job_instructions("byte", "/data/workspace/jobs/crj_2")
        for text in (patch, byte):
            for words in ("The job folder is /data/workspace/jobs/crj_", "Change nothing outside it",
                          "every program the job started is stopped", "deletes the folder",
                          "Your final answer is the job's report"):
                self.assertIn(words, text)
        self.assertIn("up to 3 Patch runs at the same time", patch)
        self.assertNotIn("Patch runs", byte)
        p = crew.role_prompt(crew.PATCH, engine=crew.HERMES)
        b = crew.role_prompt(crew.BYTE, engine=crew.CLAUDE_CODE)
        for words in ("In a crew job", "job folder", "stop every program you started", "Do not post on GitHub",
                      "every finding with its file and line", "what you did not check"):
            self.assertIn(words, p)
        for words in ("In a crew job", "Clone into the job folder", "stop every program you started",
                      "push everything that must be kept", "what to test for", "the acceptance checks"):
            self.assertIn(words, b)


if __name__ == "__main__":
    unittest.main()
