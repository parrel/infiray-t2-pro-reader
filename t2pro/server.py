"""MJPEG streaming server for the T2 Pro."""

import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np
from PIL import Image

from . import palettes
from .device import T2Pro

BOUNDARY = "t2proframe"

# Resampling filters offered for the integer upscale, worst to best.  LANCZOS
# is the default: at 3x it keeps edges crisp where BILINEAR/BICUBIC go soft,
# and its ringing is bounded by the uint8 clamp either side of an edge.
INTERPOLATORS = {"nearest": Image.NEAREST, "bilinear": Image.BILINEAR,
                 "bicubic": Image.BICUBIC, "lanczos": Image.LANCZOS}


class Broadcaster:
    """Renders each frame once and hands the JPEG to every connected client."""

    def __init__(self, camera, palette=palettes.DEFAULT, scale=2, quality=85,
                 floor=150.0, interp="lanczos"):
        self.camera = camera
        self.palette = palette
        self.scale = scale
        self.quality = quality
        self.lock_span = False          # freeze the contrast window across frames
        self.floor = floor              # narrowest autoscale window, raw counts
        self.interp = interp            # upscale filter, see INTERPOLATORS
        self._span = None
        self._jpeg = None
        self._seq = 0
        self._fps = 0.0
        self._raw_range = (0, 0)
        self._cond = threading.Condition()
        self._running = True
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        last = time.time()
        ema = None
        while self._running:
            frame = self.camera.read(timeout=2.0)
            if frame is None:
                continue
            if self.camera.busy:
                # Shutter is closed for the flat-field correction; that frame is
                # a flat grey field, not the scene.  Auto-FFC makes this happen
                # unattended, so hold the last good JPEG instead of blinking.
                continue
            corrected = self.camera.correct(frame.image)
            raw_range = frame.raw_range

            span = None
            if self.lock_span:
                if self._span is None:
                    self._span = (float(corrected.min()), float(corrected.max()))
                span = self._span
            else:
                self._span = None

            # Scale up the single-channel *intensity*, then look up the palette.
            # Interpolating palettised RGB instead would blend colours that are
            # not on the palette curve -- halfway between ironbow's purple and
            # orange is a grey that means no temperature at all -- and it costs
            # three channels of resampling to get a worse answer.
            gray = palettes.normalize(corrected, span=span, floor=self.floor)
            img = Image.fromarray(gray, "L")
            if self.scale != 1:
                img = img.resize((img.width * self.scale, img.height * self.scale),
                                 INTERPOLATORS.get(self.interp, Image.LANCZOS))
            img = Image.fromarray(palettes.apply(np.asarray(img), self.palette), "RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=self.quality)

            now = time.time()
            dt = now - last
            last = now
            if dt > 0:
                inst = 1.0 / dt
                ema = inst if ema is None else 0.9 * ema + 0.1 * inst
            with self._cond:
                self._jpeg = buf.getvalue()
                self._fps = ema or 0.0
                self._raw_range = raw_range
                self._seq += 1
                self._cond.notify_all()

    def wait(self, last_seq, timeout=5.0):
        with self._cond:
            if not self._cond.wait_for(lambda: self._seq != last_seq, timeout):
                return last_seq, None
            return self._seq, self._jpeg

    def snapshot(self):
        with self._cond:
            return self._jpeg

    def reset_span(self):
        """Drop the frozen contrast window, so the next frame re-measures it."""
        with self._cond:
            self._span = None

    @property
    def raw_range(self):
        """(min, max) raw counts of the most recently rendered frame."""
        with self._cond:
            return self._raw_range

    @property
    def fps(self):
        with self._cond:
            return self._fps

    def stop(self):
        self._running = False
        self._thread.join(timeout=2.0)


PAGE = """<!doctype html>
<meta charset="utf-8"><title>InfiRay T2 Pro</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;background:#0e0f12;color:#e8e8ea;font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
      display:flex;flex-direction:column;align-items:center;gap:16px;padding:24px}
 img{border-radius:8px;max-width:100%;box-shadow:0 6px 30px #0008}
 .row{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;max-width:760px}
 button{background:#22242b;color:#e8e8ea;border:1px solid #33363f;border-radius:6px;
        padding:7px 13px;cursor:pointer;font-size:13px}
 button:hover{background:#2c2f38}
 button.on{background:#3d6fe0;border-color:#3d6fe0;color:#fff}
 label{display:flex;align-items:center;gap:6px;color:#9a9ba3;font-size:13px}
 select{background:#22242b;color:#e8e8ea;border:1px solid #33363f;border-radius:6px;padding:6px}
 #bar{color:#9a9ba3;font-variant-numeric:tabular-nums}
</style>
<img id="v" src="/stream.mjpg" alt="thermal stream">
<div class="row" id="pal"></div>
<div class="row">
  <button onclick="post('/calibrate')">Calibrate (shutter + FFC)</button>
  <button onclick="post('/shutter')">Shutter only</button>
  <button id="lockbtn" onclick="toggleLock()">Lock contrast</button>
  <button id="autobtn" onclick="toggleAuto()">Auto FFC</button>
</div>
<div class="row">
  <button id="shadebtn" onclick="shading()">Learn shading (aim at something uniform)</button>
  <button onclick="post('/shading?clear=1')">Clear shading</button>
  <label>min contrast <input id="floor" type="range" min="0" max="600" step="10"
         oninput="fetch('/floor?counts='+this.value,{method:'POST'})"></label>
  <label>upscale <select id="in" onchange="fetch('/interp?name='+this.value,{method:'POST'})">
    <option>lanczos</option><option>bicubic</option><option>bilinear</option><option>nearest</option>
  </select></label>
</div>
<div id="bar">connecting…</div>
<script>
const pal = %%PALETTES%%;
let current = %%CURRENT%%, locked = false, auto = true;
const box = document.getElementById('pal');
pal.forEach(n => {
  const b = document.createElement('button');
  b.textContent = n; b.onclick = () => setPal(n);
  b.className = n === current ? 'on' : ''; b.dataset.n = n;
  box.appendChild(b);
});
function setPal(n){
  current = n;
  [...box.children].forEach(b => b.className = b.dataset.n === n ? 'on' : '');
  fetch('/palette?name=' + encodeURIComponent(n), {method:'POST'});
}
function post(u){ fetch(u, {method:'POST'}); }
function shading(){
  const b = document.getElementById('shadebtn');
  b.disabled = true; b.textContent = 'sampling…';
  fetch('/shading', {method:'POST'}).finally(() => {
    b.disabled = false; b.textContent = 'Learn shading (aim at something uniform)';
  });
}
function toggleLock(){
  locked = !locked;
  document.getElementById('lockbtn').className = locked ? 'on' : '';
  fetch('/lock?on=' + (locked ? 1 : 0), {method:'POST'});
}
function toggleAuto(){
  auto = !auto;
  fetch('/auto?on=' + (auto ? 1 : 0), {method:'POST'});
}
setInterval(async () => {
  try {
    const s = await (await fetch('/stats')).json();
    auto = s.auto;
    document.getElementById('autobtn').className = s.auto ? 'on' : '';
    document.getElementById('shadebtn').className = s.shading ? 'on' : '';
    const fl = document.getElementById('floor'), inp = document.getElementById('in');
    if (document.activeElement !== fl) fl.value = s.floor;
    if (document.activeElement !== inp) inp.value = s.interp;
    const ffc = s.auto
      ? ' · auto FFC in ' + Math.max(0, Math.round(s.due_in)) + 's (every ' +
        Math.round(s.interval) + 's, ' + s.count + ' so far)'
      : (s.calibrated ? ' · FFC on, auto off' : ' · FFC off (click Calibrate)');
    document.getElementById('bar').textContent =
      s.fps.toFixed(1) + ' fps · ' + s.completed + ' frames · ' + s.dropped + ' dropped · raw ' +
      s.raw_min + '–' + s.raw_max + ffc + (s.shading ? ' · shading on' : '');
  } catch(e){}
}, 1000);
</script>
"""


def make_handler(bc, camera):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                page = (PAGE
                        .replace("%%PALETTES%%", json.dumps(list(palettes.NAMES)))
                        .replace("%%CURRENT%%", json.dumps(bc.palette)))
                self._send(200, "text/html; charset=utf-8", page.encode())
            elif u.path == "/stream.mjpg":
                self._stream()
            elif u.path == "/snapshot.jpg":
                jpg = bc.snapshot()
                if jpg is None:
                    self._send(503, "text/plain", b"no frame yet")
                else:
                    self._send(200, "image/jpeg", jpg)
            elif u.path == "/stats":
                st = camera.stats
                lo, hi = bc.raw_range
                body = json.dumps({"fps": bc.fps, "palette": bc.palette,
                                   "calibrated": camera.reference is not None,
                                   "shading": camera.shading is not None,
                                   "floor": bc.floor, "interp": bc.interp,
                                   "raw_min": lo, "raw_max": hi, **st}).encode()
                self._send(200, "application/json", body)
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/shutter":
                camera.shutter()
                self._send(200, "application/json", b'{"ok":true}')
            elif u.path == "/calibrate":
                camera.calibrate()
                camera.auto.reset()
                bc.reset_span()
                self._send(200, "application/json", b'{"ok":true}')
            elif u.path == "/palette":
                name = (q.get("name") or [""])[0]
                if name in palettes.NAMES:
                    bc.palette = name
                    bc.reset_span()
                    self._send(200, "application/json", b'{"ok":true}')
                else:
                    self._send(400, "application/json", b'{"ok":false}')
            elif u.path == "/shading":
                if (q.get("clear") or ["0"])[0] == "1":
                    camera.shading = None
                    self._send(200, "application/json", b'{"ok":true,"shading":false}')
                    return
                try:
                    camera.capture_shading()
                except RuntimeError as exc:
                    self._send(409, "application/json",
                               json.dumps({"ok": False, "error": str(exc)}).encode())
                    return
                bc.reset_span()
                self._send(200, "application/json", b'{"ok":true,"shading":true}')
            elif u.path == "/floor":
                try:
                    bc.floor = max(0.0, float((q.get("counts") or ["0"])[0]))
                except ValueError:
                    self._send(400, "application/json", b'{"ok":false}')
                    return
                self._send(200, "application/json", b'{"ok":true}')
            elif u.path == "/interp":
                name = (q.get("name") or [""])[0]
                if name not in INTERPOLATORS:
                    self._send(400, "application/json", b'{"ok":false}')
                    return
                bc.interp = name
                self._send(200, "application/json", b'{"ok":true}')
            elif u.path == "/lock":
                bc.lock_span = (q.get("on") or ["0"])[0] == "1"
                bc.reset_span()
                self._send(200, "application/json", b'{"ok":true}')
            elif u.path == "/auto":
                if (q.get("on") or ["0"])[0] == "1":
                    camera.auto.reset()
                    camera.auto.start()
                else:
                    camera.auto.stop()
                self._send(200, "application/json", b'{"ok":true}')
            else:
                self._send(404, "text/plain", b"not found")

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type",
                             f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seq = -1
            try:
                while True:
                    seq, jpg = bc.wait(seq, timeout=5.0)
                    if jpg is None:
                        continue
                    self.wfile.write(
                        b"--" + BOUNDARY.encode() + b"\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n"
                        + jpg + b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def serve(host="127.0.0.1", port=8420, palette=palettes.DEFAULT, scale=2, quality=85,
          floor=150.0, interp="lanczos"):
    camera = T2Pro()
    camera.read(timeout=3.0)
    print("calibrating (the shutter will click)…")
    deadline = time.time() + 5.0
    while camera.reference is None and time.time() < deadline:
        time.sleep(0.1)
    if camera.reference is None:
        print("  first calibration did not complete; auto-FFC will retry")
    bc = Broadcaster(camera, palette=palette, scale=scale, quality=quality,
                     floor=floor, interp=interp)
    httpd = ThreadingHTTPServer((host, port), make_handler(bc, camera))
    httpd.daemon_threads = True
    print(f"T2 Pro streaming on http://{host}:{port}/")
    print(f"  MJPEG   http://{host}:{port}/stream.mjpg")
    print(f"  still   http://{host}:{port}/snapshot.jpg")
    print("Ctrl-C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        httpd.shutdown()
        bc.stop()
        camera.close()
