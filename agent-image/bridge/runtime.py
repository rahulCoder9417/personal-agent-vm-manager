"""Picks the agent runtime for each delivery from platform.json ("runtime": hermes | claude_code).

The vm-manager rewrites platform.json (SetAgentRuntime) while the container runs, so the
file is read again for every delivery and a switch needs no restart. A half-written or
unreadable file keeps the last runtime that was read successfully.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from .claude import RUNTIME_HERMES
from .platform import PlatformFile, load_platform_file

log = logging.getLogger("bridge.runtime")


class PlatformReader:
    """Reads platform.json on demand, falling back to the last good copy."""

    def __init__(self, path: str, initial: PlatformFile):
        self.path = path
        self._last = initial
        self._lock = threading.Lock()

    def load(self) -> PlatformFile:
        try:
            pf = load_platform_file(self.path)
        except (OSError, ValueError) as exc:
            log.warning("could not re-read the platform file (%s); using the last good copy", type(exc).__name__)
            with self._lock:
                return self._last
        with self._lock:
            self._last = pf
        return pf


class RuntimeRunner:
    """Runner that dispatches each turn to the runtime named in platform.json."""

    def __init__(self, load_platform: Callable[[], PlatformFile], runners: dict[str, Any],
                 default: str = RUNTIME_HERMES):
        self.load_platform = load_platform
        self.runners = runners
        self.default = default

    def runtime(self) -> str:
        rt = self.load_platform().runtime
        if rt not in self.runners:
            log.warning("unknown runtime %r; using %s", rt, self.default)
            return self.default
        return rt

    def healthy(self) -> bool:
        return self.runners[self.runtime()].healthy()

    def run(self, **kwargs: Any) -> Any:
        return self.runners[self.runtime()].run(**kwargs)
