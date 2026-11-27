"""
Clawputer web UI: the chat in a phone / PC browser, served straight from the GUI process.

Python stdlib only. A background thread runs a small HTTP server; the GUI's main loop
publishes a snapshot of its state (publish) and runs whatever the browsers asked for
(actions; a pipe wakes its select loop). Browsers long-poll /api/state.

Access needs a key (in the link / QR code on the Cardputer's splash screen); it's then kept
in a cookie. Plain HTTP: fine at home or over Tailscale, but on shared Wi-Fi anything typed
(including a sudo password) could be sniffed.
"""
import hmac
import json
import os
import queue
import secrets
import socket
import subprocess
import threading
import time
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORTS = range(8080, 8090)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass                       # phones drop connections all the time; not worth a traceback


def local_ips():
    """-> [(interface, ip)] for every IPv4 address that isn't loopback (wifi first)"""
    out = []
    try:
        txt = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, timeout=5).stdout.decode()
        for line in txt.splitlines():
            parts = line.split()
            if len(parts) > 3 and parts[1] != "lo":
                out.append((parts[1], parts[3].split("/")[0]))
    except (OSError, subprocess.TimeoutExpired):
        pass
    out.sort(key=lambda x: (not x[0].startswith("wl"), x[0].startswith("tailscale")))
    return out


class WebUI:
    def __init__(self, conf_dir, log):
        self.log = log
        self.key = self._load_key(os.path.join(conf_dir, "web.json"))
        self.inbox = queue.Queue()
        self.rfd, self.wfd = os.pipe()
        os.set_blocking(self.rfd, False)
        self.cond = threading.Condition()
        self.version = 0
        self.gen = 0
        self.entries = []          # the GUI's list (append-only between gens; read here, never changed)
        self.state = {}
        self.screen = None         # PIL image of the LCD, or None
        self.last_seen = 0         # last time a browser polled
        self.server = None
        for port in PORTS:
            try:
                self.server = Server(("0.0.0.0", port), self._handler())
                break
            except OSError:
                continue
        if self.server is None:
            raise OSError("no free port in 8080-8089")
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def _load_key(path):
        try:
            k = json.load(open(path)).get("key", "")
            if len(k) >= 8:
                return k
        except (OSError, ValueError):
            pass
        k = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(10))   # easy to type
        try:
            old = os.umask(0o077)
            try:
                with open(path, "w") as f:
                    json.dump({"key": k}, f)
            finally:
                os.umask(old)
        except OSError:
            pass
        return k

    def urls(self):
        return [(ifc, "http://%s:%d/" % (ip, self.port)) for ifc, ip in local_ips()]

    def active(self):
        return time.time() - self.last_seen < 40

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        with self.cond:
            self.version += 1
            self.cond.notify_all()
        for fd in (self.rfd, self.wfd):
            try:
                os.close(fd)
            except OSError:
                pass

    # -- called from the GUI's main thread --
    def publish(self, entries, gen, state):
        with self.cond:
            self.entries, self.gen, self.state = entries, gen, state
            self.version += 1
            self.cond.notify_all()

    def actions(self):
        try:
            os.read(self.rfd, 4096)
        except OSError:
            pass
        out = []
        while True:
            try:
                out.append(self.inbox.get_nowait())
            except queue.Empty:
                return out

    # -- http --
    def _handler(self):
        web = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def authed(self, q=None):
                hk = self.headers.get("X-CP-Key", "")
                if hk and hmac.compare_digest(hk, web.key):
                    return True
                qk = ((q or {}).get("k") or [""])[0]
                if qk and hmac.compare_digest(qk, web.key):
                    return True
                ck = self.headers.get("Cookie", "")
                for part in ck.split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "cpk" and hmac.compare_digest(v, web.key):
                        return True
                return False

            def send(self, code, body, ctype="application/json", extra=()):
                if isinstance(body, str):
                    body = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if u.path == "/":
                    k = (q.get("k") or [""])[0]
                    if k:
                        if hmac.compare_digest(k, web.key):
                            # serve the page straight away (no redirect): a QR scan opens the link from
                            # another app, and browsers drop SameSite=Strict cookies on that redirect,
                            # which bounced you back to the login page. The page also keeps the key in
                            # localStorage and sends it as a header, so cookies don't matter at all.
                            # Lax is fine: POSTs need the X-CP header anyway (no CSRF).
                            ck = "cpk=%s; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax" % web.key
                            return self.send(200, PAGE, "text/html; charset=utf-8", [("Set-Cookie", ck)])
                        return self.send(200, LOGIN_PAGE.replace("<!--bad-->", "<p class=bad>wrong key</p>"),
                                         "text/html; charset=utf-8")
                    if not self.authed():
                        return self.send(200, LOGIN_PAGE, "text/html; charset=utf-8")
                    return self.send(200, PAGE, "text/html; charset=utf-8")
                if not self.authed(q):
                    return self.send(401, '{"error":"key"}')
                if u.path == "/api/state":
                    return self.state(q)
                if u.path == "/screen.png":
                    img = web.screen
                    if img is None:
                        return self.send(404, "")
                    b = io.BytesIO()
                    img.save(b, "PNG")
                    return self.send(200, b.getvalue(), "image/png")
                self.send(404, '{"error":"not found"}')

            def state(self, q):
                def num(name, d):
                    try:
                        return int((q.get(name) or [d])[0])
                    except ValueError:
                        return d
                v, gen, have = num("v", -1), num("gen", -1), num("have", 0)
                web.last_seen = time.time()
                with web.cond:
                    if v == web.version:
                        web.cond.wait(25)
                    entries, cur_gen, st, ver = web.entries, web.gen, web.state, web.version
                web.last_seen = time.time()
                reset = gen != cur_gen or have > len(entries)
                start = 0 if reset else have
                body = json.dumps({"v": ver, "gen": cur_gen, "reset": reset, "start": start,
                                   "entries": [list(e) for e in entries[start:]], "state": st})
                self.send(200, body)

            def do_POST(self):
                if not self.authed() or self.headers.get("X-CP") != "1":   # custom header: no CSRF
                    return self.send(401, '{"error":"key"}')
                if urlparse(self.path).path != "/api/act":
                    return self.send(404, "{}")
                try:
                    n = min(int(self.headers.get("Content-Length", "0")), 100000)
                    act = json.loads(self.rfile.read(n) or b"{}")
                    if not isinstance(act, dict) or not isinstance(act.get("a"), str):
                        raise ValueError
                except ValueError:
                    return self.send(400, '{"error":"bad request"}')
                web.inbox.put(act)
                try:
                    os.write(web.wfd, b"!")
                except OSError:
                    pass
                self.send(200, '{"ok":true}')

        return H


LOGIN_PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Clawputer</title>
<style>body{background:#16161d;color:#ddd;font:16px system-ui,sans-serif;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0}form{text-align:center}
h1{color:#e8875b;font-weight:600}input{font-size:18px;padding:10px;border-radius:10px;border:1px solid #444;
background:#22222c;color:#eee;width:200px}button{font-size:18px;padding:10px 18px;border:0;border-radius:10px;
background:#e8875b;color:#111;margin-left:6px}.bad{color:#f66}p{color:#999}</style></head><body>
<form method=get action="/"><h1>Clawputer</h1><p>enter the key shown on the Cardputer</p><!--bad-->
<input name=k autofocus autocomplete=off autocapitalize=off><button>open</button></form>
<script>try{const k=localStorage.getItem('cpk');if(k&&!/[?&]k=/.test(location.search))location.replace('/?k='+encodeURIComponent(k))
else if(/[?&]k=/.test(location.search))localStorage.removeItem('cpk')}catch(e){}</script></body></html>"""

PAGE = r"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,interactive-widget=resizes-content">
<meta name=theme-color content="#0c0c10"><title>Clawputer</title>
<style>
/* same colours as the cardputer screen */
:root{--bg:#0c0c10;--panel:#17171f;--line:#23232d;--fg:#e1e1e1;--dim:#787882;--acc:#d97757;
--user:#6ec8ff;--tool:#ebbe5a;--res:#8c8c96;--box:#4b4b5c;--ok:#78d282;--err:#ff6e6e;--ask:#ff78c8;--code:#96dc96;
--colw:980px;--side:max(12px,calc((100% - var(--colw)) / 2))}
@media (min-width:1500px){:root{--colw:1120px}body{font-size:16px}}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;
display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:8px;padding:8px var(--side);border-bottom:1px solid var(--line);
background:var(--panel);position:sticky;top:0;z-index:5}
header h1{font-size:16px;margin:0;color:var(--acc);font-weight:650}
#status{color:var(--dim);font-size:13px;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#batt{color:var(--dim);font-size:12px}
.hb{background:none;border:1px solid var(--line);color:var(--fg);border-radius:9px;padding:5px 9px;font-size:13px;cursor:pointer}
.hb:hover{border-color:var(--acc)}
main{flex:1;min-height:0;overflow-y:auto;padding:12px var(--side);display:flex;flex-direction:column;gap:8px}
main>*{flex-shrink:0}
.m{max-width:92%;padding:8px 12px;border-radius:14px;white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere}
.user{align-self:stretch;max-width:100%;background:#13131a;border:1px solid var(--line);border-radius:6px;color:var(--user);padding:6px 10px}
.user::before{content:"› ";color:var(--dim)}
.assistant{align-self:stretch;max-width:100%;padding:2px 4px}
.h{color:var(--acc)}
/* a command and its output: one box, the command written on the top border (like the cardputer) */
.box{align-self:stretch;min-width:0;min-inline-size:0;width:100%;margin:2px 0;border:1px solid var(--box);border-radius:4px;padding:1px 9px 5px;
font:12.5px/1.4 ui-monospace,Menlo,Consolas,monospace;color:var(--res);--fade:var(--bg)}
.box legend{color:var(--tool);padding:0 5px;max-width:calc(100% - 8px);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.box .fullcmd{display:none;color:var(--tool);white-space:pre-wrap;word-break:break-all;
margin:1px 0 5px;padding-bottom:5px;border-bottom:1px dashed var(--box)}
.box.open .fullcmd{display:block}
.box.pend{border-color:transparent;padding:0 4px}.box.pend legend{padding:0}.box.pend legend::before{content:"▸ "}
.box.err{border-color:#6e3c3c;color:var(--err)}
.txt{position:relative;overflow:hidden;white-space:pre-wrap;overflow-wrap:anywhere}
.cut{cursor:pointer}
.cut:not(.open) .txt::after{content:"";position:absolute;left:0;right:0;bottom:0;height:1.5em;
background:linear-gradient(rgba(0,0,0,0),var(--fade))}
.more{font:11.5px system-ui,sans-serif;color:var(--acc);margin-top:2px;user-select:none}
.more::before{content:"… ";color:var(--dim)}.open .more::before{content:""}
.info,.queued{align-self:center;color:var(--dim);font-size:12.5px;padding:0 6px}
.ask{align-self:flex-start;color:var(--ask);font-size:13px;padding:0 6px}
pre{background:none;border:1px solid var(--box);border-radius:4px;padding:6px 9px;overflow-x:auto;
color:var(--code);font:12.5px ui-monospace,Menlo,Consolas,monospace;margin:6px 0;white-space:pre}
code{background:#17171f;border-radius:4px;padding:1px 4px;font:13px ui-monospace,Menlo,Consolas,monospace;color:var(--code)}
.card{margin:0 var(--side) 8px;padding:10px 12px;border-radius:12px;background:var(--panel);border:1px solid var(--ask)}
.card h3{margin:0 0 6px;font-size:14px;color:var(--ask);font-weight:600}
.card .cmd{font:13px ui-monospace,Menlo,Consolas,monospace;background:#101016;padding:6px 8px;border-radius:6px;
margin-bottom:8px;white-space:pre-wrap;word-break:break-all}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.b{border:0;border-radius:10px;padding:9px 14px;font-size:15px;cursor:pointer;background:#2c2c3a;color:var(--fg)}
.b.pri{background:var(--acc);color:#111;font-weight:600}.b.ok{background:#2e5a3c}.b.no{background:#5a2e2e}
.opt{display:block;width:100%;text-align:left;margin:4px 0}.opt small{display:block;color:var(--dim);font-size:12px}
.opt.on{outline:2px solid var(--acc)}
footer{padding:8px var(--side) 12px;border-top:1px solid var(--line);background:var(--panel);display:flex;gap:8px;align-items:flex-end}
textarea{flex:1;resize:none;background:#15151c;color:var(--fg);border:1px solid var(--line);border-radius:12px;
padding:9px 11px;font:15px system-ui,sans-serif;max-height:40vh;min-height:42px}
textarea:focus,input:focus,select:focus{outline:none;border-color:var(--acc)}
input[type=password],input[type=text],select{background:#15151c;color:var(--fg);border:1px solid var(--line);
border-radius:10px;padding:8px 10px;font-size:15px}
.sheet{position:fixed;inset:0;background:#000a;display:none;z-index:10;align-items:flex-end;justify-content:center}
.sheet.show{display:flex}
@media (min-width:900px){.sheet{align-items:center}.sheet>div{border-radius:16px}}
.sheet>div{background:var(--panel);width:min(560px,100%);max-height:85vh;overflow-y:auto;border-radius:16px 16px 0 0;padding:14px}
.sheet h2{margin:0 0 10px;font-size:17px;color:var(--acc)}
.li{display:flex;justify-content:space-between;gap:10px;padding:10px;border-radius:10px;cursor:pointer;border-bottom:1px solid var(--line)}
.li:hover{background:#2a2a36}.li span{color:var(--dim);font-size:12px;white-space:nowrap}
.li.cur{color:var(--acc)}
.set{display:flex;justify-content:space-between;align-items:center;padding:8px 2px;border-bottom:1px solid var(--line)}
#screen img{width:100%;image-rendering:pixelated;border-radius:8px;border:1px solid var(--line);background:#000}
.keys{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin-top:10px}
.keys .b{padding:10px 0}
#live{display:none;padding:4px var(--side) 6px;color:var(--acc);font-size:13.5px}
#live .g{display:inline-block;width:1.1em}#live .d{color:var(--dim)}
#spin{display:none;width:9px;height:9px;border-radius:50%;background:var(--acc);animation:p 1s infinite}
@keyframes p{50%{opacity:.25}}
#off{display:none;background:#5a2e2e;color:#fff;text-align:center;font-size:13px;padding:4px}
</style></head><body>
<header><h1>Clawputer</h1><div id=spin></div><div id=status>connecting…</div><span id=batt></span>
<button class=hb onclick="openChats()">chats</button><button class=hb onclick="openSettings()">⚙</button>
<button class=hb onclick="openScreen()">▣</button></header>
<div id=off>lost connection, retrying…</div>
<main id=log></main>
<div id=live><span class=g></span> <span class=ph></span>… <span class=d></span></div>
<div id=cards></div>
<footer><textarea id=inp rows=1 placeholder="message Claude…"></textarea>
<button class="b pri" id=send onclick="send()">send</button>
<button class="b no" id=stop onclick="act({a:'stop'})" style="display:none">stop</button></footer>

<div class=sheet id=chats onclick="if(event.target==this)hide(this)"><div><h2>chats</h2>
<button class="b pri" onclick="act({a:'new'});hide(chats)">+ new chat</button><div id=chatlist style="margin-top:8px">loading…</div></div></div>
<div class=sheet id=settings onclick="if(event.target==this)hide(this)"><div><h2>settings</h2><div id=setlist></div>
<p style="color:var(--dim);font-size:12px">web ui on your Wi-Fi uses plain http: fine at home or over Tailscale.</p></div></div>
<div class=sheet id=screen onclick="if(event.target==this){hide(this);scr=0}"><div><h2>Cardputer screen</h2><img id=scrimg alt="">
<div class=keys>
<button class=b onclick="k('esc')">esc</button><button class=b onclick="k('up')">↑</button><button class=b onclick="k('down')">↓</button>
<button class=b onclick="k('enter')">enter</button><button class=b onclick="k('tab')">tab</button>
<button class=b onclick="k('f1')">F1</button><button class=b onclick="k('f2')">F2</button><button class=b onclick="k('f3')">F3</button>
<button class=b onclick="k('left')">←</button><button class=b onclick="k('right')">→</button></div>
<div class=row style="margin-top:8px"><input type=text id=kt placeholder="type keys on the device" style="flex:1" autocapitalize=off>
<button class=b onclick="typeKeys()">type</button></div></div></div>

<script>
let K='';
try{K=new URLSearchParams(location.search).get('k')||localStorage.getItem('cpk')||'';
  if(K)localStorage.setItem('cpk',K)}catch(e){}
if(location.search)history.replaceState(null,'','/');
const H=K?{'X-CP-Key':K}:{};
let v=-1,gen=-1,have=0,S={},scr=0,lastAsk='';
const $=id=>document.getElementById(id),log=$('log'),inp=$('inp');
function hide(e){e.classList.remove('show')}
function esc(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function md(t){
  let out='',parts=t.split(/```/);
  parts.forEach((p,i)=>{
    if(i%2){p=p.replace(/^[\w+-]*\n/,'');out+='<pre>'+esc(p.replace(/\n$/,''))+'</pre>'}
    else out+=esc(p).replace(/`([^`\n]+)`/g,'<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g,'<b>$1</b>')
      .replace(/^#{1,4} (.*)$/gm,'<b class=h>$1</b>');
  });return out}
function add(kind,text){
  if(kind=='tool'){                                  // waits for its output, then becomes the box label
    const d=document.createElement('fieldset');d.className='box pend';
    const l=document.createElement('legend');l.textContent=text.replace(/^▸ /,'');d.appendChild(l);
    log.appendChild(d);return}
  if(kind=='result'||kind=='err'){
    let d=log.lastElementChild;
    if(!(d&&d.classList.contains('pend'))){d=document.createElement('fieldset');d.className='box';log.appendChild(d)}
    d.classList.remove('pend');if(kind=='err')d.classList.add('err');
    const t=document.createElement('div');t.className='txt';t.textContent=text;d.appendChild(t);
    clip(d,t);return}
  const d=document.createElement('div');d.className='m '+kind;
  if(kind=='ask')text=text.split('\n')[0];
  if(kind=='assistant')d.innerHTML=md(text);else d.textContent=text;
  log.appendChild(d)}
// output boxes start folded to the same number of lines as the cardputer (tool output setting),
// with the command cut to one line: "▸ N more lines" / "▸ full command" unfolds it
function clip(d,t){
  const lim=(S.cfg&&S.cfg.tool_lines)||'3',base=lim=='all'?'none':(lim*1.4)+'em';
  t.style.maxHeight=base;
  const l=d.querySelector('legend'),longCmd=l&&l.scrollWidth>l.clientWidth+1,over=t.scrollHeight>t.clientHeight+2;
  if(!longCmd&&!over)return;
  const lh=parseFloat(getComputedStyle(t).lineHeight)||16;
  const n=over?Math.max(1,Math.round((t.scrollHeight-t.clientHeight)/lh)):0;
  if(longCmd){                                       // unfolded: the whole command inside the box
    const f=document.createElement('div');f.className='fullcmd';f.textContent=l.textContent;d.insertBefore(f,t)}
  const m=document.createElement('div');m.className='more';d.appendChild(m);d.classList.add('cut');
  const label=()=>{m.textContent=d.classList.contains('open')?'▾ show less':(n?'▸ '+n+' more line'+(n>1?'s':''):'▸ full command')};
  label();d.onclick=()=>{if(getSelection().toString())return;
    const o=d.classList.toggle('open');t.style.maxHeight=o?'none':base;label()}}
async function act(o){
  try{const r=await fetch('/api/act',{method:'POST',headers:{...H,'X-CP':'1','Content-Type':'application/json'},body:JSON.stringify(o)});
    if(r.status==401)location.reload()}catch(e){}}
function k(key){act({a:'key',k:key});setTimeout(refreshScreen,250)}
function typeKeys(){const t=$('kt').value;$('kt').value='';[...t].forEach(c=>act({a:'key',k:c}));setTimeout(refreshScreen,300)}
function send(){const t=inp.value.trim();if(!t)return;inp.value='';grow();
  if(S.ask&&askTyping){askTyping=false;act({a:'ask_answer',id:S.ask.id,qi:S.ask.qi,answer:t});return}
  act({a:'send',text:t})}
let askTyping=false;
inp.addEventListener('keydown',e=>{if(e.key=='Enter'&&!e.shiftKey&&!('ontouchstart' in window)){e.preventDefault();send()}});
function grow(){inp.style.height='auto';inp.style.height=Math.min(inp.scrollHeight,innerHeight*.4)+'px'}
inp.addEventListener('input',grow);
function nearBottom(){return log.scrollHeight-log.scrollTop-log.clientHeight<80}
let turn=null,turnAt=0,gi=0;
function fmtTok(n){return n>=1000?(n/1000).toFixed(1)+'k':n}
function tick(){
  const L=$('live');if(!turn){L.style.display='none';return}
  const stick=nearBottom();
  L.style.display='block';
  const secs=Math.floor(turn.secs+(Date.now()-turnAt)/1000);
  L.querySelector('.g').textContent='✻✽✶✳✢·'[gi++%6];
  L.querySelector('.ph').textContent=turn.phase.charAt(0).toUpperCase()+turn.phase.slice(1);
  L.querySelector('.d').textContent=`(${secs}s · ↓ ${fmtTok(turn.tokens)} tokens)`;
  if(stick)log.scrollTop=log.scrollHeight}
setInterval(tick,300);
function render(){
  const s=S;
  $('status').textContent=s.status||'';$('batt').textContent=s.batt||'';
  $('spin').style.display=s.busy?'block':'none';
  $('stop').style.display=s.busy?'':'none';
  if(s.turn){turn=s.turn;turnAt=Date.now()}else turn=null;
  tick();
  let h='';
  if(s.sudo){h+=`<div class=card><h3>${s.sudo.retry?'wrong password? try again':'Claude wants to run as root'}</h3>
   <div class=cmd>${esc(s.sudo.cmd)}</div><div class=row><input type=password id=sp placeholder="sudo password" style="flex:1;min-width:140px"
   onkeydown="if(event.key=='Enter')sudoOk()"><label style="font-size:13px;color:var(--dim)"><input type=checkbox id=sr ${s.sudo.remember?'checked':''}> remember for this chat</label></div>
   <div class=row style="margin-top:8px"><button class="b ok" onclick="sudoOk()">run it</button><button class="b no" onclick="act({a:'sudo',pid:${s.sudo.pid},pw:null})">refuse</button></div></div>`}
  if(s.pending){h+=`<div class=card><h3>allow ${esc(s.pending.name)}?</h3><div class=cmd>${esc(s.pending.desc)}</div>
   <div class=row><button class="b ok" onclick="perm('y')">allow</button><button class=b onclick="perm('a')">always (${esc(s.pending.name)})</button>
   <button class="b no" onclick="perm('n')">deny</button></div></div>`}
  if(s.ask){const a=s.ask;h+=`<div class=card><h3>${esc(a.header||'question')} ${a.n>1?`(${a.qi+1}/${a.n})`:''}</h3><div style="margin-bottom:6px">${esc(a.question)}</div>`+
   a.options.map((o,i)=>`<button class="b opt" data-i=${i} onclick="pick(${i})">${esc(o.label)}${o.description?`<small>${esc(o.description)}</small>`:''}</button>`).join('')+
   `<div class=row style="margin-top:6px">${a.multi?'<button class="b pri" onclick="pickDone()">ok</button>':''}
   <button class=b onclick="askTyping=true;inp.placeholder='type your answer…';inp.focus()">type my own</button>
   <button class="b no" onclick="act({a:'ask_skip',id:'${a.id}'})">skip</button></div></div>`}
  const cards=$('cards'),sp=$('sp'),pw=sp?sp.value:'',key=JSON.stringify([s.sudo,s.pending,s.ask]);
  if(cards.dataset.k!==key){cards.innerHTML=h;cards.dataset.k=key;picked=new Set();
    if($('sp')){$('sp').value=pw;$('sp').focus()}}
  if(!s.ask){askTyping=false;inp.placeholder='message Claude…'}
  if($('settings').classList.contains('show'))fillSettings();
  if($('chats').classList.contains('show')&&s.chats)fillChats();
}
let picked=new Set();
function pick(i){const a=S.ask;if(a.multi){picked.has(i)?picked.delete(i):picked.add(i);
  document.querySelectorAll('.opt').forEach(b=>b.classList.toggle('on',picked.has(+b.dataset.i)));return}
  act({a:'ask_answer',id:a.id,qi:a.qi,answer:a.options[i].label})}
function pickDone(){const a=S.ask;if(!picked.size)return;act({a:'ask_answer',id:a.id,qi:a.qi,answer:[...picked].sort().map(i=>a.options[i].label).join(', ')})}
function perm(x){act({a:'perm',id:S.pending.id,k:x})}
function sudoOk(){act({a:'sudo',pid:S.sudo.pid,pw:$('sp').value,remember:$('sr').checked});$('sp').value=''}
function openChats(){$('chats').classList.add('show');$('chatlist').textContent='loading…';act({a:'chats'})}
function fillChats(){$('chatlist').innerHTML=S.chats.map(c=>`<div class="li ${c.id==S.session?'cur':''}" onclick="act({a:'open',id:'${c.id}'});hide(chats)">
  <div>${esc(c.title)}</div><span>${esc(c.ago)}</span></div>`).join('')||'<p>no saved chats</p>'}
function openSettings(){$('settings').classList.add('show');fillSettings()}
function fillSettings(){const L=$('setlist');if(L.contains(document.activeElement))return;
  L.innerHTML=(S.settings||[]).map(([key,label,ch])=>`<div class=set><span>${esc(label)}</span>
  <select onchange="act({a:'set',key:'${key}',value:this.value})">${ch.map(c=>`<option ${S.cfg[key]==c?'selected':''}>${esc(c)}</option>`).join('')}</select></div>`).join('')}
function openScreen(){$('screen').classList.add('show');scr=1;refreshScreen()}
function refreshScreen(){if(!scr)return;$('scrimg').src='/screen.png?k='+encodeURIComponent(K)+'&t='+Date.now()}
setInterval(refreshScreen,1000);
async function poll(){
  for(;;){
    try{
      const r=await fetch(`/api/state?v=${v}&gen=${gen}&have=${have}`,{headers:H});
      if(r.status==401){location.reload();return}
      const j=await r.json();$('off').style.display='none';
      const stick=nearBottom();
      if(j.reset){log.innerHTML='';have=0}
      j.entries.forEach(e=>add(e[0],e[1]));have=j.start+j.entries.length;
      v=j.v;gen=j.gen;S=j.state;render();
      if(stick||j.reset)log.scrollTop=log.scrollHeight;
    }catch(e){$('off').style.display='block';await new Promise(r=>setTimeout(r,2000))}
  }}
poll();
</script></body></html>"""
