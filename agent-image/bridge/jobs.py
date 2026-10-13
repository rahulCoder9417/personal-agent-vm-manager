"""Crew jobs: a folder of its own for each job, and the clean-up after its turn.

A crew job is a brief the user gave Patch (a review) or Byte (a change) in the Textcrew app or in chat (the
backend's crew_jobs). It arrives as one delivery whose metadata has "crew_job": {"id": ..., "member": "patch" |
"byte"}. The Bridge, not only the agent's instructions, makes the job leave nothing behind:

1. Before the turn: the job's folder, <workspace>/jobs/<job id>, is made empty. The lead and the specialist are
   told to clone into it and to start whatever the job needs (an install, a dev server, a test runner) from
   inside it.
2. After the turn, whatever happened (an answer, a failure, an exception): every process of the job is stopped,
   then the folder is deleted. A process is the job's when
   - its working directory is the job's folder or inside it (a dev server started with `cd <folder>/repo && ...
     &`, on either engine), or
   - its environment carries the job's mark, TEXTCREW_JOB_ID=<job id>. Claude Code's process for the turn gets
     the mark, and everything it starts inherits it, also a server that went into the background, made a
     session of its own or changed its directory.
   Each such process gets SIGTERM, and SIGKILL when it is still there after a grace period. That repeats a few
   times, for a process started while the first ones were stopping.

Concurrent turns are safe. A process is stopped only for its own job's folder or mark, so another turn's
processes (another job, or a chat message) are never touched. The Bridge itself, its parent (tini), PID 1 and the
long-lived engine processes the Bridge names (Hermes) are never stopped, whatever their directory.

When the Bridge starts, no job can be running (a container that stops ends every process in it), so every
folder under <workspace>/jobs is left from a turn that never finished: they are all deleted (sweep).

Linux only: processes are found through /proc (the container's own, under gVisor too).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import stat
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger("bridge.jobs")

JOB_MARK = "TEXTCREW_JOB_ID"  # the environment variable that marks a job's processes
JOB_DIR_ENV = "TEXTCREW_JOB_DIR"  # where the job's folder is, for the programs that want to know
MEMBERS = ("patch", "byte")
_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
DEFAULT_GRACE = 5.0  # seconds between SIGTERM and SIGKILL
ROUNDS = 3  # scans after the first: for processes started while the first ones were stopping


@dataclass(frozen=True)
class CrewJob:
    """The crew job a delivery is (metadata "crew_job")."""

    id: str
    member: str  # patch | byte

    @classmethod
    def from_metadata(cls, meta: Any) -> Optional["CrewJob"]:
        """None when the delivery is not a crew job, or names one in a shape we can't use (logged): the turn
        then runs as an ordinary message."""
        raw = meta.get("crew_job") if isinstance(meta, dict) else None
        if raw is None:
            return None
        job_id = raw.get("id") if isinstance(raw, dict) else None
        member = raw.get("member") if isinstance(raw, dict) else None
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id) or member not in MEMBERS:
            log.warning("metadata crew_job is not {id, member}; the turn runs without a job folder")
            return None
        return cls(id=job_id, member=member)

    def env(self, folder: str) -> dict[str, str]:
        """What a turn's engine process adds to its environment, so everything it starts is known as the job's."""
        return {JOB_MARK: self.id, JOB_DIR_ENV: folder}


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


class JobFolders:
    """The jobs' folders under <workspace>/jobs, and the processes in them."""

    def __init__(self, workspace: str, proc_root: str = "/proc", grace: float = DEFAULT_GRACE,
                 protected: Callable[[], Iterable[Optional[int]]] = lambda: (),
                 kill: Callable[[int, int], None] = os.kill, sleep: Callable[[float], None] = time.sleep):
        self.root = os.path.join(workspace, "jobs")
        self.proc_root = proc_root
        self.grace = grace
        self.protected = protected  # pids that are never stopped (the Hermes process)
        self._kill = kill
        self._sleep = sleep

    def path(self, job: CrewJob) -> str:
        return os.path.join(self.root, job.id)

    # --- the folder -----------------------------------------------------------------------------------------

    def prepare(self, job: CrewJob) -> str:
        """An empty folder for the job (what an earlier try of the same job left there is stopped and removed)."""
        folder = self.path(job)
        self._root_not_a_link()
        if os.path.lexists(folder):
            self.stop_processes(job)
            self._remove(folder)
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        os.makedirs(folder, mode=0o700)
        return folder

    def cleanup(self, job: CrewJob) -> dict:
        """After the job's turn: stop its processes, then delete its folder. Never raises; says what it did."""
        stopped, removed = 0, False
        try:
            stopped = self.stop_processes(job)
        except Exception:  # noqa: BLE001 - the folder still goes
            log.exception("stopping the processes of job %s failed", job.id)
        try:
            removed = self._remove(self.path(job))
        except Exception:  # noqa: BLE001
            log.exception("removing the folder of job %s failed", job.id)
        log.info("job %s cleaned up: %d process(es) stopped, folder removed: %s", job.id, stopped, removed)
        return {"stopped": stopped, "removed": removed}

    def sweep(self) -> int:
        """At start: delete every job folder (none can be in use then). Returns how many."""
        self._root_not_a_link()
        if not os.path.isdir(self.root):
            return 0
        count = 0
        for name in os.listdir(self.root):
            try:
                if self._remove(os.path.join(self.root, name)):
                    count += 1
            except Exception:  # noqa: BLE001 - one bad folder doesn't stop the rest
                log.exception("could not remove a leftover job folder")
        if count:
            log.info("removed %d job folder(s) left from turns that never finished", count)
        return count

    def _root_not_a_link(self) -> None:
        """The jobs root is a real folder of ours. A link put in its place (by an agent) is removed, never followed:
        everything under the root is deleted at start."""
        if os.path.islink(self.root):
            log.warning("the jobs folder was a link; the link is removed")
            os.unlink(self.root)

    def _remove(self, folder: str) -> bool:
        """Delete one job folder, only when it really is one: directly under the jobs root, which is a real folder.
        A link there is removed itself, never followed."""
        if not os.path.lexists(folder):
            return False
        if os.path.dirname(os.path.abspath(folder)) != os.path.abspath(self.root) or os.path.islink(self.root):
            log.warning("not removing %s: it is not a job folder", folder)
            return False
        if os.path.islink(folder) or not os.path.isdir(folder):
            os.unlink(folder)
            return True
        if os.path.dirname(os.path.realpath(folder)) != os.path.realpath(self.root):
            log.warning("not removing %s: it is not a job folder", folder)
            return False

        def writable_and_retry(func: Callable[[str], Any], path: str, *_: Any) -> None:
            # A read-only folder (a package cache, git's objects) can't be emptied until it is writable again.
            for p in (os.path.dirname(path), path):
                try:
                    if not os.path.islink(p):
                        os.chmod(p, stat.S_IRWXU)
                except OSError:
                    pass
            func(path)

        try:
            shutil.rmtree(folder, onexc=writable_and_retry)  # Python 3.12+
        except TypeError:
            shutil.rmtree(folder, onerror=writable_and_retry)
        return not os.path.lexists(folder)

    # --- the processes --------------------------------------------------------------------------------------

    def _never(self) -> set[int]:
        never = {1, os.getpid(), os.getppid()}
        try:
            never.update(p for p in self.protected() if p)
        except Exception:  # noqa: BLE001
            log.exception("could not read the protected processes")
        return never

    def processes(self, job: CrewJob) -> list[int]:
        """The job's live processes: in its folder, or carrying its mark."""
        folder = os.path.realpath(self.path(job))
        mark = f"\0{JOB_MARK}={job.id}\0".encode()  # one whole entry: not the start of a longer id
        never = self._never()
        found: list[int] = []
        try:
            names = os.listdir(self.proc_root)
        except OSError:
            log.warning("cannot list processes in %s; the job's processes are not stopped", self.proc_root)
            return []
        for name in names:
            if not name.isdigit() or int(name) in never:
                continue
            base = os.path.join(self.proc_root, name)
            if self._state(base) in ("Z", "X", ""):
                continue  # gone, or a zombie its parent will reap
            if _inside(self._cwd(base), folder) or mark in self._environ(base):
                found.append(int(name))
        return sorted(found)

    def stop_processes(self, job: CrewJob) -> int:
        """SIGTERM every process of the job, SIGKILL what is left after the grace period. Returns how many were
        found in all."""
        stopped: set[int] = set()
        for _ in range(1 + ROUNDS):
            pids = self.processes(job)
            if not pids:
                break
            stopped.update(pids)
            self._signal(pids, signal.SIGTERM)
            left = self._wait_gone(job, pids)
            if left:
                log.info("job %s: %d process(es) still there after %.0fs; killing", job.id, len(left), self.grace)
                self._signal(left, signal.SIGKILL)
                self._wait_gone(job, left, timeout=2.0)
        return len(stopped)

    def _signal(self, pids: list[int], sig: int) -> None:
        for pid in pids:
            try:
                self._kill(pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                log.warning("not allowed to stop process %d of a job", pid)

    def _wait_gone(self, job: CrewJob, pids: list[int], timeout: Optional[float] = None) -> list[int]:
        deadline = time.monotonic() + (self.grace if timeout is None else timeout)
        left = list(pids)
        while True:
            alive = set(self.processes(job))
            left = [p for p in left if p in alive]
            if not left or time.monotonic() >= deadline:
                return left
            self._sleep(0.05)

    @staticmethod
    def _state(base: str) -> str:
        try:
            with open(os.path.join(base, "stat"), "rb") as fh:
                raw = fh.read().decode("utf-8", "replace")
        except OSError:
            return ""
        # "pid (comm) S ...": the name may hold spaces and brackets, so read after the last ")".
        rest = raw[raw.rfind(")") + 1:].split()
        return rest[0] if rest else ""

    @staticmethod
    def _cwd(base: str) -> str:
        try:
            cwd = os.readlink(os.path.join(base, "cwd"))
        except OSError:
            return ""
        return cwd[: -len(" (deleted)")] if cwd.endswith(" (deleted)") else cwd

    @staticmethod
    def _environ(base: str) -> bytes:
        try:
            with open(os.path.join(base, "environ"), "rb") as fh:
                raw = fh.read()
        except OSError:
            return b""
        # Entries end with NUL; with one in front too, every entry is NUL-delimited on both sides.
        return b"\0" + raw + (b"" if raw.endswith(b"\0") else b"\0")
