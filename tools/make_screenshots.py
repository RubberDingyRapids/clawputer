"""Render store screenshots (320x170 PNG) on the device using the real app code + fonts,
with example content and no real Claude session. Usage: python3 make_screenshots.py OUTDIR [SRC_DIR]
Touches nothing real: no framebuffer, keyboard, askpass socket, web ui or network."""
import os
import sys
import time

OUT = sys.argv[1]
sys.path.insert(0, sys.argv[2] if len(sys.argv) > 2 else "/usr/share/clawputer")
os.environ.update(CLAWPUTER_CHECKED="1", CLAWPUTER_HIDE="1")
import clawputer_gui as g  # noqa: E402
import clawputer_setup as su  # noqa: E402

os.makedirs(OUT, exist_ok=True)
shots = []


class FB:
    prev = None
    def write(self, img): shots.append(img.copy())
    def save_raw(self, path): pass
    def close(self, blank=True): pass


class KB:
    fd = -1
    def read(self): return []
    def close(self): pass


class FakeClaude:
    def __init__(self, *a, **k): pass
    def alive(self): return True
    def say(self, t): return True
    def answer(self, *a): pass
    def close(self): pass
    def interrupt(self): return True
    def fileno(self): return -1


class FakeNet:
    online, changed = True, False


g.Framebuffer, g.Keyboard, g.ClaudeSession, g.NetCheck = FB, KB, FakeClaude, FakeNet
g.list_chats = lambda: []
g.install_claude_md = lambda: None
g.load_settings = lambda: dict(g.DEFAULTS)          # not your real settings
g.save_settings = lambda cfg: None
g.App.open_askpass = lambda self: None
g.App.set_state = lambda self, st: None
g.battery = lambda: (82, False)
g.battery_clock = lambda: "82% " + time.strftime("%H:%M")
g.net_kind = lambda: "wifi"
g.play_chime = lambda kind="done": None
g.backlight_set = lambda v: None


def app():
    a = g.App([])
    if a.setup:                       # skip the sign-in check for screenshots
        a.setup.close()
        a.setup = None
    a.begin_chat()
    a.entries.clear()
    a.lines_cache = None
    a.handle({"type": "system", "subtype": "init", "session_id": "s", "model": "claude-opus-5-5"})
    a.ctx_used, a.ctx_window = 41000, 200000
    a.cfg.update(enter="send", style="plain")
    return a


def save(a, name):
    a.dirty = True
    a.draw()
    shots[-1].save(os.path.join(OUT, name))
    print("saved", name)


# 1. chat with a command + code box
a = app()
a.add("user", "how much ram is free?")
a.add("tool", "▸ Bash: free -h")
a.add("result", "               total        used        free\nMem:           462Mi       280Mi        53Mi\nSwap:          461Mi        35Mi       426Mi")
a.add("assistant", "About 180MB is available, plenty for now. To check again later:\n```bash\nfree -h | grep Mem\n```")
save(a, "1-chat.png")

# 2. permission prompt
a = app()
a.add("user", "make a script that blinks the screen")
a.add("assistant", "I'll write it to ~/clawputer/blink.py and run it.")
a.add("tool", "▸ Write: ~/clawputer/blink.py")
a.add("result", "File created successfully")
a.pending = {"id": "x", "name": "Bash", "input": {}}
a.add("ask", "? allow Bash: python3 blink.py\n  y yes · n no · a always (Bash)")
save(a, "2-permission.png")

# 3. claude asking a question
a = app()
a.add("user", "set up a new python project for me")
a.start_ask("q", {"questions": [{"question": "What kind of project should I set up?", "header": "Project",
                  "options": [{"label": "CLI tool", "description": "A command line script with argparse"},
                              {"label": "Web server", "description": "A tiny Flask app"},
                              {"label": "Game", "description": "A small game for the 320x170 screen"}],
                  "multiSelect": False}]})
save(a, "3-question.png")

# 4. start screen + RAM menu
a = app()
a.claude = None
a.open_mode(first=False)
a.modescr["boost"] = True
a.modescr["gpu"] = 128
a.modescr["ram"] = 0
save(a, "4-start.png")

# 5. chat history
a = app()
a.add("assistant", "Hey! What are we building today?")
now = time.time()
a.hist = [{"id": "a", "title": "blink the screen script", "mtime": now - 600, "path": "", "cwd": ""},
          {"id": "b", "title": "fix my meshtastic config", "mtime": now - 7200, "path": "", "cwd": ""},
          {"id": "c", "title": "explain how zram works", "mtime": now - 86400 * 2, "path": "", "cwd": ""},
          {"id": "d", "title": "write a snake game", "mtime": now - 86400 * 5, "path": "", "cwd": ""}]
a.hist_sel, a.hist_confirm = 1, False
a.session_id = "a"
save(a, "5-history.png")

# 6. QR sign-in
a = app()
s = su.Setup(a, remote_only=True)
s.step = "signin"
s.login_url = "https://claude.ai/oauth/authorize?example"
a.setup = s
save(a, "6-signin.png")
s.close()
