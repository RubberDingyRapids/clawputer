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
import subprocess
import sys
import textwrap
import time

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFont

import clawputer_setup as setup

W, H = 320, 170
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_SIZE = int(os.environ.get("CLAUDE_GUI_FONT", "10"))
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
    ("dblspace", "double space = '. '", ["on", "off"], False),
    ("enter", "enter key", ["send", "new line"], False),
    ("web", "web ui on open", ["off", "on"], False),
    ("@web", "web ui: start / stop", None, False),
    ("@remote", "remote pc setup", None, False),
    ("@about", "about / version", None, False),
    ("@signout", "sign out of claude", None, False),
]
DEFAULTS = {"model": "default", "effort": "default", "permissions": "ask", "font": "10", "tool_lines": "3", "start": "last chat",
            "dblspace": "on", "enter": "send", "web": "off"}


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
        self.last_key = 0
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
        self.status = "ready"
        if not resume and not quiet:
            self.add("info", "new chat. type a message. ctrl+q quits." +
                     (" esc on an empty box hides it, it keeps going." if CAN_HIDE else ""))

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
            model = (m.get("model") or "").replace("claude-", "")
            self.status = ("pc · " if REMOTE else "") + "ready · " + model
        elif t in ("assistant", "user"):
            self.ingest(m)
        elif t == "_unused_user":
            content = m.get("message", {}).get("content", [])
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        txt = result_text(c.get("content")).strip()
                        lines = txt.splitlines() or [""]
                        limit = len(lines) if self.cfg["tool_lines"] == "all" else int(self.cfg["tool_lines"])
                        shown = "\n".join(lines[:limit])
                        if len(lines) > limit:
                            shown += "\n… +%d lines" % (len(lines) - limit)
                        self.add("err" if c.get("is_error") else "result", shown if txt else "(no output)")
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
                    self.add("ask", "? allow " + short_tool(name, r.get("input")) +
                             "\n  y yes · n no · a always (" + name + ")")
        elif t == "result":
            self.busy = False
            self.turn = None
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
            if self.queue:
                self.lines_cache = None
                self.send_user(self.queue.pop(0))
        self.dirty = True

    def ingest(self, m, loading=False):
        t = m.get("type")
        if m.get("isSidechain") or m.get("isMeta"):
            return
        msg = m.get("message") or {}
        content = msg.get("content")
        if t == "assistant":
            for b in content or []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    self.add("assistant", b["text"].strip())
                elif b.get("type") == "tool_use":
                    self.add("tool", "▸ " + short_tool(b.get("name", "?"), b.get("input")))
        elif t == "user":
            if isinstance(content, str):
                if loading and content.strip() and not content.lstrip().startswith("<"):
                    self.add("user", content.strip())
                return
            for c in content or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_result":
                    txt = result_text(c.get("content")).strip()
                    lines = txt.splitlines() or [""]
                    limit = len(lines) if self.cfg["tool_lines"] == "all" else int(self.cfg["tool_lines"])
                    shown = "\n".join(lines[:limit])
                    if len(lines) > limit:
                        shown += "\n… +%d lines" % (len(lines) - limit)
                    self.add("err" if c.get("is_error") else "result", shown if txt else "(no output)")
                elif loading and c.get("type") == "text" and not c.get("text", "").lstrip().startswith("<"):
                    self.add("user", c.get("text", "").strip())

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
        try:
            if REMOTE:
                self.status = "loading chat..."
                self.draw()
                src = remote_py("read", chat["path"]).splitlines()
            else:
                src = open(chat["path"], encoding="utf-8", errors="replace")
            for line in src:
                if '"type":"user"' in line or '"type":"assistant"' in line or \
                        '"type": "user"' in line or '"type": "assistant"' in line:
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
        self.session_id = None
        self.cwd = WORKDIR
        self.start_claude()
        self.load_draft()

    def open_history(self):
        self.hist = list_chats()
        self.hist_sel = 0
        self.hist_confirm = False

    def hist_key(self, k):
        items = 1 + len(self.hist)      # row 0 = "+ new chat"
        if self.hist_confirm:
            if k in ("y", "Y"):
                chat = self.hist[self.hist_sel - 1]
                try:
                    if REMOTE:
                        remote_py("delete", chat["path"])
                    else:
                        os.remove(chat["path"])
                    _chat_cache.pop(chat["path"], None)
                except OSError as e:
                    log("delete failed", e)
                if chat["id"] == self.session_id:
                    self.new_chat()
                self.hist = list_chats()
                self.hist_sel = min(self.hist_sel, len(self.hist))
            self.hist_confirm = False
            return
        if k in ("f", "F", "up"):
            self.hist_sel = (self.hist_sel - 1) % items
        elif k in ("x", "X", "down"):
            self.hist_sel = (self.hist_sel + 1) % items
        elif k in ("n", "N"):
            self.hist = None
            self.new_chat()
        elif k in ("d", "D") and self.hist_sel > 0:
            self.hist_confirm = True
        elif k == "enter":
            sel = self.hist_sel
            self.hist = None if sel == 0 else self.hist
            if sel == 0:
                self.new_chat()
            else:
                chat = self.hist[sel - 1]
                self.hist = None
                if chat["id"] != self.session_id or not self.claude.alive():
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
            if k in ("y", "Y", "a", "A", "n", "N", "esc"):
                self.answer_pending(k)
            return
        if k == "ctrl+q":
            self.ask_reboot_back()
            raise SystemExit
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
                self.add("info", "  stopping...")
            elif self.input:
                self.input = ""
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
                    "picked": set(), "answers": {}, "typing": False}
        self.dirty = True

    def ask_options(self):
        q = self.ask["qs"][self.ask["qi"]]
        return [o.get("label", "?") for o in q.get("options", [])] + ["✎ type my own answer"]

    def ask_key(self, k):
        a = self.ask
        q = a["qs"][a["qi"]]
        opts = self.ask_options()
        multi = bool(q.get("multiSelect"))
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
            self.ask = None

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
            self.ask = None

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
            self.new_chat()
            return
        if self.busy:
            # claude answers in order, so hold this until the current reply finishes;
            # otherwise it would show up above the answer to the previous message
            self.queue.append(text)
            self.lines_cache = None
            return
        self.send_user(text)

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
            self.add("err", "couldn't reach claude. press enter again to restart.")

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
            "esc          stop claude / clear box" + (" / hide" if CAN_HIDE else ""),
            "y  n  a      answer permission prompts",
            "F2           settings",
            "F3           chats (n new, d delete)",
            "ctrl+q       quit     /new  new chat",
        ])

    def show_about(self):
        self.splash("checking versions...")
        try:
            ver = open(os.path.join(SHARE, "VERSION")).read().strip()
        except OSError:
            ver = "dev"
        try:
            cmd = setup.ssh_base() + ["claude --version"] if REMOTE else [CLAUDE, "--version"]
            cv = subprocess.run(cmd, capture_output=True, timeout=20).stdout.decode(errors="replace").split()[0]
        except (OSError, IndexError, subprocess.TimeoutExpired):
            cv = "?"
        if REMOTE:
            c = setup.remote_cfg() or {}
            who, where = "(managed on the pc)", "your pc: %s@%s" % (c.get("user"), c.get("host"))
        else:
            st = setup.auth_status()
            who = st.get("email") or st.get("authMethod") or "not signed in"
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
            try:
                subprocess.run(["sudo", "-n", BOOTMODE, "enter"], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=60)
                time.sleep(60)                           # the reboot takes us down
            except (OSError, subprocess.TimeoutExpired) as e:
                log("bootmode enter", e)
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
        changed = [st for st in SETTINGS if st[3] and self.cfg.get(st[0]) != self.menu_before.get(st[0])]
        if self.cfg.get("web") == "on" and self.menu_before.get("web") != "on" and not self.web:
            self.start_web()
        if self.cfg["tool_lines"] != self.menu_before["tool_lines"]:
            self.add("info", "  tool output: applies to new results")
        if changed:
            what = ", ".join("%s=%s" % (st[1], self.cfg[st[0]]) for st in changed)
            if self.busy:
                self.add("info", "  saved (%s). applies after this reply." % what)
                self.restart_after_turn = True
            else:
                self.add("info", "  %s. restarting claude..." % what)
                self.draw()
                self.start_claude(resume=self.session_id)

    def draw_menu(self, d):
        if self.submenu is None:
            title = "settings"
            items = [(label, self.cfg.get(key, "→")) for key, label, _, _ in SETTINGS]
            sel = self.menu
            hint = "f up  x down  enter open  esc close"
        else:
            key, label, choices, _ = SETTINGS[self.menu]
            title = "settings > " + label
            items = [(c, "current" if c == self.cfg[key] else "") for c in choices]
            sel = self.submenu
            hint = "f up  x down  enter pick  esc back"
        rows_fit = 7
        first = max(0, min(sel - rows_fit // 2, len(items) - rows_fit))
        shown = items[first:first + rows_fit]
        bh = len(shown) * 14 + 32
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
        d.text(((W - self.small.getlength(hint)) // 2, top + bh - 12), hint, font=self.small, fill=DIM)

    # -- drawing --
    def wrap(self, kind, text, label=""):
        """-> list of (kind, text, boxpos); boxpos None, or top (border + label) / mid / bot"""
        out = []
        width = self.cols - 2
        inner = self.cols - 4            # text width inside a box
        hard = lambda s, w: [s[i:i + w] for i in range(0, max(len(s), 1), w)]
        if kind in ("result", "err"):
            out.append((kind, label, "top"))
            for raw in text.split("\n"):
                out.extend((kind, c, "mid") for c in hard(raw, inner))
            out.append((kind, "", "bot"))
            return out
        in_code = False
        for raw in text.split("\n"):
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
        if in_code:                           # unclosed fence
            out.append(("code", "", "bot"))
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
        for para_i, para in enumerate(self.input.split("\n")):
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
        # top bar
        spin = "|/-\\"[int(time.time() * 4) % 4] if self.busy else ""
        left = (spin + " " + self.busy_label()) if self.busy else ("claude · " + self.status)

        try:
            cap = open("/sys/class/power_supply/bq27220-0/capacity").read().strip()
            chg = open("/sys/class/power_supply/bq27220-0/status").read().strip() == "Charging"
            right = "%s%s%% %s" % ("+" if chg else "", cap, time.strftime("%H:%M"))
        except OSError:
            right = time.strftime("%H:%M")
        d.text((3, 0), left[:40], font=self.small, fill=ACCENT)
        d.text((W - 3 - self.small.getlength(right), 0), right, font=self.small, fill=DIM)
        d.line((0, 11, W, 11), fill=BAR)
        # conversation
        lines = self.all_lines()
        n = self.view_lines()
        maxscroll = max(0, len(lines) - n)
        self.scroll = min(self.scroll, maxscroll)
        end = len(lines) - self.scroll
        view = lines[max(0, end - n):end]
        colors = {"user": USER, "assistant": FG, "tool": TOOL, "result": RESULT, "code": CODE,
                  "ask": ASK, "err": ERR, "info": DIM, "gap": FG, "queued": DIM}
        y = 13
        bx0, bx1 = 3, W - 9                       # box edges (clear of the scroll bar)
        for kind, text, pos in view:
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
                        d.text((bx0 + 6, y), lab, font=self.font, fill=TOOL if kind != "code" else DIM)
                elif pos == "bot":
                    d.line((bx0, cell0, bx0, mid), fill=edge)
                    d.line((bx1, cell0, bx1, mid), fill=edge)
                    d.line((bx0, mid, bx1, mid), fill=edge)
                else:
                    d.line((bx0, cell0, bx0, cell1), fill=edge)
                    d.line((bx1, cell0, bx1, cell1), fill=edge)
                    d.text((bx0 + 5, y), text, font=self.font, fill=colors.get(kind, FG))
            else:
                d.text((3, y), text, font=self.font, fill=colors.get(kind, FG))
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
                r, prefix = "y / n / a ?", "› "
            d.text((3, top + 1 + i * self.lh), prefix + r, font=self.font, fill=col)
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
                self.hide()                  # the launcher script went away: give the screen back
                continue
            fds = [self.kb.fd] + self.sudo_fds() + ([self.web.rfd] if self.web else [])
            if self.claude.alive():
                fds.append(self.claude.fileno())
            r, _, _ = select.select(fds, [], [], 0.25)
            self.sudo_poll(r)
            self.web_tick(r)
            if self.kb.fd in r:
                for k in self.kb.read():
                    self.key(k)
                self.last_key = time.time()
            # autosave the half-typed message a second after you stop typing
            if self.input != self.draft_saved and time.time() - self.last_key > 1.0:
                self.save_draft()
            if self.claude.alive() and self.claude.fileno() in r:
                msgs = self.claude.read()
                if msgs is None:
                    self.busy = False
                    if REMOTE:
                        self.add("err", "lost the pc (is it on? ssh ok?). enter = retry.")
                    else:
                        self.add("err", "claude stopped. enter = restart.")
                else:
                    for m in msgs:
                        self.handle(m)
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
        if self.sudo_forget and not self.proc_running(self.sudo_forget):
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
        cmd = textwrap.wrap(q["cmd"], 50)
        if len(cmd) > 4:
            cmd = cmd[:3] + [cmd[3][:47] + "..."]
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
        """install the RAM boost helpers (needs the password once). -> True when done"""
        err = False
        for _ in range(3):
            pw = self.dialog("ram boost setup" if not err else "wrong password? try again",
                             ["installs 2 small helpers so clawputer can pause background "
                              "services while claude runs (undo any time).", "enter your password:"],
                             password=True, err=err)
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
        return False

    def ask_reboot_back(self):
        """quitting after a max-boost restart: offer to reboot back to normal"""
        self.reboot_after = False
        if REMOTE or not self.kb or gpu_mb() > 16 or not boost_state()[0]:
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
                run("systemctl", "--user", "stop", "xdg-desktop-portal", "xdg-desktop-portal-gtk",
                    "xdg-desktop-portal-wlr", "xdg-document-portal", "xdg-permission-store", "mpris-proxy",
                    "filter-chain", "gvfs-*", "wireplumber", "pipewire-pulse", "pipewire",
                    "pipewire.socket", "pipewire-pulse.socket")
                run("sudo", "-n", BOOST, "on")
            else:
                run("systemctl", "--user", "start", "pipewire.socket", "pipewire-pulse.socket", "wireplumber")
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
