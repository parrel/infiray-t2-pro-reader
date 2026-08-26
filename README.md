# InfiRay T2 Pro (iOS/MFi) — thermal reader

Python library for the **iOS/Lightning variant** of the InfiRay T2 Pro thermal
camera (USB `04b4:000a`), plus a small MJPEG server that streams it to a
browser with palette and shutter controls.

This is **not** the Android/USB-C variant. That one enumerates as a UVC camera
(`04b4:0100`) and works with ffmpeg, OpenCV and existing projects like
P2Pro-Viewer. This one exposes no video interface at all, so none of those tools
can see it — hence this reader.

Everything here was developed against the iOS/Lightning camera plugged into a
computer through InfiRay's own Lightning-to-USB-C converter. The converter is
passive as far as the protocol is concerned — the camera still enumerates as
`04b4:000a` with the same interfaces and the same framing — so it changes
nothing below, but it is what makes the iOS variant reachable from a desktop at
all.

## Install

    pip install .
    brew install libusb            # macOS; apt install libusb-1.0-0 on Debian/Ubuntu

On Windows, install the optional prebuilt libusb with `pip install .[libusb]`
and bind WinUSB to the InfiRay interface with [Zadig](https://zadig.akeo.ie/).
On Linux, grant access with a udev rule and replug:

    echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="04b4", ATTR{idProduct}=="000a", MODE="0666", TAG+="uaccess"' \
      | sudo tee /etc/udev/rules.d/99-infiray-t2pro.rules
    sudo udevadm control --reload-rules && sudo udevadm trigger

## Library

The library is the point of this project; it depends on numpy and libusb only,
and never touches HTTP.

```python
from t2pro import T2Pro, palettes

with T2Pro() as cam:                                 # auto-FFC is on by default
    for frame in cam.frames():
        image = cam.correct(frame.image)             # float32, 192x256
        rgb   = palettes.render(image, "ironbow")    # uint8, 192x256x3
        ...
```

`Frame` also exposes `.raw` (196x256 uint16), `.metadata` (the 4 trailing rows),
`.raw_range` and `.identity` (`('LJ1329', 'T2Pro')`). Call `cam.capture_shading()`
once over a uniform scene to also cancel the residual lens shading that the
shutter cannot reach — see below. `cam.send(word)` writes a raw 16-bit command
word, which is how the unverified registers can be probed.

    examples/save_palettes.py    library only: one frame, saved in every palette
    examples/stream_server.py    the server, driven from Python

## Server

A proof-of-concept viewer built on the library. `t2pro/server.py` is the only
module that knows about HTTP, and nothing in the library imports it.

    t2pro                          # or: python -m t2pro

Then open <http://127.0.0.1:8420/>.

    --host / --port     bind address (default 127.0.0.1:8420)
    --palette NAME      initial palette (default ironbow)
    --scale N           integer upscale factor (default 2)
    --quality N         JPEG quality (default 85)
    --floor N           narrowest autoscale window in raw counts (default 150)
    --interp NAME       upscale filter: lanczos/bicubic/bilinear/nearest

Endpoints:

| path             | method | purpose                                     |
|------------------|--------|---------------------------------------------|
| `/`              | GET    | viewer page with palette + shutter controls |
| `/stream.mjpg`   | GET    | MJPEG stream (`multipart/x-mixed-replace`)  |
| `/snapshot.jpg`  | GET    | latest frame as JPEG                        |
| `/stats`         | GET    | fps, frame counts, calibration state        |
| `/calibrate`     | POST   | fire shutter and rebuild the FFC reference  |
| `/shutter`       | POST   | fire the shutter only                       |
| `/auto?on=0\|1`  | POST   | enable/disable automatic FFC                |
| `/palette?name=` | POST   | switch palette                              |
| `/lock?on=0\|1`  | POST   | freeze the contrast window                  |
| `/shading`       | POST   | learn the residual shading (`?clear=1` drops)|
| `/floor?counts=` | POST   | narrowest autoscale window, raw counts      |
| `/interp?name=`  | POST   | upscale filter                              |

There is no authentication; the default bind is localhost, and it should stay
that way on an untrusted network.

Palettes: `white-hot`, `black-hot`, `ironbow`, `lava`, `arctic`, `rainbow`,
`medical`, `amber`, `sepia`, `green-hot`. Add one by listing colour stops in
`t2pro/palettes.py`.

## Automatic flat-field correction

The reference goes stale as the camera warms up, so `T2Pro` runs an `AutoFFC`
controller in the background that fires the shutter on a **tapering schedule** —
immediately, then 8 s, 16 s, 32 s, 64 s … out to a 5-minute steady state. That
mirrors what the vendor app does: frequent corrections right after power-on,
rare ones once warm.

```python
with T2Pro(auto_ffc=False) as cam:   # take over the timing yourself
    cam.calibrate()

cam.auto.stats     # {'auto': True, 'due_in': 41.3, 'interval': 64.0, 'count': 3}
cam.auto.reset()   # re-arm the taper, e.g. after a change of environment
cam.auto.stop()    # / .start()
cam.busy           # True while the shutter is closed
```

### Why it is a schedule and not a trigger

Firing only when a correction is *needed* would be better, but measured on this
hardware there is no signal to trigger on:

- **No temperature register.** The UVC variants of this sensor family report a
  shutter and a core temperature in their metadata rows, which is the natural
  trigger. This MFi variant does not. Across two cold plug-ins the only metadata
  words that move are row 192 words 0 and 1 — the frame mean and max, i.e. the
  scene — plus word 16, which dithers between 15816 and 15818. Rows 193-195 are
  static calibration blobs.
- **No image proxy survives the scene.** Frame mean tracks drift well on a
  *static* scene (0.43 counts of reference drift per count of mean change), but
  ordinary scene motion moves it 823 counts — far more than any drift. Column
  parity is 7x quieter, yet still moves 7.2 counts with the scene against 1.9
  counts of total warm-up travel; regressing out its scene dependence leaves an
  SNR of 1.5.
- **There is no "grain" to threshold on.** Nearly all the drift is a uniform DC
  shift (-27.98 counts of a 28.46 RMS move over the first 20 s), and `correct()`
  already cancels DC by adding `reference.mean()` back. The *structured* part
  that actually degrades the image saturates — 5.2 counts RMS after 20 s, 7.0
  after 40 s, then flat out to five minutes — and it is low spatial frequency,
  exactly where scene content lives.

The schedule is therefore dense over the first minute, where that structured
error accrues, and backs off afterwards.

## Why a flat scene looks patterned and grainy

Point the camera at a wall, or put the cap on, and the picture is a smooth
blotchy pattern crawling with grain. Neither is a shutter problem — measured
on hardware right after a fresh FFC:

| component                                | counts RMS |
|------------------------------------------|-----------:|
| temporal (frame-to-frame) noise           |       2.5  |
| high-frequency fixed pattern, post-FFC    |       2.1  |
| **smooth residual shading, post-FFC**     |   **16.5** |

So the shutter is doing its job: subtract an 8x8 local mean from the corrected
frame and only 2.1 counts survive, which is the noise floor. The FFC also
repeats — two closed-shutter references taken a minute apart differ by 2.4
counts RMS, again just noise. And it is not a gain error either; regressing the
residual on the reference explains 1% of its variance.

What is left is **in front of the shutter**. The shutter sits behind the lens,
so a closed-shutter reference only ever sees the sensor; lens shading,
narcissus and the barrel's own thermal gradient are identical with the shutter
open or closed and survive the subtraction untouched. That residual is 86
counts peak to peak, smooth, and stable (correlation 0.99 across captures
minutes apart).

The grain is then a **rendering** effect on top of it. `normalize()` autoscales
each frame to its own 1st-99th percentiles, and on a flat scene that window is
only 67.6 counts wide — almost all of it the shading. So the palette gets
stretched across the pattern, and 2.5 counts of noise land 9.5 grey levels
apart. Real scenes span hundreds to thousands of counts and never show this.

Two fixes, both on by default:

- **`cam.capture_shading()`** (the *Learn shading* button, `POST /shading`).
  Aim at something uniform — cap on, or a blank wall — and it averages 40
  corrected frames, keeps only spatial frequencies broader than ~13 px, and
  subtracts that from every later frame. Measured: fixed pattern 22.2 → 1.63
  counts RMS, below the noise floor. Blurring is what keeps it safe: it cannot
  eat per-pixel FPN, and a capture over a non-uniform scene costs a soft
  gradient rather than a burned-in ghost. `POST /shading?clear=1` drops it.
- **A contrast floor** (`--floor`, default 150 counts). The autoscaler will not
  open a window narrower than this, so a scene with no real thermal contrast
  renders flat instead of being amplified to full palette. This matters *more*
  once shading correction is on: it drops the flat-scene span to 13.9 counts,
  which without a floor would amplify the noise five times harder than before.

### Does the shading profile survive later FFCs?

Yes — and the FFCs help rather than hurt. Measured on a thermally settled
camera, residual RMS on a uniform scene after learning a profile:

| arm                                  | residual over the run |
|--------------------------------------|-----------------------|
| A: no FFC at all, 180 s              | 1.7 → 4.6 counts      |
| B: six FFCs ~5 s apart               | 4.8 → 2.1 counts      |
| C: quiet 90 s after B, then one FFC  | 2.6 → 8.6, then 3.8   |

Arm A rules out the FFC as the cause: with no shutter events at all the
residual still climbs, so what drifts is elapsed time. Arm B shows the shutter
*correcting* that drift back down, and arm C shows it returning the moment the
corrections stop and collapsing again on the next one. This is the ordinary
sensor-offset drift `AutoFFC` exists to chase, riding on top of a shading
profile that is not itself moving.

The profile stays valid across all of it. Re-learning after a burst of six
FFCs produces a profile differing from the original by only 4.9 counts RMS out
of 16.6 — about 91% of the correction unchanged — and that 4.9 is the same
size as the between-FFC drift, i.e. it is the drift being absorbed rather than
the shading having shifted. So learn it once and leave it; keep auto-FFC on and
the residual sits at 2-4 counts, near the 2.5-count noise floor. Recapture only
after a lens change or a big ambient shift.

## Sharpness

192x256 is the real sensor resolution, so the only thing that can be done for
apparent sharpness is to resample well:

- **Upscale the intensity, then look up the palette.** Interpolating palettised
  colour blends entries that are not on the palette curve — halfway between
  ironbow's purple and orange is a grey that means no temperature at all — and
  costs three channels of resampling to get a worse answer. Normalising to
  8-bit intensity first, resizing that, and applying the LUT last keeps every
  output pixel an exact palette entry.
- **A real resampling filter** (`--interp`, default `lanczos`). NEAREST is what
  makes an upscaled thermal image look blocky. LANCZOS keeps edges crisp where
  BILINEAR and BICUBIC go soft, and its ringing is bounded by the uint8 clamp
  either side of an edge.

There is deliberately **no unsharp mask**. Applied in raw counts before the
palette it is in the right place, but with no sub-pixel detail to recover all
it reliably does is lift the 2.5 counts RMS of per-pixel noise, which reads as
grain rather than sharpness.

## Protocol

Reverse-engineered against the hardware; `t2pro/protocol.py` is the reference.

**Enumeration.** Interface 0, `com.infiRay.XthermProtocol`, class `0xFF` /
subclass `0xF0`. Alt-setting 0 has no endpoints; **alt-setting 1** exposes bulk
IN `0x82` and bulk OUT `0x04`. Interface 1 is the iAP/MFi interface, unused
here — leave it alone.

**Framing.** Every USB transfer is one 524-byte message: a 12-byte header plus
512 bytes of payload. Wire packetization splits it 512 + 12, so the final
12 payload bytes arrive as a short packet that also terminates the transfer.
Reading only the first 512 bytes silently truncates every row — that was the
single most misleading detail in working this out.

    off  size  field
    0    1     0x0c, header length
    1    1     channel: 0x8c or 0x8d
    2    4     device timestamp, little-endian
    6    2     0x0001, constant
    8    4     row index within frame, little-endian, 1..196

**Geometry.** Each message carries one row of 256 uint16 (little-endian). A
frame is 196 rows: 192 image rows plus 4 metadata rows. Row 192 holds the frame
raw min/max; row 194 holds ASCII `"<serial>\0T2Pro"`. The device alternates
channels `0x8c` and `0x8d` at 12.5 Hz each, giving the rated **25 Hz**.

**Control.** Commands are a bare 2-byte **big-endian** word written to bulk OUT
`0x04`: high byte = register, low byte = parameter. This is the same command
word the UVC variant tunnels through its Zoom (Absolute) control. `0x8000`
fires the shutter — the only word verified here. Four more parameters of
register `0x80` are documented in `protocol.py`, taken from
[IR-Py-Thermal](https://github.com/diminDDL/IR-Py-Thermal) which drives the UVC
variants: `0x8004` raw mode, `0x8020`/`0x8021` temperature range, `0x80FF` save
parameters. **None are verified here**, and `0x80FF` writes to flash. The rest
of the register map — firmware palettes, gain mode, temperature units — is
unknown, as is the radiometric conversion from raw counts to °C, so this
library reports raw counts throughout. Note that `bRequest=0xA0` on the control
endpoint is the Cypress EZ-USB firmware RAM interface; writing to it can halt
the device.

Two quirks worth knowing:

- **The first bulk-OUT write of a session is silently swallowed.** Not delayed —
  discarded, no matter how long you wait first. `T2Pro` spends one priming write
  on open so every later command lands.
- **The shutter stays closed for ~950 ms**, which makes averaging a clean
  reference easy.

**The stream is raw and uncorrected.** Odd and even columns are read out by
different channels with a fixed ~1950-count offset between them, on top of
ordinary per-pixel fixed-pattern noise — so an uncorrected frame looks like
vertical bars, not a picture. `calibrate()` fires the shutter, averages the
closed-shutter frames, and subtracts that reference. Measured effect on a
frame: spatial std 1086 → 17.7, column-to-column difference 1948 → 3.2.

## Throughput

pyusb's synchronous reads deliver only ~30% of frames intact: the device ends
nearly every transfer with a short packet, so each `read()` returns a single row
and whatever arrives while Python is between calls is lost.
`t2pro/libusb_async.py` is a small ctypes binding to libusb's async API that
keeps 64 transfers queued on the endpoint; that yields ~97% complete frames at a
steady 25 fps.

## License

MIT — see [LICENSE](LICENSE).
