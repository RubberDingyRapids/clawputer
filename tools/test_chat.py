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
    def say(self, t): self.said.append(t); return True
    def interrupt(self): return True
    def answer(self, *a): pass
    def close(self): self.open = False
    def fileno(self): return -1


class FakeNet:
    online, changed = True, False


g.Framebuffer, g.Keyboard, g.ClaudeSession = FB, KB, FakeClaude
g.NetCheck = FakeNet
g.list_chats = lambda: []
g.install_claude_md = lambda: None
g.play_chime = lambda: chimes.append(time.time())
g.save_settings = lambda cfg: None
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

print("\n%d failed" % len(fails) if fails else "\nall passed")
sys.exit(1 if fails else 0)
