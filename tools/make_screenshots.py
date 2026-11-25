"""Render store screenshots (320x170 PNG) on the device using the real app code + fonts,
with example content and no real Claude session. Usage: python3 make_screenshots.py OUTDIR"""
import os, sys, time
sys.path.insert(0, "/usr/share/clawputer")
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
    def interrupt(self): return True


g.Framebuffer, g.Keyboard, g.ClaudeSession = FB, KB, FakeClaude
g.list_chats = lambda: []
g.install_claude_md = lambda: None


def fresh():
    a = g.App.__new__(g.App)
    g.App.__init__  # noqa
    return a


def app():
    a = g.App([])
    if a.setup:                       # skip the sign-in check for screenshots
        a.setup.close()
        a.setup = None
    a.begin_chat()
    a.entries.clear(); a.lines_cache = None
    a.status = "ready · opus-5-5"
    a.cfg["enter"] = "send"
    return a


def save(a, name):
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

# 4. settings
a = app()
a.add("user", "hi!")
a.add("assistant", "Hey! What are we building today?")
a.open_menu()
save(a, "4-settings.png")

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
