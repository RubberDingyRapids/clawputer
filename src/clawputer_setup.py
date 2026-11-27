"""
Clawputer setup flows: install Claude Code, sign in (QR + phone web page), remote PC setup.

A tiny web server (LAN only, random token in the URL, only while a setup screen is open)
lets you do the fiddly bits from a phone or PC instead of the Cardputer keyboard:
  - sign in: open the Claude sign-in link on the phone, paste the code back into the page
  - remote PC: one short command on the PC fetches a setup script from the Cardputer, sets up
    SSH with the Cardputer's key, and reports the PC's address back automatically
"""
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
import urllib.request

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
RELEASES = "https://downloads.claude.ai/claude-code-releases"
DOWNLOADS = os.path.expanduser("~/.claude/downloads")
HELPER_PORT = 8765
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


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


def quick_signed_in():
    """fast (no claude start-up) check for saved Claude credentials; the real check
    (`claude auth status`) has to load the 385MB claude binary, which is slow after a reboot"""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    if os.path.exists(os.path.join(cfg, ".credentials.json")):
        return True
    for p in (os.path.join(cfg, ".claude.json"), os.path.expanduser("~/.claude.json")):
        try:
            d = json.load(open(p))
            if d.get("oauthAccount") or d.get("primaryApiKey"):
                return True
        except (OSError, ValueError):
            pass
    return False


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
    """footer as (text, highlighted) segments, e.g. tab: switch QR (wifi/tailscale/link)"""
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
    return True, "connected to %s (%s)" % (cfg["host"], cfg["os"])


# ---------------------------------------------------------------------------
# PC setup scripts (served to the PC by the Cardputer; key + reply address baked in)
# ---------------------------------------------------------------------------
WIN_SCRIPT = r'''# Clawputer: let this PC run Claude Code for your Cardputer. Run in an ADMIN PowerShell.
$ErrorActionPreference = "Stop"
$key  = "__KEY__"
$back = "http://__BACK__/__TOK__/remote"
$cpLan = "__CPLAN__"; $cpTs = "__CPTS__"
function say($t, $c = "Gray") { Write-Host $t -ForegroundColor $c }
say "== Clawputer PC setup ==" "DarkYellow"
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  say "Please run this in an ADMIN PowerShell (right-click PowerShell > Run as administrator)." "Red"; return }
$user = $env:USERNAME

say "1/5 SSH server..."
$cap = Get-WindowsCapability -Online -Name "OpenSSH.Server*" | Select-Object -First 1
if ($cap.State -ne "Installed") { say "    installing OpenSSH Server (can take a minute)"; Add-WindowsCapability -Online -Name $cap.Name | Out-Null }
Set-Service sshd -StartupType Automatic; Start-Service sshd

say "2/5 authorising the Cardputer's key..."
$admins = (net localgroup administrators) 2>$null
$isAdmin = [bool]($admins | Where-Object { $_ -eq $user -or $_ -like "*\$user" })
if ($isAdmin) { $ak = "C:\ProgramData\ssh\administrators_authorized_keys" }
else { New-Item -ItemType Directory -Force "$HOME\.ssh" | Out-Null; $ak = "$HOME\.ssh\authorized_keys" }
if (-not (Test-Path $ak) -or -not (Select-String -Path $ak -SimpleMatch $key -Quiet)) { Add-Content -Path $ak -Value $key -Encoding ascii }
if ($isAdmin) { icacls $ak /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F" | Out-Null }

say "3/5 network + firewall..."
Get-NetConnectionProfile | Where-Object { $_.NetworkCategory -eq "Public" } | ForEach-Object {
  Set-NetConnectionProfile -InterfaceIndex $_.InterfaceIndex -NetworkCategory Private }
if (-not (Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (sshd)" -Enabled True `
    -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -Profile Private | Out-Null }

say "4/5 checking Claude Code + Python..."
$claude = (Get-Command claude -ErrorAction SilentlyContinue) -or (Test-Path "$HOME\.local\bin\claude.exe")
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $claude) { say "    Claude Code not found. In a NORMAL PowerShell run:  irm https://claude.ai/install.ps1 | iex" "Yellow"
                    say "    then run 'claude' once and sign in." "Yellow" }
if (-not $py) { say "    Python not found (used for chat history): winget install -e --id Python.Python.3.12" "Yellow" }

say "5/5 telling the Cardputer where this PC is..."
$addr = $null
$ts = "C:\Program Files\Tailscale\tailscale.exe"
if ($cpTs -and (Test-Path $ts)) { $addr = (& $ts ip -4 2>$null | Select-Object -First 1) }
if (-not $addr) {
  $pre = ($cpLan -split "\.")[0..2] -join "."
  $addr = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like "$pre.*" } | Select-Object -First 1).IPAddress }
if (-not $addr) { $addr = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254*" } | Select-Object -First 1).IPAddress }
Invoke-RestMethod -Method Post -Uri $back -Body @{ host = $addr; user = $user; port = 22 } | Out-Null
say "done! the Cardputer is testing the connection to $user@$addr now." "Green"
'''

UNIX_SCRIPT = r'''#!/bin/bash
# Clawputer: let this computer run Claude Code for your Cardputer.
KEY="__KEY__"
BACK="http://__BACK__/__TOK__/remote"
CPLAN="__CPLAN__"; CPTS="__CPTS__"
echo "== Clawputer PC setup =="
echo "1/4 authorising the Cardputer's key..."
mkdir -p ~/.ssh && chmod 700 ~/.ssh
grep -qxF "$KEY" ~/.ssh/authorized_keys 2>/dev/null || echo "$KEY" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
echo "2/4 SSH server (may ask for your password)..."
if [ "$(uname)" = Darwin ]; then
  sudo systemsetup -setremotelogin on 2>/dev/null || echo "   turn on: System Settings > General > Sharing > Remote Login"
else
  command -v sshd >/dev/null || sudo apt-get install -y openssh-server || sudo dnf install -y openssh-server
  sudo systemctl enable --now ssh 2>/dev/null || sudo systemctl enable --now sshd
fi
echo "3/4 checking Claude Code..."
PATH="$HOME/.local/bin:$PATH" command -v claude >/dev/null || {
  echo "   Claude Code not found: curl -fsSL https://claude.ai/install.sh | bash   (then run 'claude' and sign in)"; }
command -v python3 >/dev/null || echo "   python3 not found (used for chat history)"
echo "4/4 telling the Cardputer where this computer is..."
ADDR=""
[ -n "$CPTS" ] && command -v tailscale >/dev/null && ADDR=$(tailscale ip -4 2>/dev/null | head -1)
if [ -z "$ADDR" ]; then
  PRE=$(echo "$CPLAN" | cut -d. -f1-3)
  if [ "$(uname)" = Darwin ]; then ADDR=$(ifconfig | awk -v p="$PRE." '$1=="inet" && index($2,p)==1 {print $2; exit}')
  else ADDR=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep "^$PRE\." | head -1); fi
fi
curl -s -X POST --data-urlencode "host=$ADDR" --data-urlencode "user=$USER" -d port=22 "$BACK" >/dev/null
echo "done! the Cardputer is testing the connection to $USER@$ADDR now."
'''


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

TOKEN_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"   # no look-alikes (l/1, o/0, i)


class Helper:
    """LAN-only HTTP helper. Pages are produced by callbacks; posts go into an event list."""

    def __init__(self):
        self.token = "".join(secrets.choice(TOKEN_CHARS) for _ in range(5))
        self.events = []
        self.lock = threading.Lock()
        self.pages = {}            # name -> callable() -> html body
        self.raw = {}              # name -> callable(host) -> (content_type, text)
        helper = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _parts(self):
                p = urllib.parse.urlparse(self.path).path.strip("/").split("/")
                return p if p and p[0] == helper.token else None

            def _send_bytes(self, data, ctype, code=200):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _send(self, body, code=200):
                self._send_bytes(PAGE.format(body=body).encode(), "text/html; charset=utf-8", code)

            def do_GET(self):
                p = self._parts()
                if p is None:
                    return self._send("<p>Not found.</p>", 404)
                name = p[1] if len(p) > 1 and p[1] else "main"
                if name in helper.raw:
                    ctype, text = helper.raw[name](self.headers.get("Host", ""))
                    return self._send_bytes(text.encode(), ctype)
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

        try:                                   # a fixed port keeps the PC command short
            self.server = http.server.ThreadingHTTPServer(("0.0.0.0", HELPER_PORT), H)
        except OSError:
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
# installer progress (parses Anthropic's installer output + watches the download)
# ---------------------------------------------------------------------------
class InstallTracker:
    def __init__(self):
        self.t0 = time.time()
        self.steps = [["getting latest version", False]]
        self.log = []                     # detail lines for the current step
        self.sizes = {}                   # "plain"/"zst" -> expected bytes
        self.buf = ""
        threading.Thread(target=self._fetch_sizes, daemon=True).start()

    def _fetch_sizes(self):
        try:
            ver = urllib.request.urlopen(RELEASES + "/latest", timeout=15).read().decode().strip()
            plat = "linux-arm64"
            for kind, name in (("plain", "manifest.json"), ("zst", "manifest.zst.json")):
                try:
                    m = json.loads(urllib.request.urlopen("%s/%s/%s" % (RELEASES, ver, name), timeout=15).read())
                    size = (m.get("platforms") or {}).get(plat, {}).get("size")
                    if size:
                        self.sizes[kind] = int(size)
                except (OSError, ValueError):
                    pass
        except (OSError, ValueError):
            pass

    def _step(self, title):
        for s in self.steps:
            s[1] = True
        self.steps.append([title, False])
        self.log = []

    def feed(self, text):
        self.buf += ANSI.sub("", text).replace("\r", "\n")
        *lines, self.buf = self.buf.split("\n")
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            low = line.lower()
            if line.endswith(("...", "…")):
                self._step(line.rstrip(".…").strip().lower())
            elif "complete" in low or line.startswith("✅") or line.startswith("✔"):
                for s in self.steps:
                    s[1] = True
                self.log = [line]
            else:
                self.log = (self.log + [line])[-3:]

    def copying(self):
        """-> bytes copied so far while the installer copies the binary into place, else None"""
        vdir = os.path.expanduser("~/.local/share/claude/versions")
        try:
            tmps = [f for f in os.listdir(vdir) if ".tmp." in f]
        except OSError:
            return None
        if not tmps:
            return None
        return max(os.path.getsize(os.path.join(vdir, f)) for f in tmps)

    def download(self):
        """-> (bytes_so_far, expected_or_None) while the binary is downloading, else None"""
        try:
            files = [f for f in os.listdir(DOWNLOADS) if f.startswith("claude-")]
        except OSError:
            return None
        if not files:
            return None
        f = max(files, key=lambda n: os.path.getmtime(os.path.join(DOWNLOADS, n)))
        size = os.path.getsize(os.path.join(DOWNLOADS, f))
        exp = self.sizes.get("zst") if f.endswith(".zst") else self.sizes.get("plain")
        return size, exp

    def lines(self, dots):
        dl = self.download()
        if dl and len(self.steps) == 1:
            self._step("downloading claude code")
        out = []
        for title, done in self.steps:
            out.append((("✓ " if done else "▸ ") + title + ("" if done else dots), "ok" if done else "accent"))
        if self.steps[-1][0] == "downloading claude code" and not self.steps[-1][1]:
            if dl:
                got, exp = dl
                mb = got / 1048576
                if exp:
                    out.append(("__bar__", min(1.0, got / exp), "%.0fMB / %.0fMB" % (mb, exp / 1048576)))
                else:
                    out.append(("  %.0fMB downloaded" % mb, "dim"))
        cur = self.steps[-1]
        if not cur[1] and cur[0] != "downloading claude code" and cur[0] != "getting latest version":
            # the installer copies + checks the ~385MB program here; slow on this hardware
            copied = self.copying()
            total = self.sizes.get("plain")
            if copied and total:
                out.append(("__bar__", min(1.0, copied / total),
                            "%.0fMB / %.0fMB" % (copied / 1048576, total / 1048576)))
            out.append(("  big file (~%dMB): can take a few minutes" % ((total or 385 * 1048576) // 1048576), "dim"))
        out += [("  " + l[:46], "dim") for l in self.log]
        return out


# ---------------------------------------------------------------------------
# setup screens (drawn by the GUI; this class holds state + logic)
# ---------------------------------------------------------------------------
class Setup:
    """step: check | install | installing | choose | signin | checking | remote | error"""

    def __init__(self, app, remote_only=False):
        self.app = app
        self.helper = None
        self.proc = None
        self.out = ""
        self.login_url = ""
        self.qr_mode = 0           # sign-in: wifi page / tailscale page / claude link
        self.rview = 0             # remote: command (wifi/tailscale) / QR (wifi/tailscale)
        self.msg = ""
        self.finished = False
        self.remote_msg = ""
        self.remote_ok = False
        self._chk = None
        self.did_login = False
        self.inst = None
        self.who = ""
        if remote_only:
            self.step = "remote"
            ensure_key()
            self.open_helper()
        elif not claude_installed():
            self.step = "install"
        elif quick_signed_in():
            self.step = "check"
            self.finished = True             # saved login found: skip the slow check
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
        self.helper.raw["win"] = lambda host: ("text/plain; charset=utf-8", self.script(WIN_SCRIPT, host))
        self.helper.raw["unix"] = lambda host: ("text/plain; charset=utf-8", self.script(UNIX_SCRIPT, host))

    def script(self, tmpl, host):
        back = host or "%s:%d" % (lan_ip(), self.helper.port)
        return (tmpl.replace("__KEY__", ensure_key()).replace("__BACK__", back)
                .replace("__TOK__", self.helper.token).replace("__CPLAN__", lan_ip())
                .replace("__CPTS__", tailscale_ip() or ""))

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
        k = html.escape(ensure_key())
        c = remote_cfg() or {}
        status = html.escape(self.remote_msg or "not tested yet")
        base = "http://%s:%d/%s" % (lan_ip(), self.helper.port, self.helper.token)
        return f"""
<p>Run Claude on a PC and use the Cardputer as the screen. Fast, no RAM limits.
The PC needs <b>Claude Code</b> installed and signed in, and must be on the same network as the
Cardputer (or both on Tailscale).</p>
<h2>Easy way: one command on the PC</h2>
<p><b>Windows</b>, in an <b>admin</b> PowerShell:</p><pre>irm {base}/win | iex</pre>
<p><b>macOS / Linux</b>, in a terminal:</p><pre>curl -s {base}/unix | bash</pre>
<p class="dim">It sets up SSH with this Cardputer's key and tells the Cardputer the PC's address.</p>
<h2>Manual way</h2><p>This Cardputer's key:</p><pre id="k">{k}</pre>
<button onclick="navigator.clipboard.writeText(document.getElementById('k').innerText)">Copy key</button>
<p class="dim">Add it to the PC's SSH authorized keys (Windows admins:
C:\\ProgramData\\ssh\\administrators_authorized_keys), enable the SSH server, then:</p>
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
        self.inst = InstallTracker()
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
            self.close()
            self.finished = True             # the GUI moves on to "where should claude run?"
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
        self.app.input = code                        # show it in the code box, however it arrived
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
        self.remote_msg = "testing %s@%s..." % (user, host)
        self.app.dirty = True
        self.app.draw()
        ok, m = detect_remote(cfg)
        self.remote_ok = ok
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
                text = chunk.decode(errors="replace")
                self.out = (self.out + text)[-4000:]
                if self.inst:
                    self.inst.feed(text)
                changed = True
                m = re.search(r"https://\S*(?:oauth|authorize)\S*", self.out)
                if m and not self.login_url:
                    self.login_url = m.group(0)
        if self.step == "installing":
            changed = True                    # keep the progress bar / dots moving
        if self.proc and self.proc.poll() is not None:
            rc = self.proc.returncode
            self.proc = None
            changed = True
            if self.step == "installing":
                if rc == 0 and claude_installed():
                    self.start_check()
                elif rc == 137:
                    self.step, self.msg = "error", "install ran out of memory. close other apps, then enter to retry."
                else:
                    self.step, self.msg = "error", "install failed (exit %d). check wifi, then enter to retry." % rc
            elif self.step in ("signin", "checking"):
                st = auth_status()
                if st.get("loggedIn"):
                    self.who = st.get("email") or "signed in"
                    self.app.input = ""
                    self.close()
                    self.finished = True
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
            elif k == "backspace":
                self.app.input = self.app.input[:-1]
            elif len(k) == 1:
                self.app.input += k
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
                    self.remote_ok = ok
                    self.remote_msg = ("✓ " if ok else "✗ ") + m
                else:
                    self.remote_msg = "no PC saved yet"
            elif getattr(self, "remote_del", False):
                self.remote_del = False
                if k in ("y", "Y"):
                    try:
                        os.remove(REMOTE_FILE)
                    except OSError:
                        pass
                    self.remote_ok = False
                    self.remote_msg = "PC removed"
                else:
                    self.remote_msg = "kept"
            elif k == "d":
                if os.environ.get("CLAUDE_REMOTE") == "1":
                    self.remote_msg = "you're using this PC right now: quit first"
                elif remote_cfg():
                    self.remote_del = True
                    self.remote_msg = "forget this PC? y / n"
                else:
                    self.remote_msg = "no PC saved yet"
            elif k in ("tab", "right", "left"):
                n = len(self.remote_views())
                self.rview = (self.rview + (-1 if k == "left" else 1)) % n
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

    def remote_views(self):
        """remote screen views: (name, kind, host)  kind = cmd | qr"""
        v = [("command", "cmd", lan_ip())]
        ts = tailscale_ip()
        if ts:
            v.append(("cmd-ts", "cmd", ts))
        v.append(("qr", "qr", lan_ip()))
        if ts:
            v.append(("qr-ts", "qr", ts))
        return v

    # ---- drawing ----
    def lines(self):
        """-> (title, [lines], qr_text, footer). A line is a str, (text, style) with style
        ok/accent/dim/cmd, or ("__bar__", fraction, label)."""
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
            secs = int(time.time() - self.inst.t0) if self.inst else 0
            body = self.inst.lines(dots) if self.inst else ["starting" + dots]
            return ("Installing Claude Code", body, None, "%d:%02d  this can take a few minutes" % (secs // 60, secs % 60))
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
            lines = how + ["", "or type the code + enter:", ("> " + self.app.input[-26:] + "_", "accent")]
            if self.msg:
                lines.append((self.msg, "dim"))
            if not self.login_url:
                lines, qr = ["starting sign-in" + dots], None
            names = [m[0] for m in self.qr_modes(True)]
            return ("Sign in (%s)" % kind, lines, qr,
                    mode_footer("tab: switch QR (", names, kind, ")  esc: back"))
        if s == "error":
            return ("Hmm", [self.msg or "something went wrong", "", "enter  try again", "q  quit"], None, "")
        if s == "remote":
            c = remote_cfg()
            views = self.remote_views()
            name, kind, host = views[self.rview % len(views)]
            pc = ("PC: %s@%s" % (c["user"], c["host"])) if c else "PC: not set up yet"
            status = (self.remote_msg[:44], "ok" if self.remote_ok else "dim") if self.remote_msg else ("", "dim")
            foot = mode_footer("tab ", [v[0] for v in views], name, "  t test d del esc")
            if kind == "qr":
                url = self.helper.url("remote", host) if self.helper else None
                return ("Remote PC", ["scan with your phone or PC", "for the setup page (needs the",
                                      "same wifi)" if host == lan_ip() else "tailscale)", "", pc, status],
                        url, foot)
            base = "http://%s:%d/%s" % (host, self.helper.port if self.helper else HELPER_PORT,
                                        self.helper.token if self.helper else "?")
            return ("Remote PC", ["On the PC, in an ADMIN PowerShell:",
                                  ("irm %s/win|iex" % base, "cmd"),
                                  "Mac/Linux terminal:",
                                  ("curl -s %s/unix|bash" % base, "cmd"),
                                  ("sets up SSH + tells me the PC's address", "dim"),
                                  pc, status], None, foot)
        return ("", [], None, "")
