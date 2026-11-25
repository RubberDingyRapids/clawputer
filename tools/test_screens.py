"""Render the new screens on the device with a fake screen/keyboard (no real Claude):
mode screen (first + normal), boost/reboot prompts, install progress, remote setup, help, about."""
import os, sys, time, json, threading
sys.path.insert(0, "/usr/share/clawputer")
os.environ["CLAWPUTER_CHECKED"] = "1"
import clawputer_gui as g
import clawputer_setup as su

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
shots = []


class FB:
    def write(self, img): shots.append(img.copy())
    def close(self): pass


class KB:
    fd = -1
    def close(self): pass


class FakeClaude:
    def __init__(self, *a, **k): pass
    def alive(self): return True
    def say(self, t): return True
    def answer(self, *a): pass
    def close(self): pass


g.Framebuffer, g.Keyboard, g.ClaudeSession = FB, KB, FakeClaude
g.list_chats = lambda: []
g.install_claude_md = lambda: None


def save(a, name):
    a.draw(); shots[-1].save(os.path.join(OUT, name)); print("saved", name)


a = g.App([])
a.entries.clear(); a.lines_cache = None
# mode screen
a.open_mode(first=True); save(a, "m1_first.png")
a.modescr["first"] = False; a.modescr["boost"] = False
a.modescr["ram"] = 0; save(a, "m2_ram_max.png")
a.modescr["ram"] = 2; save(a, "m3_ram_plain.png")
a.modescr["ram"] = None; a.modescr["boost"] = True; a.modescr["gpu"] = 16; a.modescr["sel"] = 1; save(a, "m4_boost_active.png")
a.modescr = None

# install progress (fake tracker state)
s = su.Setup.__new__(su.Setup)
s.app, s.helper, s.proc, s.msg, s.finished, s.remote_msg, s.remote_ok = a, None, None, "", False, "", False
s.step = "installing"
t = su.InstallTracker.__new__(su.InstallTracker)
t.t0 = time.time() - 75; t.steps = [["getting latest version", True], ["downloading claude code", False]]
t.log = []; t.sizes = {"plain": 110 * 1048576}; t.buf = ""
t.download = lambda: (47 * 1048576, 110 * 1048576)
s.inst = t
a.setup = s; save(a, "i1_download.png")
t.download = lambda: None
t.feed("Setting up Claude Code...\nChecking installation...\nInstalling Claude Code native build latest...\n  fetching manifest\n  verifying checksum\n  linking ~/.local/bin/claude\n")
save(a, "i2_installing.png")
t.copying = lambda: 230 * 1048576
t.sizes["plain"] = 385 * 1048576
save(a, "i3_copying.png")
a.setup = None

# remote setup (real helper, fake no-config)
r = su.Setup(a, remote_only=True)
a.setup = r; save(a, "r1_command.png")
r.rview = 2 if len(r.remote_views()) > 2 else 1; save(a, "r2_qr.png")
import urllib.request
win = urllib.request.urlopen(r.helper.url("win"), timeout=5).read().decode()
print("win script ok:", "authorized_keys" in win and "__KEY__" not in win and "/remote" in win)
r.close(); a.setup = None

# help + about overlays over a chat
a.add("user", "hi"); a.add("assistant", "Hey! What are we building today?")
a.show_help(); save(a, "h1_help.png")
a.show_about(); save(a, "h2_about.png")
