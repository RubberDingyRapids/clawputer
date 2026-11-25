"""
Clawputer setup flows: install Claude Code, sign in (QR + phone web page), remote PC setup.

A tiny web server (LAN only, random token in the URL, only while a setup screen is open)
lets you do the fiddly bits from a phone or PC browser instead of the Cardputer keyboard:
  - sign in: open the Claude sign-in link on the phone, paste the code back into the page
  - remote PC: copy this Cardputer's SSH key, follow per-OS steps, enter the PC's address
"""
import base64
import html
import http.server
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse

try:
    import qrcode
except ImportError:          # QR is a nice-to-have; links are still shown as text
    qrcode = None
from PIL import Image

CONFIG_DIR = os.path.expanduser("~/.config/clawputer")
REMOTE_FILE = os.path.join(CONFIG_DIR, "remote.json")
KEY_FILE = os.path.join(CONFIG_DIR, "remote_key")
KNOWN_HOSTS = os.path.join(CONFIG_DIR, "known_hosts")
INSTALL_CMD = "curl -fsSL https://claude.ai/install.sh | bash"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def claude_path():
    p = os.path.expanduser("~/.local/bin/claude")
    if os.path.exists(p):
        return p
    return shutil.which("claude") or p


def claude_installed():
    return os.path.exists(claude_path())


def auth_status():
    try:
        r = subprocess.run([claude_path(), "auth", "status"], capture_output=True, timeout=30)
        return json.loads(r.stdout.decode(errors="replace") or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))        # no packets sent; just picks the LAN route
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def tailscale_ip():
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "tailscale0"], capture_output=True,
                             timeout=3).stdout.decode()
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def mode_footer(prefix, names, current, suffix):
    """footer as (text, highlighted) segments, e.g. u: switch QR (wifi/tailscale/link)"""
    segs = [(prefix, False)]
    for i, n in enumerate(names):
        if i:
            segs.append(("/", False))
        segs.append((n, n == current))
    segs.append((suffix, False))
    return segs


def qr_image(text, size):
    if not qrcode or not text:
        return None
    q = qrcode.QRCode(border=1, box_size=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
    q.add_data(text)
    q.make(fit=True)
    img = q.make_image(fill_color="black", back_color="white").convert("RGB")
    n = img.size[0]
    scale = max(1, size // n)
    return img.resize((n * scale, n * scale), Image.NEAREST)


# ---------------------------------------------------------------------------
# remote PC config
# ---------------------------------------------------------------------------
def remote_cfg():
    try:
        with open(REMOTE_FILE) as f:
            c = json.load(f)
        return c if c.get("host") and c.get("user") else None
    except (OSError, ValueError):
        return None


def save_remote_cfg(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(REMOTE_FILE, "w") as f:
        json.dump(cfg, f, indent=1)


def ensure_key():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if not os.path.exists(KEY_FILE):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "clawputer@" + socket.gethostname(),
                        "-f", KEY_FILE], check=False)
    try:
        return open(KEY_FILE + ".pub").read().strip()
    except OSError:
        return ""


def ssh_base(cfg=None):
    cfg = cfg or remote_cfg() or {}
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=" + KNOWN_HOSTS, "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=15", "-i", KEY_FILE, "-p", str(cfg.get("port", 22)),
            "%s@%s" % (cfg.get("user", ""), cfg.get("host", ""))]


def detect_remote(cfg):
    """-> (ok, message). Also records the PC's OS in the config."""
    try:
        r = subprocess.run(ssh_base(cfg) + ["echo %OS%"], capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        return False, "timed out reaching %s" % cfg["host"]
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace").strip().splitlines()
        return False, (err[-1] if err else "ssh failed")[:120]
    cfg["os"] = "windows" if "Windows_NT" in r.stdout.decode(errors="replace") else "unix"
    probe = "where claude" if cfg["os"] == "windows" else 'PATH="$HOME/.local/bin:$PATH" command -v claude'
    r2 = subprocess.run(ssh_base(cfg) + [probe], capture_output=True, timeout=20)
    save_remote_cfg(cfg)
    if r2.returncode != 0:
        return False, "connected, but Claude Code isn't installed on the PC"
    return True, "connected to %s (%s), claude found" % (cfg["host"], cfg["os"])


# ---------------------------------------------------------------------------
# tiny web helper
# ---------------------------------------------------------------------------
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clawputer</title><style>
body{{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:18px;max-width:640px}}
h1{{color:#d97757;font-size:22px}} h2{{font-size:17px;margin-top:26px}}
a.btn,button{{display:inline-block;background:#d97757;color:#fff;border:0;border-radius:10px;
padding:14px 18px;font-size:17px;text-decoration:none;margin:6px 0;cursor:pointer}}
input,textarea{{width:100%;box-sizing:border-box;font-size:16px;padding:12px;border-radius:8px;
border:1px solid #444;background:#1c1c22;color:#eee;margin:6px 0}}
code,pre{{background:#1c1c22;border:1px solid #333;border-radius:6px;padding:8px;display:block;
white-space:pre-wrap;word-break:break-all;font-size:13px}}
.ok{{color:#7d7}} .dim{{color:#999;font-size:14px}}
</style></head><body><h1>Clawputer</h1>{body}
<p class="dim">Unofficial Claude Code app for CardputerZero. This page is served by your
Cardputer on your local network and closes when you leave the setup screen.</p></body></html>"""


class Helper:
    """LAN-only HTTP helper. Pages are produced by callbacks; posts go into an event list."""

    def __init__(self):
        self.token = secrets.token_urlsafe(6)
        self.events = []
        self.lock = threading.Lock()
        self.pages = {}            # name -> callable() -> html body
        helper = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _parts(self):
                p = urllib.parse.urlparse(self.path).path.strip("/").split("/")
                return p if p and p[0] == helper.token else None

            def _send(self, body, code=200):
                data = PAGE.format(body=body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                p = self._parts()
                if p is None:
                    return self._send("<p>Not found.</p>", 404)
                name = p[1] if len(p) > 1 and p[1] else "main"
                fn = helper.pages.get(name)
                self._send(fn() if fn else "<p>Nothing to do here right now.</p>")

            def do_POST(self):
                p = self._parts()
                if p is None or len(p) < 2:
                    return self._send("<p>Not found.</p>", 404)
                n = int(self.headers.get("Content-Length") or 0)
                form = urllib.parse.parse_qs(self.rfile.read(min(n, 20000)).decode(errors="replace"))
                form = {k: v[0].strip() for k, v in form.items()}
                with helper.lock:
                    helper.events.append((p[1], form))
                self._send('<p class="ok">Sent! Look at your Cardputer.</p>'
                           '<p><a class="btn" href="/%s/%s">back</a></p>' % (helper.token, p[1]))

        self.server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, page="", host=None):
        return "http://%s:%d/%s/%s" % (host or lan_ip(), self.port, self.token, page)

    def pop_events(self):
        with self.lock:
            ev, self.events = self.events, []
        return ev

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# setup screens (drawn by the GUI; this class holds state + logic)
# ---------------------------------------------------------------------------
class Setup:
    """step: install | installing | choose | signin | checking | done | remote | error"""

    def __init__(self, app, remote_only=False):
        self.app = app
        self.helper = None
        self.proc = None
        self.out = ""
        self.login_url = ""
        self.qr_mode = 0           # index into qr_modes(): wifi page / tailscale page / claude link
        self.msg = ""
        self.finished = False
        self.remote_msg = ""
        self._chk = None
        self.did_login = False
        if remote_only:
            self.step = "remote"
            self.open_helper()
        elif not claude_installed():
            self.step = "install"
        else:
            self.start_check()

    # ---- helper pages ----
    def open_helper(self):
        if self.helper:
            return
        try:
            self.helper = Helper()
        except OSError as e:
            self.msg = "web helper failed: %s" % e
            return
        self.helper.pages["main"] = self.page_signin
        self.helper.pages["code"] = self.page_signin
        self.helper.pages["remote"] = self.page_remote

    def close(self):
        if self.helper:
            self.helper.close()
            self.helper = None
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def page_signin(self):
        if self.step not in ("signin", "checking"):
            return "<p>No sign-in in progress. Start it on the Cardputer.</p>"
        if not self.login_url:
            return "<p>Starting sign-in... refresh in a moment.</p>"
        return ("<h2>1. Sign in to Claude</h2><p><a class='btn' target='_blank' href='%s'>Open Claude sign-in</a></p>"
                "<h2>2. Paste the code it gives you</h2><form method='post' action='/%s/code'>"
                "<input name='code' autocomplete='off' autocapitalize='off' placeholder='paste code here'>"
                "<button>Send to Cardputer</button></form>") % (html.escape(self.login_url), self.helper.token)

    def page_remote(self):
        key = html.escape(ensure_key())
        c = remote_cfg() or {}
        status = html.escape(self.remote_msg or "not tested yet")
        k = key
        return f"""
<p>Run Claude on a PC and use the Cardputer as the screen. Fast, no RAM limits.
The PC must be on, reachable over SSH (same network, or both on Tailscale), and have
<b>Claude Code</b> installed and signed in.</p>
<h2>1. This Cardputer's key</h2><pre id="k">{k}</pre>
<button onclick="navigator.clipboard.writeText(document.getElementById('k').innerText)">Copy key</button>
<h2>2. Set up the PC</h2>
<p><b>Windows</b> (admin PowerShell):</p><pre>Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic; Start-Service sshd
# if your account is an administrator:
Add-Content C:\\ProgramData\\ssh\\administrators_authorized_keys "{k}"
icacls C:\\ProgramData\\ssh\\administrators_authorized_keys /inheritance:r /grant Administrators:F /grant SYSTEM:F
# otherwise: Add-Content $HOME\\.ssh\\authorized_keys "{k}"
# Claude Code + Python (Python is used for chat history):
irm https://claude.ai/install.ps1 | iex
winget install Python.Python.3.12</pre>
<p class="dim">Make sure the network is set to Private (Settings &gt; Network) so the firewall lets SSH in.</p>
<p><b>macOS</b>: System Settings &gt; General &gt; Sharing &gt; Remote Login: on. Then in Terminal:</p>
<pre>mkdir -p ~/.ssh && echo "{k}" &gt;&gt; ~/.ssh/authorized_keys
curl -fsSL https://claude.ai/install.sh | bash</pre>
<p><b>Linux</b>:</p><pre>sudo apt install openssh-server
mkdir -p ~/.ssh && echo "{k}" &gt;&gt; ~/.ssh/authorized_keys
curl -fsSL https://claude.ai/install.sh | bash</pre>
<p class="dim">Then run <code>claude</code> once on the PC and sign in.</p>
<h2>3. Tell the Cardputer where the PC is</h2>
<form method="post" action="/{self.helper.token}/remote">
<input name="host" placeholder="PC address: IP or Tailscale name" value="{html.escape(c.get('host', ''))}">
<input name="user" placeholder="PC username" value="{html.escape(c.get('user', ''))}">
<input name="port" placeholder="SSH port" value="{c.get('port', 22)}">
<button>Save &amp; test</button></form>
<p>Last test: {status}</p>"""

    # ---- flow ----
    def start_install(self):
        self.step = "installing"
        self.out = ""
        self.proc = subprocess.Popen(["bash", "-c", INSTALL_CMD], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        os.set_blocking(self.proc.stdout.fileno(), False)

    def start_check(self):
        """ask claude if we're signed in, in the background (takes a few seconds)"""
        self.step = "check"
        self._chk = None
        threading.Thread(target=lambda: setattr(self, "_chk", auth_status()), daemon=True).start()

    def finish_check(self, st):
        if st.get("loggedIn"):
            self.who = st.get("email") or st.get("authMethod") or "signed in"
            if self.did_login:
                self.step = "done"           # show "you're in" after a fresh sign-in
            else:
                self.finished = True         # already signed in: straight to chat
        else:
            self.step = "choose"

    def start_login(self, console=False):
        self.step = "signin"
        self.did_login = True
        self.out = ""
        self.login_url = ""
        self.msg = ""
        self.open_helper()
        args = [claude_path(), "auth", "login"] + (["--console"] if console else [])
        env = dict(os.environ, BROWSER="true")       # don't try to open a browser on the Cardputer
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.PIPE, env=env)
        os.set_blocking(self.proc.stdout.fileno(), False)

    def send_code(self, code):
        code = code.strip()
        if not code or not self.proc or self.proc.poll() is not None:
            return
        try:
            self.proc.stdin.write((code + "\n").encode())
            self.proc.stdin.flush()
            self.step = "checking"
            self.msg = "checking the code..."
        except OSError as e:
            self.msg = "couldn't send code: %s" % e

    def save_remote(self, form):
        host = form.get("host", "").strip()
        user = form.get("user", "").strip()
        if not host or not user:
            self.remote_msg = "need both address and username"
            return
        try:
            port = int(form.get("port") or 22)
        except ValueError:
            port = 22
        cfg = {"host": host, "user": user, "port": port}
        save_remote_cfg(cfg)
        self.remote_msg = "testing..."
        self.app.dirty = True
        self.app.draw()
        ok, m = detect_remote(cfg)
        self.remote_msg = ("✓ " if ok else "✗ ") + m

    def poll(self):
        """called from the GUI loop; returns True if something changed"""
        changed = False
        if self.proc and self.proc.stdout:
            try:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
            except (BlockingIOError, OSError):
                chunk = None
            if chunk:
                self.out += chunk.decode(errors="replace")
                self.out = self.out[-4000:]
                changed = True
                m = re.search(r"https://\S*(?:oauth|authorize)\S*", self.out)
                if m and not self.login_url:
                    self.login_url = m.group(0)
        if self.proc and self.proc.poll() is not None:
            rc = self.proc.returncode
            self.proc = None
            changed = True
            if self.step == "installing":
                if rc == 0 and claude_installed():
                    self.start_check()
                else:
                    self.step, self.msg = "error", "install failed (exit %d). check wifi, then enter to retry." % rc
            elif self.step in ("signin", "checking"):
                st = auth_status()
                if st.get("loggedIn"):
                    self.who = st.get("email") or "signed in"
                    self.step = "done"
                    self.close()
                else:
                    tail = [l for l in self.out.strip().splitlines() if l.strip()][-1:] or ["sign-in failed"]
                    self.step, self.msg = "error", tail[0][:120] + "  (enter = try again)"
        if self.step == "check":
            changed = True                   # keeps the dots moving
            if self._chk is not None:
                self.finish_check(self._chk)
        if self.helper:
            for name, form in self.helper.pop_events():
                changed = True
                if name == "code":
                    self.send_code(form.get("code", ""))
                elif name == "remote":
                    self.save_remote(form)
        return changed

    def key(self, k):
        s = self.step
        if k == "ctrl+q":
            raise SystemExit
        if s == "install":
            if k in ("y", "Y", "enter"):
                self.start_install()
            elif k in ("q", "Q", "esc"):
                raise SystemExit
        elif s == "choose":
            if k == "1":
                self.start_login(False)
            elif k == "2":
                self.start_login(True)
            elif k in ("q", "Q", "esc"):
                raise SystemExit
        elif s in ("signin", "checking"):
            # tab / arrows switch the QR, so every letter can still be typed into the code box
            if k in ("tab", "right", "left"):
                n = len(self.qr_modes(with_link=True))
                self.qr_mode = (self.qr_mode + (-1 if k == "left" else 1)) % n
                return
            if k == "esc":
                if self.app.input:
                    self.app.input = ""
                    return
                self.close()
                self.step = "choose"
                return
            if k == "enter":
                self.send_code(self.app.input)
                self.app.input = ""
            elif k == "backspace":
                self.app.input = self.app.input[:-1]
            elif len(k) == 1:
                self.app.input += k
        elif s == "done":
            if k in ("enter", " "):
                self.finished = True
        elif s == "error":
            if k == "enter":
                if claude_installed():
                    self.start_check()
                else:
                    self.step = "install"
            elif k in ("q", "Q", "esc"):
                raise SystemExit
        elif s == "remote":
            if k == "t":
                c = remote_cfg()
                if c:
                    self.remote_msg = "testing..."
                    self.app.draw()
                    ok, m = detect_remote(c)
                    self.remote_msg = ("✓ " if ok else "✗ ") + m
                else:
                    self.remote_msg = "no PC saved yet"
            elif k == "d":
                try:
                    os.remove(REMOTE_FILE)
                except OSError:
                    pass
                self.remote_msg = "PC removed"
            elif k in ("tab", "right", "left", "u"):
                n = len(self.qr_modes(with_link=False))
                self.qr_mode = (self.qr_mode + (-1 if k == "left" else 1)) % n
            elif k in ("esc", "enter", "q"):
                self.close()
                self.finished = True

    def qr_modes(self, with_link):
        """the ways to reach the helper page (+ optionally the raw Claude link)"""
        modes = [("wifi", lan_ip())]
        ts = tailscale_ip()
        if ts:
            modes.append(("tailscale", ts))
        if with_link:
            modes.append(("link", None))
        return modes

    def current_qr(self, page, with_link):
        modes = self.qr_modes(with_link)
        kind, host = modes[self.qr_mode % len(modes)]
        if kind == "link":
            return kind, self.login_url
        return kind, (self.helper.url(page, host) if self.helper else None)

    # ---- drawing ----
    def lines(self):
        """-> (title, [text lines], qr_text, footer)"""
        s = self.step
        dots = "." * (int(time.time() * 3) % 4)
        if s == "check":
            return ("Clawputer", ["checking your claude sign-in" + dots], None, "")
        if s == "install":
            return ("Welcome to Clawputer",
                    ["Clawputer is a chat app for Claude Code",
                     "(unofficial). It needs Claude Code,",
                     "Anthropic's command line tool.",
                     "",
                     "y  install it now (~2 min, needs wifi)",
                     "q  quit"], None, "uses Anthropic's official installer")
        if s == "installing":
            tail = [l for l in self.out.replace("\r", "\n").splitlines() if l.strip()][-4:]
            return ("Installing Claude Code" + dots, tail or ["starting" + dots], None, "this can take a few minutes")
        if s == "choose":
            return ("Sign in to Claude",
                    ["How do you pay for Claude?", "",
                     "1  Claude subscription (Pro / Max)",
                     "2  Anthropic Console (API billing)",
                     "", "q  quit"], None, "you only need to do this once")
        if s in ("signin", "checking"):
            kind, qr = self.current_qr("", with_link=True)
            if kind == "link":
                how = ["scan: opens Claude sign-in", "(works on any network),", "then type the code below"]
            else:
                how = ["scan with your phone, sign in", "there and paste the code back.",
                       "phone must be on the same wifi" if kind == "wifi" else "phone must be on tailscale"]
            lines = how + ["", "or type the code + enter:", "> " + self.app.input[-24:] + "_"]
            if self.msg:
                lines.append(self.msg)
            if not self.login_url:
                lines, qr = ["starting sign-in" + dots], None
            names = [m[0] for m in self.qr_modes(True)]
            return ("Sign in (%s)" % kind, lines, qr,
                    mode_footer("tab: switch QR (", names, kind, ")  esc: back"))
        if s == "done":
            return ("You're in ✓", ["signed in: %s" % getattr(self, "who", ""), "",
                                    "enter  start chatting", "",
                                    "F1 settings  F2 chats  ctrl+q quit"], None, "")
        if s == "error":
            return ("Hmm", [self.msg or "something went wrong", "", "enter  try again", "q  quit"], None, "")
        if s == "remote":
            c = remote_cfg()
            kind, qr = self.current_qr("remote", with_link=False)
            lines = ["run Claude on your PC, this is", "just the screen. scan to set up",
                     "(phone or PC browser on the",
                     "same wifi)" if kind == "wifi" else "on tailscale)", "",
                     "PC: " + ("%s@%s" % (c["user"], c["host"]) if c else "not set"),
                     self.remote_msg[:34]]
            foot = "t test  d remove  esc back"
            names = [m[0] for m in self.qr_modes(False)]
            if len(names) > 1:
                foot = mode_footer("tab ", names, kind, "  " + foot)
            return ("Remote PC (%s)" % kind, lines, qr, foot)
        return ("", [], None, "")
