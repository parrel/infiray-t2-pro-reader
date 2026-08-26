"""Frame reassembly and control for the T2 Pro."""

import threading
import time

import numpy as np

from . import protocol as P
from .libusb_async import AsyncBulkReader


def _box_blur(a, radius):
    """Separable box blur with edge clamping.  numpy only, no scipy."""
    out = a
    for axis in (0, 1):
        n = out.shape[axis]
        pad = [(0, 0), (0, 0)]
        pad[axis] = (radius, radius)
        padded = np.pad(out, pad, mode="edge")
        cs = np.cumsum(padded, axis=axis, dtype=np.float64)
        zero = np.zeros_like(np.take(cs, [0], axis=axis))
        cs = np.concatenate([zero, cs], axis=axis)
        hi = np.take(cs, np.arange(2 * radius + 1, 2 * radius + 1 + n), axis=axis)
        lo = np.take(cs, np.arange(0, n), axis=axis)
        out = (hi - lo) / (2 * radius + 1)
    return out.astype(np.float32)


class Frame:
    """One decoded 196-row frame."""

    __slots__ = ("channel", "raw", "timestamp")

    def __init__(self, raw, channel, timestamp):
        self.raw = raw                # uint16 view, shape (196, 256)
        self.channel = channel
        self.timestamp = timestamp

    @property
    def image(self):
        """The 192 image rows, uint16, shape (192, 256)."""
        return self.raw[:P.IMAGE_ROWS]

    @property
    def metadata(self):
        """The 4 trailing metadata rows, uint16, shape (4, 256)."""
        return self.raw[P.IMAGE_ROWS:]

    @property
    def raw_range(self):
        """(min, max) raw counts the device reports for this frame, from row 192."""
        row = self.raw[P.IMAGE_ROWS]
        return int(row[0]), int(row[1])

    @property
    def identity(self):
        """(serial, model) strings from metadata row 194."""
        blob = self.raw[P.IMAGE_ROWS + 2].tobytes()
        parts = [chunk.decode("ascii", "replace") for chunk in blob.split(b"\x00") if chunk]
        serial = parts[0] if parts else ""
        model = parts[1] if len(parts) > 1 else ""
        return serial, model


class AutoFFC:
    """Fires flat-field corrections on a tapering schedule.

    The sensor is uncooled, so its fixed-pattern noise drifts as the body warms
    up and the reference goes stale.  This runs a shutter on a schedule that
    starts tight and backs off, which is what the vendor app appears to do:
    frequent corrections right after power-on, rare ones once warm.

    Why a schedule and not a trigger
    --------------------------------
    Measured on this hardware, there is no usable "is a correction needed yet?"
    signal, so nothing here is condition-triggered:

    * **No temperature register.**  The UVC variants of this sensor family
      report a shutter and a core temperature in their metadata rows (see
      IR-Py-Thermal, which decodes them), and that would be the right trigger.
      This MFi variant does not: across two cold plug-ins, the only metadata
      words that move at all are row 192 words 0 and 1 -- which track the frame
      mean and max, i.e. the scene -- and word 16, which only dithers between
      15816 and 15818.  Rows 193-195 are static calibration blobs.

    * **No image-derived proxy survives the scene.**  The frame mean tracks
      drift well on a static scene (reference RMS drift ~ 0.43 counts per count
      of mean change), but ordinary scene motion moves it 823 counts -- far more
      than any drift.  The even/odd column parity offset is 7x quieter but moves
      7.2 counts with the scene against only 1.9 counts of warm-up travel, and
      regressing out its scene dependence still leaves an SNR of 1.5.

    * **Graininess does not grow the way it looks like it should.**  Nearly all
      the drift is a uniform DC shift (-27.98 counts of a 28.46 RMS move over
      the first 20 s), and `T2Pro.correct` already cancels DC by adding
      `reference.mean()` back.  What survives is the *structured* part, and that
      saturates: 5.2 counts RMS after 20 s, 7.0 after 40 s, then flat out to
      five minutes.  It is also low spatial frequency, which is exactly where
      scene content lives -- there is no high-frequency "grain" to threshold on.

    Hence the intervals below: dense over the first minute, where the structured
    error actually accrues, then doubling out to `max_interval` as insurance
    against slow ambient changes, which are real but undetectable from here.
    """

    def __init__(self, camera, first_interval=8.0, max_interval=300.0, growth=2.0):
        self.camera = camera
        self.first_interval = first_interval
        self.max_interval = max_interval
        self.growth = growth
        self.count = 0
        self.last_ffc = None        # None => never corrected, so one is due now
        self._interval = first_interval
        self._running = False
        self._thread = None

    # -- state ---------------------------------------------------------------
    @property
    def due_in(self):
        """Seconds until the next correction; negative once overdue."""
        if self.last_ffc is None:
            return 0.0
        return self.last_ffc + self._interval - time.time()

    @property
    def stats(self):
        return {"auto": self._running, "due_in": round(self.due_in, 1),
                "interval": round(self._interval, 1), "count": self.count}

    # -- action --------------------------------------------------------------
    def fire(self):
        """Run a correction and back the schedule off one step."""
        self.camera.calibrate()
        self.last_ffc = time.time()
        self.count += 1
        # The very first correction just gets the camera calibrated at all; the
        # taper should start counting from the one after it, so that the early
        # intervals still land where the drift is (5.2 counts RMS of structure
        # by 20 s, 7.0 by 40 s).
        if self.count > 1:
            self._interval = min(self.max_interval, self._interval * self.growth)

    def reset(self):
        """Re-arm the tapering schedule from the start.

        Worth calling if the camera's thermal environment changes -- taken
        outdoors, say -- since that restarts the drift we cannot observe.
        """
        self._interval = self.first_interval
        self.last_ffc = time.time()

    # -- background loop -----------------------------------------------------
    def _loop(self):
        while self._running:
            # Wake often enough to be responsive to stop(), but the decision is
            # purely "has the interval elapsed".
            if self.camera.read(timeout=1.0) is None:
                continue
            if self._running and not self.camera.busy and self.due_in <= 0:
                try:
                    self.fire()
                except (OSError, RuntimeError, TimeoutError):
                    # A failed correction must not kill the loop; re-arm and let
                    # the next pass try again.
                    self.last_ffc = time.time()

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None


class T2Pro:
    """Streams frames from the camera and sends it commands.

    Use as a context manager::

        with T2Pro() as cam:
            for frame in cam.frames():
                ...

    A background `AutoFFC` keeps the flat-field reference fresh on a tapering
    schedule, so the first frames are already corrected and stay that way.  Pass
    `auto_ffc=False` to take over shutter timing yourself; the controller is
    still on `.auto`, so `cam.auto.start()` turns it back on.
    """

    def __init__(self, vid=P.VENDOR_ID, pid=P.PRODUCT_ID, queue_depth=64,
                 auto_ffc=True):
        self._partial = {ch: {} for ch in P.CHANNELS}
        self._latest = None
        self._lock = threading.Lock()
        self._event = threading.Condition(self._lock)
        self._seq = 0
        self._dropped = 0
        self._completed = 0
        self._primed = False
        self._busy = False
        self.reference = None
        self.shading = None
        self.auto = AutoFFC(self)
        self._reader = AsyncBulkReader(
            vid, pid, P.INTERFACE, P.ALT_SETTING, P.EP_IN,
            self._on_data, depth=queue_depth, chunk=16384,
        )
        if auto_ffc:
            self.auto.start()

    # -- ingest -------------------------------------------------------------
    def _on_data(self, data):
        parsed = P.parse_header(data)
        if parsed is None:
            return
        channel, row = parsed
        rows = self._partial[channel]
        if row == 1:
            if len(rows) == P.FRAME_ROWS:
                self._emit(channel, rows)
            elif rows:
                self._dropped += 1
            rows = self._partial[channel] = {}
        rows[row] = data[P.HEADER_LEN:P.MESSAGE_LEN]

    def _emit(self, channel, rows):
        buf = b"".join(rows[i] for i in range(1, P.FRAME_ROWS + 1))
        raw = np.frombuffer(buf, dtype="<u2").reshape(P.FRAME_ROWS, P.WIDTH)
        with self._event:
            self._latest = Frame(raw, channel, time.time())
            self._completed += 1
            self._seq += 1
            self._event.notify_all()

    # -- output -------------------------------------------------------------
    def read(self, timeout=2.0):
        """Block until the next frame arrives. Returns a Frame, or None on timeout."""
        with self._event:
            start = self._seq
            if not self._event.wait_for(lambda: self._seq != start, timeout):
                return None
            return self._latest

    def frames(self, timeout=2.0):
        """Yield frames as they arrive."""
        while True:
            frame = self.read(timeout)
            if frame is None:
                raise TimeoutError(f"no frame within {timeout:.1f}s")
            yield frame

    @property
    def busy(self):
        """True while the shutter is closed, so frames are not of the scene."""
        return self._busy

    @property
    def stats(self):
        return {"completed": self._completed, "dropped": self._dropped,
                **self.auto.stats}

    # -- control ------------------------------------------------------------
    def _prime(self):
        """The device swallows the first bulk-OUT write of a session.

        Nothing observable happens for that write -- not even a shutter when the
        command is 0x8000 -- so spend it deliberately once and every subsequent
        command lands.  Verified: without this the first shutter() of a session
        is silently lost no matter how long we wait beforehand.
        """
        if self._primed:
            return
        if self._latest is None:
            self.read(timeout=2.0)
        self._reader.bulk_write(P.EP_OUT, P.command(P.CMD_SHUTTER))
        self._primed = True
        time.sleep(0.05)

    def send(self, word):
        """Send a 16-bit command word (high byte register, low byte parameter)."""
        self._prime()
        self._reader.bulk_write(P.EP_OUT, P.command(word))

    def shutter(self):
        """Fire a flat-field correction. The shutter is audible."""
        self._prime()
        self._reader.bulk_write(P.EP_OUT, P.command(P.CMD_SHUTTER))

    # -- flat-field correction ----------------------------------------------
    def calibrate(self, settle=0.30, samples=12, reopen=0.7):
        """Fire the shutter and average the closed-shutter frames into a reference.

        The stream is raw and uncorrected: odd and even columns are read out by
        different channels with a fixed offset between them (~1950 counts), on
        top of ordinary per-pixel fixed-pattern noise.  With the shutter closed
        the sensor sees a uniform field, so whatever structure remains *is* the
        fixed-pattern noise and can simply be subtracted.

        The shutter stays closed for roughly 950 ms, so sampling from `settle`
        to `settle + samples/25 s` lands safely inside that window.
        """
        self._busy = True
        try:
            self.shutter()
            time.sleep(settle)
            acc = None
            taken = 0
            deadline = time.time() + 0.6
            while taken < samples and time.time() < deadline:
                frame = self.read(timeout=1.0)
                if frame is None:
                    break
                values = frame.image.astype(np.float32)
                acc = values if acc is None else acc + values
                taken += 1
            if not taken:
                raise RuntimeError("no frames captured during shutter-closed window")
            self.reference = acc / taken
            time.sleep(reopen)
        finally:
            self._busy = False
        return self.reference

    def correct(self, image):
        """Apply the flat-field reference. Returns float32; a no-op if uncalibrated."""
        if self.reference is None:
            return image if self.shading is None else image - self.shading
        out = image.astype(np.float32) - self.reference + float(self.reference.mean())
        if self.shading is not None:
            out -= self.shading
        return out

    def capture_shading(self, samples=40, radius=6, timeout=1.0):
        """Learn the residual shading left over *after* flat-field correction.

        Point the camera at something uniform -- the lens cap on, or a blank
        wall -- and call this.  It averages `samples` corrected frames, keeps
        only the low spatial frequencies, and stores the result on `.shading`
        so `correct()` subtracts it from every later frame.

        Why this is a second stage and not part of `calibrate()`
        -------------------------------------------------------
        The shutter sits *behind* the lens, so a closed-shutter reference sees
        only the sensor.  Anything the optics and housing contribute -- lens
        shading, narcissus, the barrel's own thermal gradient -- is identical
        with the shutter open and closed, so subtracting the reference leaves
        it completely intact.  Measured here after a fresh FFC, on a uniform
        scene: 16.5 counts RMS of residual, 86 counts peak-to-peak, and it is
        smooth -- take an 8x8 local mean away and only 2.1 counts survive,
        which is the temporal noise floor (2.5 counts RMS).  So the shutter is
        doing its job on the sensor; what is left is in front of it.

        That residual is why a flat scene looks patterned.  It is also almost
        all of the frame's dynamic range when there is no real scene, so the
        autoscaler stretches it to full contrast (see `palettes.normalize`).

        Blurring at `radius` is what keeps this honest: it only ever removes
        structure broader than ~13 pixels, so it cannot eat the per-pixel
        fixed-pattern noise `calibrate()` is responsible for, and a mistaken
        capture over a non-uniform scene costs a soft gradient rather than a
        burned-in ghost.  Set `.shading = None` to drop it.
        """
        if self.reference is None:
            raise RuntimeError("calibrate() before capturing a shading profile")
        acc = None
        taken = 0
        while taken < samples:
            frame = self.read(timeout=timeout)
            if frame is None:
                break
            if self.busy:
                continue
            values = self.correct(frame.image)
            acc = values if acc is None else acc + values
            taken += 1
        if not taken:
            raise RuntimeError("no frames captured for the shading profile")
        flat = acc / taken
        profile = _box_blur(flat - flat.mean(), radius)
        # Compose with any profile already in force, since `correct()` above
        # already subtracted it -- otherwise a second capture would only ever
        # measure the leftovers of the first.
        self.shading = profile if self.shading is None else self.shading + profile
        return self.shading

    # -- lifecycle ----------------------------------------------------------
    def close(self):
        self.auto.stop()
        self._reader.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
