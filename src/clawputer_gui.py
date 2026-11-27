#!/usr/bin/env python3
"""
Clawputer: a pocket chat app for Claude Code on the CardputerZero (unofficial).
(320x170 LCD + built-in keyboard)

Runs Claude Code headless (stream-json) and draws a compact chat view straight to
the framebuffer:
  - tiny battery + clock top-right, status top-left
  - conversation in a small mono font; tool calls squashed to one-liners
  - message box at the bottom
  - permission prompts as y / n / a(lways this session)

Keys
  type + Enter ........ send message
  Up / Down ........... scroll one line
  Left / Right ........ scroll one page
  Esc ................. stop Claude's current turn (or clear the input); on an
  Ctrl+Q or /exit ..... quit            /new ... fresh conversation
"""
import fcntl
import glob
import json
import mmap
import os
import re
import select
import signal
import socket
import struct
import threading
import subprocess
import sys
import textwrap
import time

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFont

import clawputer_setup as setup

W, H = 320, 170
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
EMOJI_FONT = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"     # fonts-noto-color-emoji
EMOJI_PAD = "\ue000"      # placeholder after each emoji: emoji are 2 cells wide in the monospace grid
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
KEEP_FLAG = "/etc/clawputer/keep-max-boost"   # max boost survives reboots (clawputer-bootmode keep on)
CLAUDE = setup.claude_path()
WORKDIR = os.path.expanduser("~/clawputer")
CONF = setup.CONFIG_DIR
LOGFILE = os.path.join(CONF, "gui.log")
SETTINGS_FILE = os.path.join(CONF, "gui.json")
DRAFTS_FILE = os.path.join(CONF, "drafts.json")
STATE_FILE = os.path.join(CONF, "gui.state")   # "shown" / "hidden", read by the launcher script
CTL_FILE = os.path.join(CONF, "gui.ctl")       # pid of the launcher script showing us right now
# esc on an empty box minimises the chat (it runs as the clawputer-chat user service, see the
# launcher script). While minimised and idle, claude itself is stopped to give its RAM back
# and resumed (same chat) when you come back.
CAN_HIDE = os.environ.get("CLAWPUTER_HIDE") == "1"
BOOST_FLAG = os.path.join(CONF, "boost.on")      # exists while the RAM boost is on (cleanup hook)
HIDDEN_MIN_MB = 60      # minimised and free RAM below this: stop claude even if it's working
ASKPASS_SOCK = os.path.join(CONF, "askpass.sock")   # sudo -A password popups (see clawputer-askpass)
SHARE = os.path.dirname(os.path.abspath(__file__))

# (key, label, choices, restarts claude?)
SETTINGS = [
    ("model", "model", ["default", "fable", "opus", "sonnet", "haiku"], True),
    ("effort", "effort", ["default", "low", "medium", "high", "xhigh", "max"], True),
    ("permissions", "permissions", ["ask", "acceptEdits", "auto", "plan", "dontAsk", "bypassPermissions"], True),
    ("font", "font size", ["9", "10", "11", "12"], False),
    ("tool_lines", "tool output", ["3", "6", "12", "all"], False),
    ("start", "on start", ["last chat", "new chat"], False),
    ("style", "chat style", ["plain", "bubbles"], False),
    ("chime", "chime when minimised", ["on", "off"], False),
    ("prompt_sound", "sound on prompts", ["on", "off"], False),
    ("dim", "dim when idle", ["1 min", "5 min", "off"], False),
    ("maxboost", "max boost after reboot", ["restore", "keep"], False),
    ("dblspace", "double space = '. '", ["on", "off"], False),
    ("enter", "enter key", ["send", "new line"], False),
    ("web", "web ui on open", ["off", "on"], False),
    ("@web", "web ui: start / stop", None, False),
    ("@switch", "switch where claude runs", None, False),
    ("@remote", "remote pc setup", None, False),
    ("@about", "about / version", None, False),
    ("@signout", "sign out of claude", None, False),
]
# one line about each setting (and each choice, where it matters), shown under the menu
SETTING_HELP = {
    "model": "which claude model. default = your plan's default.",
    "effort": "how hard claude thinks: higher is slower but better.",
    "permissions": "what claude may do without asking you first.",
    "font": "text size in the chat.",
    "tool_lines": "lines of command output shown in each box (the web ui can unfold them).",
    "start": "what opens when you start a chat.",
    "style": "plain = like a terminal. bubbles = your messages in bubbles + nicer markdown.",
    "chime": "a sound when claude finishes or needs you while minimised.",
    "prompt_sound": "a sound when claude asks permission or a question, while you're in the chat.",
    "dim": "dims the screen when you don't press anything for a while (saves battery). any key wakes it.",
    "maxboost": "restore = a reboot gives the gpu memory back. keep = max boost stays on across reboots.",
    "dblspace": "two spaces type a full stop and a space, like a phone keyboard.",
    "enter": "what enter does. the other way to send is ctrl+enter.",
    "web": "start the web ui (use the chat from a phone / pc browser) every time.",
    "@web": "the chat in any browser on your network, with a key. on / off now.",
    "@switch": "carry on in a fresh chat on the other machine (your pc <-> this cardputer).",
    "@remote": "set up / test / forget the pc that runs claude in pc mode.",
    "@about": "versions, account, memory.",
    "@signout": "sign this cardputer out of claude.",
}
CHOICE_HELP = {
    ("permissions", "ask"): "asks before running commands or editing files.",
    ("permissions", "acceptEdits"): "edits files without asking; still asks for commands.",
    ("permissions", "auto"): "claude decides what's safe and only asks about risky things.",
    ("permissions", "plan"): "plans only: looks around, changes nothing.",
    ("permissions", "dontAsk"): "never asks: anything not already allowed is refused.",
    ("permissions", "bypassPermissions"): "⚠ runs everything without asking. only if you trust it.",
    ("model", "default"): "whatever your plan uses by default.",
    ("model", "haiku"): "fastest and lightest, good for quick questions.",
    ("model", "opus"): "the most capable; slower and uses more of your limits.",
    ("effort", "max"): "⚠ thinks the longest: slow and heavy on usage limits.",
}
DEFAULTS = {"model": "default", "effort": "default", "permissions": "ask", "font": "10", "tool_lines": "3", "start": "last chat",
            "dblspace": "on", "enter": "send", "web": "off", "style": "plain", "chime": "on",
            "prompt_sound": "on", "dim": "1 min", "maxboost": "restore"}


def load_settings():
    cfg = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE) as f:
            cfg.update({k: str(v) for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save_settings(cfg):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(cfg, f, indent=1)
    except OSError as e:
        log("save settings failed", e)


def claude_args(cfg):
    a = []
    if cfg["model"] != "default":
        a += ["--model", cfg["model"]]
    if cfg["effort"] != "default":
        a += ["--effort", cfg["effort"]]
    if cfg["permissions"] != "ask":
        a += ["--permission-mode", cfg["permissions"]]
    return a

# colours
BG = (12, 12, 16)
FG = (225, 225, 225)
DIM = (120, 120, 130)
USER = (110, 200, 255)
TOOL = (235, 190, 90)
RESULT = (140, 140, 150)
CODE = (150, 220, 150)
ASK = (255, 120, 200)
ERR = (255, 110, 110)
BAR = (35, 35, 45)
ACCENT = (217, 119, 87)
BOX = (75, 75, 92)
BUBBLE = (30, 52, 72)        # your messages in the "bubbles" chat style
BUBBLE_FG = (215, 235, 255)
BRIGHT = (255, 255, 255)     # **bold** text
OK = (120, 210, 130)
MD_TOKEN = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`)")
CHIME_FILE = os.path.join(CONF, "chime.wav")
PROMPT_GRACE = 0.6       # seconds after a prompt appears when answer keys are ignored (you were typing)
SCROLL_KEYS = ("up", "down", "left", "right", "pgup", "pgdn", "end")
RESULT_MAX_LINES, RESULT_MAX_CHARS = 400, 40000   # tool output kept per result (web ui can unfold it)


def log(*a):
    try:
        with open(LOGFILE, "a") as f:
            f.write(time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# hardware (framebuffer + keyboard), same approach as meshcore-zero
# ---------------------------------------------------------------------------
class Framebuffer:
    def __init__(self):
        dev = os.getenv("APPLAUNCH_LINUX_FBDEV_DEVICE") or "/dev/fb_lcd"
        self.fd = os.open(dev, os.O_RDWR)
        var = bytearray(160)
        fcntl.ioctl(self.fd, 0x4600, var)
        self.width, self.height, _, _, self.xoff, self.yoff, self.bpp = struct.unpack_from("7I", var)
        self.fields = [struct.unpack_from("3I", var, o) for o in (32, 44, 56)]
        fix = bytearray(80)
        fcntl.ioctl(self.fd, 0x4602, fix)
        self.stride = struct.unpack_from("I", fix, 48)[0]
        smem_len = struct.unpack_from("I", fix, 16 + struct.calcsize("L"))[0]
        self.mem = mmap.mmap(self.fd, smem_len, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
        self.prev = None

    def write(self, img):
        box = ImageChops.difference(self.prev, img).getbbox() if self.prev else (0, 0, W, H)
        if box is None:
            return
        l, t, r, b = box
        rgb = np.asarray(img.crop(box), dtype=np.uint32)
        packed = np.zeros(rgb.shape[:2], dtype=np.uint32)
        for ch, (off, length, _) in enumerate(self.fields):
            packed |= (rgb[:, :, ch] >> (8 - length)) << off
        px = packed.astype("<u2" if self.bpp == 16 else "<u4").tobytes()
        rowbytes = (r - l) * self.bpp // 8
        for row, y in enumerate(range(t, b)):
            o = (y + self.yoff) * self.stride + (l + self.xoff) * self.bpp // 8
            self.mem[o:o + rowbytes] = px[row * rowbytes:(row + 1) * rowbytes]
        self.prev = img.copy()

    def save_raw(self, path):
        """the visible frame exactly as the panel wants it, for `cat file > /dev/fb_lcd`"""
        try:
            n = (self.yoff + self.height) * self.stride
            data = bytes(self.mem[:n])
            if not os.path.exists(path) or os.path.getsize(path) != len(data):
                with open(path + ".tmp", "wb") as f:
                    f.write(data)
                os.replace(path + ".tmp", path)
        except (OSError, ValueError) as e:
            log("splash cache", e)

    def close(self, blank=True):
        try:
            if blank:
                self.write(Image.new("RGB", (W, H), (0, 0, 0)))
            self.mem.close()
        finally:
            os.close(self.fd)


class Keyboard:
    SPECIAL = {1: "esc", 14: "backspace", 15: "tab", 28: "enter", 103: "up", 108: "down",
               105: "left", 106: "right", 104: "pgup", 109: "pgdn", 102: "home", 107: "end",
               111: "delete", 59: "f1", 60: "f2", 61: "f3"}
    CHARS = dict(zip(range(2, 14), "1234567890-="))
    CHARS.update(dict(zip(range(16, 28), "qwertyuiop[]")))
    CHARS.update(dict(zip(range(30, 42), "asdfghjkl;'`")))
    CHARS.update(dict(zip(range(44, 54), "zxcvbnm,./")))
    CHARS.update({43: "\\", 57: " "})
    SHIFT = dict(zip("1234567890-=[];'`,./\\", '!@#$%^&*()_+{}:"~<>?|'))
    SYMBOLS = dict(zip(
        (26, 27, 39, 40, 41, 43, 51, 52, 53, 94, 55, 69, 70, 71, 72, 73, 74, 75, 76, 77, 79, 80, 81, 82, 83, 85, 86, 89, 90, 91, 92, 93),
        "!@#$%^&*()~`_-+=[]{};:'\"<>\\|,./?"))

    def __init__(self):
        self.fd = None
        for path in glob.glob("/sys/class/input/event*/device/name"):
            if "tca8418" in open(path).read().lower():
                self.fd = os.open("/dev/input/" + path.split("/")[4], os.O_RDONLY | os.O_NONBLOCK)
                break
        if self.fd is None:
            raise RuntimeError("Cardputer keyboard not found")
        fcntl.ioctl(self.fd, 0x40044590, 1)  # EVIOCGRAB
        self.shift = set()
        self.ctrl = False
        self.buf = b""

    def read(self):
        out = []
        try:
            self.buf += os.read(self.fd, 4096)
        except BlockingIOError:
            return out
        size = struct.calcsize("llHHi")
        while len(self.buf) >= size:
            _, _, kind, code, value = struct.unpack("llHHi", self.buf[:size])
            self.buf = self.buf[size:]
            k = self.translate(kind, code, value)
            if k is not None:
                out.append(k)
        return out

    def translate(self, kind, code, value):
        if kind != 1:
            return None
        if code in (42, 54):
            self.shift.add(code) if value else self.shift.discard(code)
            return None
        if code in (29, 97):
            self.ctrl = bool(value)
            return None
        if value not in (1, 2):
            return None
        if self.ctrl and code == 28:
            return "ctrl+enter"
        if self.ctrl and code in self.CHARS:
            return "ctrl+" + self.CHARS[code]
        if code in self.SYMBOLS:
            return self.SYMBOLS[code]
        if code in self.SPECIAL:
            return self.SPECIAL[code]
        if code in self.CHARS:
            c = self.CHARS[code]
            return self.SHIFT.get(c, c.upper()) if self.shift else c
        log("unmapped key code", code)
        return None

    def close(self):
        try:
            fcntl.ioctl(self.fd, 0x40044590, 0)
        finally:
            os.close(self.fd)


# ---------------------------------------------------------------------------
# saved chats (Claude Code keeps every session as ~/.claude/projects/<dir>/<id>.jsonl)
# ---------------------------------------------------------------------------
PROJECTS = os.path.expanduser("~/.claude/projects")
_chat_cache = {}


def chat_info(path):
    st = os.stat(path)
    hit = _chat_cache.get(path)
    if hit and hit["mtime"] == st.st_mtime:
        return hit
    title, cwd, last = "", None, ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if '"type":"user"' not in line and '"type": "user"' not in line \
                        and '"cwd"' not in line and "last-prompt" not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not cwd and r.get("cwd"):
                    cwd = r["cwd"]
                if r.get("type") == "last-prompt":
                    last = r.get("lastPrompt") or last
                if not title and r.get("type") == "user" and not r.get("isMeta") and not r.get("isSidechain"):
                    c = (r.get("message") or {}).get("content")
                    if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                        title = c.strip().replace("\n", " ")
    except OSError:
        pass
    info = {"path": path, "id": os.path.basename(path)[:-6], "mtime": st.st_mtime,
            "title": title or last or "(empty chat)", "cwd": cwd or WORKDIR}
    _chat_cache[path] = info
    return info


# ---------------------------------------------------------------------------
# remote mode: Claude runs on the PC (ssh host "claude-pc"), this app is just the screen
# ---------------------------------------------------------------------------
REMOTE = os.environ.get("CLAUDE_REMOTE") == "1"
RCFG = setup.remote_cfg() or {}
WIN = RCFG.get("os") == "windows"

# runs on the PC (Windows python), fed over ssh stdin -> no quoting headaches
REMOTE_PY = r'''
import os, sys, json, glob, re
home = os.path.expanduser("~")
wd = os.path.join(home, "clawputer")
proj = os.path.join(home, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", wd))
cmd = sys.argv[1]
if cmd == "list":
    out = []
    for p in glob.glob(os.path.join(proj, "*.jsonl")):
        title = last = ""
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"type":"user"' not in line and "last-prompt" not in line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("type") == "last-prompt":
                        last = r.get("lastPrompt") or last
                    if not title and r.get("type") == "user" and not r.get("isMeta") and not r.get("isSidechain"):
                        c = (r.get("message") or {}).get("content")
                        if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                            title = c.strip().replace("\n", " ")
        except OSError:
            continue
        out.append({"path": p, "id": os.path.basename(p)[:-6], "mtime": os.path.getmtime(p),
                    "title": title or last or "(empty chat)", "cwd": wd})
    print(json.dumps(out))
elif cmd == "read":
    with open(sys.argv[2], encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"type":"user"' in line or '"type":"assistant"' in line:
                sys.stdout.write(line)
elif cmd == "delete":
    os.remove(sys.argv[2])
elif cmd == "init":
    import base64
    os.makedirs(wd, exist_ok=True)
    md = os.path.join(wd, "CLAUDE.md")
    if not os.path.exists(md):
        open(md, "wb").write(base64.b64decode(sys.argv[2]))
'''


def remote_py(*args, timeout=25):
    try:
        r = subprocess.run(setup.ssh_base() + ["python" if WIN else "python3", "-"] + list(args), input=REMOTE_PY.encode(),
                           capture_output=True, timeout=timeout)
        if r.returncode != 0:
            log("remote_py failed", args, r.stderr.decode(errors="replace")[:300])
        return r.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        log("remote_py error", e)
        return ""


def list_chats():
    if REMOTE:
        try:
            out = json.loads(remote_py("list") or "[]")
        except ValueError:
            out = []
        out.sort(key=lambda c: -c["mtime"])
        return out
    out = []
    for path in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        try:
            out.append(chat_info(path))
        except OSError:
            pass
    out.sort(key=lambda c: -c["mtime"])
    return out


def ago(t):
    d = max(0, time.time() - t)
    if d < 3600:
        return "%dm" % (d // 60)
    if d < 86400:
        return "%dh" % (d // 3600)
    return "%dd" % (d // 86400)


# ---------------------------------------------------------------------------
# Claude headless session
# ---------------------------------------------------------------------------
class NetCheck(threading.Thread):
    """background check: can claude reach what it needs? (anthropic's api, or the pc in pc mode)"""
    def __init__(self):
        super().__init__(daemon=True)
        self.target = (RCFG.get("host", ""), int(RCFG.get("port", 22))) if REMOTE else ("api.anthropic.com", 443)
        self.online = None          # None = not checked yet
        self.changed = False
        self.start()

    def run(self):
        while True:
            try:
                socket.create_connection(self.target, timeout=5).close()
                ok = True
            except (OSError, ValueError):
                ok = False
            if ok != self.online:
                self.online = ok
                self.changed = True
            time.sleep(15 if ok else 4)


class ClaudeSession:
    def __init__(self, extra_args, resume=None, cwd=None):
        args = [CLAUDE, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--permission-prompt-tool", "stdio", "--include-partial-messages"] + list(extra_args)
        if resume:
            args += ["--resume", resume]
        env = dict(os.environ, DISABLE_AUTOUPDATER="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
        if not REMOTE:
            # `sudo -A cmd` -> password popup in the GUI (the password never goes through claude)
            env.update(SUDO_ASKPASS=os.path.join(SHARE, "clawputer-askpass"), CLAWPUTER_ASKPASS_SOCK=ASKPASS_SOCK)
        os.makedirs(WORKDIR, exist_ok=True)
        if REMOTE:
            # same headless claude, but on the PC; args have no spaces so the remote shell is happy
            if WIN:
                remote_cmd = ('cd /d "%USERPROFILE%" && (if not exist clawputer mkdir clawputer) && '
                              'cd clawputer && claude ' + " ".join(args[1:]))
            else:
                remote_cmd = ('PATH="$HOME/.local/bin:$PATH"; mkdir -p ~/clawputer && cd ~/clawputer && claude '
                              + " ".join(args[1:]))
            args = setup.ssh_base() + [remote_cmd]
            cwd = WORKDIR
        else:
            cwd = cwd if cwd and os.path.isdir(cwd) else WORKDIR
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=open(LOGFILE, "a"), cwd=cwd, env=env)
        os.set_blocking(self.proc.stdout.fileno(), False)
        self.buf = b""
        self.req = 0

    def fileno(self):
        return self.proc.stdout.fileno()

    def alive(self):
        return self.proc.poll() is None

    def send(self, obj):
        try:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode())
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def say(self, text):
        return self.send({"type": "user", "message": {"role": "user", "content": text}})

    def interrupt(self):
        self.req += 1
        return self.send({"type": "control_request", "request_id": "gui-int-%d" % self.req,
                          "request": {"subtype": "interrupt"}})

    def answer(self, request_id, allow, tool_input):
        resp = {"behavior": "allow", "updatedInput": tool_input} if allow else \
               {"behavior": "deny", "message": "The user denied this action."}
        self.send({"type": "control_response", "response": {"subtype": "success",
                   "request_id": request_id, "response": resp}})

    def read(self):
        msgs = []
        try:
            chunk = os.read(self.fileno(), 65536)
        except BlockingIOError:
            return msgs
        if not chunk:
            return None
        self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            if line.strip():
                try:
                    msgs.append(json.loads(line))
                except ValueError:
                    log("bad json", line[:200])
        return msgs

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(3)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def short_tool(name, inp):
    if not isinstance(inp, dict):
        return name
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
        if key in inp and inp[key]:
            val = str(inp[key]).replace("\n", " ")
            if key == "file_path":
                val = val.replace(os.path.expanduser("~"), "~")
            return "%s: %s" % (name, val)
    return name


def result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif isinstance(c, dict):
                parts.append("[%s]" % c.get("type", "?"))
        return "\n".join(parts)
    return str(content)


class App:
    def __init__(self, extra_args):
        self.extra_args = extra_args
        self.cfg = load_settings()
        self.fb = Framebuffer()
        self.kb = Keyboard()
        self.small = ImageFont.truetype(FONT_PATH, 9)
        self.lines_cache = None
        self.set_font(int(self.cfg["font"]))
        self.menu = None           # selected row while settings are open
        self.menu_before = None
        self.restart_after_turn = False
        self.hist = None           # chat list while history is open
        self.hist_sel = 0
        self.hist_confirm = False
        self.cwd = WORKDIR
        self.entries = []          # (kind, text)
        self.lines_cache = None
        self.scroll = 0            # lines up from the bottom
        self.input = ""
        self.busy = False
        self.status = "starting claude..."
        self.pending = None        # permission request dict
        self.always = set()
        self.queue = []            # messages typed while claude was busy
        self.last_space = 0
        self.ask = None            # AskUserQuestion in progress
        self.draft_saved = ""
        self.last_key = time.time()
        self.session_id = None
        self.dirty = True
        self.last_clock = 0
        self.t_start = None
        self.claude = None
        self.setup = None
        self.setup_return = False
        self.modescr = None        # "where should claude run?" screen (menu mode)
        self.overlay = None        # help / about box: (title, [lines])
        self.pc_after_setup = False
        self.hidden = False
        self.want_show = False
        self.hidden_since = 0
        self.parked = False        # minimised: claude stopped to save RAM, resumes on wake
        self.boosted = False
        self.boost_wanted = os.environ.get("CLAWPUTER_BOOST") == "1" and not REMOTE
        self.reboot_after = False  # "reboot back to normal?" said yes on quit
        self.unseen = False        # something happened while minimised
        self.prompt_t = 0          # when the current permission prompt / question appeared
        self.wrap_memo = {}        # (message, layout settings) -> wrapped lines
        self.todos = []            # claude's to-do list (TodoWrite)
        self.skip_results = set()  # tool_use ids whose result isn't worth showing (TodoWrite)
        self.image_after = {}      # tool_use id -> image path to preview once its result arrives
        self.glyph_ok = {}         # char -> the main font has it
        self.emoji_cache = {}      # (char, size) -> RGBA image
        self.thumbs = {}           # (path, mtime, width) -> thumbnail
        self.canvas = None         # the image being drawn (for pasting emoji / pictures)
        self.dimmed = False
        self.bright = backlight_get() or 60
        self.seen_len = 0          # chat length at the last draw (keeps the view still while scrolled up)
        self.new_below = False     # new lines arrived below while you're scrolled up
        self.interrupted = False   # you stopped claude: don't send the next queued message
        self.cc_version = ""       # claude code version (from its init message)
        self.model = ""            # short model name for the status bar badge
        self.ctx_used = None       # tokens in claude's context (None = unknown, e.g. just compacted)
        self.ctx_window = 200000
        self.ctx_warned = False
        self.sudo_sock = None      # listening socket for sudo password popups
        self.sudo_req = None       # popup on screen: {"conn", "cmd", "pw", "retry"}
        self.sudo_last = (0, 0)    # (sudo pid, time) of the last answer, to spot a wrong password
        self.sudo_remember = False # "remember for this chat" (tab in the popup)
        self.sudo_forget = None    # sudo pid: when it ends, run `sudo -k` (password not remembered)
        self.sudo_used = False     # typed a sudo password this chat -> `sudo -k` on close
        self.web = None            # clawputer_web.WebUI while the web ui runs
        self.web_splash = None     # [(interface, url)] while the "open this on your phone" screen shows
        self.web_sig = None        # what the browsers last got (only publish changes)
        self.web_chats = None      # chat list a browser asked for
        self.entries_gen = 0       # bumped whenever the chat is cleared / switched
        self.turn = None           # live progress while claude works: phase, tokens (see on_stream)
        self.menu_mode = "--menu" in self.extra_args
        self.net = None if self.menu_mode else NetCheck()
        self.extra_args = [a for a in self.extra_args if a != "--menu"]
        self.splash("starting clawputer...")
        if not self.menu_mode and not REMOTE:
            self.open_askpass()
        if self.menu_mode:
            # first: Claude Code installed + signed in? (background check with a progress
            # screen; installs / signs in if needed), then the mode screen
            self.setup = setup.Setup(self)
        elif REMOTE:
            self.splash("connecting to your pc...")
            self.begin_chat()
        elif os.environ.get("CLAWPUTER_CHECKED") == "1":
            self.begin_chat()                # the menu already checked install + sign-in
        else:
            self.setup = setup.Setup(self)

    def splash(self, text):
        if getattr(self, "hidden", False):
            return
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.text((6, 3), "Clawputer", font=self.font, fill=ACCENT)
        d.line((0, 16, W, 16), fill=BAR)
        d.text((6, 24), text, font=self.small, fill=FG)
        self.fb.write(img)
        if text == "starting clawputer...":
            self.fb.save_raw(SPLASH_FILE)        # the launcher script shows this instantly next time

    def begin_chat(self):
        install_claude_md()
        chats = list_chats() if self.cfg["start"] == "last chat" else []
        if chats:
            self.open_chat(chats[0])
        else:
            self.start_claude()
        if self.cfg.get("web") == "on" and not self.web:
            self.start_web()

    def set_font(self, size):
        self.font = ImageFont.truetype(FONT_PATH, size)
        try:
            self.bold = ImageFont.truetype(BOLD_PATH, size)
        except OSError:
            self.bold = self.font
        bb = self.font.getbbox("M")
        self.cw = max(1, round(self.font.getlength("M")))
        self.lh = (bb[3] - bb[1]) + 3
        self.cols = (W - 12) // self.cw   # leave room for the scroll bar
        self.lines_cache = None

    # -- session --
    def start_claude(self, resume=None, quiet=False):
        if self.claude:
            self.claude.close()
        if self.boost_wanted and not self.boosted:
            self.boost(True)
        self.claude = ClaudeSession(claude_args(self.cfg) + list(self.extra_args), resume=resume, cwd=self.cwd)
        self.busy = False
        self.pending = None
        self.turn = None
        self.restart_after_turn = False
        self.status = "ready"
        if not resume and not quiet:
            self.add("info", "new chat. type a message. ctrl+q quits." +
                     (" ctrl+h minimises the app, claude keeps going." if CAN_HIDE else ""))

    def add(self, kind, text):
        self.entries.append((kind, text))
        self.lines_cache = None
        if self.scroll == 0:
            pass  # stay pinned to bottom
        self.dirty = True

    # -- claude messages --
    def handle(self, m):
        t = m.get("type")
        if t == "stream_event":
            self.on_stream(m.get("event") or {}, m.get("parent_tool_use_id"))
            return
        if t == "user" and self.turn and not m.get("parent_tool_use_id"):
            self.turn["phase"] = "thinking"          # tool finished, claude's on it again
        if t == "system" and m.get("subtype") == "init":
            self.session_id = m.get("session_id")
            self.model = short_model(m.get("model") or "")
            self.cc_version = m.get("claude_code_version") or self.cc_version
            if "[1m]" in (m.get("model") or ""):
                self.ctx_window = 1000000
            self.status = ("pc · " if REMOTE else "") + "ready"
        elif t in ("assistant", "user") or (t == "system" and m.get("subtype") == "compact_boundary"):
            self.ingest(m)
        elif t == "control_request":
            r = m.get("request", {})
            if r.get("subtype") == "can_use_tool":
                name = r.get("tool_name", "?")
                if name == "AskUserQuestion":
                    self.start_ask(m["request_id"], r.get("input", {}))
                elif name in self.always:
                    self.claude.answer(m["request_id"], True, r.get("input", {}))
                else:
                    self.pending = {"id": m["request_id"], "name": name, "input": r.get("input", {})}
                    self.prompt_t = time.time()
                    self.add("ask", "? allow " + short_tool(name, r.get("input")) +
                             "\n  y yes · n no · a always (" + name + ")")
                if name not in self.always:
                    self.notify()                    # claude needs you
                    self.wake_screen()
                    if not self.hidden and self.cfg.get("prompt_sound") == "on":
                        play_chime("prompt")
        elif t == "result":
            self.busy = False
            self.turn = None
            wins = [v.get("contextWindow") for v in (m.get("modelUsage") or {}).values() if isinstance(v, dict)]
            wins = [w for w in wins if isinstance(w, int) and w > 0]
            if wins:
                self.ctx_window = max(wins)
            self.notify()                            # claude's done
            self.ctx_check()
            if self.restart_after_turn:
                self.restart_after_turn = False
                self.add("info", "  applying settings...")
                self.start_claude(resume=self.session_id)
            secs = (time.time() - self.t_start) if self.t_start else 0
            if m.get("is_error") or m.get("subtype") not in ("success", None):
                err = str(m.get("result") or m.get("subtype"))
                self.add("err", "✗ " + err[:200])
                if re.search(r"log ?in|auth|401|api key|oauth|credential", err, re.I) and not REMOTE:
                    self.add("info", "  sign-in problem? F2 > sign out of claude, then sign in again.")
            self.status = "ready · %.0fs" % secs
            stopped, self.interrupted = self.interrupted, False
            if self.queue and not stopped and m.get("subtype") == "success" and not self.offline():
                self.lines_cache = None
                self.send_user(self.queue.pop(0))
            elif self.queue:
                self.add("info", "  %d queued: enter sends the next one, esc edits it." % len(self.queue))
        self.dirty = True

    def ingest(self, m, loading=False):
        t = m.get("type")
        if m.get("isSidechain") or m.get("isMeta"):
            return
        msg = m.get("message") or {}
        content = msg.get("content")
        if t == "system" and m.get("subtype") == "compact_boundary":
            post = (m.get("compact_metadata") or {}).get("post_tokens")
            self.ctx_used = post if isinstance(post, int) and post > 0 else None
            self.ctx_warned = False
            self.add("info", "── chat compacted: claude keeps a summary ──")
            return
        if t == "assistant":
            if not m.get("parent_tool_use_id"):
                self.note_usage(msg.get("usage"))
            for b in content or []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    self.add("assistant", b["text"].strip())
                elif b.get("type") == "tool_use":
                    self.tool_use(b, loading)
        elif t == "user":
            if isinstance(content, str):
                if loading and content.strip() and not content.lstrip().startswith("<"):
                    self.add("user", content.strip())
                return
            for c in content or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_result" and c.get("tool_use_id") in self.skip_results:
                    continue                         # (e.g. "todos updated")
                if c.get("type") == "tool_result":
                    img = self.image_after.pop(c.get("tool_use_id"), None)
                    if img and not c.get("is_error"):
                        self.add("image", img)
                        continue
                    # keep the whole output (the web ui unfolds it); the cardputer trims it when
                    # drawing. capped so one huge file can't eat the RAM
                    txt = result_text(c.get("content")).strip()
                    lines = txt.splitlines()
                    if len(lines) > RESULT_MAX_LINES:
                        txt = "\n".join(lines[:RESULT_MAX_LINES]) + \
                            "\n… +%d more lines (not kept)" % (len(lines) - RESULT_MAX_LINES)
                    self.add("err" if c.get("is_error") else "result", txt[:RESULT_MAX_CHARS] if txt else "(no output)")
                elif loading and c.get("type") == "text" and not c.get("text", "").lstrip().startswith("<"):
                    self.add("user", c.get("text", "").strip())

    def tool_use(self, b, loading=False):
        name, inp, tid = b.get("name", "?"), b.get("input") or {}, b.get("id")
        if name == "TodoWrite" and isinstance(inp.get("todos"), list):
            self.todos = inp["todos"]
            self.skip_results.add(tid)
            self.add("todo", todo_text(self.todos))
            return
        self.add("tool", "▸ " + short_tool(name, inp))
        if name in ("Edit", "MultiEdit", "Write"):
            diff = edit_diff(name, inp, None if REMOTE else self.cwd)
            if diff:
                self.add("diff", diff)
        if REMOTE:
            return
        path = None
        if name == "Read" and str(inp.get("file_path", "")).lower().endswith(IMAGE_EXT):
            path = inp["file_path"]
        elif name == "Bash":                 # a command that makes a picture: show it afterwards
            m = re.findall(r"[\w./~-]+\.(?:png|jpe?g|gif|bmp|webp)\b", str(inp.get("command", "")), re.I)
            if m:
                path = m[-1]
        if path and tid:
            p = os.path.expanduser(path)
            self.image_after[tid] = p if os.path.isabs(p) else os.path.join(self.cwd or WORKDIR, p)

    # -- drafts (half-typed messages survive quitting / switching chats) --
    def draft_key(self):
        return ("pc:" if REMOTE else "local:") + (self.session_id or "_new")

    def save_draft(self):
        try:
            with open(DRAFTS_FILE) as f:
                drafts = json.load(f)
        except (OSError, ValueError):
            drafts = {}
        if self.input.strip():
            drafts[self.draft_key()] = self.input
        else:
            drafts.pop(self.draft_key(), None)
        try:
            with open(DRAFTS_FILE, "w") as f:
                json.dump(drafts, f)
        except OSError as e:
            log("draft save failed", e)
        self.draft_saved = self.input

    def load_draft(self):
        try:
            with open(DRAFTS_FILE) as f:
                self.input = json.load(f).get(self.draft_key(), "")
        except (OSError, ValueError):
            self.input = ""
        self.draft_saved = self.input

    # -- chats --
    def open_chat(self, chat):
        if self.claude:
            self.save_draft()
        self.entries.clear()
        self.entries_gen += 1
        self.lines_cache = None
        self.scroll = 0
        self.always.clear()
        self.queue.clear()
        self.ask = None
        self.ctx_used, self.ctx_warned = None, False
        self.todos = []
        try:
            if REMOTE:
                self.status = "loading chat..."
                self.draw()
                src = remote_py("read", chat["path"]).splitlines()
            else:
                src = open(chat["path"], encoding="utf-8", errors="replace")
            for line in src:
                if '"type":"user"' in line or '"type":"assistant"' in line or \
                        '"type": "user"' in line or '"type": "assistant"' in line or "compact_boundary" in line:
                    try:
                        self.ingest(json.loads(line), loading=True)
                    except ValueError:
                        pass
        except OSError as e:
            self.add("err", "couldn't read chat: %s" % e)
        self.session_id = chat["id"]
        self.cwd = chat.get("cwd") or WORKDIR
        self.start_claude(resume=chat["id"])
        self.add("info", "── resumed: %s ──" % chat["title"][:40])
        self.load_draft()

    def new_chat(self):
        if self.claude:
            self.save_draft()
        self.entries.clear()
        self.entries_gen += 1
        self.lines_cache = None
        self.scroll = 0
        self.always.clear()
        self.queue.clear()
        self.ask = None
        self.ctx_used, self.ctx_warned = None, False
        self.todos = []
        self.session_id = None
        self.cwd = WORKDIR
        self.start_claude()
        self.load_draft()

    def open_history(self):
        if REMOTE:
            self.splash("loading chats from your pc...")
        self.hist = list_chats()
        self.hist_sel = 0
        self.hist_confirm = False

    def ok_to_switch(self):
        """claude mid-reply: switching chats stops it, so ask first"""
        if not self.busy:
            return True
        k = self.dialog("claude is still working", ["switching chats stops this reply."],
                        hint="y switch   any other key stay")
        self.dirty = True
        return k in ("y", "Y")

    def hist_key(self, k):
        items = 1 + len(self.hist)      # row 0 = "+ new chat"
        if self.hist_confirm:
            if k in ("y", "Y"):
                chat = self.hist[self.hist_sel - 1]
                if chat["id"] == self.session_id:
                    if not self.ok_to_switch():
                        self.hist_confirm = False
                        return
                    self.new_chat()          # stop claude first, so it can't write the file back
                try:
                    if REMOTE:
                        remote_py("delete", chat["path"])
                    else:
                        os.remove(chat["path"])
                    _chat_cache.pop(chat["path"], None)
                except OSError as e:
                    log("delete failed", e)
                self.hist = list_chats()
                self.hist_sel = min(self.hist_sel, len(self.hist))
            self.hist_confirm = False
            return
        if k in ("f", "F", "up"):
            self.hist_sel = (self.hist_sel - 1) % items
        elif k in ("x", "X", "down"):
            self.hist_sel = (self.hist_sel + 1) % items
        elif k in ("n", "N"):
            if self.ok_to_switch():
                self.hist = None
                self.new_chat()
        elif k in ("d", "D") and self.hist_sel > 0:
            self.hist_confirm = True
        elif k == "enter":
            sel = self.hist_sel
            chat = self.hist[sel - 1] if sel else None
            same = chat and chat["id"] == self.session_id and self.claude.alive()
            if same or self.ok_to_switch():
                self.hist = None
                if sel == 0:
                    self.new_chat()
                elif not same:
                    self.open_chat(chat)
        elif k in ("esc", "f3", "ctrl+o"):
            self.hist = None

    def draw_history(self, d):
        rows = [("+ new chat", "")] + [(c["title"], ago(c["mtime"])) for c in self.hist]
        fit = 8
        first = max(0, min(self.hist_sel - fit // 2, len(rows) - fit))
        shown = rows[first:first + fit]
        bh = len(shown) * 14 + 32
        top = (H - bh) // 2
        d.rectangle((6, top, W - 6, top + bh), fill=(25, 25, 35), outline=ACCENT)
        d.text((14, top + 4), "chats (%d)" % len(self.hist), font=self.small, fill=ACCENT)
        maxw = W - 60
        for j, (title, age) in enumerate(shown):
            i = first + j
            y = top + 18 + j * 14
            on = i == self.hist_sel
            if on:
                d.rectangle((10, y - 1, W - 10, y + 12), fill=(55, 45, 50))
            cur = i > 0 and self.hist[i - 1]["id"] == self.session_id
            label = ("> " if on else "  ") + ("* " if cur else "") + title
            while self.small.getlength(label) > maxw and len(label) > 4:
                label = label[:-2]
            d.text((14, y), label, font=self.small, fill=FG if on else (USER if cur else DIM))
            if age:
                d.text((W - 14 - self.small.getlength(age), y), age, font=self.small, fill=DIM)
        hint = "delete this chat? y / n" if self.hist_confirm else "f/x move enter open n new d del esc"
        d.text(((W - self.small.getlength(hint)) // 2, top + bh - 12), hint, font=self.small,
               fill=ERR if self.hist_confirm else DIM)

    # -- input --
    def key(self, k):
        self.dirty = True
        if self.sudo_req:
            self.sudo_key(k)
            return
        if self.web_splash:
            self.web_splash = None           # any key closes the web ui address screen
            return
        if self.setup:
            self.setup.key(k)
            return
        if self.modescr is not None:
            self.mode_key(k)
            return
        if k == "ctrl+h" and CAN_HIDE and not self.menu_mode:
            self.hide()                      # minimise any time, even mid-reply or mid-question
            return
        if k == "ctrl+q":
            self.ask_reboot_back()
            raise SystemExit
        if self.overlay:
            self.overlay = None              # any key closes help / about
            return
        if self.menu is not None:
            self.menu_key(k)
            return
        if self.hist is not None:
            self.hist_key(k)
            return
        if self.ask:
            if self.ask["typing"]:
                if k == "esc":                     # back to the options
                    self.ask["typing"] = False
                    self.input = ""
                    return
            else:
                self.ask_key(k)
                return
        if k == "f1":
            self.show_help()
            return
        if k in ("f2", "ctrl+s"):
            self.open_menu()
            return
        if k in ("f3", "ctrl+o"):
            self.open_history()
            return
        if self.pending:
            if k in ("j", "J"):
                self.scroll = 0              # jump to the bottom; the prompt stays
                return
            if k in ("y", "Y", "a", "A", "n", "N", "esc"):
                if time.time() - self.prompt_t >= PROMPT_GRACE:   # not a key you were already typing
                    self.answer_pending(k)
                return
            if k not in SCROLL_KEYS:         # scrolling still works, so you can read what it's about
                return
        if k == "up":
            self.scroll += 1
        elif k == "down":
            self.scroll = max(0, self.scroll - 1)
        elif k in ("left", "pgup"):
            self.scroll += self.view_lines() - 1
        elif k in ("right", "pgdn"):
            self.scroll = max(0, self.scroll - (self.view_lines() - 1))
        elif k == "end":
            self.scroll = 0
        elif k == "esc":
            if self.busy:
                self.claude.interrupt()
                self.interrupted = True
                self.add("info", "  stopping...")
            elif self.input:
                self.input = ""
            elif self.queue:                 # pull the last queued message back to edit it
                self.input = self.queue.pop()
                self.lines_cache = None
            elif CAN_HIDE and not self.menu_mode:
                self.hide()
        elif k == "backspace":
            self.input = self.input[:-1]
        elif k == "ctrl+enter":
            self.submit()
        elif k == "enter":
            if self.cfg["enter"] == "send" or (self.ask and self.ask["typing"]):
                self.submit()
            elif self.input.endswith("\n"):        # enter twice without typing = send
                self.input = self.input.rstrip("\n")
                self.submit()
            elif self.input:
                self.input += "\n"
        elif k == "tab":
            self.input += "  "
        elif k == " ":
            # double-tap space -> ". " (like phones), if the setting is on
            now = time.time()
            if (self.cfg["dblspace"] == "on" and self.input.endswith(" ")
                    and now - self.last_space < 0.5 and len(self.input) >= 2
                    and self.input[-2] not in " .!?,:;\n"):
                self.input = self.input[:-1] + ". "
                self.last_space = 0
            else:
                self.input += " "
                self.last_space = now
        elif len(k) == 1:
            self.input += k

    # -- live progress ("thinking 12s · ↓ 340 tokens"), from --include-partial-messages --
    def on_stream(self, ev, parent):
        tu = self.turn
        if tu is None:
            if not self.busy:
                return
            tu = self.turn = {"phase": "thinking", "done": 0, "chars": 0, "msg": 0}
        et = ev.get("type")
        if et == "message_start":
            tu["done"] += tu["msg"] or tu["chars"] // 4     # previous message, if it never got a total
            tu["msg"], tu["chars"] = 0, 0
        elif et == "content_block_delta":
            dl = ev.get("delta") or {}
            tu["chars"] += len(dl.get("text") or dl.get("thinking") or dl.get("partial_json") or "")
        elif et == "message_delta":
            out = (ev.get("usage") or {}).get("output_tokens")
            if out:
                tu["msg"] = out
        if parent:                                   # a subagent: counts, but our phase stays
            return
        if et == "content_block_start":
            cb = ev.get("content_block") or {}
            kind = cb.get("type")
            if kind in ("thinking", "redacted_thinking"):
                tu["phase"] = "thinking"
            elif kind == "text":
                tu["phase"] = "writing"
            elif kind in ("tool_use", "server_tool_use"):
                tu["phase"] = "using " + cb.get("name", "a tool")
        elif et == "message_stop" and tu["phase"].startswith("using "):
            tu["phase"] = "running " + tu["phase"][6:]

    def turn_tokens(self):
        tu = self.turn
        return tu["done"] + (tu["msg"] or tu["chars"] // 4) if tu else 0

    @staticmethod
    def fmt_tokens(n):
        return "%.1fk" % (n / 1000) if n >= 1000 else str(n)

    def busy_label(self):
        """'thinking 12s · ↓ 340 tokens' (or the plain status if there's no turn info)"""
        tu = self.turn
        if not tu:
            return self.status
        phase = "waiting for you" if (self.pending or self.ask or self.sudo_req) else tu["phase"]
        secs = int(time.time() - (self.t_start or time.time()))
        return "%s %ds · ↓ %s tokens" % (phase, secs, self.fmt_tokens(self.turn_tokens()))

    def answer_pending(self, k):
        p = self.pending
        allow = k in ("y", "Y", "a", "A")
        if k in ("a", "A"):
            self.always.add(p["name"])
        self.claude.answer(p["id"], allow, p["input"])
        self.add("info", ("  ✓ allowed" if allow else "  ✗ denied"))
        self.pending = None

    # -- AskUserQuestion --
    def start_ask(self, request_id, tool_input):
        qs = tool_input.get("questions") or []
        if not qs:
            self.claude.answer(request_id, True, tool_input)
            return
        self.ask = {"id": request_id, "input": tool_input, "qs": qs, "qi": 0, "sel": 0,
                    "picked": set(), "answers": {}, "typing": False, "draft": self.input}
        self.prompt_t = time.time()
        self.wake_screen()
        self.dirty = True

    def ask_options(self):
        q = self.ask["qs"][self.ask["qi"]]
        return [o.get("label", "?") for o in q.get("options", [])] + ["✎ type my own answer"]

    def ask_key(self, k):
        a = self.ask
        q = a["qs"][a["qi"]]
        opts = self.ask_options()
        multi = bool(q.get("multiSelect"))
        if k not in ("f", "F", "x", "X", "up", "down") and time.time() - self.prompt_t < PROMPT_GRACE:
            return                           # still typing your message when the question popped up
        if k in ("f", "F", "up"):
            a["sel"] = (a["sel"] - 1) % len(opts)
        elif k in ("x", "X", "down"):
            a["sel"] = (a["sel"] + 1) % len(opts)
        elif k == " " and multi and a["sel"] < len(opts) - 1:
            a["picked"] ^= {a["sel"]}
        elif k in ("t", "T") or (k == "enter" and a["sel"] == len(opts) - 1):
            a["typing"] = True
            self.input = ""
        elif k == "enter":
            if multi:
                idx = sorted(a["picked"]) or [a["sel"]]
                self.ask_answer(", ".join(opts[i] for i in idx))
            else:
                self.ask_answer(opts[a["sel"]])
        elif k == "esc":
            self.claude.answer(a["id"], False, a["input"])
            self.add("info", "  ✗ skipped the question")
            self.end_ask()

    def ask_answer(self, answer):
        a = self.ask
        q = a["qs"][a["qi"]]
        a["answers"][q.get("question", "")] = answer
        self.add("user", answer)
        a["qi"] += 1
        a["sel"], a["picked"], a["typing"] = 0, set(), False
        if a["qi"] >= len(a["qs"]):
            done = dict(a["input"], answers=a["answers"])
            self.claude.answer(a["id"], True, done)
            self.end_ask()

    def end_ask(self):
        """question done: give you back the message you were typing before it popped up"""
        draft = (self.ask or {}).get("draft", "")
        self.ask = None
        if not self.input.strip():
            self.input = draft

    def draw_pending(self, d):
        """permission prompt floating over the chat while you're scrolled up reading"""
        p = self.pending
        cw = max(1, round(self.small.getlength("M")))
        rows = textwrap.wrap(short_tool(p["name"], p["input"]), (W - 28) // cw)[:3]
        bottom = H - (len(self.input_lines()) * self.lh + 7)
        top = bottom - (30 + len(rows) * 11)
        d.rectangle((6, top, W - 6, bottom), fill=(25, 25, 35), outline=ASK)
        d.text((14, top + 3), "allow %s?" % p["name"], font=self.small, fill=ASK)
        for i, ln in enumerate(rows):
            d.text((14, top + 16 + i * 11), ln, font=self.small, fill=FG)
        hint = "y yes  n no  a always  j jmp btm"
        d.text(((W - self.small.getlength(hint)) // 2, bottom - 12), hint, font=self.small, fill=DIM)

    def draw_ask(self, d):
        a = self.ask
        q = a["qs"][a["qi"]]
        multi = bool(q.get("multiSelect"))
        opts = self.ask_options()
        cw = max(1, round(self.small.getlength("M")))
        width = (W - 28) // cw
        qlines = textwrap.wrap(q.get("question", ""), width)[:3]
        descs = [o.get("description", "") for o in q.get("options", [])] + ["write your own answer"]
        dlines = textwrap.wrap(descs[a["sel"]] or "", width)[:2]
        fit = 5
        first = max(0, min(a["sel"] - fit // 2, len(opts) - fit))
        shown = opts[first:first + fit]
        bh = 16 + len(qlines) * 11 + 4 + len(shown) * 13 + 4 + len(dlines) * 10 + 14
        top = max(1, (H - bh) // 2)
        d.rectangle((6, top, W - 6, top + bh), fill=(25, 25, 35), outline=ASK)
        title = (q.get("header") or "question")
        if len(a["qs"]) > 1:
            title += "  (%d/%d)" % (a["qi"] + 1, len(a["qs"]))
        d.text((14, top + 3), title, font=self.small, fill=ASK)
        y = top + 16
        for ln in qlines:
            d.text((14, y), ln, font=self.small, fill=FG)
            y += 11
        y += 4
        for j, label in enumerate(shown):
            i = first + j
            on = i == a["sel"]
            if on:
                d.rectangle((10, y - 1, W - 10, y + 11), fill=(55, 45, 50))
            box = ""
            if multi and i < len(opts) - 1:
                box = "[x] " if i in a["picked"] else "[ ] "
            d.text((14, y), ("> " if on else "  ") + box + label, font=self.small, fill=FG if on else DIM)
            y += 13
        y += 4
        for ln in dlines:
            d.text((14, y), ln, font=self.small, fill=DIM)
            y += 10
        hint = "f/x move space tick enter ok esc skip" if multi else "f/x move enter pick t type esc skip"
        d.text(((W - self.small.getlength(hint)) // 2, top + bh - 12), hint, font=self.small, fill=DIM)

    def submit(self):
        text = self.input.strip()
        if self.ask and self.ask["typing"]:
            if text:
                self.input = ""
                self.ask_answer(text)
            return
        if not text:
            if self.claude and not self.claude.alive() and not self.parked:
                self.add("info", "  restarting claude...")
                self.start_claude(resume=self.session_id, quiet=True)
            elif self.queue and not self.busy:
                self.send_user(self.queue.pop(0))
                self.lines_cache = None
            return
        self.input = ""
        self.scroll = 0
        if text in ("/exit", "/quit"):
            self.ask_reboot_back()
            raise SystemExit
        if text in ("/settings", "/s"):
            self.open_menu()
            return
        if text in ("/help", "/?"):
            self.show_help()
            return
        if text in ("/history", "/h", "/chats"):
            self.open_history()
            return
        if text in ("/new", "/clear"):
            if self.ok_to_switch():
                self.new_chat()
            return
        if self.offline() and not self.busy:
            self.queue.append(text)
            self.lines_cache = None
            self.add("info", "  offline: it'll send when you're back online.")
            return
        if self.busy:
            # claude answers in order, so hold this until the current reply finishes;
            # otherwise it would show up above the answer to the previous message
            self.queue.append(text)
            self.lines_cache = None
            return
        self.send_user(text)

    # -- context meter + chime --
    def note_usage(self, u):
        if not isinstance(u, dict):
            return
        used = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                           "cache_creation_input_tokens", "output_tokens"))
        if used > 0:
            self.ctx_used = used
            self.dirty = True

    def ctx_frac(self):
        return None if self.ctx_used is None else min(1.0, self.ctx_used / max(1, self.ctx_window))

    def ctx_check(self):
        f = self.ctx_frac()
        if f is not None and f >= 0.8 and not self.ctx_warned:
            self.ctx_warned = True
            self.add("info", "  context %d%% full. /compact shrinks it, /new starts fresh." % (f * 100))

    def notify(self):
        """while minimised: remember there's something new, and chime"""
        if not self.hidden:
            return
        self.unseen = True
        if self.cfg.get("chime") == "on":
            play_chime()

    def offline(self):
        return bool(self.net) and self.net.online is False

    def net_tick(self):
        """connection came / went: redraw; back online -> send what was held"""
        if not self.net or not self.net.changed:
            return
        self.net.changed = False
        self.dirty = True
        if self.net.online and self.queue and not self.busy and not self.parked and self.claude:
            self.send_user(self.queue.pop(0))

    def send_user(self, text):
        if not self.claude.alive():
            self.add("info", "claude restarted.")
            self.start_claude(resume=self.session_id)
        self.add("user", text)
        if self.claude.say(text):
            self.busy = True
            self.t_start = time.time()
            self.status = "thinking"
            self.turn = {"phase": "thinking", "done": 0, "chars": 0, "msg": 0}
        else:
            self.entries.pop()               # not sent: put it back in the box
            self.lines_cache = None
            self.input = text
            self.add("err", "couldn't reach claude. enter to try again.")

    # -- settings menu --
    def open_menu(self):
        self.menu = 0
        self.submenu = None        # highlighted choice while a setting is open
        self.menu_before = dict(self.cfg)

    def menu_key(self, k):
        # f / up = up, x / down = down, enter = select, esc = back (esc on top level = close)
        up = k in ("f", "F", "up")
        down = k in ("x", "X", "down")
        key, label, choices, _ = SETTINGS[self.menu]
        if self.submenu is None:
            if up:
                self.menu = (self.menu - 1) % len(SETTINGS)
            elif down:
                self.menu = (self.menu + 1) % len(SETTINGS)
            elif k == "enter" and choices is None:
                self.close_menu()
                self.run_action(key)
            elif k == "enter":
                self.submenu = choices.index(self.cfg[key]) if self.cfg[key] in choices else 0
            elif k in ("esc", "f2", "ctrl+s"):
                self.close_menu()
        else:
            if up:
                self.submenu = (self.submenu - 1) % len(choices)
            elif down:
                self.submenu = (self.submenu + 1) % len(choices)
            elif k == "enter":
                self.cfg[key] = choices[self.submenu]
                if key == "font":
                    self.set_font(int(self.cfg["font"]))
                self.submenu = None
            elif k == "esc":
                self.submenu = None

    # -- help / about overlays --
    def show_help(self):
        self.overlay = ("keys", [
            "enter        send message",
            "fn+f / fn+x  scroll a line",
            "fn+z / fn+c  scroll a page",
            "esc          stop claude / clear box",
        ] + (["ctrl+h       minimise app (claude keeps going)"] if CAN_HIDE else []) + [
            "y  n  a      answer prompts  (j = jump down)",
            "F2           settings",
            "F3           chats (n new, d delete)",
            "in menus     f / x move, enter pick, esc back",
            "/new         new chat",
            "ctrl+q       quit",
        ])

    def show_about(self):
        try:
            ver = open(os.path.join(SHARE, "VERSION")).read().strip()
        except OSError:
            ver = "dev"
        # (no `claude --version` / `claude auth status`: each loads another ~385MB claude next
        # to the running one. the version comes from claude's start-up message instead)
        cv = self.cc_version or "? (starts with the chat)"
        if REMOTE:
            c = setup.remote_cfg() or {}
            who, where = "(managed on the pc)", "your pc: %s@%s" % (c.get("user"), c.get("host"))
        else:
            who = claude_email() or ("yes" if setup.quick_signed_in() else "not signed in")
            where = "this cardputer"
        inst, gpu = boost_state()
        boost = ("active, +112MB" if gpu <= 16 else "on") if inst else "not set up"
        self.overlay = ("about", [
            "Clawputer %s (unofficial)" % ver,
            "claude code %s" % cv,
            "signed in: %s" % who[:30],
            "running on: %s" % where[:28],
            "ram free: %dMB   boost: %s" % (mem_free_mb(), boost),
            "github.com/RubberDingyRapids/clawputer",
        ])

    def draw_overlay(self, d):
        title, lines = self.overlay
        bh = len(lines) * 12 + 30
        top = max(2, (H - bh) // 2)
        d.rectangle((8, top, W - 8, top + bh), fill=(25, 25, 35), outline=ACCENT)
        d.text((16, top + 4), title, font=self.small, fill=ACCENT)
        for i, ln in enumerate(lines):
            d.text((16, top + 18 + i * 12), ln, font=self.small, fill=FG)
        hint = "any key to close"
        d.text(((W - self.small.getlength(hint)) // 2, top + bh - 12), hint, font=self.small, fill=DIM)

    # -- mode screen: where should claude run? (menu mode) --
    MODE_DESC = {
        "local": "runs here on the cardputer. claude uses ~200MB of the ~350MB ram, so a ram boost is recommended.",
        "pc": "runs on your pc over ssh: fast, no ram limits. the pc must be on.",
    }

    def open_mode(self, first=False):
        inst, gpu = boost_state()
        self.modescr = {"first": first, "boost": inst, "gpu": gpu, "ram": None, "sel": 0}
        last = {"gui": "local", "remote": "pc"}.get(read_file(os.path.join(CONF, "ui")), None)
        last = last or read_file(os.path.join(CONF, "ui")) or "local"
        keys = [i[0] for i in self.mode_items()]
        self.modescr["sel"] = keys.index(last) if last in keys else 0
        self.dirty = True

    def mode_items(self):
        rc = setup.remote_cfg()
        return [("local", "on this cardputer"),
                ("pc", "on my pc" if rc else "on my pc  (set up)")]

    def ram_options(self):
        """-> [(key, label, description, choice)] for the 'on this cardputer' RAM menu"""
        m = self.modescr
        pw = " asks your password once." if not m["boost"] else ""
        return [
            ("max", "max ram boost (recommended)",
             "pauses background stuff, then restarts with gpu memory moved to ram (+112MB, "
             "camera off until the next restart). open Clawputer again after." + pw,
             "reboot" if m["boost"] else "boost-setup-max"),
            ("boost", "ram boost",
             "pauses background stuff (desktop, printing etc) while claude runs, no restart." + pw,
             "local" if m["boost"] else "boost-setup-local"),
            ("plain", "just run it",
             "no changes. slower, and big tasks may run out of memory.", "local-noboost"),
        ]

    def mode_key(self, k):
        m = self.modescr
        if m["ram"] is not None:                        # the RAM options menu
            opts = self.ram_options()
            if k in ("f", "F", "up"):
                m["ram"] = (m["ram"] - 1) % len(opts)
            elif k in ("x", "X", "down"):
                m["ram"] = (m["ram"] + 1) % len(opts)
            elif k in ("1", "2", "3"):
                m["ram"] = int(k) - 1
                self.pick_ram()
            elif k == "enter":
                self.pick_ram()
            elif k == "esc":
                m["ram"] = None
            return
        items = self.mode_items()
        keys = [i[0] for i in items]
        hot = {"g": "local", "c": "local", "r": "pc", "p": "pc"}
        if k in ("f", "F", "up"):
            m["sel"] = (m["sel"] - 1) % len(items)
        elif k in ("x", "X", "down"):
            m["sel"] = (m["sel"] + 1) % len(items)
        elif k == "enter":
            self.mode_pick(keys[m["sel"]])
        elif k in hot:
            m["sel"] = keys.index(hot[k])
            self.mode_pick(hot[k])
        elif k in ("q", "Q", "esc", "ctrl+q"):
            self.choose("quit")

    def pick_ram(self):
        key, _, _, choice = self.ram_options()[self.modescr["ram"]]
        if choice in ("reboot", "local") and boost_helpers_outdated():
            choice = "boost-setup-max" if choice == "reboot" else "boost-setup-local"
            self.boost_update = True
        try:
            with open(os.path.join(CONF, "ram"), "w") as f:
                f.write(key)
        except OSError:
            pass
        self.choose(choice)

    def mode_pick(self, key):
        m = self.modescr
        if key == "local":
            if m["boost"] and m["gpu"] <= 16:
                self.choose("local")                     # already max-boosted this boot
            last = read_file(os.path.join(CONF, "ram")) or "max"
            keys = [o[0] for o in self.ram_options()]
            m["ram"] = keys.index(last) if last in keys else 0
        elif key == "pc" and not setup.remote_cfg():
            self.pc_after_setup = True
            self.setup = setup.Setup(self, remote_only=True)
        else:
            self.choose(key)

    def choose(self, choice):
        if choice in ("boost-setup-max", "boost-setup-local"):
            if not self.setup_boost():
                self.dirty = True
                return                                   # back to the menu
            choice = "reboot" if choice == "boost-setup-max" else "local"
        if choice == "reboot":
            self.splash("restarting with more ram...\nopen Clawputer again after it restarts.")
            err = ""
            try:
                p = subprocess.run(["sudo", "-n", BOOTMODE, "enter"], stdin=subprocess.DEVNULL,
                                   capture_output=True, timeout=60)
                if p.returncode == 0:
                    time.sleep(60)                       # the reboot takes us down
                    err = "it didn't restart"
                else:
                    err = (p.stderr.decode(errors="replace").strip().splitlines() or ["error %d" % p.returncode])[-1]
            except (OSError, subprocess.TimeoutExpired) as e:
                err = str(e)
            log("bootmode enter", err)
            self.dialog("couldn't restart", ["the max boost restart didn't work: " + err[:80],
                                             "carrying on without it."], hint="any key")
            choice = "local"                             # didn't reboot: just run it
        try:
            os.makedirs(CONF, exist_ok=True)
            with open(os.path.join(CONF, "choice"), "w") as f:
                f.write(choice)
            ui = "pc" if choice == "pc" else ("local" if choice != "quit" else None)
            if ui:
                with open(os.path.join(CONF, "ui"), "w") as f:
                    f.write(ui)
        except OSError as e:
            log("choice", e)
        raise SystemExit

    def draw_mode(self, img, d):
        m = self.modescr
        title = "You're in ✓  where should claude run?" if m["first"] else "Clawputer · where should claude run?"
        d.text((6, 3), title, font=self.small, fill=ACCENT)
        right = battery_clock()
        d.text((W - 4 - self.small.getlength(right), 3), right, font=self.small, fill=DIM)
        d.line((0, 15, W, 15), fill=BAR)
        items = self.mode_items()
        y = 21
        for i, (key, label) in enumerate(items):
            on = i == m["sel"]
            if on:
                d.rectangle((4, y - 2, W - 4, y + 12), fill=(55, 45, 50))
            d.text((10, y), ("> " if on else "  ") + label, font=self.font, fill=FG if on else DIM)
            y += 16
        desc = self.MODE_DESC.get(items[m["sel"]][0], "")
        if items[m["sel"]][0] == "pc" and setup.remote_cfg():
            c = setup.remote_cfg()
            desc += " pc: %s@%s" % (c["user"], c["host"])
        y += 4
        for ln in textwrap.wrap(desc, 56)[:3]:
            d.text((8, y), ln, font=self.small, fill=DIM)
            y += 11
        if m["boost"] and m["gpu"] <= 16:
            st, col = "max ram boost active · +112MB · %dMB free" % mem_free_mb(), (120, 210, 130)
        else:
            st, col = "ram free %dMB" % mem_free_mb(), DIM
        d.text((6, H - 24), st, font=self.small, fill=col)
        d.text((6, H - 12), "f/x move  enter pick  q quit", font=self.small, fill=DIM)
        if m["ram"] is not None:
            opts = self.ram_options()
            desc = textwrap.wrap(opts[m["ram"]][2], 52)[:4]
            bh = 18 + len(opts) * 14 + 4 + len(desc) * 11 + 16
            top = max(2, (H - bh) // 2)
            d.rectangle((8, top, W - 8, top + bh), fill=(25, 25, 35), outline=ACCENT)
            d.text((16, top + 4), "running on the cardputer: ram", font=self.small, fill=ACCENT)
            yy = top + 18
            for i, (_, label, _, _) in enumerate(opts):
                on = i == m["ram"]
                if on:
                    d.rectangle((12, yy - 1, W - 12, yy + 11), fill=(55, 45, 50))
                d.text((18, yy), ("> " if on else "  ") + "%d %s" % (i + 1, label), font=self.small,
                       fill=FG if on else DIM)
                yy += 14
            yy += 4
            for ln in desc:
                d.text((18, yy), ln, font=self.small, fill=DIM)
                yy += 11
            hint = "f/x move  enter pick  esc back"
            d.text(((W - self.small.getlength(hint)) // 2, top + bh - 13), hint, font=self.small, fill=ACCENT)
        self.fb.write(img)
        self.dirty = False

    def run_action(self, key):
        if key == "@switch":
            target = "local" if REMOTE else "pc"
            if target == "pc" and not setup.remote_cfg():
                self.add("info", "  no pc set up yet: F2 > remote pc setup first.")
                return
            if target == "local" and not setup.claude_installed():
                self.add("info", "  claude code isn't on this cardputer yet: quit, then pick 'on this cardputer'.")
                return
            if not self.ok_to_switch():
                return
            try:
                with open(os.path.join(CONF, "switch"), "w") as f:
                    f.write(target)              # the launcher script starts the other kind of chat
                with open(os.path.join(CONF, "ui"), "w") as f:
                    f.write(target)
            except OSError as e:
                log("switch", e)
                return
            self.splash("switching to " + ("this cardputer..." if target == "local" else "your pc..."))
            raise SystemExit
        if key == "@web":
            if self.web:
                self.stop_web()
                self.add("info", "  web ui stopped.")
            else:
                self.start_web()
            return
        if key == "@about":
            self.show_about()
            return
        if key == "@remote":
            self.setup = setup.Setup(self, remote_only=True)
            self.setup_return = True
        elif key == "@signout":
            if REMOTE:
                self.add("info", "  sign out on the PC itself (this chat runs there).")
                return
            if self.claude:
                self.claude.close()
            subprocess.run([CLAUDE, "auth", "logout"], capture_output=True, timeout=30)
            self.entries.clear()
            self.entries_gen += 1
            self.lines_cache = None
            self.setup = setup.Setup(self)
            self.setup_return = False

    def close_menu(self):
        self.menu = None
        save_settings(self.cfg)
        self.lines_cache = None              # chat style may have changed
        changed = [st for st in SETTINGS if st[3] and self.cfg.get(st[0]) != self.menu_before.get(st[0])]
        if self.cfg.get("maxboost") != self.menu_before.get("maxboost"):
            self.set_keep_boost(self.cfg["maxboost"] == "keep")
        if self.cfg.get("web") == "on" and self.menu_before.get("web") != "on" and not self.web:
            self.start_web()
        if changed:
            what = ", ".join("%s=%s" % (st[1], self.cfg[st[0]]) for st in changed)
            if self.busy:
                self.add("info", "  saved (%s). applies after this reply." % what)
                self.restart_after_turn = True
            else:
                self.add("info", "  %s. restarting claude..." % what)
                self.draw()
                self.start_claude(resume=self.session_id)

    def menu_value(self, key):
        if key == "@web":
            return "on" if self.web else "off"
        if key == "@switch":
            return "→ cardputer" if REMOTE else "→ pc"
        return self.cfg.get(key, "→")

    def draw_menu(self, d):
        if self.submenu is None:
            title = "settings"
            items = [(label, self.menu_value(key)) for key, label, _, _ in SETTINGS]
            sel = self.menu
            hint = "f up  x down  enter open  esc close"
            key = SETTINGS[sel][0]
            about = SETTING_HELP.get(key, "")
        else:
            key, label, choices, _ = SETTINGS[self.menu]
            title = "settings > " + label
            items = [(c, "current" if c == self.cfg[key] else "") for c in choices]
            sel = self.submenu
            hint = "f up  x down  enter pick  esc back"
            about = CHOICE_HELP.get((key, choices[sel])) or SETTING_HELP.get(key, "")
        about = textwrap.wrap(about, int((W - 50) / self.small.getlength("M")))[:2]
        rows_fit = 6 if about else 7
        first = max(0, min(sel - rows_fit // 2, len(items) - rows_fit))
        shown = items[first:first + rows_fit]
        bh = len(shown) * 14 + 32 + len(about) * 11 + (3 if about else 0)
        top = (H - bh) // 2
        d.rectangle((14, top, W - 14, top + bh), fill=(25, 25, 35), outline=ACCENT)
        d.text((22, top + 4), title, font=self.small, fill=ACCENT)
        for j, (name, val) in enumerate(shown):
            i = first + j
            y = top + 18 + j * 14
            on = i == sel
            if on:
                d.rectangle((18, y - 1, W - 18, y + 12), fill=(55, 45, 50))
            d.text((24, y), ("> " if on else "  ") + name, font=self.small, fill=FG if on else DIM)
            if val:
                d.text((W - 24 - self.small.getlength(val), y), val, font=self.small, fill=ACCENT if on else FG)
        y = top + 18 + len(shown) * 14 + 1
        for ln in about:
            d.text((22, y), ln, font=self.small, fill=ERR if about[0].startswith("⚠") else DIM)
            y += 11
        d.text(((W - self.small.getlength(hint)) // 2, top + bh - 12), hint, font=self.small, fill=DIM)

    # -- drawing --
    def wrap(self, kind, text, label=""):
        """cached _wrap(): a message is laid out once (until the font / style / settings change)"""
        k = (kind, text, label, self.cols, self.cfg.get("style"), self.cfg["tool_lines"])
        hit = self.wrap_memo.get(k)
        if hit is None:
            if len(self.wrap_memo) > 3000:
                self.wrap_memo.clear()
            hit = self.wrap_memo[k] = self._wrap(kind, text, label)
        return hit

    def _wrap(self, kind, text, label=""):
        """-> list of (kind, text, boxpos); boxpos None, or top (border + label) / mid / bot"""
        if kind == "image":
            return self.image_rows(text)
        text = self.pad_emoji(text)
        label = self.pad_emoji(label)
        out = []
        if kind in ("diff", "todo"):
            hard = lambda s, w: [s[i:i + w] for i in range(0, max(len(s), 1), w)]
            out.append((kind, "changes" if kind == "diff" else todo_label(self.todos, text), "top"))
            for raw in text.split("\n"):
                out.extend((kind, c, "mid") for c in hard(raw, self.cols - 4))
            out.append((kind, "", "bot"))
            return out
        width = self.cols - 2
        inner = self.cols - 4            # text width inside a box
        bubbles = self.cfg.get("style") == "bubbles"
        hard = lambda s, w: [s[i:i + w] for i in range(0, max(len(s), 1), w)]
        if kind in ("result", "err"):
            out.append((kind, label, "top"))
            rows = text.split("\n")
            limit = len(rows) if self.cfg["tool_lines"] == "all" else int(self.cfg["tool_lines"])
            if len(rows) > limit:
                rows = rows[:limit] + ["… +%d lines" % (len(rows) - limit)]
            for raw in rows:
                out.extend((kind, c, "mid") for c in hard(raw, inner))
            out.append((kind, "", "bot"))
            return out
        in_code = False
        table = []
        for raw in text.split("\n"):
            if kind == "assistant" and not in_code and raw.strip().startswith("|"):
                table.append(raw)                 # markdown table: collect, draw as a grid
                continue
            if table:
                out.extend(table_lines(table, width))
                table = []
            if kind == "assistant" and raw.strip().startswith("```"):
                if in_code:
                    out.append(("code", "", "bot"))
                else:
                    out.append(("code", raw.strip()[3:].strip(), "top"))
                in_code = not in_code
                continue
            if in_code:
                out.extend(("code", c, "mid") for c in hard(raw, inner))
                continue
            line = raw
            if kind == "assistant" and bubbles:
                out.extend(self.md_lines(raw, width))
                continue
            if kind == "user" and bubbles:
                continue                          # done below, as one bubble
            if kind == "assistant":
                line = line.replace("**", "").replace("`", "")
                if line.lstrip().startswith("#"):
                    line = line.lstrip("# ").upper()
            chunks = textwrap.wrap(line, width, break_long_words=True, replace_whitespace=False,
                                   drop_whitespace=True) or [""]
            for i, c in enumerate(chunks):
                if kind == "user":
                    c = ("› " if i == 0 else "  ") + c
                out.append((kind, c, None))
        if table:
            out.extend(table_lines(table, width))
        if in_code:                           # unclosed fence
            out.append(("code", "", "bot"))
        if kind == "user" and bubbles:
            bw = max(10, int(self.cols * 0.8))
            rows = []
            for raw in text.split("\n"):
                rows.extend(textwrap.wrap(raw, bw, break_long_words=True) or [""])
            span = max(len(r) for r in rows)
            out.extend((kind, r, ("bubble", span, i == len(rows) - 1)) for i, r in enumerate(rows))
        return out

    def md_lines(self, raw, width):
        """one line of claude's markdown -> wrapped lines of styled runs [(text, style)]
        styles: None, "b" bold, "code", "h" heading, "dot" list bullet"""
        head = re.match(r"^\s*#{1,6}\s+(.*)", raw)
        prefix, body = "", raw
        if head:
            runs = [(head.group(1).replace("**", "").replace("`", ""), "h")]
        else:
            m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)", raw)
            if m:
                prefix = m.group(1) + ("•" if m.group(2) in "-*+" else m.group(2)) + " "
                body = m.group(3)
            runs = []
            for part in MD_TOKEN.split(body):
                if not part:
                    continue
                if len(part) > 4 and part.startswith("**") and part.endswith("**"):
                    runs.append((part[2:-2], "b"))
                elif len(part) > 2 and part[0] == "`" and part[-1] == "`":
                    runs.append((part[1:-1], "code"))
                else:
                    runs.append((part.replace("**", ""), None))
        chars = "".join(t for t, _ in runs)
        styles = [st for t, st in runs for _ in t]
        w = max(8, width - len(prefix))
        spans, start = [], 0                  # greedy word wrap, keeping char positions
        while start < len(chars):
            while start < len(chars) and chars[start] == " " and spans:
                start += 1
            end = min(len(chars), start + w)
            if end < len(chars):
                cut = chars.rfind(" ", start, end + 1)
                if cut > start:
                    end = cut
            spans.append((start, end))
            start = end
        out = []
        for i, (a, b) in enumerate(spans or [(0, 0)]):
            line = [(prefix if i == 0 else " " * len(prefix), "dot")] if prefix else []
            j = a
            while j < b:
                k = j
                while k < b and styles[k] == styles[j]:
                    k += 1
                line.append((chars[j:k], styles[j]))
                j = k
            out.append(("assistant", line, None))
        return out

    def all_lines(self):
        if self.lines_cache is None:
            lines = []
            ents = self.entries
            skip = False
            for i, (kind, text) in enumerate(ents):
                if skip:
                    skip = False
                    continue
                if kind == "user" and lines:
                    lines.append(("gap", "", None))
                nxt = ents[i + 1][0] if i + 1 < len(ents) else None
                if kind == "tool" and nxt in ("result", "err"):
                    # the tool call becomes the label on top of its output box
                    lines.extend(self.wrap(nxt, ents[i + 1][1], label=text.lstrip("▸ ")))
                    skip = True
                    continue
                lines.extend(self.wrap(kind, text))
            for q in self.queue:
                lines.append(("gap", "", None))
                lines.extend(("queued", t, None) for _, t, _ in self.wrap("user", "(queued) " + q))
            self.lines_cache = lines
        return self.lines_cache

    def input_lines(self):
        """word-wrap the message box (keeps spaces, honours new lines), cursor at the end"""
        width = self.cols - 2
        rows, cur = [], ""
        for para_i, para in enumerate(self.pad_emoji(self.input).split("\n")):
            if para_i:
                rows.append(cur)
                cur = ""
            for tok in re.findall(r"\S+|\s", para):
                if len(cur) + len(tok) <= width:
                    cur += tok
                elif tok.isspace():
                    rows.append(cur)
                    cur = ""
                else:
                    if cur.strip():
                        rows.append(cur)
                        cur = ""
                    while len(tok) > width:
                        rows.append(tok[:width])
                        tok = tok[width:]
                    cur = tok
        cur += "_"
        if len(cur) > width:
            rows.append(cur[:-1])
            cur = "_"
        rows.append(cur)
        return rows[-3:]

    def view_lines(self):
        top = 12
        bottom = H - (len(self.input_lines()) * self.lh + 5)
        return max(1, (bottom - top) // self.lh)

    # -- emoji: the monospace font has none; draw them from Noto Color Emoji, 2 cells wide --
    def has_glyph(self, ch):
        ok = self.glyph_ok.get(ch)
        if ok is None:
            notdef = bytes(self.font.getmask("\U0010FFFF"))
            ok = self.glyph_ok[ch] = bytes(self.font.getmask(ch)) != notdef
        return ok

    def is_emoji(self, ch):
        o = ord(ch)
        return (o >= 0x1F000 or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF) and not self.has_glyph(ch)

    def pad_emoji(self, s):
        if not isinstance(s, str) or s.isascii():
            return s
        s = s.replace("\ufe0f", "").replace("\u200d", "")
        return "".join(ch + EMOJI_PAD if self.is_emoji(ch) else ch for ch in s)

    def emoji_image(self, ch, size):
        key = (ch, size)
        if key not in self.emoji_cache:
            im = None
            if os.path.exists(EMOJI_FONT):
                try:
                    f = ImageFont.truetype(EMOJI_FONT, 109)      # bitmap font: only size 109 exists
                    big = Image.new("RGBA", (140, 130), (0, 0, 0, 0))
                    ImageDraw.Draw(big).text((0, 0), ch, font=f, embedded_color=True)
                    box = big.getbbox()
                    if box:
                        im = big.crop(box).resize((size, size), Image.LANCZOS)
                except (OSError, ValueError) as e:
                    log("emoji", e)
            self.emoji_cache[key] = im
        return self.emoji_cache[key]

    def text(self, d, xy, s, font, fill):
        """d.text that also draws emoji (and skips their padding). -> x after the text"""
        x, y = xy
        if s.isascii() or EMOJI_PAD not in s:
            d.text((x, y), s, font=font, fill=fill)
            return x + font.getlength(s)
        cell = self.font.getlength("M")
        run = ""
        for ch in s:
            if ch == EMOJI_PAD:
                continue
            if self.is_emoji(ch):
                if run:
                    d.text((x, y), run, font=font, fill=fill)
                    x += font.getlength(run)
                    run = ""
                size = max(8, min(self.lh - 2, int(cell * 2) - 1))
                im = self.emoji_image(ch, size)
                if im is not None and self.canvas is not None:
                    self.canvas.paste(im, (int(x + (cell * 2 - size) / 2), y + 1), im)
                else:
                    d.text((x, y), "▫", font=font, fill=DIM)
                x += cell * 2
            else:
                run += ch
        if run:
            d.text((x, y), run, font=font, fill=fill)
            x += font.getlength(run)
        return x

    # -- pictures claude made or opened --
    def thumb(self, path):
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return None
        key = (path, mt, self.cols)
        if key not in self.thumbs:
            try:
                im = Image.open(path)
                im.draft("RGB", (W, 6 * self.lh))
                im = im.convert("RGB")
                im.thumbnail((W - 24, 6 * self.lh))
                if len(self.thumbs) > 20:
                    self.thumbs.clear()
                self.thumbs[key] = im
            except Exception as e:                  # anything PIL can't read
                log("image", path, e)
                self.thumbs[key] = None
        return self.thumbs[key]

    def image_rows(self, path):
        im = self.thumb(path)
        name = os.path.basename(path)
        if im is None:
            return [("info", "  (picture %s isn't there / can't be shown)" % name[:40], None)]
        rows = -(-(im.size[1] + 2) // self.lh)
        return [("info", "  🖼" + EMOJI_PAD + " " + name[:44], None)] + \
            [("image", path, ("img", i, rows)) for i in range(rows)]

    def paste_image(self, canvas, path, y0):
        im = self.thumb(path)
        if im is None:
            return
        top, bottom = 13, H - (len(self.input_lines()) * self.lh + 5)
        crop_top = max(0, top - y0)
        visible = im.crop((0, crop_top, im.size[0], max(crop_top, min(im.size[1], bottom - y0))))
        if visible.size[1] > 0:
            canvas.paste(visible, (12, y0 + crop_top + 2))

    # -- dim the backlight when idle --
    def dim_tick(self):
        mins = {"1 min": 60, "5 min": 300}.get(self.cfg.get("dim"))
        if mins and not self.dimmed and not self.pending and time.time() - self.last_key > mins:
            cur = backlight_get()
            if cur:
                self.bright = cur
            backlight_set(max(3, self.bright // 8))
            self.dimmed = True

    def wake_screen(self):
        if self.dimmed:
            backlight_set(self.bright)
            self.dimmed = False

    def draw_status(self, d):
        """top bar: status on the left; context meter, model, network, battery, clock on the right"""
        offline = self.offline()
        if self.busy:
            left = "|/-\\"[int(time.time() * 4) % 4] + " " + self.busy_label()
        elif offline:
            left = "offline: " + ("can't reach your pc" if REMOTE else "no internet")
        else:
            left = "claude · " + self.status
        if self.queue:                       # first, so a full status bar cuts the rest, not this
            left = "%d queued · " % len(self.queue) + left.replace("claude · ", "")
        sm = self.small
        x = W - 3
        clock = time.strftime("%H:%M")
        x -= sm.getlength(clock)
        d.text((x, 0), clock, font=sm, fill=DIM)
        cap, chg = battery()
        if cap is not None:
            pct = "%d%%" % cap
            x -= sm.getlength(pct) + 4
            d.text((x, 0), pct, font=sm, fill=DIM)
            x -= 17
            col = OK if cap > 40 or chg else (TOOL if cap > 15 else ERR)
            d.rectangle((x, 2, x + 12, 8), outline=DIM)
            d.rectangle((x + 13, 4, x + 14, 6), fill=DIM)
            if cap:
                d.rectangle((x + 2, 4, x + 2 + max(1, round(8 * cap / 100)), 6), fill=col)
            if chg:
                d.polygon([(x + 7, 0), (x + 3, 6), (x + 6, 6), (x + 5, 10), (x + 10, 4), (x + 7, 4)],
                          fill=(255, 220, 90), outline=BG)
        x -= 14
        self.draw_net_icon(d, x, 1, offline)
        if self.model and not self.busy:
            mw = sm.getlength(self.model)
            x -= mw + 10
            d.rounded_rectangle((x, 1, x + mw + 6, 10), radius=3, outline=BOX)
            d.text((x + 3, 0), self.model, font=sm, fill=DIM)
        if self.todos and any(t.get("status") != "completed" for t in self.todos):
            done = sum(t.get("status") == "completed" for t in self.todos)
            tl = "✓%d/%d" % (done, len(self.todos))
            x -= sm.getlength(tl) + 6
            d.text((x, 0), tl, font=sm, fill=OK)
        f = self.ctx_frac()
        if f is not None:
            x -= 24
            col = ERR if f >= 0.9 else (TOOL if f >= 0.7 else DIM)
            d.rectangle((x, 3, x + 19, 7), outline=BOX)
            d.rectangle((x + 1, 4, x + 1 + max(1, round(17 * f)), 6), fill=col)
            x -= sm.getlength("ctx") + 3
            d.text((x, 0), "ctx", font=sm, fill=DIM)
        room = x - 8
        while left and sm.getlength(left) > room:
            left = left[:-2] + "…" if len(left) > 2 else ""
        d.text((3, 0), left, font=sm, fill=ERR if offline else ACCENT)
        d.line((0, 11, W, 11), fill=BAR)

    @staticmethod
    def draw_net_icon(d, x, y, offline):
        kind = net_kind()
        if kind == "tether" and not offline:          # bluetooth rune: phone tethering
            c, cx = USER, x + 5
            d.line((cx, y, cx, y + 9), fill=c)
            d.line([(cx, y), (cx + 3, y + 2), (cx - 3, y + 7)], fill=c)
            d.line([(cx, y + 9), (cx + 3, y + 7), (cx - 3, y + 2)], fill=c)
            return
        c = ERR if offline or not kind else FG          # wifi fan
        cx, cy = x + 5, y + 9
        for r in (9, 6, 3):
            d.arc((cx - r, cy - r, cx + r, cy + r), 225, 315, fill=c)
        d.point((cx, cy - 1), fill=c)
        if offline or not kind:
            d.line((x, y, x + 10, y + 9), fill=ERR)

    def draw_setup(self, img, d):
        title, lines, qr, foot = self.setup.lines()
        d.text((6, 3), title, font=self.font, fill=ACCENT)
        d.line((0, 16, W, 16), fill=BAR)
        q = setup.qr_image(qr, 118) if qr else None
        textw = W - 12 - (q.size[0] + 8 if q else 0)
        maxc = max(10, int(textw // max(1, self.small.getlength("M"))))
        styles = {"ok": (120, 210, 130), "accent": ACCENT, "dim": DIM, "cmd": TOOL}
        y = 22
        for ln in lines:
            if isinstance(ln, tuple) and ln[0] == "__bar__":      # progress bar
                _, frac, label = ln
                bw = int(textw * 0.62)
                d.rectangle((10, y + 2, 10 + bw, y + 9), outline=BOX)
                d.rectangle((11, y + 3, 11 + int((bw - 2) * frac), y + 8), fill=ACCENT)
                d.text((16 + bw, y), "%3d%% %s" % (frac * 100, label), font=self.small, fill=FG)
                y += 13
                continue
            text, style = (ln if isinstance(ln, tuple) else (ln, None))
            parts = [text] if style == "cmd" else (textwrap.wrap(text, maxc) or [""])
            for part in parts:
                d.text((6, y), part, font=self.small, fill=styles.get(style, FG))
                y += 12
        if q:
            img.paste(q, (W - q.size[0] - 6, 22))
        if isinstance(foot, list):            # segments: highlighted ones in orange
            x = 6
            for text, hi in foot:
                d.text((x, H - 12), text, font=self.small, fill=ACCENT if hi else DIM)
                x += self.small.getlength(text)
        elif foot:
            d.text((6, H - 12), foot, font=self.small, fill=DIM)
        self.fb.write(img)
        self.dirty = False

    def draw(self):
        if self.hidden:
            return
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        if self.setup:
            return self.draw_setup(img, d)
        if self.modescr is not None:
            return self.draw_mode(img, d)
        self.canvas = img
        self.draw_status(d)
        # conversation
        lines = self.all_lines()
        n = self.view_lines()
        if self.scroll and len(lines) > self.seen_len:
            self.scroll += len(lines) - self.seen_len   # keep what you're reading where it is
            self.new_below = True
        self.seen_len = len(lines)
        if not self.scroll:
            self.new_below = False
        maxscroll = max(0, len(lines) - n)
        self.scroll = min(self.scroll, maxscroll)
        end = len(lines) - self.scroll
        view = lines[max(0, end - n):end]
        colors = {"user": USER, "assistant": FG, "tool": TOOL, "result": RESULT, "code": CODE,
                  "ask": ASK, "err": ERR, "info": DIM, "gap": FG, "queued": DIM}
        y = 13
        bx0, bx1 = 3, W - 9                       # box edges (clear of the scroll bar)
        # bubbles style: paint your message bubbles first, text goes on top
        start = None
        for row, (kind, text, pos) in enumerate(view):
            ry = 13 + row * self.lh
            if isinstance(pos, tuple) and pos[0] == "bubble":
                start = ry if start is None else start
                if pos[2] or row == len(view) - 1:
                    x0 = bx1 - int(self.font.getlength("M" * pos[1])) - 10
                    d.rounded_rectangle((x0, start - 1, bx1, ry + self.lh - 1), radius=5, fill=BUBBLE)
                    start = None
            else:
                start = None
        drawn_imgs = set()
        for kind, text, pos in view:
            if isinstance(pos, tuple) and pos[0] == "img":    # a picture spanning several rows
                if text not in drawn_imgs:
                    drawn_imgs.add(text)
                    self.paste_image(img, text, y - pos[1] * self.lh)
                y += self.lh
                continue
            if isinstance(pos, tuple):            # a line of your message bubble
                x0 = bx1 - int(self.font.getlength("M" * pos[1])) - 10
                self.text(d, (x0 + 5, y), text, self.font, BUBBLE_FG)
                y += self.lh
                continue
            if isinstance(text, list):            # claude's markdown, styled
                x = 3
                for t, st in text:
                    f = self.bold if st in ("b", "h") else self.font
                    col = {"b": BRIGHT, "h": ACCENT, "code": CODE, "dot": ACCENT}.get(st, FG)
                    x = self.text(d, (x, y), t, f, col)
                y += self.lh
                continue
            if kind in ("table", "tableh"):
                col = ACCENT if kind == "tableh" else (DIM if set(text) <= set("─┼ ") else FG)
                self.text(d, (3, y), text, self.bold if kind == "tableh" else self.font, col)
                y += self.lh
                continue
            if pos:
                edge = (110, 60, 60) if kind == "err" else BOX
                mid = y + self.lh // 2 - 1
                cell0, cell1 = y - 1, y + self.lh - 1
                if pos == "top":
                    d.line((bx0, mid, bx1, mid), fill=edge)
                    d.line((bx0, mid, bx0, cell1), fill=edge)
                    d.line((bx1, mid, bx1, cell1), fill=edge)
                    if text:
                        lab = " " + text[:self.cols - 6] + " "
                        lw = self.font.getlength(lab)
                        d.rectangle((bx0 + 6, y, bx0 + 6 + lw, y + self.lh - 3), fill=BG)
                        self.text(d, (bx0 + 6, y), lab, self.font,
                                  DIM if kind == "code" else (ACCENT if kind in ("diff", "todo") else TOOL))
                elif pos == "bot":
                    d.line((bx0, cell0, bx0, mid), fill=edge)
                    d.line((bx1, cell0, bx1, mid), fill=edge)
                    d.line((bx0, mid, bx1, mid), fill=edge)
                else:
                    d.line((bx0, cell0, bx0, cell1), fill=edge)
                    d.line((bx1, cell0, bx1, cell1), fill=edge)
                    col = colors.get(kind, FG)
                    if kind == "diff":
                        col = OK if text[:1] == "+" else (ERR if text[:1] == "-" else (DIM if text[:2] == "@@" else RESULT))
                    elif kind == "todo":
                        col = DIM if text[:1] == "✓" else (ACCENT if text[:1] == "▶" else FG)
                    self.text(d, (bx0 + 5, y), text, self.font, col)
            else:
                self.text(d, (3, y), text, self.font, colors.get(kind, FG))
            y += self.lh
        # scroll bar
        track_top, track_bot = 13, H - (len(self.input_lines()) * self.lh + 7)
        if len(lines) > n:
            th = track_bot - track_top
            thumb = max(6, int(th * n / len(lines)))
            first = len(lines) - n - self.scroll
            ty = track_top + int((th - thumb) * first / max(1, len(lines) - n))
            d.rectangle((W - 5, track_top, W - 2, track_bot), fill=BAR)
            d.rectangle((W - 5, ty, W - 2, ty + thumb), fill=ACCENT if self.scroll else DIM)
        # input box
        rows = self.input_lines()
        top = H - len(rows) * self.lh - 4
        d.rectangle((0, top - 1, W, H), fill=BAR)
        col = ASK if self.pending else (DIM if self.busy else FG)
        for i, r in enumerate(rows):
            prefix = "› " if i == 0 else "  "
            if self.pending:
                r, prefix = "y / n / a ?" + ("   j = jump to bottom" if self.scroll else ""), "› "
            self.text(d, (3, top + 1 + i * self.lh), prefix + r, self.font, col)
            if self.pending:
                break
            if not self.input and self.ask and self.ask["typing"]:
                hint = "type your answer, enter = send, esc = back"
                d.text((W - 8 - self.small.getlength(hint), top + 2), hint, font=self.small, fill=ASK)
            elif not self.input and not self.busy:
                hint = "F1 help  F2 settings  F3 chats"
                if self.cfg["enter"] == "new line":
                    hint = "ctrl+enter send  F1 help  F2 set"
                d.text((W - 8 - self.small.getlength(hint), top + 2), hint, font=self.small, fill=DIM)
        if self.ask and not self.ask["typing"] and self.menu is None and self.hist is None:
            self.draw_ask(d)
        if self.pending and self.scroll and self.menu is None and self.hist is None:
            self.draw_pending(d)
        elif self.new_below and self.scroll and self.menu is None and self.hist is None:
            pill = " ↓ new "
            pw = self.small.getlength(pill)
            py = H - (len(self.input_lines()) * self.lh + 18)
            d.rounded_rectangle((W - 14 - pw, py, W - 10, py + 11), radius=5, fill=ACCENT)
            d.text((W - 12 - pw, py), pill, font=self.small, fill=BG)
        if self.menu is not None:
            self.draw_menu(d)
        elif self.hist is not None:
            self.draw_history(d)
        elif self.overlay:
            self.draw_overlay(d)
        if self.web_splash:
            self.draw_web_splash(img, d)
        if self.sudo_req:
            self.draw_sudo(d)
        self.fb.write(img)
        self.dirty = False

    # -- main loop --
    def run(self):
        while True:
            if self.setup:
                r, _, _ = select.select([self.kb.fd], [], [], 0.2)
                if self.kb.fd in r:
                    for k in self.kb.read():
                        self.key(k)
                        if not self.setup:
                            break
                if self.setup and self.setup.poll():
                    self.dirty = True
                if self.setup and self.setup.finished:
                    back = getattr(self, "setup_return", False)
                    first = self.setup.did_login
                    remote_ok = self.setup.remote_ok
                    self.setup.close()
                    self.setup = None
                    self.input = ""
                    if self.menu_mode:
                        if self.pc_after_setup:          # came from "on my pc (set up)"
                            self.pc_after_setup = False
                            if remote_ok and setup.remote_cfg():
                                self.choose("pc")
                        if self.modescr is None:
                            self.open_mode(first=first)
                    elif not back or self.claude is None:
                        self.splash("opening chat...")
                        self.begin_chat()
                    self.dirty = True
                self.draw()
                continue
            if self.modescr is not None:
                r, _, _ = select.select([self.kb.fd], [], [], 1.0)
                if self.kb.fd in r:
                    for k in self.kb.read():
                        self.key(k)
                self.draw()
                continue
            if self.hidden:
                self.hidden_tick()
                continue
            if CAN_HIDE and not self.ctl_alive():
                if self.sudo_req:
                    self.sudo_answer(None)   # nobody's there to type it
                self.hide()                  # the launcher script went away: give the screen back
                if not self.hidden:
                    time.sleep(0.5)          # (can't hide right now: don't spin)
                continue
            fds = [self.kb.fd] + self.sudo_fds() + ([self.web.rfd] if self.web else [])
            if self.claude.alive():
                fds.append(self.claude.fileno())
            r, _, _ = select.select(fds, [], [], 0.25)
            self.sudo_poll(r)
            self.web_tick(r)
            if self.kb.fd in r:
                keys = self.kb.read()
                if self.dimmed and keys:
                    self.wake_screen()               # the key that wakes the screen does nothing else
                    keys = []
                for k in keys:
                    self.key(k)
                self.last_key = time.time()
            self.dim_tick()
            # autosave the half-typed message a second after you stop typing
            if self.input != self.draft_saved and time.time() - self.last_key > 1.0:
                self.save_draft()
            if self.claude.alive() and self.claude.fileno() in r:
                msgs = self.claude.read()
                if msgs is None:
                    self.busy = False
                    self.pending = self.ask = self.turn = None
                    if REMOTE:
                        self.add("err", "lost the pc (is it on? ssh ok?). enter = retry.")
                    else:
                        self.add("err", "claude stopped. enter = restart.")
                else:
                    for m in msgs:
                        self.handle(m)
            self.net_tick()
            if self.busy or time.time() - self.last_clock > 20:
                self.dirty = True
                self.last_clock = time.time()
            if self.dirty:
                self.draw()

    # -- sudo password popup: sudo -A runs clawputer-askpass, which connects here --
    def open_askpass(self):
        try:
            if os.path.exists(ASKPASS_SOCK):
                os.unlink(ASKPASS_SOCK)
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            old = os.umask(0o077)
            try:
                s.bind(ASKPASS_SOCK)
            finally:
                os.umask(old)
            s.listen(2)
            s.setblocking(False)
            self.sudo_sock = s
            self.sudo_ino = os.stat(ASKPASS_SOCK).st_ino
            self.sudo_checked = time.time()
            self.sudo_clear()        # start clean: a password typed elsewhere mustn't let claude in
        except OSError as e:
            log("askpass socket", e)

    def sudo_fds(self):
        fds = [self.sudo_sock.fileno()] if self.sudo_sock else []
        if self.sudo_req:
            fds.append(self.sudo_req["conn"].fileno())
        return fds

    @staticmethod
    def sudo_parent(pid):
        """-> (sudo pid, its command line) if pid's parent really is a running sudo, else None"""
        try:
            st = dict(l.split(":", 1) for l in open("/proc/%d/status" % pid).read().splitlines() if ":" in l)
            ppid = int(st["PPid"])
            pst = dict(l.split(":", 1) for l in open("/proc/%d/status" % ppid).read().splitlines() if ":" in l)
            # only the setuid sudo binary can be named sudo AND run with effective uid 0
            if pst["Name"].strip() != "sudo" or pst["Uid"].split()[1] != "0":
                return None
            args = open("/proc/%d/cmdline" % ppid, "rb").read().decode(errors="replace").split("\0")
            return ppid, " ".join(a for a in args if a)
        except (OSError, KeyError, ValueError, IndexError):
            return None

    @staticmethod
    def sudo_authed(pid):
        """sudo has a child that isn't our askpass helper -> the password was accepted"""
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                if int(open("/proc/%s/stat" % d).read().rsplit(")", 1)[1].split()[1]) != pid:
                    continue
                if b"clawputer-askpass" not in open("/proc/%s/cmdline" % d, "rb").read():
                    return True
            except (OSError, ValueError, IndexError):
                continue
        return False

    def sudo_poll(self, r):
        if self.sudo_sock and time.time() - self.sudo_checked > 5:
            # someone replaced / removed our socket file (another clawputer that has since
            # closed?): take it back, or sudo -A can't reach us
            self.sudo_checked = time.time()
            try:
                stolen = os.stat(ASKPASS_SOCK).st_ino != self.sudo_ino
            except OSError:
                stolen = True
            if stolen and not self.sudo_req:
                log("askpass socket was replaced; reopening")
                self.sudo_sock.close()
                self.sudo_sock = None
                self.open_askpass()
                return
        # forget the password the moment sudo has checked it (not when the command ends): sudo's
        # timestamp is device-wide, so a long command would otherwise let other sudo -n calls in
        if self.sudo_forget and (not self.proc_running(self.sudo_forget) or self.sudo_authed(self.sudo_forget)):
            self.sudo_forget = None
            self.sudo_clear()
        if self.sudo_req and self.sudo_req["conn"].fileno() in r:
            try:
                gone = not self.sudo_req["conn"].recv(256)    # askpass sends its prompt, then waits
            except OSError:
                gone = True
            if gone:                                         # sudo gave up / was killed
                self.sudo_answer(None)
        if not self.sudo_sock or self.sudo_sock.fileno() not in r:
            return
        try:
            conn, _ = self.sudo_sock.accept()
        except OSError:
            return
        conn.setblocking(True)
        conn.settimeout(5)
        try:
            conn.recv(256)          # askpass's prompt line. read it now: closing with unread
        except OSError:             # data would reset the connection and lose our answer
            pass
        try:
            pid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
        except OSError:
            pid = 0
        who = self.sudo_parent(pid) if pid else None
        nobody = self.hidden and not (self.web and self.web.active())
        if who is None or nobody or self.sudo_req:
            log("askpass: refused (%s)" % ("not sudo" if who is None else "busy/hidden"))
            try:
                conn.sendall(b"0")
            except OSError:
                pass
            conn.close()
            return
        spid, cmd = who
        retry = self.sudo_last[0] == spid and time.time() - self.sudo_last[1] < 120
        self.sudo_req = {"conn": conn, "cmd": cmd, "pw": "", "retry": retry, "pid": spid,
                         "remember": self.sudo_remember}
        self.dirty = True

    def sudo_key(self, k):
        q = self.sudo_req
        if k == "enter":
            self.sudo_answer(q["pw"])
        elif k == "esc":
            self.sudo_answer(None)
        elif k == "tab":
            q["remember"] = not q["remember"]
        elif k == "backspace":
            q["pw"] = q["pw"][:-1]
        elif len(k) == 1:
            q["pw"] += k

    def sudo_answer(self, pw):
        q, self.sudo_req = self.sudo_req, None
        try:
            q["conn"].sendall(b"0" if pw is None else b"1" + pw.encode())
        except OSError:
            pass
        q["conn"].close()
        if pw is not None:
            self.sudo_used = True
            self.sudo_remember = q["remember"]
            self.sudo_forget = None if q["remember"] else q["pid"]
        q["pw"] = pw = ""
        self.sudo_last = (q["pid"], time.time())
        self.dirty = True

    @staticmethod
    def proc_running(pid):
        try:
            return "\nState:\tZ" not in open("/proc/%d/status" % pid).read()
        except OSError:
            return False

    @staticmethod
    def sudo_clear():
        """forget sudo's cached password (timestamp_type=global on Pi OS: it's device-wide)"""
        try:
            subprocess.run(["sudo", "-k"], stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            log("sudo -k", e)

    def draw_sudo(self, d):
        q = self.sudo_req
        shown = re.sub(r"[\x00-\x1f\x7f]", lambda c: "⏎" if c.group() == "\n" else "�", q["cmd"])
        cmd = textwrap.wrap(shown, 50)
        if len(cmd) > 5:                     # the start AND the end: a long command can't hide its tail
            cmd = cmd[:2] + ["  … %d more lines …" % (len(cmd) - 4)] + cmd[-2:]
        title = "wrong password? try again" if q["retry"] else "claude wants to run as root:"
        bh = 30 + len(cmd) * 11 + 40
        top = max(2, (H - bh) // 2)
        d.rectangle((6, top, W - 6, top + bh), fill=(25, 25, 35), outline=ERR if q["retry"] else ASK)
        d.text((14, top + 4), title, font=self.small, fill=ERR if q["retry"] else ASK)
        for i, ln in enumerate(cmd):
            d.text((14, top + 18 + i * 11), ln, font=self.small, fill=FG)
        y = top + 22 + len(cmd) * 11
        d.text((14, y), "sudo password: " + "*" * min(len(q["pw"]), 30) + "_", font=self.small, fill=FG)
        d.text((14, y + 12), "remember for this chat: " + ("yes" if q["remember"] else "no"),
               font=self.small, fill=ASK if q["remember"] else DIM)
        hint = "enter run   tab remember   esc refuse"
        d.text(((W - self.small.getlength(hint)) // 2, top + bh - 13), hint, font=self.small, fill=DIM)

    # -- web ui (clawputer_web.py): the chat in a phone / pc browser --
    def start_web(self):
        if self.web:
            self.web_splash = self.web.urls()
            return
        try:
            import clawputer_web
            self.web = clawputer_web.WebUI(CONF, log)
        except OSError as e:
            log("web ui", e)
            self.add("err", "couldn't start the web ui: %s" % e)
            return
        self.web_sig = None
        self.web_splash = self.web.urls()
        self.add("info", "  web ui on: " + (", ".join(u for _, u in self.web_splash) or "no network?"))
        self.web_publish()

    def stop_web(self):
        w, self.web, self.web_splash = self.web, None, None
        if w:
            w.close()

    def web_tick(self, r):
        if not self.web:
            return
        if self.web.rfd in r:
            for a in self.web.actions():
                try:
                    self.web_act(a)
                except Exception as e:           # a bad request mustn't take the chat down
                    log("web action", a.get("a"), repr(e))
                self.dirty = True
        self.web_publish()

    def web_act(self, a):
        kind = a.get("a")
        if kind == "send":
            text = str(a.get("text", "")).strip()
            if text:
                saved, self.input = self.input, text
                self.submit()
                if self.input == "":
                    self.input = saved           # keep what's half-typed on the cardputer
        elif kind == "stop":
            if self.busy:
                self.claude.interrupt()
                self.add("info", "  stopping...")
        elif kind == "perm":
            if self.pending and self.pending["id"] == a.get("id") and a.get("k") in ("y", "n", "a"):
                self.answer_pending(a["k"])
        elif kind == "ask_answer":
            if self.ask and self.ask["id"] == a.get("id") and self.ask["qi"] == a.get("qi"):
                self.ask["typing"] = False
                self.ask_answer(str(a.get("answer", ""))[:2000])
        elif kind == "ask_skip":
            if self.ask and self.ask["id"] == a.get("id"):
                self.claude.answer(self.ask["id"], False, self.ask["input"])
                self.add("info", "  ✗ skipped the question")
                self.ask = None
        elif kind == "sudo":
            q = self.sudo_req
            if q and q["pid"] == a.get("pid"):
                if a.get("pw") is None:
                    self.sudo_answer(None)
                else:
                    q["remember"] = bool(a.get("remember"))
                    self.sudo_answer(str(a["pw"]))
        elif kind in ("new", "open") and self.busy:
            self.add("info", "  claude is still working: stop it first, then switch chats.")
        elif kind == "new":
            self.hist = None
            self.new_chat()
        elif kind == "chats":
            self.web_chats = [{"id": c["id"], "title": c["title"][:80], "ago": ago(c["mtime"])} for c in list_chats()]
        elif kind == "open":
            for c in list_chats():
                if c["id"] == a.get("id"):
                    self.hist = None
                    if c["id"] != self.session_id or not self.claude.alive():
                        self.open_chat(c)
                    break
        elif kind == "set":
            st = [x for x in SETTINGS if x[0] == a.get("key") and x[2]]
            if st and a.get("value") in st[0][2] and self.menu is None:
                self.menu_before = dict(self.cfg)
                self.cfg[st[0][0]] = a["value"]
                if st[0][0] == "font":
                    self.set_font(int(self.cfg["font"]))
                self.close_menu()
        elif kind == "key":
            k = a.get("k")
            if isinstance(k, str) and k and k != "ctrl+q":
                self.key(k)

    def web_publish(self):
        w = self.web
        if not w:
            return
        w.screen = self.fb.prev
        q, p, ask = self.sudo_req, self.pending, self.ask
        st = {
            "status": self.status, "busy": self.busy, "batt": battery_clock(), "hidden": self.hidden,
            "session": self.session_id, "queue": len(self.queue), "cfg": self.cfg,
            "settings": [[k, label, ch] for k, label, ch, _ in SETTINGS if ch],
            "pending": {"id": p["id"], "name": p["name"], "desc": short_tool(p["name"], p["input"])} if p else None,
            "sudo": {"pid": q["pid"], "cmd": q["cmd"], "retry": q["retry"], "remember": q["remember"]} if q else None,
            "chats": self.web_chats, "ask": None,
            "turn": None,
        }
        if self.busy and self.turn:
            tu = self.turn
            st["turn"] = {"phase": "waiting for you" if (p or ask or q) else tu["phase"],
                          "tokens": self.turn_tokens()}
        if ask:
            qq = ask["qs"][ask["qi"]]
            st["ask"] = {"id": ask["id"], "qi": ask["qi"], "n": len(ask["qs"]), "header": qq.get("header", ""),
                         "question": qq.get("question", ""), "multi": bool(qq.get("multiSelect")),
                         "options": [{"label": o.get("label", "?"), "description": o.get("description", "")}
                                     for o in qq.get("options", [])]}
        sig = (self.entries_gen, len(self.entries), json.dumps(st, sort_keys=True, default=str))
        if sig != self.web_sig:
            self.web_sig = sig
            if st["turn"]:
                st["turn"]["secs"] = time.time() - (self.t_start or time.time())   # browser counts on
            w.publish(self.entries, self.entries_gen, st)

    def draw_web_splash(self, img, d):
        import qrcode
        urls = self.web_splash
        key = self.web.key if self.web else ""
        d.rectangle((0, 0, W, H), fill=BG)
        d.text((8, 4), "web ui", font=self.font, fill=ACCENT)
        if not urls:
            d.text((8, 30), "no network: connect to wi-fi first", font=self.small, fill=ERR)
            d.text((8, H - 13), "any key to close", font=self.small, fill=DIM)
            return
        qr = qrcode.QRCode(border=2, box_size=1)
        qr.add_data(urls[0][1] + "?k=" + key)
        qr.make(fit=True)
        q = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        scale = max(1, 140 // q.size[0])
        q = q.resize((q.size[0] * scale, q.size[1] * scale), Image.NEAREST)
        img.paste(q, (W - q.size[0] - 8, (H - q.size[1]) // 2 + 4))
        x, y = 8, 24
        d.text((x, y), "scan, or open on your phone:", font=self.small, fill=DIM)
        y += 14
        for ifc, u in urls[:3]:
            label = "tailscale" if ifc.startswith("tailscale") else ("wi-fi" if ifc.startswith("wl") else ifc)
            d.text((x, y), label, font=self.small, fill=DIM)
            d.text((x, y + 11), u.replace("http://", "").rstrip("/"), font=self.small, fill=FG)
            y += 26
        d.text((x, y + 2), "key: " + key, font=self.font, fill=ASK)
        d.text((x, H - 13), "any key to close", font=self.small, fill=DIM)

    # -- small blocking dialogs (things that used to be typed in a terminal) --
    def dialog(self, title, lines, password=False, hint=None, timeout=None, err=False):
        """-> typed text (password=True) or the key pressed; None on esc / timeout"""
        pw, t0 = "", time.time()
        self.kb.read()                                   # drop keys pressed before it showed
        while True:
            img = Image.new("RGB", (W, H), BG)
            d = ImageDraw.Draw(img)
            body = [ln for line in lines for ln in (textwrap.wrap(line, 50) or [""])]
            bh = 26 + len(body) * 11 + (14 if password else 0) + 16
            top = max(2, (H - bh) // 2)
            d.rectangle((6, top, W - 6, top + bh), fill=(25, 25, 35), outline=ERR if err else ASK)
            d.text((14, top + 4), title, font=self.small, fill=ERR if err else ASK)
            for i, ln in enumerate(body):
                d.text((14, top + 18 + i * 11), ln, font=self.small, fill=FG)
            if password:
                d.text((14, top + 20 + len(body) * 11), "password: " + "*" * min(len(pw), 30) + "_",
                       font=self.small, fill=FG)
            hint = hint or ("enter ok   esc cancel" if password else "")
            d.text(((W - self.small.getlength(hint)) // 2, top + bh - 13), hint, font=self.small, fill=DIM)
            self.fb.write(img)
            r, _, _ = select.select([self.kb.fd], [], [], 1.0)
            if timeout and time.time() - t0 > timeout:
                return None
            for k in (self.kb.read() if r else []):
                if k == "esc":
                    return None
                if not password:
                    return k
                if k == "enter":
                    return pw
                if k == "backspace":
                    pw = pw[:-1]
                elif len(k) == 1:
                    pw += k

    def setup_boost(self):
        """install (or update) the RAM boost helpers (needs the password once). -> True when done"""
        err = False
        upd = getattr(self, "boost_update", False)
        why = ("clawputer was updated: the ram boost helpers need updating too." if upd else
               "installs 2 small helpers so clawputer can pause background services while claude "
               "runs (undo any time).")
        for _ in range(3):
            pw = self.dialog(("update ram boost" if upd else "ram boost setup") if not err
                             else "wrong password? try again",
                             [why, "enter your password:"], password=True, err=err)
            if pw is None:
                return False
            self.splash("setting up the ram boost...")
            try:
                p = subprocess.run(["sudo", "-S", "-k", "-p", "", os.path.join(SHARE, "boost", "enable-boost.sh")],
                                   input=(pw + "\n").encode(), capture_output=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired) as e:
                log("boost setup", e)
                p = None
            pw = ""
            if p is not None and p.returncode == 0:
                return True
            if p is not None:
                log("boost setup", p.returncode, p.stderr.decode(errors="replace")[-300:])
            err = True
        self.dialog("ram boost not set up", ["3 wrong passwords (or the setup failed).",
                                             "pick 'just run it', or try again."], hint="any key", err=True)
        return False

    def set_keep_boost(self, keep):
        try:
            p = subprocess.run(["sudo", "-n", BOOTMODE, "keep", "on" if keep else "off"],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
            ok = p.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        if ok:
            self.add("info", "  max boost " + ("stays on across reboots." if keep else "goes back to normal on the next reboot."))
        else:
            self.add("info", "  couldn't change it yet: pick a ram boost option on the start screen once "
                             "(it updates the boost helpers, asks your password).")

    def ask_reboot_back(self):
        """quitting after a max-boost restart: offer to reboot back to normal"""
        self.reboot_after = False
        if REMOTE or not self.kb or gpu_mb() > 16 or not boost_state()[0] or os.path.exists(KEEP_FLAG):
            return
        k = self.dialog("reboot back to normal?",
                        ["gives the gpu memory back and turns the camera on again."],
                        hint="y reboot   any other key no", timeout=30)
        self.reboot_after = k in ("y", "Y")

    def boost(self, on):
        """RAM boost on/off: pause background services while claude runs here"""
        if on == self.boosted or REMOTE:
            return
        if not self.hidden:
            self.splash("freeing ram..." if on else "putting things back...")
        run = lambda *a: subprocess.run(list(a), stdin=subprocess.DEVNULL, capture_output=True, timeout=90)
        try:
            if on:
                open(BOOST_FLAG, "w").close()            # the cleanup hook undoes it if we crash
                # (audio / pipewire stays up: the launcher keeps a pulse connection, and restarting
                # pipewire under it hung the launcher when it took the screen back)
                run("systemctl", "--user", "stop", "xdg-desktop-portal", "xdg-desktop-portal-gtk",
                    "xdg-desktop-portal-wlr", "xdg-document-portal", "xdg-permission-store", "mpris-proxy",
                    "filter-chain", "gvfs-*")
                run("sudo", "-n", BOOST, "on")
            else:
                run("sudo", "-n", BOOST, "off")
                try:
                    os.unlink(BOOST_FLAG)
                except OSError:
                    pass
        except (OSError, subprocess.TimeoutExpired) as e:
            log("boost", on, e)
        self.boosted = on

    # -- hide / show (esc on an empty box; the launcher script brings us back with SIGUSR1) --
    def ctl_alive(self):
        try:
            pid = int(read_file(CTL_FILE))
            return "clawputer" in open("/proc/%d/cmdline" % pid).read()
        except (ValueError, OSError):
            return False

    def set_state(self, st):
        try:
            with open(STATE_FILE + ".tmp", "w") as f:
                f.write(st)
            os.replace(STATE_FILE + ".tmp", STATE_FILE)
        except OSError as e:
            log("state", e)

    def hide(self):
        if self.sudo_req or self.menu_mode:
            return                           # answer the password popup first
        self.save_draft()
        self.wake_screen()
        self.kb.close()                      # let go of the keyboard so the launcher gets it
        self.kb = None
        self.hidden = True
        self.hidden_since = time.time()
        self.want_show = False
        self.set_state("hidden")             # the launcher script sees this and exits: launcher's back

    def show(self):
        self.hidden = False
        self.kb = Keyboard()
        self.fb.prev = None                  # the launcher drew over us: repaint everything
        if self.unseen:
            self.unseen = False
            self.scroll = 0                  # straight to what happened while you were away
        self.dirty = True
        self.last_key = time.time()
        if self.parked:                      # claude was stopped to save RAM: pick the chat back up
            self.parked = False
            self.splash("waking claude...")
            self.start_claude(resume=self.session_id, quiet=True)
            self.status = "resuming chat..."
            if self.queue:
                self.send_user(self.queue.pop(0))
        self.set_state("shown")

    def park(self, why=None):
        """minimised: stop claude (gives its ~200MB back) and the ram boost; resumes on wake"""
        if self.claude and self.claude.alive():
            if self.busy:
                self.claude.interrupt()
            self.claude.close()
        self.parked = True
        self.busy = False
        self.turn = None
        if self.pending or self.ask:
            self.pending = self.ask = None
            self.add("info", "  (the question was dropped, ask claude again)")
        if why:
            self.add("info", why)
        self.boost(False)
        log("parked", why or "idle")

    def hidden_tick(self):
        # keep reading claude so its replies (and our chat) stay up to date, but draw nothing
        live = self.claude and self.claude.alive() and not self.parked
        fds = [self.claude.fileno()] if live else []
        r, _, _ = select.select(fds + self.sudo_fds() + ([self.web.rfd] if self.web else []), [], [], 0.5)
        self.sudo_poll(r)                    # (says no while hidden, unless a browser is open)
        self.web_tick(r)
        if fds and fds[0] in r:
            msgs = self.claude.read()
            if msgs is None:
                self.busy = False
                self.pending = self.ask = self.turn = None
                self.add("err", "claude stopped. enter = restart.")
            else:
                for m in msgs:
                    self.handle(m)
        if live and not REMOTE:
            web = self.web and self.web.active()
            waiting = self.pending or self.ask or self.sudo_req
            if not web and not self.busy and not waiting and not self.queue:
                self.park()                  # idle: no reason to keep claude in memory
            elif mem_free_mb() < HIDDEN_MIN_MB:
                self.park("── claude was stopped to save memory while hidden. say 'carry on' to continue. ──")
            elif waiting and not web and time.time() - self.hidden_since > 600:
                self.park("── claude waited 10 min for you while hidden, so it was stopped. ──")
        elif self.parked and self.boosted:
            self.boost(False)
        self.net_tick()
        if self.want_show:
            self.show()

    def close(self):
        try:
            if self.sudo_req:
                self.sudo_answer(None)
            if self.sudo_used:
                self.sudo_clear()
            if self.web:
                self.stop_web()
            if self.sudo_sock:
                self.sudo_sock.close()
                try:
                    os.unlink(ASKPASS_SOCK)
                except OSError:
                    pass
            if self.setup:
                self.setup.close()
            self.save_draft()
            if self.claude:
                self.claude.close()
            self.boost(False)
            self.wake_screen()
        finally:
            try:
                if self.kb:
                    self.kb.close()
            finally:
                self.fb.close(blank=not self.hidden)   # hidden: the launcher owns the screen
                if CAN_HIDE:
                    self.set_state("closed")


BOOST = "/usr/local/sbin/clawputer-boost"
BOOTMODE = "/usr/local/sbin/clawputer-bootmode"


def short_model(name):
    """'claude-opus-5-5[1m]' -> 'opus 5.5'"""
    name = re.sub(r"\[.*?\]", "", name).replace("claude-", "")
    parts = [p for p in name.split("-") if p and not (p.isdigit() and len(p) == 8)]   # drop dates
    if not parts:
        return ""
    fam, nums = parts[0], [p for p in parts[1:] if p.isdigit()]
    return fam + (" " + ".".join(nums) if nums else "")


def play_chime(kind="done"):
    """a soft sound, made once into a little wav, played without blocking.
    done = two notes up (claude finished), prompt = a quick double tap (claude needs you)"""
    path = CHIME_FILE if kind == "done" else os.path.join(CONF, "prompt.wav")
    notes = ((880, 0.11), (1320, 0.18)) if kind == "done" else ((1175, 0.07), (0, 0.05), (1175, 0.09))
    try:
        if not os.path.exists(path):
            import math
            import wave
            rate, frames = 22050, bytearray()
            for freq, dur in notes:
                n = int(rate * dur)
                for i in range(n):
                    env = min(1.0, i / 200) * (1 - i / n) ** 2
                    frames += struct.pack("<h", int(9000 * env * math.sin(2 * math.pi * freq * i / rate)))
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(bytes(frames))
        for cmd in (["pw-play", path], ["paplay", path], ["aplay", "-q", path]):
            try:
                subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                return
            except OSError:
                continue
    except OSError as e:
        log("chime", e)


SPLASH_FILE = os.path.join(CONF, "splash.raw")


def backlight_path():
    return (glob.glob("/sys/class/backlight/*/brightness") or [None])[0]


def backlight_get():
    try:
        return int(read_file(backlight_path()))
    except (TypeError, ValueError):
        return 0


def backlight_set(v):
    p = backlight_path()
    if p:
        try:
            with open(p, "w") as f:
                f.write(str(int(v)))
        except OSError as e:
            log("backlight", e)


def todo_text(todos):
    mark = {"completed": "✓", "in_progress": "▶"}
    return "\n".join("%s %s" % (mark.get(t.get("status"), "☐"),
                                 t.get("activeForm") if t.get("status") == "in_progress" and t.get("activeForm")
                                 else t.get("content", "")) for t in todos)


def todo_label(todos, text):
    n = text.count("\n") + 1
    done = text.count("✓ ")
    return "to-do %d/%d" % (done, n)


def edit_diff(name, inp, cwd, limit=14):
    """old -> new text of an Edit / MultiEdit / Write as a short unified diff"""
    import difflib
    pairs = []
    if name == "Edit":
        pairs = [(inp.get("old_string", ""), inp.get("new_string", ""))]
    elif name == "MultiEdit":
        pairs = [(e.get("old_string", ""), e.get("new_string", "")) for e in inp.get("edits", []) if isinstance(e, dict)]
    elif name == "Write":
        old = ""
        path = inp.get("file_path", "")
        if cwd is not None and path:
            try:
                with open(path if os.path.isabs(path) else os.path.join(cwd, path), errors="replace") as f:
                    old = f.read(200000)
            except OSError:
                old = ""
        pairs = [(old, inp.get("content", ""))]
    out = []
    for a, b in pairs:
        if not isinstance(a, str) or not isinstance(b, str):
            continue
        for ln in difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=1):
            if ln.startswith(("---", "+++")):
                continue
            out.append(ln)
    if not out:
        return ""
    more = len(out) - limit
    return "\n".join(out[:limit] + (["… +%d more lines" % more] if more > 0 else []))


def table_lines(rows, width):
    """markdown table rows -> grid lines: [("tableh"/"table", text, None)]"""
    cells = []
    for r in rows:
        c = [x.strip().replace("**", "").replace("`", "") for x in r.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", x) for x in c if x):
            continue                                     # the |---|---| line
        cells.append(c)
    if not cells:
        return []
    n = max(len(c) for c in cells)
    cells = [c + [""] * (n - len(c)) for c in cells]
    w = [max(len(c[i]) for c in cells) for i in range(n)]
    while sum(w) + 3 * (n - 1) > width and max(w) > 3:
        w[w.index(max(w))] -= 1
    fit = lambda s, k: s if len(s) <= k else s[:max(1, k - 1)] + "…"
    out = []
    for i, c in enumerate(cells):
        out.append(("tableh" if i == 0 else "table", " │ ".join(fit(c[j], w[j]).ljust(w[j]) for j in range(n)), None))
        if i == 0 and len(cells) > 1:
            out.append(("table", "─┼─".join("─" * k for k in w), None))
    return out


def claude_email():
    """the signed-in account's email, read from claude's own config (no extra claude process)"""
    try:
        with open(os.path.expanduser("~/.claude.json")) as f:
            return ((json.load(f).get("oauthAccount") or {}).get("emailAddress")) or ""
    except (OSError, ValueError, AttributeError):
        return ""


def boost_helpers_outdated():
    """the installed root helpers differ from this version's (an upgrade doesn't update them)"""
    try:
        for name in ("clawputer-boost", "clawputer-bootmode"):
            if not os.path.exists("/usr/local/sbin/" + name):
                return False                 # not set up at all: that's a different question
            with open("/usr/local/sbin/" + name, "rb") as a, open(os.path.join(SHARE, "boost", name), "rb") as b:
                if a.read() != b.read():
                    return True
    except OSError:
        pass
    return False


def battery():
    """-> (percent or None, charging?)"""
    for ps in ["/sys/class/power_supply/bq27220-0"] + glob.glob("/sys/class/power_supply/*"):
        cap = read_file(ps + "/capacity")
        if cap.isdigit():
            return int(cap), read_file(ps + "/status") == "Charging"
    return None, False


def net_kind():
    """-> 'tether' (phone bluetooth), 'wifi' or None"""
    if os.path.exists("/sys/class/net/bnep0"):
        return "tether"
    if read_file("/sys/class/net/wlan0/operstate") == "up":
        return "wifi"
    return None


def read_file(path):
    try:
        return open(path).read().strip()
    except OSError:
        return ""


def gpu_mb():
    try:
        out = subprocess.run(["vcgencmd", "get_mem", "gpu"], capture_output=True, timeout=5).stdout.decode()
        return int(re.sub(r"\D", "", out) or 128)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 128


def boost_state():
    """-> (ram boost set up for this user?, gpu memory MB)"""
    inst = False
    if os.path.exists(BOOST):
        try:
            inst = subprocess.run(["sudo", "-n", "-l", BOOST, "on"], capture_output=True, timeout=5).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    return inst, gpu_mb()


def mem_free_mb():
    for line in read_file("/proc/meminfo").splitlines():
        if line.startswith("MemAvailable"):
            return int(line.split()[1]) // 1024
    return 0


def battery_clock():
    try:
        ps = glob.glob("/sys/class/power_supply/*/capacity")[0].rsplit("/", 1)[0]
        cap = read_file(ps + "/capacity")
        chg = read_file(ps + "/status") == "Charging"
        return "%s%s%% %s" % ("+" if chg else "", cap, time.strftime("%H:%M"))
    except IndexError:
        return time.strftime("%H:%M")


def install_claude_md():
    """give Claude a short briefing about the device, in the chat working folder"""
    try:
        src = os.path.join(SHARE, "CLAUDE.md")
        data = open(src, "rb").read()
        if REMOTE:
            import base64
            md = open(os.path.join(SHARE, "CLAUDE.remote.md"), "rb").read()
            remote_py("init", base64.b64encode(md).decode())
            return
        os.makedirs(WORKDIR, exist_ok=True)
        dst = os.path.join(WORKDIR, "CLAUDE.md")
        if not os.path.exists(dst):
            open(dst, "wb").write(data)
    except OSError as e:
        log("claude.md", e)


def main():
    os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)
    def bye(*_):
        raise SystemExit
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGHUP, bye)
    app = App(sys.argv[1:])
    if CAN_HIDE:
        app.set_state("shown")

        def wake(*_):
            app.want_show = True
        signal.signal(signal.SIGUSR1, wake)
    try:
        app.run()
    except (SystemExit, KeyboardInterrupt):
        pass
    except Exception as e:
        log("crash", repr(e))
        import traceback
        log(traceback.format_exc())
        raise
    finally:
        app.close()
        if app.reboot_after:
            subprocess.run(["sudo", "-n", BOOTMODE, "exit"], stdin=subprocess.DEVNULL, capture_output=True)


if __name__ == "__main__":
    main()
