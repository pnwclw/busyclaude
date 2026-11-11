#!/usr/bin/env python3
"""BusyClaude, the half that runs on your computer.

    python3 busyclaude.py setup      install the app on the bar, add the Claude Code hooks, say hello
    python3 busyclaude.py test perm  put a sample prompt on the bar and print which button you pressed
    python3 busyclaude.py status     what is set up, and what the bar is holding
    python3 busyclaude.py demo       walk the mascot through every kind of notification, silently
    python3 busyclaude.py usage 37 5 hand over the 5-hour and weekly numbers yourself
    python3 busyclaude.py hook       what Claude Code runs for every event; the payload is on stdin

The other half is the app on the BUSY Bar (src/). Nothing runs in between: no
server, no daemon. A hook is a process that lives for one event:

    1. notes what the bar is showing, so it can be put back afterwards
    2. reads the bar's mailbox      GET  /api/storage/read
    3. writes the event into it     POST /api/storage/write
    4. launches the app             POST /api/apps/launch   (what `busy-cli app run` does)
    5. for a prompt or a question, reads the mailbox until a button was pressed
    6. once the app says it is done, puts back what was there

The mailbox is the app's `localStorage`, which the firmware keeps as one JSON
file on the bar's storage (src/mailbox.ts has the three keys). An app on the bar
has no way to be sent anything, and this is the one place both sides can reach.

Standard library only, Python 3.9+. Everything a hook does is logged to
~/.cache/busyclaude/events.jsonl; it prints nothing and always exits 0, with one
exception: a prompt answered on the bar prints that decision.
"""
import argparse
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# Must match src/appmeta/manifest.json; test/host.test.mjs checks that it does.
APP_ID = "app.pnwclw.busyclaude"
# Where the firmware keeps an app's localStorage (js_runner/js_local_storage.c).
MAILBOX_DIR = "/ext/apps_data/jsrunner"
MAILBOX = f"{MAILBOX_DIR}/{APP_ID}.localstorage.json"
MAILBOX_FORMAT = 1  # the firmware's file format
INBOX_VERSION = 1  # ours, inside it

DEFAULT_ADDR = "10.0.4.20"  # the bar's fixed address over USB
CONFIG_FILE = os.path.expanduser("~/.config/busyclaude/config.json")
CACHE_DIR = os.path.expanduser("~/.cache/busyclaude")
LOG_FILE = os.path.join(CACHE_DIR, "events.jsonl")
LOG_KEEP = 200  # lines retained when the log is trimmed
STATE_FILE = os.path.join(CACHE_DIR, "state.json")
LOCK_FILE = os.path.join(CACHE_DIR, "lock")
CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
# This repository is also a Claude Code plugin: .claude-plugin/, hooks/, skills/.
PLUGIN_NAME = "busyclaude"
# What a plugin's hook is given to run. Claude Code fills in where it put the plugin.
PLUGIN_HOOK_COMMAND = 'python3 "${CLAUDE_PLUGIN_ROOT}/busyclaude.py" hook --plugin'

# A request is ~60-150 ms over USB; the bar spikes past a second at random.
TIMEOUT = 3.0
INSTALL_TIMEOUT = 120.0
# A live bar accepts a connection in ~30 ms. An unplugged one must not hold up
# a permission prompt for a full request timeout before the terminal gets it.
PROBE_TIMEOUT = 0.3

# --- What an event looks like on the bar --------------------------------------
# Claude Code has dozens of hook events. Only these draw; PreToolUse, PostToolUse
# and the like fire many times per turn and would flash the bar continuously.
ALERT_EVENTS = {
    "Stop", "StopFailure", "Notification", "PermissionRequest",
    "SubagentStop", "TaskCompleted", "Elicitation", "TeammateIdle",
}
# Events that mean "the user just answered": whatever that session had on the
# bar is taken down instead of being left to time out.
DISMISS_EVENTS = {"UserPromptSubmit", "ElicitationResult", "PermissionDenied"}
DISMISS_NOTIFICATION_TYPES = {"elicitation_complete", "elicitation_response"}
# The events the hooks are registered for, and how long Claude Code waits.
HOOK_EVENTS = sorted(ALERT_EVENTS | DISMISS_EVENTS)

# How long a prompt can be answered from the bar: the "Answer time" set on the
# app's Setup screen, one of these. There is a fuse for each (FUSES in
# tools/assets.py and in src/screens.ts), and it burns for exactly that long.
# The long ones are for walking away from the desk: the session stands still
# until a button comes back or the fuse goes out.
WAIT_CHOICES = (15, 30, 60, 300, 600)
DEFAULT_WAIT = 60
# A question has to be read first, so it never gets less than this.
QUESTION_MIN_WAIT = 30
# The app counts a prompt's time from when it started, a moment after the hook
# wrote it, so the hook listens this much longer than the fuse burns.
ANSWER_GRACE = 5.0
ANSWER_POLL = 0.4
# Ten minutes asked about every 0.4 s would be fifteen hundred requests to a
# device whose web server shares one small CPU with everything else. After the
# first minute, whoever was going to reach over has walked away.
ANSWER_POLL_SLOW = 2.0
ANSWER_POLL_AFTER = 60.0

# How long Claude Code waits for each of the hooks. A permission prompt is the
# one event it is held up by, so that hook has to outlast the longest answer
# time; 600 seconds is its own default for a command hook, and nothing in the
# documentation puts a ceiling above that.
DEFAULT_HOOK_TIMEOUT = 10
HOOK_TIMEOUT = {"PermissionRequest": int(max(WAIT_CHOICES) + ANSWER_GRACE + 15)}

# What the confirmation at the end of `setup` waits for a button, in seconds:
# long enough to walk to the bar, and one of WAIT_CHOICES so it has a fuse.
CONFIRM_WAIT = 30

# Asking the user something goes through the permission system like any other
# tool. A question with one list of options is answered from the bar by its
# number: the wheel picks, START takes it. Anything more involved (several
# questions, several answers) is shown and left to the terminal.
QUESTION_TOOL = "AskUserQuestion"
MAX_OPTIONS = 9
USAGE_EVENTS = {"Stop", "SubagentStop", "TaskCompleted"}

# Seconds each kind stays on the bar (src/kinds.ts has the same numbers).
SHOW_SECONDS = {"perm": 10, "question": 10, "ask": 10, "done": 8, "agent": 8,
                "error": 10, "hello": 5}
# Added to those for the item's life in the mailbox: the app takes a moment to
# start, and should run out its own clock rather than be cut short by this one.
START_ALLOWANCE = 5
# A second completion replaces a first one still waiting; a prompt never is.
COLLAPSE_KINDS = {"done", "agent"}
HISTORY_KEEP = 8
# Usage numbers older than this are not shown at all: a stale "42%" is worse
# than no number.
USAGE_MAX_AGE = 15 * 60

# Where the bar's Apps menu remembers the app it would resume
# (applications/main/apps_menu in the firmware).
APPS_MENU_STATE = "/ext/apps_data/apps_menu/settings.json"
# What the bar says when asked to remove an element drawn under another name.
NOT_THE_OWNER = "currently not displaying"
PROBE_NAME = "busyclaude.nobody"
PROBE_ELEMENT = "busyclaude.probe"
# How long after its last item a launch is still taken to be on screen, when
# the hook that should have ended it never reported back.
UP_GRACE = 60

# Line two has room for about 11 characters before it scrolls.
ERROR_TEXT = {
    "rate_limit": "rate limit", "overloaded": "overloaded",
    "authentication_failed": "auth failed", "oauth_org_not_allowed": "org blocked",
    "billing_error": "billing", "invalid_request": "bad request",
    "model_not_found": "no model", "server_error": "server down",
    "max_output_tokens": "too long", "unknown": "API error",
}
PROJECT_MARKERS = (".claude", ".git", "package.json", "pyproject.toml",
                   "go.mod", "Cargo.toml", "Gemfile", "composer.json")
TEXT_ROOM = 47 / 4.0  # px of text column over the rough width of a `small` glyph


def ascii_only(text):
    """The bar's fonts are bitmap ASCII and the draw API rejects anything else."""
    text = str(text).replace("’", "'").replace("—", "-").replace("–", "-")
    text = "".join(ch for ch in text if 0x20 <= ord(ch) <= 0x7E)
    return re.sub(r"\s+", " ", text).strip()


def humanize(text):
    text = re.sub(r"[_-]+", " ", text)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def short_tool(name):
    """mcp__Claude_Browser__computer -> computer: the last segment names the tool."""
    name = ascii_only(name)
    if name.startswith("mcp__"):
        name = name.rsplit("__", 1)[-1]
    return name or "a tool"


def ask_line(tool):
    """'allow Bash?' where there is room, 'computer?' where there is not."""
    tool = short_tool(tool)
    polite = f"allow {tool}?"
    return polite if len(polite) <= TEXT_ROOM else f"{tool}?"


# A prompt says what is about to happen, not just which tool wants to do it:
# START runs it, and "allow Bash?" is not something anyone can judge. For each
# tool, the part of its input that says what, and the word to put before it.
PROMPT_SUBJECT = {
    "Bash": ("command", "$"),
    "Edit": ("file_path", "Edit"), "MultiEdit": ("file_path", "Edit"),
    "Write": ("file_path", "Write"), "NotebookEdit": ("notebook_path", "Edit"),
    "Read": ("file_path", "Read"),
    "WebFetch": ("url", "Fetch"), "WebSearch": ("query", "Search"),
    "Glob": ("pattern", "Glob"), "Grep": ("pattern", "Grep"),
    "Task": ("description", "Agent"), "Agent": ("description", "Agent"),
}
# The line scrolls past at about nine characters a second (HURRY in
# src/screens.ts), so this much can be read once through inside the shortest
# answer time.
PROMPT_TEXT_MAX = 100


def visible(text):
    """Printable ASCII on one line, with a `?` wherever something else stood. A
    prompt must not quietly drop a character from what is about to run."""
    text = str(text).replace("\u2019", "'").replace("\u2014", "-").replace("\u2013", "-")
    text = re.sub(r"\s+", " ", text).strip()
    return "".join(ch if 0x20 <= ord(ch) <= 0x7E else "?" for ch in text)


def prompt_line(payload, project_dir=None):
    """Line two of a permission prompt: '$ rm -rf node_modules', 'Edit src/app.ts'.
    Falls back to 'allow Tool?' for a tool whose input says nothing readable."""
    tool = str(payload.get("tool_name") or "")
    field, word = PROMPT_SUBJECT.get(tool, (None, None))
    tool_input = payload.get("tool_input")
    value = tool_input.get(field) if field and isinstance(tool_input, dict) else None
    if not isinstance(value, str) or not value.strip():
        return ask_line(tool)
    if field.endswith("_path"):
        # Inside the project a path is shown from its root; elsewhere, from home.
        root = project_dir or payload.get("cwd") or ""
        home = os.path.expanduser("~")
        if root and value.startswith(root.rstrip(os.sep) + os.sep):
            value = value[len(root.rstrip(os.sep)) + 1:]
        elif value.startswith(home + os.sep):
            value = "~" + value[len(home):]
    elif field == "url":
        value = re.sub(r"^https?://", "", value)
    text = visible(f"{word} {value}")
    if len(text) > PROMPT_TEXT_MAX:
        text = text[:PROMPT_TEXT_MAX - 3].rstrip() + "..."
    return text


def project_name(payload, project_dir=None):
    """The project a session belongs to. CLAUDE_PROJECT_DIR is the root and does
    not move; `cwd` follows every Bash `cd`, so it is only the fallback."""
    root = project_dir if project_dir and os.path.isdir(project_dir) else None
    if root is None:
        cwd = os.path.abspath(payload.get("cwd") or os.getcwd())
        home = os.path.expanduser("~")
        root = path = cwd
        # Stop below home: ~/.claude exists, and would name every project "you".
        while path.startswith(home + os.sep):
            if any(os.path.exists(os.path.join(path, m)) for m in PROJECT_MARKERS):
                root = path
                break
            path = os.path.dirname(path)
    return ascii_only(os.path.basename(root.rstrip(os.sep))) or "claude"


def dismisses(payload):
    event = payload.get("hook_event_name") or ""
    if event in DISMISS_EVENTS:
        return True
    if event != "Notification":
        return False
    return any(str(payload.get(k, "")) in DISMISS_NOTIFICATION_TYPES
               for k in ("notification_type", "type", "matcher"))


def _notification(payload):
    """Notification carries its own sub-type; fall back to the message text,
    since the field name has moved between versions."""
    ntype = next((v for v in (payload.get(k) for k in
                              ("notification_type", "type", "matcher", "notification"))
                  if isinstance(v, str) and v), "")
    message = ascii_only(payload.get("message") or "")
    probe = f"{ntype} {message}".lower()
    if "permission" in probe:
        tool = re.search(r"\buse\s+([A-Za-z_][\w.:-]*)", message)
        if tool and tool.group(1) == QUESTION_TOOL:
            return "question", "a question"
        return "perm", ask_line(tool.group(1)) if tool else "allow?"
    if "agent_completed" in probe:
        return "done", "agent done"
    if "idle" in probe or "agent_needs_input" in probe or "waiting" in probe:
        return "ask", "your turn"
    return "ask", message or "your turn"


def classify(payload, project_dir=None):
    """(kind, short message) for an event that draws."""
    event = payload.get("hook_event_name") or ""
    if event == "Notification":
        return _notification(payload)
    if event == "PermissionRequest":
        tool = str(payload.get("tool_name") or "")
        return ("question", "a question") if tool == QUESTION_TOOL else ("perm", prompt_line(payload, project_dir))
    if event == "StopFailure":
        code = ascii_only(payload.get("error") or "unknown")
        return "error", ERROR_TEXT.get(code, humanize(code) or "API error")
    if event == "SubagentStop":
        return "agent", "agent done"
    if event == "TaskCompleted":
        return "agent", "task done"
    if event == "Elicitation":
        return "ask", "needs input"
    if event == "TeammateIdle":
        who = ascii_only(payload.get("teammate_name") or "")
        return "ask", f"{who} idle" if who else "teammate idle"
    if event == "Stop":
        return "done", "finished"
    return "ask", humanize(event) or "event"


def permission_decision(answer):
    """The one thing a hook is ever allowed to say on stdout. PermissionRequest
    takes its verdict from a `decision` object and ignores exit code 2."""
    verdict = {"behavior": answer}
    if answer == "deny":
        verdict["message"] = "Denied from the BUSY Bar."
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PermissionRequest", "decision": verdict}})


def question_options(payload):
    """The option labels of a question the bar can answer, or []: exactly one
    question, exactly one answer, a handful of options."""
    questions = (payload.get("tool_input") or {}).get("questions")
    if not isinstance(questions, list) or len(questions) != 1 or not isinstance(questions[0], dict):
        return []
    question = questions[0]
    options = question.get("options")
    if question.get("multiSelect") or not isinstance(options, list):
        return []
    labels = [o.get("label") for o in options if isinstance(o, dict)]
    if not 2 <= len(labels) <= MAX_OPTIONS or not all(isinstance(l, str) and l for l in labels):
        return []
    return labels


def question_decision(payload, number):
    """Answer an AskUserQuestion with its option `number`, counted from 1. The
    tool takes its answer as `answers` beside the questions it was asked."""
    tool_input = payload.get("tool_input") or {}
    question = tool_input["questions"][0]
    answers = {question.get("question") or "": question_options(payload)[number - 1]}
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": "allow", "updatedInput": dict(tool_input, answers=answers)}}})


# --- Small files ----------------------------------------------------------------
def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, data, private=False):
    os.makedirs(os.path.dirname(path), mode=0o700 if private else 0o755, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644),
              "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def record(payload, outcome):
    """One line per event: the only place a hook's result is ever visible."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        line = json.dumps({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": payload.get("hook_event_name"),
            "session": str(payload.get("session_id") or "")[:8],
            "tool": payload.get("tool_name"),
            "outcome": outcome,
        })
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if os.path.getsize(LOG_FILE) > 256 * 1024:
            with open(LOG_FILE, encoding="utf-8") as fh:
                tail = fh.readlines()[-LOG_KEEP:]
            with open(LOG_FILE, "w", encoding="utf-8") as fh:
                fh.writelines(tail)
    except OSError:
        pass


class Lock:
    """One hook at a time between reading the mailbox and launching the app.
    Stop and SubagentStop land in the same second, and each rewrites the file."""

    def __enter__(self):
        self.fh = None
        try:
            import fcntl
            os.makedirs(CACHE_DIR, exist_ok=True)
            self.fh = open(LOCK_FILE, "w")
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass  # no flock here: the race costs a notification at worst
        return self

    def __exit__(self, *exc):
        if self.fh:
            self.fh.close()


# --- Reaching the bar -----------------------------------------------------------
class BarError(Exception):
    """The bar answered, and the answer was no."""

    def __init__(self, status, text):
        super().__init__(f"HTTP {status} {text}".strip())
        self.status = status


class Bar:
    def __init__(self, addr=None, token=None):
        config = read_json(CONFIG_FILE, {}) or {}
        addr = addr or os.environ.get("BUSYBAR_ADDR") or config.get("addr") or DEFAULT_ADDR
        self.token = token or os.environ.get("BUSYBAR_TOKEN") or config.get("token")
        self.base = addr.rstrip("/") if "://" in addr else f"http://{addr}"

    def reachable(self):
        import socket
        import urllib.parse
        url = urllib.parse.urlsplit(self.base)
        try:
            socket.create_connection((url.hostname, url.port or (443 if url.scheme == "https" else 80)),
                                     timeout=PROBE_TIMEOUT).close()
        except OSError:
            return False
        return True

    def call(self, method, path, query=None, body=None, timeout=TIMEOUT):
        """-> the reply body. OSError when the bar is not there, BarError when it refuses."""
        import urllib.error
        import urllib.parse
        import urllib.request
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Content-Type": "application/octet-stream" if isinstance(body, bytes)
                   else "application/json"}
        if self.token:
            headers["X-API-Token"] = self.token
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise BarError(exc.code, exc.read().decode("utf-8", "replace")[:200])

    def json(self, method, path, query=None, body=None, timeout=TIMEOUT):
        raw = self.call(method, path, query, body, timeout)
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    # The mailbox: {"key": "string"}, the app's localStorage.
    def read_box(self):
        try:
            doc = json.loads(self.call("GET", "/api/storage/read", {"path": MAILBOX}))
        except (BarError, ValueError):
            return {}  # no file yet: the app has never run and no hook has written
        data = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(data, dict) or doc.get("format_version") != MAILBOX_FORMAT:
            return {}
        return {k: v for k, v in data.items() if isinstance(v, str)}

    def write_box(self, data):
        body = json.dumps({"format_version": MAILBOX_FORMAT, "data": data}).encode()
        try:
            self.call("POST", "/api/storage/write", {"path": MAILBOX}, body)
        except BarError:
            # The firmware makes this folder the first time any app saves to
            # localStorage; on a bar where none has, it is not there yet.
            try:
                self.call("POST", "/api/storage/mkdir", {"path": MAILBOX_DIR})
            except BarError:
                pass
            self.call("POST", "/api/storage/write", {"path": MAILBOX}, body)

    def launch(self, app_id=APP_ID):
        self.call("POST", "/api/apps/launch", {"app_id": app_id})

    def draws_as(self, name):
        """False when something is on the display that `name` did not draw; None
        when that cannot be told apart from an empty display.

        The bar has no call for "what is running". But removing an element is
        refused differently depending on who owns the display, and asking to
        remove one that never existed changes nothing."""
        try:
            self.call("DELETE", "/api/display/draw",
                      body={"application_name": name, "element_ids": [PROBE_ELEMENT]})
        except BarError as exc:
            return False if NOT_THE_OWNER in str(exc) else None
        return None

    def origin(self):
        """The app on screen right now, or "" for the bar's own screens.

        An app is taken to be up when something is drawn, the Apps menu names
        an app as the one it resumes, and that app is who drew it."""
        try:
            if self.draws_as(PROBE_NAME) is not False:
                return ""  # nothing drawn: a built-in screen, or the Apps menu itself
            doc = json.loads(self.call("GET", "/api/storage/read", {"path": APPS_MENU_STATE}))
            app_id = str((doc.get("values") or {}).get("active_application") or "")
            if app_id and self.draws_as(app_id) is None:
                return app_id
        except (BarError, OSError, ValueError, AttributeError):
            pass
        return ""

    def restore(self):
        """Put back what was on the bar before the app was launched over it.

        Another app is started again in place of ours, which leaves the Apps
        menu's memory of it untouched. Anything else is one of the bar's own
        screens, and quitting the app is what brings that back."""
        state = read_json(STATE_FILE, {}) or {}
        origin = str(state.get("origin") or "")
        write_json(STATE_FILE, {"busy_until": 0, "up": False, "origin": ""})
        if origin:
            try:
                self.launch(origin)
                return f"returned to {origin}"
            except BarError:
                pass  # uninstalled meanwhile, or the bar is busy: fall through
        self.quit()
        return "returned to the bar's own screen"

    def quit(self):
        try:
            self.call("POST", "/api/apps/quit")
        except BarError:
            pass  # 409: nothing is running, which is what was wanted


def loads(text, default=None):
    try:
        value = json.loads(text) if text else default
    except ValueError:
        return default
    return value if isinstance(value, type(default)) or default is None else default


def answer_window(bar, question):
    """Seconds a prompt may be answered in: the app's "Answer time" setting."""
    wait = DEFAULT_WAIT
    try:
        values = bar.json("GET", "/api/apps/settings", {"app_id": APP_ID}).get("values") or {}
        if int(values.get("wait")) in WAIT_CHOICES:
            wait = int(values["wait"])
    except (BarError, OSError, TypeError, ValueError):
        pass  # an older app on the bar, or no settings yet: the default
    return max(wait, QUESTION_MIN_WAIT) if question else wait


def nonce():
    return f"{int(time.time() * 1000):x}{random.randrange(0x10000):04x}"


# --- Delivering one event -------------------------------------------------------
def live_items(inbox, now):
    return [i for i in inbox.get("items", [])
            if isinstance(i, dict) and float(i.get("exp") or 0) > now]


def pack(inbox, items, history, usage, fresh, now):
    """The inbox document. Each item carries `exp` for the next hook, on this
    computer's clock, and `ttl` for the app, which has only its own."""
    for item in items:
        item["ttl"] = max(1, int(round(item["exp"] - now)))
    doc = {"v": INBOX_VERSION, "n": nonce(), "fresh": fresh, "items": items,
           "history": history[:HISTORY_KEEP]}
    usage = usage or inbox.get("usage")
    try:
        if usage and now - float(usage["at"]) <= USAGE_MAX_AGE:
            doc["usage"] = usage
    except (KeyError, TypeError, ValueError):
        pass  # numbers with no date on them are not shown
    return doc


def deliver(bar, payload, project_dir=None, usage=None):
    """Put one event on the bar. -> {"outcome", "id", "n", "ask", "wait"}."""
    event = payload.get("hook_event_name") or ""
    kind, text = ((payload.get("kind"), payload.get("text")) if event == "Test"
                  else classify(payload, project_dir))
    sid = str(payload.get("session_id") or "")
    # Only a PermissionRequest has a hook waiting for the answer: a prompt takes
    # yes or no, a question the number of one of its options.
    waited_for = event in ("PermissionRequest", "Test")
    options = len(question_options(payload)) if kind == "question" and waited_for else 0
    # `ask` and `window` are how `setup` has the bar ask for a button of its
    # own. Only a Test is listened to: a question with several parts arrives
    # with no options and must not turn into something START can allow.
    test = event == "Test"
    ask = waited_for and (kind == "perm" or options > 0 or (test and payload.get("ask") is True))
    now = time.time()
    if ask:
        life = (float(payload.get("window") or 0) if test else 0) or answer_window(bar, options > 0)
    else:
        life = SHOW_SECONDS.get(kind, 10) + START_ALLOWANCE
    item = {"id": nonce(), "kind": kind, "title": project_name(payload, project_dir),
            "text": ascii_only(text) or "event", "ask": ask, "options": options,
            "window": life, "sid": sid, "exp": now + life}

    with Lock():
        box = bar.read_box()
        inbox = loads(box.get("inbox"), {})
        answers = loads(box.get("answers"), {})
        items = [i for i in live_items(inbox, now) if i.get("id") not in answers]
        mine = [i for i in items if i.get("sid") == sid]

        if any(i.get("ask") for i in items) and kind in COLLAPSE_KINDS:
            # Someone is being asked something. A completion is not worth
            # taking the prompt off the screen for.
            return {"outcome": "held back: a prompt is up"}
        if kind in ("perm", "question") and not ask and any(i.get("ask") for i in mine):
            # Claude Code announces a prompt or a question twice, as
            # PermissionRequest and, a few seconds later, as a Notification.
            # The first is already up and can be answered; showing the second
            # would take it off the screen and put the wheel back on option 1.
            return {"outcome": "duplicate of the prompt already up"}
        if kind == "perm":
            items = [i for i in items if not (i.get("sid") == sid and i.get("kind") == "perm")]
        if kind in COLLAPSE_KINDS:
            items = [i for i in items if not (i.get("sid") == sid and i.get("kind") in COLLAPSE_KINDS)]

        # What to put back afterwards. If the app is already up from an earlier
        # event, what it covered is on record; otherwise look before launching.
        state = read_json(STATE_FILE, {}) or {}
        if state.get("up") and now < float(state.get("busy_until") or 0) + UP_GRACE:
            origin = str(state.get("origin") or "")
        else:
            origin = bar.origin()

        items.append(item)
        history = [{"kind": kind, "title": item["title"], "text": item["text"]}]
        history += [h for h in inbox.get("history", []) if isinstance(h, dict)]
        doc = pack(inbox, items, history, usage, item["id"], now)
        box["inbox"] = json.dumps(doc, separators=(",", ":"))
        bar.write_box(box)
        try:
            bar.launch()
        except BarError as exc:
            # 409: a BUSY session, or something else that outranks an app. The
            # bar is saying do not disturb. Mark the inbox as dealt with, so
            # opening the app later does not replay it as if it were news.
            box["seen"] = json.dumps({"n": doc["n"]})
            bar.write_box(box)
            return {"outcome": f"written, not shown: {exc}"}
        write_json(STATE_FILE, {"busy_until": max(i["exp"] for i in items), "up": True,
                                "origin": origin})

    alone = len(items) == 1
    return {"outcome": "shown", "id": item["id"], "n": doc["n"], "ask": ask, "life": life,
            # When to first look for the app being done: its time on screen if
            # it is alone, sooner if it is one of several.
            "wait": (SHOW_SECONDS.get(kind, 10) if alone else 3) + 1.0,
            "limit": sum(i["ttl"] for i in items) + 10}


def dismiss(bar, payload):
    """The user answered in the terminal: take that session's items off the bar."""
    state = read_json(STATE_FILE, {}) or {}
    if time.time() > float(state.get("busy_until") or 0):
        return "nothing on the bar"  # the common case, and it costs no request
    sid = str(payload.get("session_id") or "")
    now = time.time()
    with Lock():
        box = bar.read_box()
        inbox = loads(box.get("inbox"), {})
        seen = loads(box.get("seen"), {})
        items = live_items(inbox, now)
        rest = [i for i in items if i.get("sid") != sid]
        if len(rest) == len(items):
            return "nothing of this session on the bar"
        showing = seen.get("n") != inbox.get("n")  # the app has not finished that inbox
        doc = pack(inbox, rest, inbox.get("history", []), None, "", now)
        box["inbox"] = json.dumps(doc, separators=(",", ":"))
        if not rest:
            box["seen"] = json.dumps({"n": doc["n"]})
        bar.write_box(box)
        if rest:
            bar.launch()  # start over with what is left
            write_json(STATE_FILE, dict(state, busy_until=max(i["exp"] for i in rest)))
            return "dismissed, others remain"
        if showing:
            return "dismissed, " + bar.restore()
        write_json(STATE_FILE, {"busy_until": 0, "up": False, "origin": ""})
        return "dismissed"


def wait_for_answer(bar, item_id, deadline):
    """Read the mailbox until the app has recorded a button for this item."""
    started = time.time()
    while time.time() < deadline:
        time.sleep(ANSWER_POLL if time.time() - started < ANSWER_POLL_AFTER else ANSWER_POLL_SLOW)
        try:
            answer = loads(bar.read_box().get("answers"), {}).get(item_id)
        except OSError:
            continue
        if isinstance(answer, str) and answer:
            return answer
    return None


# How often to look while a notice is still expected to be up (it can be waved
# away with a button long before its time), and once it should be over.
LOOK_EARLY, LOOK_LATE = 3.0, 1.0


def hand_back(bar, n, expected, limit):
    """Put the bar back the way it was, once the app has finished.

    An app that ends leaves its Start/Setup menu up, so something has to take
    it down. The app says when it is done by writing `seen`; this looks for
    that every few seconds, at the moment the app is `expected` to be done,
    and every second after. It does nothing if a later event took over:
    that event's hook will do it."""
    started = time.time()
    deadline = started + expected + limit
    while True:
        elapsed = time.time() - started
        if elapsed < expected:
            time.sleep(min(LOOK_EARLY, expected - elapsed))
        else:
            time.sleep(LOOK_LATE)
        try:
            box = bar.read_box()
        except OSError:
            return "bar gone"
        if loads(box.get("inbox"), {}).get("n") != n:
            return "a later event took over"
        if loads(box.get("seen"), {}).get("n") == n:
            return bar.restore()
        if time.time() > deadline:
            return "the app never said it was done"


# --- Usage ----------------------------------------------------------------------
# How much of the 5-hour and weekly limits is gone. The hook only ever reads
# ~/.cache/busyclaude/usage.json; three things can fill it:
#
#   the status line  Claude Code hands every status line command the numbers
#                    (`rate_limits`), so `busyclaude.py statusline` writes them
#                    down as they change. No credentials involved. Terminal only.
#   by hand          `busyclaude.py usage 37 5`, which is also what an assistant
#                    that can see the numbers runs.
#   a fetch          `/api/oauth/usage`, the undocumented endpoint Claude Code
#                    itself calls, with the token `claude` keeps in the Keychain.
#                    The most likely thing here to break. The token is read and
#                    never refreshed or written back: refreshing rotates the
#                    refresh token, and a notifier that does that can log you out.
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_FILE = os.path.expanduser("~/.claude/.credentials.json")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_USER_AGENT = "claude-code/2.1.220"
USAGE_TIMEOUT = 4.0
USAGE_CACHE = os.path.join(CACHE_DIR, "usage.json")
USAGE_TTL = 120  # Stop fires every turn; two minutes of a five-hour window is nothing
# The token belongs to whatever the `claude` command last logged in to, which
# need not be the subscription being spent: a Team seat next to a Max plan reads
# 0% all day. Nothing in a hook says which one is in use, so the one check is
# when numbers are handed over by hand: the fetch is asked at the same moment,
# and a token that disagrees by more than this many points is left alone until
# a later hand-over agrees or `claude` logs in somewhere else.
USAGE_AGREE = 5
USAGE_DISTRUST = os.path.join(CACHE_DIR, "usage-fetch.json")


def credentials():
    blob = None
    if sys.platform == "darwin":
        try:
            found = subprocess.run(
                ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                capture_output=True, timeout=5)
            if found.returncode == 0:
                blob = found.stdout.decode("utf-8", "replace")
        except (subprocess.SubprocessError, OSError):
            blob = None
    if blob is None:
        try:
            with open(CREDENTIALS_FILE, encoding="utf-8") as fh:
                blob = fh.read()
        except OSError:
            return None
    try:
        oauth = json.loads(blob)["claudeAiOauth"]
    except (ValueError, KeyError, TypeError):
        return None
    return oauth if isinstance(oauth, dict) else None


def token_expired(stamp):
    """Only an optimisation, so it answers False whenever it does not know."""
    try:
        stamp = float(stamp)
    except (TypeError, ValueError):
        return False
    if stamp <= 0:
        return False
    return (stamp / 1000 if stamp > 1e11 else stamp) <= time.time()


def usage_diagnose():
    if os.environ.get("BUSYCLAUDE_FETCH", "1") == "0":
        return "the fetch is off (BUSYCLAUDE_FETCH=0)"
    oauth = credentials()
    if oauth is not None and fetch_distrusted(oauth):
        said = (read_json(USAGE_DISTRUST, {}) or {}).get("said") or {}
        return (f"the token `claude` keeps measures another subscription (it said 5h "
                f"{said.get('five', 0):.0f}%, 7d {said.get('week', 0):.0f}% against the numbers "
                "handed over), so it is not asked")
    if oauth is None:
        return (f"no credentials: neither the Keychain entry '{KEYCHAIN_SERVICE}' "
                f"nor {CREDENTIALS_FILE} could be read")
    if not oauth.get("accessToken"):
        return "the credentials record holds no accessToken"
    if token_expired(oauth.get("expiresAt")):
        return "the stored token expired; run `claude` to refresh it"
    return "the endpoint refused the token"


def token_identity(oauth):
    """Which subscription a token speaks for, as far as its record says. Stable
    across refreshes, unlike the token itself."""
    return f"{oauth.get('subscriptionType') or '?'}/{oauth.get('rateLimitTier') or '?'}"


def fetch_distrusted(oauth):
    return (read_json(USAGE_DISTRUST, {}) or {}).get("identity") == token_identity(oauth)


def usable_token():
    """The `claude` command's token, if a fetch may use it at all."""
    if os.environ.get("BUSYCLAUDE_FETCH", "1") == "0":
        return None
    oauth = credentials()
    if not oauth or token_expired(oauth.get("expiresAt")) or not oauth.get("accessToken"):
        return None
    return oauth


def usage_ask(oauth):
    """What the endpoint says for this token: {"five": pct, "week": pct}, or None."""
    import urllib.error
    import urllib.request
    request = urllib.request.Request(USAGE_URL, method="GET", headers={
        "Authorization": f"Bearer {oauth['accessToken']}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": USAGE_USER_AGENT,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=USAGE_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
        return {"five": float(data["five_hour"]["utilization"]),
                "week": float(data["seven_day"]["utilization"])}
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        return None


def usage_fetch():
    oauth = usable_token()
    if oauth is None or fetch_distrusted(oauth):
        return None
    asked = usage_ask(oauth)
    if asked is None:
        return None
    snapshot = dict(asked, at=time.time(), source="fetch")
    write_json(USAGE_CACHE, snapshot)
    return snapshot


def usage_crosscheck(handed):
    """Ask the fetch while numbers that are known to be right are fresh. -> a
    line for the user when the two disagree, or None."""
    oauth = usable_token()
    asked = usage_ask(oauth) if oauth is not None else None
    if asked is None:
        return None
    if (abs(asked["five"] - handed["five"]) <= USAGE_AGREE
            and abs(asked["week"] - handed["week"]) <= USAGE_AGREE):
        if os.path.exists(USAGE_DISTRUST):
            os.remove(USAGE_DISTRUST)
        return None
    write_json(USAGE_DISTRUST, {"identity": token_identity(oauth), "said": asked, "at": time.time()})
    return (f"the fetch says 5h {asked['five']:.0f}%, 7d {asked['week']:.0f}%: the token `claude` "
            "keeps measures another subscription, so it is not asked again until a later "
            "hand-over agrees with it or `claude` logs in elsewhere")


def usage_store(five, week, source):
    snapshot = {"five": float(five), "week": float(week), "at": time.time(), "source": source}
    write_json(USAGE_CACHE, snapshot)
    return snapshot


def usage_current():
    """{"five": pct, "week": pct, "at": stamp}, or None when nothing recent enough is known."""
    if os.environ.get("BUSYCLAUDE_USAGE", "1") == "0":
        return None
    snapshot = read_json(USAGE_CACHE)
    try:
        age = time.time() - float(snapshot["at"])
        float(snapshot["five"]), float(snapshot["week"])
    except (KeyError, TypeError, ValueError):
        snapshot, age = None, None
    # Numbers handed over by the status line or by hand come from the session
    # itself and cannot be asked for again, so they stand until they are too
    # old to show at all; only a fetch's own numbers are refreshed by a fetch.
    handed = snapshot is not None and snapshot.get("source") != "fetch" and age <= USAGE_MAX_AGE
    if snapshot is None or (not handed and age > USAGE_TTL):
        fetched = usage_fetch()
        if fetched is not None:
            snapshot = fetched
        elif snapshot is not None and age > USAGE_MAX_AGE:
            snapshot = None
    return ({"five": snapshot["five"], "week": snapshot["week"], "at": snapshot["at"]}
            if snapshot else None)


# --- The hook -------------------------------------------------------------------
def spawn(command, handoff):
    """Run `busyclaude.py <command>` detached from this process, with `handoff`
    on its stdin. The hook returns to Claude Code at once; this outlives it."""
    options = {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL,
               "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        options["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    child = subprocess.Popen([sys.executable, os.path.abspath(__file__), command], **options)
    child.stdin.write(json.dumps(handoff).encode())
    child.stdin.close()


def read_stdin_json():
    try:
        value = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        value = {}
    return value if isinstance(value, dict) else {}


def plugin_enabled(path=CLAUDE_SETTINGS):
    """True when this project's plugin is switched on in Claude Code."""
    enabled = (read_json(path, {}) or {}).get("enabledPlugins")
    return isinstance(enabled, dict) and any(
        on and str(name).split("@")[0] == PLUGIN_NAME for name, on in enabled.items())


def cmd_hook(args):
    # With the plugin on, its hooks do the work. Hooks left in settings.json by an
    # earlier `setup` would show every event twice, so those stand aside.
    if not args.plugin and plugin_enabled():
        return 0
    payload = read_stdin_json()
    event = payload.get("hook_event_name") or ""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    try:
        answerable = (event == "PermissionRequest"
                      and os.environ.get("BUSYCLAUDE_APPROVE", "1") != "0")
        if answerable:
            # The one event Claude Code is waiting on: stay, and bring back the button.
            bar = Bar()
            if not bar.reachable():
                record(payload, f"no bar at {bar.base}")
                return 0
            result = deliver(bar, payload, project_dir)
            if result.get("ask"):
                answer = wait_for_answer(bar, result["id"], time.time() + result["life"] + ANSWER_GRACE)
                if answer in ("allow", "deny"):
                    print(permission_decision(answer), flush=True)
                elif answer and answer.isdigit() and 1 <= int(answer) <= len(question_options(payload)):
                    print(question_decision(payload, int(answer)), flush=True)
                    answer = f"option {answer}"
                # Anything else, "skip" included, leaves the question to the terminal.
                record(payload, answer or "no answer on the bar")
                spawn("handback", {"n": result["n"], "wait": 1.5 if answer else 0.5, "limit": 20})
            elif result.get("n"):
                record(payload, result["outcome"])
                spawn("handback", {"n": result["n"], "wait": result["wait"], "limit": result["limit"]})
            else:
                record(payload, result["outcome"])
        elif event in ALERT_EVENTS or dismisses(payload):
            # Everything else is done after Claude Code has been let go.
            spawn("deliver", {"payload": payload, "project_dir": project_dir})
    except Exception as exc:  # a hook must never be the thing that breaks a session
        record(payload, f"failed: {type(exc).__name__}: {exc}")
    return 0


def cmd_deliver(_args):
    """The detached half of a hook: deliver, then give the screen back."""
    handoff = read_stdin_json()
    payload = handoff.get("payload") or {}
    try:
        bar = Bar()
        if dismisses(payload):
            record(payload, dismiss(bar, payload))
            return 0
        if not bar.reachable():
            record(payload, f"no bar at {bar.base}")
            return 0
        usage = None
        if payload.get("hook_event_name") in USAGE_EVENTS:
            usage = usage_current()
        result = deliver(bar, payload, handoff.get("project_dir"), usage)
        record(payload, result["outcome"])
        if result.get("n"):
            hand_back(bar, result["n"], result["wait"], result["limit"])
    except Exception as exc:
        record(payload, f"failed: {type(exc).__name__}: {exc}")
    return 0


def cmd_handback(_args):
    handoff = read_stdin_json()
    try:
        hand_back(Bar(), handoff.get("n"), float(handoff.get("wait") or 1), float(handoff.get("limit") or 20))
    except Exception:
        pass
    return 0


# --- Setting it up --------------------------------------------------------------
def manifest():
    return read_json(os.path.join(HERE, "src", "appmeta", "manifest.json"), {}) or {}


def version_tuple(text):
    return tuple(int(p) for p in re.findall(r"\d+", str(text))[:3])


def find_package(explicit=None):
    """The app to install: the one named, the one a checkout that is being worked
    on builds, or the one this repository ships."""
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    built = os.path.join(HERE, "dist", f"{APP_ID}.tgz")
    shipped = os.path.join(HERE, "release", f"{APP_ID}.tgz")
    npm = shutil.which("npm")
    # A checkout with its build tools installed is someone's working copy, and
    # gets what its sources say now. Everyone else, a plugin's copy included,
    # gets the package that was built, tested and committed: no Node needed.
    if npm and os.path.isdir(os.path.join(HERE, "node_modules")):
        sources = [os.path.join(root, name) for root, _, names in os.walk(os.path.join(HERE, "src"))
                   for name in names]
        if not os.path.isfile(built) or any(os.path.getmtime(s) > os.path.getmtime(built) for s in sources):
            print("building the app (npm run build)...")
            subprocess.run([npm, "run", "--silent", "build"], cwd=HERE, check=True)
        return built if os.path.isfile(built) else None
    return shipped if os.path.isfile(shipped) else None


def off_screen(bar):
    """Take the app off the bar's display, if it is the one on it.

    An animation that is playing is a file the bar holds open, and the bar
    cannot replace an app while one of its files is open: the install fails
    halfway and leaves no app at all."""
    if bar.draws_as(PROBE_NAME) is False and bar.draws_as(APP_ID) is None:
        bar.quit()
        try:
            bar.call("DELETE", "/api/display/draw", body={"application_name": APP_ID})
        except BarError:
            pass
        write_json(STATE_FILE, {"busy_until": 0, "up": False, "origin": ""})
        time.sleep(1.0)


def install_app(bar, package):
    with open(package, "rb") as fh:
        body = fh.read()

    def attempt():
        staged = bar.json("POST", "/api/apps/stage", body=body, timeout=INSTALL_TIMEOUT)
        bar.call("POST", "/api/apps/install", {"install_key": staged["install_key"]},
                 timeout=INSTALL_TIMEOUT)
        return staged.get("staged") or {}

    off_screen(bar)
    try:
        return attempt()
    except BarError:
        # Refused, most likely because a file of the old copy could not be
        # deleted. By now the bar has removed what it could of it, so clear
        # away the rest and install onto nothing.
        off_screen(bar)
        try:
            bar.call("DELETE", "/api/display/draw", body={"application_name": APP_ID})
        except BarError:
            pass
        time.sleep(1.0)
        try:
            bar.call("DELETE", "/api/storage/remove", {"path": f"/ext/user_assets/{APP_ID}"})
        except BarError:
            pass
        return attempt()


def python_path():
    """The interpreter to name in the hooks. On macOS /usr/bin/python3 reports
    itself as a path inside Xcode, which moves when Xcode does."""
    if "/Developer/usr/bin/" in sys.executable and os.path.exists("/usr/bin/python3"):
        return "/usr/bin/python3"
    return sys.executable


def hook_command():
    return f"{shlex.quote(python_path())} {shlex.quote(os.path.abspath(__file__))} hook"


def hooks_block(command=None):
    """The `hooks` object for a Claude Code settings.json."""
    command = command or hook_command()
    return {event: [{"hooks": [{"type": "command", "command": command,
                                "timeout": HOOK_TIMEOUT.get(event, DEFAULT_HOOK_TIMEOUT)}]}]
            for event in HOOK_EVENTS}


def is_ours(hook):
    """A hook entry written by this project, in any of its generations."""
    text = " ".join([str(hook.get("command") or "")] + [str(a) for a in hook.get("args") or []])
    return "busyclaude.py" in text or bool(re.search(r"busyclaude[^ ]*/app\.py", text))


def merge_hooks(settings, block):
    """Replace this project's hooks in `settings`, leaving everyone else's alone."""
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    for event in list(hooks):
        groups = []
        for group in hooks[event] if isinstance(hooks[event], list) else []:
            kept = [h for h in group.get("hooks", []) if not is_ours(h)]
            if kept:
                groups.append(dict(group, hooks=kept))
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    for event, groups in (block or {}).items():
        hooks.setdefault(event, []).extend(groups)
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return settings


def write_hooks(path, block, statusline=None):
    """`statusline`: True adds the usage status line, False removes it, None leaves it."""
    settings = read_json(path)
    if settings is None:
        if os.path.exists(path):
            raise SystemExit(f"{path} is not valid JSON; fix it first, nothing was changed")
        settings = {}
    if os.path.exists(path):
        backup = f"{path}.before-busyclaude"
        shutil.copy2(path, backup)
        print(f"  kept the old file as {backup}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    settings = merge_hooks(settings, block)
    if statusline is not None:
        settings = merge_statusline(settings, statusline)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")


def explain_unreachable(bar, exc):
    return (f"no BUSY Bar at {bar.base} ({exc}).\n"
            "  Over USB: plug it in; its address there is 10.0.4.20.\n"
            "  Over Wi-Fi: python3 busyclaude.py setup --addr <the bar's IP> [--token <API key>]\n"
            "  (the key is in the bar's web UI under Network > HTTP API, if protection is on).")


def cmd_setup(args):
    bar = Bar(args.addr, args.token)
    try:
        if not bar.reachable():
            raise OSError("nothing answers there")
        api = bar.json("GET", "/api/version").get("api_semver", "?")
    except BarError as exc:
        raise SystemExit(f"the bar at {bar.base} refused: {exc}\n  Over Wi-Fi it wants --token <API key>.")
    except OSError as exc:
        raise SystemExit(explain_unreachable(bar, exc))
    print(f"1/4  bar at {bar.base}, HTTP API {api}")
    needs = manifest().get("api_version", "0")
    if version_tuple(api) < version_tuple(needs):
        raise SystemExit(f"     the app needs HTTP API {needs} (firmware 1.3.1 or newer): update the bar first")

    package = find_package(args.package)
    if not package:
        raise SystemExit(
            f"     no app package to install: release/{APP_ID}.tgz is missing from this copy.\n"
            "     Download the .tgz from the project's releases and pass it with --package <file>.")
    staged = install_app(bar, package)
    print(f"2/4  installed {staged.get('name', APP_ID)} v{staged.get('version', '?')} on the bar")

    if args.addr or args.token:
        config = read_json(CONFIG_FILE, {}) or {}
        if args.addr:
            config["addr"] = args.addr
        if args.token:
            config["token"] = args.token
        write_json(CONFIG_FILE, config, private=True)
        print(f"     saved the bar's address to {CONFIG_FILE}")

    if args.plugin or (not args.no_hooks and plugin_enabled(args.settings)):
        # The plugin brings the hooks. Ones written into settings.json by an
        # earlier setup would only double every event, so they go.
        settings = read_json(args.settings, {}) or {}
        ours = any(is_ours(h) for groups in (settings.get("hooks") or {}).values()
                   for g in groups for h in g.get("hooks", []))
        if ours:
            write_hooks(args.settings, None)
        print("3/4  hooks come with the plugin"
              + (f"; the ones an earlier setup left in {args.settings} were removed" if ours else ""))
    elif args.no_hooks:
        print("3/4  hooks left alone (--no-hooks); `busyclaude.py hooks` prints them")
    else:
        write_hooks(args.settings, hooks_block(), True if args.statusline else None)
        print(f"3/4  {len(HOOK_EVENTS)} hooks in {args.settings}"
              + (", and the status line that hands over the usage numbers" if args.statusline else ""))

    hello = {"hook_event_name": "Test", "kind": "hello", "text": "ready",
             "session_id": "setup", "cwd": HERE}
    if args.no_confirm:
        result = deliver(bar, hello)
        print(f"4/4  hello: {result['outcome']}")
        if result.get("n"):
            hand_back(bar, result["n"], result["wait"], result["limit"])
    else:
        # The one thing none of the steps above can show: that the bar now
        # holding a mascot is the bar in front of you, and that its buttons
        # travel back. Everything else here would look the same against a bar
        # in another room.
        result = deliver(bar, dict(hello, ask=True, window=CONFIRM_WAIT, text="press START"))
        # Every early return from deliver (a BUSY session, nothing to show) comes
        # back without `ask`, and there is then no button to wait for.
        if not result.get("ask"):
            print(f"4/4  hello: {result['outcome']}")
        else:
            print(f"4/4  press START on the bar ({CONFIRM_WAIT}s)...", flush=True)
            answer = wait_for_answer(bar, result["id"], time.time() + CONFIRM_WAIT + ANSWER_GRACE)
            hand_back(bar, result["n"], 1.5 if answer else 0.5, 20)
            if answer in ("allow", "deny"):
                print(f"     {'START' if answer == 'allow' else 'BACK'}: that is the right bar, "
                      "and its buttons reach the hook")
            else:
                print("     no button came back. The app is installed and the hooks are in place,\n"
                      "     but nothing has proved that the bar showing the mascot is the one in\n"
                      "     front of you. `busyclaude.py status` says which address it is using.")
    print("\nDone. Start a new Claude Code session (hooks are read at startup),\n"
          f"or try it now:  python3 {shlex.quote(os.path.abspath(__file__))} test perm")
    return 0


TEST_TEXT = {"perm": "$ npm test", "question": "a question", "ask": "your turn",
             "done": "finished", "agent": "agent done", "error": "rate limit", "hello": "ready"}
TEST_QUESTION = {"questions": [{
    "question": "Which one?", "header": "Test", "multiSelect": False,
    "options": [{"label": "First"}, {"label": "Second"}, {"label": "Third"}]}]}


def cmd_test(args):
    bar = Bar()
    usage = usage_current() if args.kind in COLLAPSE_KINDS else None
    payload = {"hook_event_name": "Test", "kind": args.kind, "session_id": "test",
               "text": args.text or TEST_TEXT[args.kind], "cwd": HERE}
    if args.kind == "question":
        payload["tool_input"] = TEST_QUESTION
    try:
        if not bar.reachable():
            raise OSError("nothing answers there")
        result = deliver(bar, payload, usage=usage)
    except OSError as exc:
        raise SystemExit(explain_unreachable(bar, exc))
    print(result["outcome"])
    if result.get("ask"):
        print("the wheel picks an option, START takes it, BACK leaves it..." if args.kind == "question"
              else "START allows, BACK denies...")
        answer = wait_for_answer(bar, result["id"], time.time() + result["life"] + ANSWER_GRACE)
        print(answer or "no answer on the bar")
        print(hand_back(bar, result["n"], 1.5 if answer else 0.5, 20))
    elif result.get("n"):
        print(hand_back(bar, result["n"], result["wait"], result["limit"]))
    return 0


def cmd_demo(_args):
    """Every kind of notification once, a few seconds each, with no sound: for
    looking at the mascot."""
    bar = Bar()
    if not bar.reachable():
        raise SystemExit(explain_unreachable(bar, "nothing answers there"))
    now = time.time()
    kinds = ["perm", "question", "ask", "done", "agent", "error", "hello"]
    with Lock():
        box = bar.read_box()
        inbox = loads(box.get("inbox"), {})
        state = read_json(STATE_FILE, {}) or {}
        origin = (str(state.get("origin") or "")
                  if state.get("up") and now < float(state.get("busy_until") or 0) + UP_GRACE
                  else bar.origin())
        # Nothing is `fresh`, so nothing chimes; several at once get four seconds each.
        items = [{"id": nonce(), "kind": kind, "title": kind, "text": TEST_TEXT[kind], "ask": False,
                  "options": 0, "window": 0, "sid": "demo", "exp": now + 4 * len(kinds) + START_ALLOWANCE}
                 for kind in kinds]
        doc = pack(inbox, items, inbox.get("history", []), None, "", now)
        box["inbox"] = json.dumps(doc, separators=(",", ":"))
        bar.write_box(box)
        bar.launch()
        write_json(STATE_FILE, {"busy_until": items[0]["exp"], "up": True, "origin": origin})
    print(f"showing {', '.join(kinds)}; any button skips to the next")
    print(hand_back(bar, doc["n"], 4 * len(kinds) + 1, 15))
    return 0


def cmd_usage(args):
    if args.five is not None:
        if args.week is None:
            raise SystemExit("usage takes both numbers: the 5-hour percentage, then the weekly one")
        snapshot = usage_store(args.five, args.week, "by hand")
        print(f"noted: 5h {snapshot['five']:.0f}%, 7d {snapshot['week']:.0f}%; "
              f"shown with notifications for the next {USAGE_MAX_AGE // 60} minutes")
        verdict = usage_crosscheck(snapshot)
        if verdict:
            print(verdict)
        return 0
    usage = usage_current()
    if usage:
        cached = read_json(USAGE_CACHE, {}) or {}
        print(f"5h {usage['five']:.0f}%, 7d {usage['week']:.0f}%  "
              f"({cached.get('source', 'fetch')}, {(time.time() - usage['at']) / 60:.0f} min ago)")
    else:
        print(f"no recent numbers. A fetch is not possible: {usage_diagnose()}.\n"
              "`busyclaude.py setup --statusline` has Claude Code hand them over in a terminal session.")
    return 0


def cmd_statusline(args):
    """A status line command for Claude Code: writes down the usage numbers it is
    handed, then lets the status line you already had (`--then`) do the talking."""
    raw = sys.stdin.read()
    try:
        limits = json.loads(raw).get("rate_limits") or {}
        five = limits["five_hour"]["used_percentage"]
        week = limits["seven_day"]["used_percentage"]
        old = read_json(USAGE_CACHE, {}) or {}
        # This runs on every redraw of the status line; the file is rewritten
        # when a number moves, or once a minute to keep its date fresh.
        if (old.get("five"), old.get("week")) != (float(five), float(week)) \
                or time.time() - float(old.get("at") or 0) > 60:
            usage_store(five, week, "status line")
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        pass  # no limits in this session (an API key, or nothing sent yet): nothing to note
    if args.then:
        # The status line that was there before: same input, its output is the status line.
        return subprocess.run(args.then, shell=True, input=raw.encode()).returncode
    return 0


STATUSLINE_MARK = "busyclaude.py statusline"


def merge_statusline(settings, install):
    """Put this project's status line command in front of whatever is there, or take it out again."""
    line = settings.get("statusLine") if isinstance(settings.get("statusLine"), dict) else None
    command = str((line or {}).get("command") or "")
    theirs = command
    if STATUSLINE_MARK in command:
        # Ours, with theirs kept inside it as one quoted argument.
        try:
            words = shlex.split(command)
            theirs = words[words.index("--then") + 1] if "--then" in words else ""
        except (ValueError, IndexError):
            theirs = ""
    if install:
        ours = f"{shlex.quote(python_path())} {shlex.quote(os.path.abspath(__file__))} statusline"
        settings["statusLine"] = dict(line or {"type": "command"},
                                      command=ours + (f" --then {shlex.quote(theirs)}" if theirs else ""))
    elif STATUSLINE_MARK in command:
        if theirs:
            settings["statusLine"] = dict(line, command=theirs)
        else:
            settings.pop("statusLine", None)
    return settings


def cmd_status(args):
    bar = Bar()
    print(f"bar      {bar.base}" + ("  (with an API key)" if bar.token else ""))
    try:
        if not bar.reachable():
            raise OSError("nothing answers there")
        api = bar.json("GET", "/api/version").get("api_semver", "?")
        firmware = bar.json("GET", "/api/status/firmware").get("version", "?")
        print(f"         reachable: firmware {firmware}, HTTP API {api}")
        apps = bar.json("GET", "/api/apps/list").get("apps", [])
        mine = next((a for a in apps if a.get("id") == APP_ID), None)
        local = manifest().get("version", "?")
        print(f"app      {'v' + mine['version'] + ' installed' if mine else 'NOT installed'}"
              f"; this checkout is v{local}")
        box = bar.read_box()
        inbox = loads(box.get("inbox"), {})
        seen = loads(box.get("seen"), {})
        answered = loads(box.get("answers"), {})
        live = [i for i in live_items(inbox, time.time()) if i.get("id") not in answered]
        print(f"mailbox  {len(live)} live item(s), {len(inbox.get('history', []))} in the history"
              + ("" if not inbox else "; the app has shown it" if seen.get("n") == inbox.get("n")
                 else "; not shown yet"))
        for item in live:
            print(f"         - {item.get('title')}: {item.get('text')} ({item.get('kind')})")
    except BarError as exc:
        print(f"         refused: {exc}")
    except OSError as exc:
        print("         " + explain_unreachable(bar, exc))
    settings = read_json(args.settings, {}) or {}
    events = [e for e, groups in (settings.get("hooks") or {}).items()
              if any(is_ours(h) and "busyclaude.py" in str(h.get("command")) for g in groups
                     for h in g.get("hooks", []))]
    legacy = [e for e, groups in (settings.get("hooks") or {}).items()
              if any(is_ours(h) and "busyclaude.py" not in str(h.get("command")) for g in groups
                     for h in g.get("hooks", []))]
    print(f"hooks    {len(events)} of {len(HOOK_EVENTS)} in {args.settings}"
          + (f"; {len(legacy)} from an older version, `setup` replaces them" if legacy else ""))
    if os.environ.get("BUSYCLAUDE_USAGE", "1") == "0":
        print("usage    off (BUSYCLAUDE_USAGE=0)")
    else:
        usage = usage_current()
        print("usage    " + (f"5h {usage['five']:.0f}%, 7d {usage['week']:.0f}%" if usage
                              else f"unavailable: {usage_diagnose()}"))
    try:
        with open(LOG_FILE, encoding="utf-8") as fh:
            tail = fh.readlines()[-3:]
    except OSError:
        tail = []
    for line in tail:
        entry = loads(line, {})
        if "outcome" in entry:  # lines without one were written by an older version
            print(f"log      {entry.get('at')}  {entry.get('event')}: {entry.get('outcome')}")
    return 0


def cmd_hooks(_args):
    print(json.dumps({"hooks": hooks_block()}, indent=2))
    return 0


def cmd_uninstall(args):
    if os.path.exists(args.settings):
        write_hooks(args.settings, None, False)
        print(f"hooks removed from {args.settings}")
    if args.app:
        bar = Bar()
        try:
            bar.call("DELETE", "/api/apps", {"app_id": APP_ID}, timeout=INSTALL_TIMEOUT)
            print("app removed from the bar (its settings are kept)")
        except (BarError, OSError) as exc:
            print(f"app not removed: {exc}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="install the app on the bar and the hooks in Claude Code")
    setup.add_argument("--addr", help=f"the bar's address (default: {DEFAULT_ADDR}, its address over USB)")
    setup.add_argument("--token", help="the bar's HTTP API key, if it asks for one over Wi-Fi")
    setup.add_argument("--package", help="a built .tgz to install instead of building this checkout")
    setup.add_argument("--no-hooks", action="store_true", help="do not touch Claude Code's settings")
    setup.add_argument("--plugin", action="store_true",
                       help="set up as the Claude Code plugin does: the plugin brings the hooks")
    setup.add_argument("--statusline", action="store_true",
                       help="also have Claude Code's status line hand over the usage numbers "
                            "(terminal sessions; a status line you already have keeps working)")
    setup.add_argument("--settings", default=CLAUDE_SETTINGS, help="the settings.json to add hooks to")
    setup.add_argument("--no-confirm", action="store_true",
                       help="do not wait for a button on the bar at the end")
    setup.set_defaults(run=cmd_setup)

    test = sub.add_parser("test", help="send a sample notification")
    test.add_argument("kind", nargs="?", default="done", choices=sorted(SHOW_SECONDS))
    test.add_argument("text", nargs="?")
    test.set_defaults(run=cmd_test)

    sub.add_parser("demo", help="every kind of notification once, silently").set_defaults(run=cmd_demo)

    usage = sub.add_parser("usage", help="show the usage numbers, or hand them over: usage 37 5")
    usage.add_argument("five", nargs="?", type=float, help="percent of the 5-hour limit used")
    usage.add_argument("week", nargs="?", type=float, help="percent of the weekly limit used")
    usage.set_defaults(run=cmd_usage)

    statusline = sub.add_parser("statusline", help="a Claude Code status line command that notes the usage numbers")
    statusline.add_argument("--then", metavar="COMMAND", help="the status line command to run after, as one argument")
    statusline.set_defaults(run=cmd_statusline)

    status = sub.add_parser("status", help="what is set up, and what the bar is holding")
    status.add_argument("--settings", default=CLAUDE_SETTINGS)
    status.set_defaults(run=cmd_status)

    sub.add_parser("hooks", help="print the hooks block for a settings.json").set_defaults(run=cmd_hooks)

    uninstall = sub.add_parser("uninstall", help="remove the hooks")
    uninstall.add_argument("--app", action="store_true", help="also remove the app from the bar")
    uninstall.add_argument("--settings", default=CLAUDE_SETTINGS)
    uninstall.set_defaults(run=cmd_uninstall)

    hook = sub.add_parser("hook", help="run by Claude Code; the event is on stdin")
    hook.add_argument("--plugin", action="store_true", help="set by the plugin's own hooks")
    hook.set_defaults(run=cmd_hook)
    for name, run in (("deliver", cmd_deliver), ("handback", cmd_handback)):
        sub.add_parser(name).set_defaults(run=run)  # the detached halves of a hook

    args = parser.parse_args()
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
