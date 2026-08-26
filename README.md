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
and bind WinUSB to the InfiRay interface with [Zadig](https://zadig.akeo.ie/). (Not tested)

On Linux, grant access with a udev rule and replug:

    echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="04b4", ATTR{idProduct}=="000a", MODE="0666", TAG+="uaccess"' \
      | sudo tee /etc/udev/rules.d/99-infiray-t2pro.rules
    sudo udevadm control --reload-rules && sudo udevadm trigger

## Library

Depends on numpy and libusb only.

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

A proof-of-concept viewer built on the library, in `t2pro/server.py`.

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

## Residual shading

Point the camera at a wall and the picture is a smooth blotchy pattern crawling
with grain. That is not a shutter problem: the shutter sits behind the lens, so
lens shading, narcissus and the barrel's own thermal gradient are identical
open or closed and survive the FFC untouched. On a flat scene the autoscaler
then stretches the palette across that pattern, which is what makes the noise
look like grain.

Two fixes, both on by default:

- **`cam.capture_shading()`** (the *Learn shading* button, `POST /shading`).
  Aim at something uniform — cap on, or a blank wall — and it averages 40
  corrected frames, keeps only the low spatial frequencies, and subtracts that
  from every later frame. Learn it once; recapture only after a lens change or
  a big ambient shift. `POST /shading?clear=1` drops it.
- **A contrast floor** (`--floor`, default 150 counts). The autoscaler will not
  open a window narrower than this, so a scene with no real thermal contrast
  renders flat instead of being amplified to full palette.

## Sharpness

192x256 is the real sensor resolution, so all that can be done is to resample
well. The intensity is upscaled first and the palette looked up afterwards, so
every output pixel is an exact palette entry rather than a blend of two.
`--interp` picks the filter (default `lanczos`, which keeps edges crisper than
bicubic or bilinear).

There is deliberately no unsharp mask: with no sub-pixel detail to recover it
would only lift the per-pixel noise, which reads as grain rather than sharpness.

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
