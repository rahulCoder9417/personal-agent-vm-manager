"""Bridge: the only way into a user's agent container.

The vm-manager calls it over HTTP on the sandbox network (see README "Bridge
contract"). It runs Hermes Agent as a child process (its local OpenAI-style API
server on 127.0.0.1 only) and drives one Hermes run per delivered message.
"""

__version__ = "0.1.0"
