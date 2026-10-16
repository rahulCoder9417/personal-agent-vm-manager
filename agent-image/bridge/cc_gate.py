"""A Claude Code `PreToolUse` hook: Textcrew's own "auto mode" for Claude Code's commands and file changes.

The rule list (docs/backend-report.md in the backend, "Approvals for the agent's own actions"):

- Runs without asking: reading and searching; edits and commands inside the workspace (the job's folder is
  inside it) or the temporary folders; tests and builds; local git; pushing (the git gateway decides about
  pushes: a new branch goes through, a branch that already existed or a force push needs the user's yes).
- Asks first: network commands (curl, wget, ssh, an interpreter's inline code that opens a connection, git
  with an address that is not GitHub) while Scout's browser is on, because only then can the container reach
  any website; changing git's remotes (git remote add / set-url, git config remote.* / url.* / http.* /
  credential.*); deleting or overwriting files outside the workspace (rm, mv, cp, a `>` redirection, Edit and
  Write, ...).
- Never: the platform's own and the credential files (the container token, Claude Code's sign-in, Hermes's
  config), and deleting a branch on GitHub (git push --delete, a `:branch` refspec, --mirror, --prune).

How a call is decided:

- allow: the hook answers "allow". The tool is not on the session's allow list, so without this answer it
  would be refused (the session runs in dontAsk mode).
- never: the hook answers "deny" with the reason.
- ask: the hook asks the backend (POST <gate url> with the container's own token, read from the platform file;
  the backend knows the user from the token). The backend answers `allow` when the user approved exactly this
  call and it was not used yet (it is used now, once); `asked` when it has just asked the user, with the
  usual Approve / Deny card in the chat; `refused` during a crew job, whose brief covers nothing like this.
  Anything but `allow` is a "deny" with the backend's words: the agent ends its turn, and after the yes a new
  turn makes the same call again.

Failing closed: Bash, Edit, Write and NotebookEdit are not on the session's allow list, so a hook that crashes,
cannot start or runs into its timeout means the call is refused (code.claude.com/docs/en/hooks: such a hook
decides nothing, and the permission rules then refuse a tool that is not allowed). Read, Glob and Grep stay on
the allow list (reading needs no OK); the credential files are also in the session's deny rules
(claude.never_rules), which hold whatever this hook does.

This is a careful reading of the command, not a proof: a shell can do anything, and a script written to a file
and run later is not looked into. The walls stay the sandbox (gVisor, a non-root user, the egress proxy) and
the git gateway, which checks every push itself.

How Claude Code runs it: like lead_gate.py, a command hook with `python -I`, the event as JSON on stdin; the
answer is JSON on stdout ({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"
| "deny", "permissionDecisionReason": ...}}), exit code 0. Standard library only.

Usage: python -I cc_gate.py --data /data --workspace /data/workspace --gate-url URL --platform-file PATH
       [--tmp DIR]... [--browser]
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

# The hook starts before every command, so it imports little: no dataclasses or typing (the annotations are
# never evaluated), and glob and urllib only when they are needed.

ALLOW, ASK, NEVER = "allow", "ask", "never"
GATED_WRITES = ("Bash", "Edit", "Write", "NotebookEdit")
READERS = ("Read", "Glob", "Grep")
GATED_TOOLS = (*GATED_WRITES, *READERS)
HTTP_TIMEOUT = 15  # seconds; the hook's own timeout (claude.GATE_TIMEOUT) is longer

WHY_NETWORK = "it reaches the internet, and Scout's browser is on, so any website can be reached"
WHY_REMOTE = "it changes where git fetches from or pushes to"
WHY_OUTSIDE = "it deletes or overwrites files outside the workspace"
WHY_UNKNOWN = "the files it changes could not be told, and they may be outside the workspace"

NEVER_FILES = ("This touches the platform's own files (the container's token, the sign-in of Claude Code, the agent's "
               "config). That is never allowed, and asking your user does not change it. Do it another way.")
NEVER_DELETE = ("Deleting a branch on GitHub is never allowed (git push --delete, a \":branch\" refspec, --mirror, "
                "--prune). Leave the branch as it is.")
NEVER_SEARCH = ("Searching from here would read the platform's own files. Search inside the workspace, or in a "
                "folder that holds none of them.")
UNREADABLE = "The gate could not read this command, so it was not run. Write it more simply."


class Context:
    """What the hook is told on its command line."""

    def __init__(self, data: str = "/data", workspace: str = "/data/workspace", gate_url: str = "",
                 platform_file: str = "", tmp: list[str] | None = None, browser: bool = False, home: str = ""):
        self.data, self.workspace, self.gate_url, self.platform_file = data, workspace, gate_url, platform_file
        self.tmp = ["/tmp"] if tmp is None else tmp
        self.browser, self.home = browser, home

    @property
    def never_dirs(self) -> list[str]:
        d = self.data.rstrip("/") or "/"
        return [os.path.join(d, ".platform"), os.path.join(d, "hermes"), os.path.join(d, "claude")]

    @property
    def read_ok(self) -> list[str]:
        """Inside a never folder, what may still be read: Claude Code's own saved tool output and transcripts."""
        return [os.path.join(self.data.rstrip("/") or "/", "claude", "projects")]

    @property
    def write_roots(self) -> list[str]:
        return [self.workspace, *self.tmp]


def _norm(path: str) -> str:
    return os.path.normpath(path) if path else path


def _inside(path: str, root: str) -> bool:
    root = root.rstrip("/") or "/"
    return path == root or path.startswith(root + "/") or root == "/"


def _real(path: str) -> str:
    """The path with every link that exists resolved (a link into a never folder counts as that folder)."""
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return _norm(path)


_PROC_SECRET = re.compile(r"^/proc/[^/]+/(environ|cmdline|mem|maps)$")


def never_path(path: str, ctx: Context, reading: bool = False) -> bool:
    for p in {_norm(path), _real(path)}:
        if _PROC_SECRET.match(p):
            return True
        for d in ctx.never_dirs:
            if _inside(p, d) and not (reading and any(_inside(p, ok) for ok in ctx.read_ok)):
                return True
    return False


def holds_never(path: str, ctx: Context) -> bool:
    """A folder that a recursive search from here would read a never folder through (e.g. / or /data)."""
    p = _real(path)
    return any(_inside(d, p) for d in ctx.never_dirs)


def may_write(path: str, ctx: Context, deleting: bool = False) -> bool:
    """Inside the workspace or a temporary folder. Deleting or moving one of those folders itself is not."""
    p = _real(path)
    if p in ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty") or p.startswith("/dev/fd/"):
        return True
    for root in (_real(r) for r in ctx.write_roots):
        if root != "/" and _inside(p, root) and not (deleting and p == root):
            return True
    return False


def resolve(word: str, cwd: Optional[str], ctx: Context) -> Optional[str]:
    """A word of a command as a path, or None when it can't be told (a variable we don't know, an unknown folder)."""
    w = word
    for name, value in (("HOME", ctx.home), ("TMPDIR", ctx.tmp[0] if ctx.tmp else ""), ("PWD", cwd or ""),
                        ("TEXTCREW_JOB_DIR", os.environ.get("TEXTCREW_JOB_DIR", ""))):
        if value:
            w = w.replace("${" + name + "}", value).replace("$" + name, value)
    if w == "~" or w.startswith("~/"):
        if not ctx.home:
            return None
        w = ctx.home + w[1:]
    if "$" in w or "`" in w:
        return None
    if not w.startswith("/"):
        if cwd is None:
            return None
        w = os.path.join(cwd, w)
    return _norm(w)


def expand(path: str) -> list[str]:
    """A path with glob characters: the files it names now, and the folder it starts from."""
    if not any(c in path for c in "*?["):
        return [path]
    import glob

    found = glob.glob(path)
    head = path
    while any(c in head for c in "*?[") and head not in ("/", ""):
        head = os.path.dirname(head)
    return [*found, head or "/"]


# --- reading a command --------------------------------------------------------------------------------------------

_SEPARATORS = {";", "&&", "||", "|", "&", "|&", "\n", "(", ")", "{", "}", "!", ";;"}
_REDIRECTS = {">", ">>", ">|", "&>", "&>>", "<", "<<", "<<<", "<>", "1>", "2>", "1>>", "2>>"}
_HEREDOC = re.compile(r"(?<!<)<<-?(?!<)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_SUBST = re.compile(r"\$\(([^()]*(?:\([^()]*\)[^()]*)*)\)|`([^`]*)`")
# Shell words before the command itself: `if x; then rm y; fi` runs rm.
_KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "{", "}", "[[", "]]"}
_WRAPPERS = {"sudo", "nohup", "time", "nice", "ionice", "exec", "command", "builtin", "stdbuf", "env", "timeout",
             "xargs", "watch", "doas", "setsid", "chronic", "unbuffer"}


def strip_heredocs(command: str) -> tuple[str, list[str]]:
    """The command without the bodies of its here-documents (they are data, not commands), and those bodies."""
    lines = command.split("\n")
    out: list[str] = []
    bodies: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in _HEREDOC.finditer(line):
            word = m.group(2)
            body: list[str] = []
            while i < len(lines) and lines[i].strip() != word:
                body.append(lines[i])
                i += 1
            i += 1  # the closing word
            bodies.append("\n".join(body))
    return "\n".join(out), bodies


def substitutions(command: str) -> list[str]:
    """The commands inside $( ... ) and backticks: they run too."""
    return [m.group(1) if m.group(1) is not None else m.group(2) for m in _SUBST.finditer(command)]


def tokens(command: str) -> list[str]:
    lex = shlex.shlex(command.replace("\\\n", " "), posix=True, punctuation_chars=";&|()<>\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    # No comments: bash starts one only at the start of a word ("foo#; rm x" runs rm), so "#" is read as a word and
    # whatever follows it is looked at too.
    lex.commenters = ""
    return list(lex)


def simple_commands(command: str) -> list[list[str]]:
    """The command split into simple commands: each a list of words, redirections kept as ("<op>", target)."""
    out: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens(command):
        if tok in _SEPARATORS or (tok and set(tok) <= set(";&|()\n") and tok not in _REDIRECTS):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        out.append(cur)
    return out


def _program(words: list[str]) -> tuple[str, list[str]]:
    """The program a simple command runs and its arguments, past `VAR=value`, sudo, env, timeout and the like."""
    i = 0
    while i < len(words):
        w = words[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            i += 1
            continue
        name = os.path.basename(w)
        if w in _KEYWORDS:
            i += 1
            continue
        if name in _WRAPPERS:
            i += 1
            # their own options and values: `timeout 5`, `env -i`, `nice -n 5`, `sudo -u x`
            while i < len(words) and (words[i].startswith("-") or re.match(r"^[0-9.]+[smhd]?$", words[i])):
                i += 1
            continue
        return name, words[i + 1:]
    return "", []


def _split_redirections(words: list[str]) -> tuple[list[str], list[str]]:
    """The words without redirections, and the files the command writes to through them."""
    args: list[str] = []
    writes: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        m = re.match(r"^([0-9]*|&)(>>?|>\||<>?|<<<?)(.*)$", w)
        if w in _REDIRECTS or (m and m.group(2)):
            op = m.group(2) if m else w
            target = m.group(3) if m and m.group(3) else (words[i + 1] if i + 1 < len(words) else "")
            if not (m and m.group(3)):
                i += 1
            if ">" in op and not target.startswith("&"):
                writes.append(target)
            i += 1
            continue
        args.append(w)
        i += 1
    return args, writes


def _paths(args: Iterable[str]) -> list[str]:
    """The arguments that are not options (the value of `--opt=value` counts too)."""
    out: list[str] = []
    end_of_options = False
    for a in args:
        if a == "--":
            end_of_options = True
            continue
        if not end_of_options and a.startswith("-"):
            if "=" in a:
                out.append(a.split("=", 1)[1])
            continue
        out.append(a)
    return out


NET_PROGRAMS = {"curl", "wget", "nc", "ncat", "netcat", "socat", "telnet", "ssh", "scp", "sftp", "ftp", "lftp",
                "http", "https", "xh", "aria2c", "lynx", "w3m", "links", "elinks", "dig", "nslookup", "host",
                "whois", "openssl", "websocat", "grpcurl", "httpie"}
INTERPRETERS = {"python", "python3", "node", "nodejs", "deno", "bun", "perl", "ruby", "php", "pwsh", "lua", "Rscript"}
INLINE_FLAGS = {"-c", "-e", "--eval", "-E", "-r", "--command", "-p", "--print", "eval", "-"}
NET_MARKERS = ("http://", "https://", "socket", "urllib", "requests", "http.client", "httpx", "aiohttp", "fetch(",
               "xmlhttprequest", "net.connect", "lwp::", "net::http", "open-uri", "websocket", "ftplib", "smtplib")
_URL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*)://([^/\s:@'\"]+@)?([^/\s:'\"]+)")
GITHUB_HOSTS = ("github.com", "api.github.com", "codeload.github.com", "raw.githubusercontent.com",
                "objects.githubusercontent.com")
DELETES = {"rm", "rmdir", "unlink", "shred", "truncate", "srm"}
CHANGES = {"chmod", "chown", "chgrp", "touch", "setfacl", "chattr"}
COPIES = {"cp", "install", "ln", "rsync"}
# Programs that read whole folders: from / or /data they would read the platform's files too.
ALWAYS_RECURSIVE = {"rg", "ag", "ack", "tar", "zip", "7z", "rsync", "scp"}
RECURSIVE_WITH_FLAG = {"grep", "egrep", "fgrep", "cp", "chmod", "chown"}
_SECRET_VARS = re.compile(r"\$\{?(CLAUDE_CONFIG_DIR|PLATFORM_FILE|HERMES_HOME)\b")
_SECRET_PROC = re.compile(r"/proc/[^/\s]*/(environ|mem)\b")
_GIT_ENV = re.compile(r"^(GIT_CONFIG|GIT_SSH|GIT_PROXY|GIT_ASKPASS)[A-Z0-9_]*=")
_GIT_REMOTE_KEYS = re.compile(r"(?i)^(remote\.|url\.|http\.|https\.|credential|include|includeif|core\.sshcommand"
                              r"|core\.gitproxy|protocol\.|submodule\.|core\.hookspath|core\.fsmonitor)")
_GIT_CONFIG_READS = {"--get", "--get-all", "--get-regexp", "--list", "-l", "--get-urlmatch", "--show-origin",
                     "--show-scope", "--name-only"}


class Verdict:
    """allow | ask | never, and why (for ask: the words the user reads; for never: the agent's reason)."""

    def __init__(self, decision: str, why: str = ""):
        self.decision, self.why = decision, why

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Verdict) and (self.decision, self.why) == (other.decision, other.why)

    def __repr__(self) -> str:
        return f"Verdict({self.decision!r}, {self.why!r})"

    def worse(self, other: "Verdict") -> "Verdict":
        rank = {ALLOW: 0, ASK: 1, NEVER: 2}
        return other if rank[other.decision] > rank[self.decision] else self


def _non_github_url(words: Iterable[str]) -> bool:
    for w in words:
        for m in _URL.finditer(w):
            scheme, host = m.group(1).lower(), m.group(3).lower()
            if scheme == "file":
                continue
            if host not in GITHUB_HOSTS:
                return True
        if re.match(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:", w) and not w.split("@", 1)[1].startswith("github.com:"):
            return True  # scp-like git address: user@host:path
    return False


def _git(args: list[str], ctx: Context) -> Verdict:
    """git: local work runs; the gateway decides about pushes; remotes, deletes and other hosts are the rule list's."""
    # `git -c key=value`, `git -C dir`, `--git-dir=...` come before the subcommand.
    i = 0
    v = Verdict(ALLOW)
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a == "-c" and i + 1 < len(args):
            if _GIT_REMOTE_KEYS.match(args[i + 1].split("=", 1)[0]):
                v = v.worse(Verdict(ASK, WHY_REMOTE))
            i += 2
            continue
        if a in ("-C", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(args):
            i += 2
            continue
        i += 1
    if i >= len(args):
        return v
    sub, rest = args[i], args[i + 1:]
    if sub == "push":
        if any(a in ("--delete", "-d", "--mirror", "--prune") or (a.startswith(":") and len(a) > 1)
               or a.startswith("+:") for a in rest):
            return Verdict(NEVER, NEVER_DELETE)
        if ctx.browser and _non_github_url(rest):
            return v.worse(Verdict(ASK, WHY_NETWORK))
        return v
    if sub == "remote":
        action = next((a for a in rest if not a.startswith("-")), "")
        if action in ("add", "set-url", "rename", "remove", "rm", "set-head", "set-branches"):
            return v.worse(Verdict(ASK, WHY_REMOTE))
        return v
    if sub == "config":
        if any(a in _GIT_CONFIG_READS for a in rest):
            return v
        keys = [a for a in rest if not a.startswith("-")]
        if keys and _GIT_REMOTE_KEYS.match(keys[0]) and (len(keys) > 1 or any(
                a in ("--unset", "--unset-all", "--add", "--replace-all", "--remove-section", "--rename-section",
                      "-e", "--edit") for a in rest)):
            return v.worse(Verdict(ASK, WHY_REMOTE))
        if any(a in ("-e", "--edit", "--file", "-f") for a in rest):
            return v.worse(Verdict(ASK, WHY_REMOTE))
        return v
    if sub in ("clone", "fetch", "pull", "ls-remote", "submodule", "archive") and ctx.browser and _non_github_url(rest):
        return v.worse(Verdict(ASK, WHY_NETWORK))
    return v


def _written_paths(program: str, args: list[str]) -> Optional[list[str]]:
    """What a program deletes or overwrites, of its arguments (None: it changes nothing we look at)."""
    paths = _paths(args)
    if program in DELETES:
        return paths
    if program == "mv":
        return paths
    if program in CHANGES:
        return paths[1:] if program in ("chmod", "chown", "chgrp", "setfacl", "chattr") else paths
    if program in COPIES:
        if "-t" in args:
            i = args.index("-t")
            return [args[i + 1]] if i + 1 < len(args) else []
        target = [a.split("=", 1)[1] for a in args if a.startswith("--target-directory=")]
        if target:
            return target
        return paths[-1:] if len(paths) >= 2 else []
    if program == "tee":
        return paths
    if program == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    if program in ("sed", "perl") and any(a == "-i" or a.startswith("-i") or a.startswith("--in-place") for a in args):
        return paths[1:] if program == "sed" else paths[1:]
    if program == "find" and any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for a in args):
        roots = []
        for a in args:
            if a.startswith("-") or a in ("(", ")", "!"):
                break
            roots.append(a)
        return roots or ["."]
    if program in ("tar",) and any(re.match(r"^-?[a-zA-Z]*x", a) for a in args[:1]) or (
            program == "tar" and any(a in ("-x", "--extract", "--get") for a in args)):
        if "-C" in args:
            i = args.index("-C")
            return [args[i + 1]] if i + 1 < len(args) else []
        return ["."]
    if program == "unzip":
        if "-d" in args:
            i = args.index("-d")
            return [args[i + 1]] if i + 1 < len(args) else []
        return ["."]
    return None


def _check_paths(words: list[str], cwd: Optional[str], ctx: Context, why: str, deleting: bool = False) -> Verdict:
    v = Verdict(ALLOW)
    for w in words:
        if not w:
            continue
        p = resolve(w, cwd, ctx)
        if p is None:
            v = v.worse(Verdict(ASK, WHY_UNKNOWN))
            continue
        for one in expand(p):
            if never_path(one, ctx):
                return Verdict(NEVER, NEVER_FILES)
            if not may_write(one, ctx, deleting):
                v = v.worse(Verdict(ASK, why))
    return v


def bash(command: str, cwd: Optional[str], ctx: Context, depth: int = 0) -> Verdict:
    """The verdict on one Bash command line (and on what it runs through bash -c, eval and $( ... ))."""
    if depth > 4:
        return Verdict(ASK, WHY_UNKNOWN)
    text = command or ""
    # The platform's own files, named in any way: never, whatever the command does with them.
    low = text.lower()
    for d in ctx.never_dirs:
        if d.lower() in low:
            return Verdict(NEVER, NEVER_FILES)
    if ".credentials.json" in low or _SECRET_VARS.search(text) or _SECRET_PROC.search(text):
        return Verdict(NEVER, NEVER_FILES)
    body_less, bodies = strip_heredocs(text)
    v = Verdict(ALLOW)
    for inner in substitutions(body_less):
        v = v.worse(bash(inner, cwd, ctx, depth + 1))
        if v.decision == NEVER:
            return v
    try:
        commands = simple_commands(body_less)
    except ValueError:
        return Verdict(NEVER, UNREADABLE)
    here = cwd
    for words in commands:
        args_all, redirected = _split_redirections(words)
        program, args = _program(args_all)
        wrapped_xargs = any(os.path.basename(w) == "xargs" for w in args_all[: max(len(args_all) - len(args) - 1, 0)])
        # Every word that names a path inside the platform's files: never (also through a link or a glob).
        for w in [*args, *redirected]:
            for part in ([w] + ([w.split("=", 1)[1]] if "=" in w else [])):
                p = resolve(part, here, ctx)
                if p and any(never_path(one, ctx, reading=True) for one in expand(p)):
                    return Verdict(NEVER, NEVER_FILES)
        if any(_GIT_ENV.match(w) for w in args_all):
            v = v.worse(Verdict(ASK, WHY_REMOTE))
        recursive = program in ALWAYS_RECURSIVE or (
            program in RECURSIVE_WITH_FLAG and any(re.match(r"^-[a-zA-Z]*[rRa]", a) or a in (
                "--recursive", "--dereference-recursive", "--archive") for a in args)) or (
            program == "find" and any(a in ("-exec", "-execdir", "-ok", "-okdir") for a in args))
        if recursive:
            for w in _paths(args):
                p = resolve(w, here, ctx)
                if p and holds_never(p, ctx):
                    return Verdict(NEVER, NEVER_SEARCH)
        # Redirections that write a file.
        v = v.worse(_check_paths(redirected, here, ctx, WHY_OUTSIDE))
        if not program:
            continue
        if program in ("cd", "pushd"):
            dest = _paths(args)[:1]
            if not dest:
                here = ctx.home or None
            elif dest[0] == "-":
                here = None
            else:
                here = resolve(dest[0], here, ctx)
            continue
        if program in ("bash", "sh", "zsh", "dash", "ksh") and "-c" in args:
            i = args.index("-c")
            if i + 1 < len(args):
                v = v.worse(bash(args[i + 1], here, ctx, depth + 1))
            continue
        if program == "eval":
            v = v.worse(bash(" ".join(args), here, ctx, depth + 1))
            continue
        if program == "git":
            v = v.worse(_git(args, ctx))
            continue
        if ctx.browser:
            if program in NET_PROGRAMS:
                v = v.worse(Verdict(ASK, WHY_NETWORK))
            elif program == "rsync" and any(":" in a and not a.startswith("-") for a in args):
                v = v.worse(Verdict(ASK, WHY_NETWORK))
            elif program in INTERPRETERS or re.match(r"^python3?\.[0-9]+$", program):
                inline = [args[i + 1] for i, a in enumerate(args[:-1]) if a in INLINE_FLAGS]
                code = " ".join([*inline, *bodies]).lower()
                if any(m in code for m in NET_MARKERS):
                    v = v.worse(Verdict(ASK, WHY_NETWORK))
        written = _written_paths(program, args)
        if written is not None:
            if wrapped_xargs:
                v = v.worse(Verdict(ASK, WHY_UNKNOWN))
            v = v.worse(_check_paths(written, here, ctx, WHY_OUTSIDE,
                                     deleting=program in DELETES or program == "mv"))
        if v.decision == NEVER:
            return v
    return v


def decide(event: dict, ctx: Context) -> Verdict:
    """The verdict on one tool call (only the gated tools; any other passes)."""
    tool = event.get("tool_name")
    args = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) and event.get("cwd") else ctx.workspace
    if tool == "Bash":
        return bash(str(args.get("command") or ""), cwd, ctx)
    if tool in ("Edit", "Write", "NotebookEdit"):
        path = str(args.get("file_path") or args.get("notebook_path") or "")
        p = resolve(path, cwd, ctx) if path else None
        if p is None:
            return Verdict(ASK, WHY_UNKNOWN)
        if never_path(p, ctx):
            return Verdict(NEVER, NEVER_FILES)
        return Verdict(ALLOW) if may_write(p, ctx) else Verdict(ASK, WHY_OUTSIDE)
    if tool == "Read":
        p = resolve(str(args.get("file_path") or ""), cwd, ctx)
        return Verdict(NEVER, NEVER_FILES) if p and never_path(p, ctx, reading=True) else Verdict(ALLOW)
    if tool in ("Glob", "Grep"):
        root = resolve(str(args.get("path") or cwd), cwd, ctx)
        pattern = str(args.get("pattern") or "") if tool == "Glob" else ""
        if pattern.startswith("/"):
            root = pattern
            while any(c in root for c in "*?[{") and root not in ("/", ""):
                root = os.path.dirname(root)
        if root and (never_path(root, ctx, reading=True) or holds_never(root, ctx)):
            return Verdict(NEVER, NEVER_FILES if never_path(root, ctx, reading=True) else NEVER_SEARCH)
        return Verdict(ALLOW)
    return Verdict(ALLOW)


# --- asking the backend ---------------------------------------------------------------------------------------------

def target_of(event: dict) -> str:
    args = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    if event.get("tool_name") == "Bash":
        return str(args.get("command") or "")
    return str(args.get("file_path") or args.get("notebook_path") or "")


def container_token(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        token = json.load(fh).get("mcp_token")
    if not isinstance(token, str) or not token:
        raise ValueError("no container token")
    return token


def ask_backend(event: dict, why: str, ctx: Context) -> dict:
    """POST the call to the gate. Returns the backend's answer ({decision, message, approvalId?})."""
    import urllib.request  # only here: most calls never ask, and the hook starts faster without it

    body = {"tool": event.get("tool_name"), "target": target_of(event), "why": why,
            "cwd": event.get("cwd") if isinstance(event.get("cwd"), str) else None}
    req = urllib.request.Request(ctx.gate_url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {container_token(ctx.platform_file)}")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # env proxies apply, as for the MCP
        answer = json.load(resp)
    if not isinstance(answer, dict) or answer.get("decision") not in ("allow", "asked", "refused"):
        raise ValueError("not a gate answer")
    return answer


def _answer(decision: str, reason: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                              "permissionDecisionReason": reason}})


SPECIALIST = " You are a specialist: stop here and tell the lead, who ends the turn."


def run(event: Any, ctx: Context) -> tuple[str, str]:
    """(allow | deny, reason) for one call."""
    if not isinstance(event, dict) or event.get("tool_name") not in GATED_TOOLS:
        return "allow", ""
    verdict = decide(event, ctx)
    if verdict.decision == ALLOW:
        return "allow", "Textcrew's rules: this runs without asking."
    if verdict.decision == NEVER:
        return "deny", verdict.why
    sub = bool(event.get("agent_id") or event.get("agent_type"))
    try:
        answer = ask_backend(event, verdict.why, ctx)
    except (OSError, ValueError) as exc:  # urllib's errors are OSErrors
        return "deny", (f"This needs your user's OK ({verdict.why}), and they could not be asked just now "
                        f"({type(exc).__name__}). It was not run. Say so in your answer.")
    if answer["decision"] == "allow":
        return "allow", str(answer.get("message") or "Your user approved this.")
    return "deny", str(answer.get("message") or "This needs your user's OK.") + (SPECIALIST if sub else "")


def parse_args(argv: list[str]) -> Context:
    ctx = Context(tmp=[])
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--browser":
            ctx.browser = True
            i += 1
            continue
        value = argv[i + 1] if i + 1 < len(argv) else ""
        if a == "--data":
            ctx.data = value
        elif a == "--workspace":
            ctx.workspace = value
        elif a == "--gate-url":
            ctx.gate_url = value
        elif a == "--platform-file":
            ctx.platform_file = value
        elif a == "--tmp":
            ctx.tmp.append(value)
        elif a == "--home":
            ctx.home = value
        i += 2
    ctx.tmp = ctx.tmp or ["/tmp"]
    return ctx


def main(argv: list[str]) -> int:
    ctx = parse_args(argv)
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        # Nothing to decide on: no answer, so the session's rules apply (a gated write is refused).
        return 0
    try:
        decision, reason = run(event, ctx)
    except Exception as exc:  # noqa: BLE001 - whatever goes wrong, a gated write is refused, never run
        tool = event.get("tool_name") if isinstance(event, dict) else None
        if tool in GATED_WRITES:
            sys.stdout.write(_answer("deny", f"The gate could not check this ({type(exc).__name__}); it was not run.")
                             + "\n")
        return 0
    sys.stdout.write(_answer(decision, reason) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
