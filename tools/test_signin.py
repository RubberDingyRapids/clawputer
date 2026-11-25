"""On-device test of the first-run flow with fake screen/keyboard (run with CLAUDE_CONFIG_DIR set
to an empty folder so the real login is untouched). Saves PNGs of each screen to /tmp."""
import os, sys, time, urllib.request
sys.path.insert(0, "/usr/share/clawputer")
import clawputer_gui as g

shots = []


class FB:
    def write(self, img): shots.append(img.copy())
    def close(self): pass


class KB:
    fd = -1
    def close(self): pass


g.Framebuffer, g.Keyboard = FB, KB
app = g.App([])
print("setup step:", app.setup.step if app.setup else None)
app.draw(); shots[-1].save("/tmp/s0_checking.png")
t0 = time.time()
while app.setup.step == "check" and time.time() - t0 < 30:
    app.setup.poll(); time.sleep(0.2)
app.draw(); shots[-1].save("/tmp/s1_choose.png")
print("after poll:", app.setup.step)
app.key("1")
t0 = time.time()
while not app.setup.login_url and time.time() - t0 < 30:
    app.setup.poll(); time.sleep(0.3)
print("login url found:", bool(app.setup.login_url))
app.draw(); shots[-1].save("/tmp/s2_signin_qr.png")
url = app.setup.helper.url()
print("helper url:", url)
page = urllib.request.urlopen(url, timeout=5).read().decode()
print("page has sign-in link:", "claude.ai/oauth" in page or "authorize" in page, "| has code form:", "name='code'" in page)
bad = urllib.request.urlopen(url.rsplit("/", 2)[0] + "/wrongtoken/", timeout=5) if False else None
app.key("tab"); app.draw(); shots[-1].save("/tmp/s3_signin_ts.png")
app.key("tab"); app.draw(); shots[-1].save("/tmp/s4_signin_link.png")
app.close()
print("done")
