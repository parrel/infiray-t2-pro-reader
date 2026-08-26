"""Minimal ctypes binding to libusb's asynchronous bulk API.

pyusb only exposes synchronous transfers.  The T2 Pro terminates almost every
bulk transfer with a 12-byte short packet, so a synchronous reader gets one
~524-byte packet per read() and drops whatever the device sends while Python is
between calls -- in practice ~70% of frames arrive incomplete.  Keeping a ring
of transfers permanently queued on the endpoint fixes that.
"""

import contextlib
import ctypes
import ctypes.util
import sys
import threading


class LibusbNotFound(OSError):
    """libusb-1.0 could not be located."""


_HINTS = {
    "darwin": "brew install libusb",
    "linux": "sudo apt install libusb-1.0-0   (or your distro's equivalent)",
    "win32": "pip install libusb-package, or drop libusb-1.0.dll beside python.exe",
}


def _candidates():
    """Library names/paths to try, most portable first."""
    found = ctypes.util.find_library("usb-1.0") or ctypes.util.find_library("usb")
    if found:
        yield found
    # libusb-package ships prebuilt binaries for Windows/macOS/Linux.
    try:
        import libusb_package
        path = libusb_package.get_library_path()
    except (ImportError, AttributeError, OSError):
        path = None          # optional dependency; absence is normal
    if path:
        yield str(path)
    if sys.platform == "darwin":
        yield "/opt/homebrew/lib/libusb-1.0.dylib"   # Apple silicon
        yield "/usr/local/lib/libusb-1.0.dylib"      # Intel
    elif sys.platform == "win32":
        yield "libusb-1.0.dll"
    else:
        yield "libusb-1.0.so.0"
        yield "/usr/lib/aarch64-linux-gnu/libusb-1.0.so.0"
        yield "/usr/lib/arm-linux-gnueabihf/libusb-1.0.so.0"
        yield "/usr/lib/x86_64-linux-gnu/libusb-1.0.so.0"


def _load():
    tried = []
    for cand in _candidates():
        try:
            return ctypes.CDLL(cand)
        except OSError:
            tried.append(cand)
    hint = _HINTS.get(sys.platform, "install libusb-1.0")
    raise LibusbNotFound(
        "libusb-1.0 not found. Install it with: " + hint
        + (f"\nTried: {', '.join(tried)}" if tried else "")
    )


_lib = None


def _ensure():
    """Load libusb and configure prototypes on first use.

    Deliberately lazy: importing t2pro must work on a machine with no libusb at
    all, so that `t2pro.palettes` and `t2pro.protocol` -- which need no hardware
    -- stay usable without the USB stack present.
    """
    global _lib
    if _lib is not None:
        return _lib
    lib = _load()
    _configure(lib)
    _lib = lib
    return _lib

LIBUSB_TRANSFER_TYPE_BULK = 2
LIBUSB_TRANSFER_COMPLETED = 0
LIBUSB_TRANSFER_CANCELLED = 3


class _Transfer(ctypes.Structure):
    pass


_CB = ctypes.CFUNCTYPE(None, ctypes.POINTER(_Transfer))

_Transfer._fields_ = [
    ("dev_handle", ctypes.c_void_p),
    ("flags", ctypes.c_uint8),
    ("endpoint", ctypes.c_uint8),
    ("type", ctypes.c_uint8),
    ("timeout", ctypes.c_uint),
    ("status", ctypes.c_int),
    ("length", ctypes.c_int),
    ("actual_length", ctypes.c_int),
    ("callback", _CB),
    ("user_data", ctypes.c_void_p),
    ("buffer", ctypes.POINTER(ctypes.c_ubyte)),
    ("num_iso_packets", ctypes.c_int),
]


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


def _configure(lib):
    lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
    lib.libusb_open_device_with_vid_pid.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
    lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_set_interface_alt_setting.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    lib.libusb_close.argtypes = [ctypes.c_void_p]
    lib.libusb_exit.argtypes = [ctypes.c_void_p]
    lib.libusb_alloc_transfer.restype = ctypes.POINTER(_Transfer)
    lib.libusb_alloc_transfer.argtypes = [ctypes.c_int]
    lib.libusb_free_transfer.argtypes = [ctypes.POINTER(_Transfer)]
    lib.libusb_submit_transfer.argtypes = [ctypes.POINTER(_Transfer)]
    lib.libusb_cancel_transfer.argtypes = [ctypes.POINTER(_Transfer)]
    lib.libusb_handle_events_timeout.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Timeval)]
    lib.libusb_bulk_transfer.argtypes = [
        ctypes.c_void_p, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
    ]
    lib.libusb_set_auto_detach_kernel_driver.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_control_transfer.argtypes = [
        ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint16,
        ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint16, ctypes.c_uint,
    ]


LIBUSB_ERROR_ACCESS = -3
LIBUSB_ERROR_BUSY = -6


def _open_help(vid, pid):
    msg = [f"device {vid:04x}:{pid:04x} not found, or the OS will not let us open it."]
    if sys.platform == "win32":
        msg.append("On Windows the device needs the WinUSB driver: run Zadig, select the "
                   "InfiRay interface, and install WinUSB.")
    elif sys.platform.startswith("linux"):
        msg.append("On Linux, grant access with a udev rule: write "
                   f'SUBSYSTEM=="usb", ATTR{{idVendor}}=="{vid:04x}", '
                   f'ATTR{{idProduct}}=="{pid:04x}", MODE="0666", TAG+="uaccess" '
                   "to /etc/udev/rules.d/99-infiray-t2pro.rules, run "
                   "'udevadm control --reload-rules', and replug the camera. "
                   "Or run as root.")
    return " ".join(msg)


def _claim_help(interface, rc):
    msg = [f"could not claim interface {interface} (libusb error {rc})."]
    if rc == LIBUSB_ERROR_BUSY:
        msg.append("Another process already has it -- close any other t2pro instance.")
    elif rc == LIBUSB_ERROR_ACCESS:
        if sys.platform == "win32":
            msg.append("Bind WinUSB to this interface with Zadig.")
        elif sys.platform.startswith("linux"):
            msg.append('Add a udev rule for this device in /etc/udev/rules.d/ '
                       '(MODE="0666", TAG+="uaccess"), reload udev, and replug.')
    return " ".join(msg)


class AsyncBulkReader:
    """Keeps `depth` bulk-IN transfers queued and feeds completions to `on_data`.

    `on_data` runs on the event thread and must not block.
    """

    def __init__(self, vid, pid, interface, altsetting, endpoint,
                 on_data, depth=64, chunk=16384):
        # Set before anything that can fail, so close() is safe on a partial init.
        self._closed = False
        self._running = False
        self._h = None
        self._iface = interface
        self._transfers = []
        self._bufs = []
        self._inflight = 0
        self._thread = None
        _ensure()
        self._ctx = ctypes.c_void_p()
        if _lib.libusb_init(ctypes.byref(self._ctx)) != 0:
            raise OSError("libusb_init failed")
        self._h = _lib.libusb_open_device_with_vid_pid(self._ctx, vid, pid)
        if not self._h:
            _lib.libusb_exit(self._ctx)
            raise OSError(_open_help(vid, pid))
        # On Linux a kernel driver may hold the interface; hand it over to us.
        # Not supported on macOS/Windows; harmless there.
        with contextlib.suppress(AttributeError, OSError):
            _lib.libusb_set_auto_detach_kernel_driver(self._h, 1)
        rc = _lib.libusb_claim_interface(self._h, interface)
        if rc != 0:
            self.close()
            raise OSError(_claim_help(interface, rc))
        rc = _lib.libusb_set_interface_alt_setting(self._h, interface, altsetting)
        if rc != 0:
            self.close()
            raise OSError(f"set_interface_alt_setting({interface},{altsetting}) failed: {rc}")

        self._on_data = on_data
        self._running = True
        # Keep a Python reference to every ctypes object libusb will touch, so
        # the GC cannot free a buffer that still has a transfer queued on it.
        self._cb = _CB(self._complete)
        for _ in range(depth):
            buf = (ctypes.c_ubyte * chunk)()
            t = _lib.libusb_alloc_transfer(0)
            t.contents.dev_handle = self._h
            t.contents.endpoint = endpoint
            t.contents.type = LIBUSB_TRANSFER_TYPE_BULK
            t.contents.timeout = 1000
            t.contents.buffer = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
            t.contents.length = chunk
            t.contents.callback = self._cb
            t.contents.user_data = None
            t.contents.num_iso_packets = 0
            self._bufs.append(buf)
            self._transfers.append(t)

        for t in self._transfers:
            if _lib.libusb_submit_transfer(t) == 0:
                self._inflight += 1

        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _complete(self, t):
        st = t.contents.status
        if st == LIBUSB_TRANSFER_COMPLETED and t.contents.actual_length:
            n = t.contents.actual_length
            self._on_data(bytes(bytearray(t.contents.buffer[:n])))
        if (self._running and st != LIBUSB_TRANSFER_CANCELLED
                and _lib.libusb_submit_transfer(t) == 0):
            return
        self._inflight -= 1

    def _pump(self):
        tv = _Timeval(0, 50000)
        while self._running or self._inflight > 0:
            _lib.libusb_handle_events_timeout(self._ctx, ctypes.byref(tv))

    def control(self, bmRequestType, bRequest, wValue, wIndex, data_or_len, timeout=1000):
        """Vendor control transfer. Pass bytes to write, or an int length to read."""
        if isinstance(data_or_len, int):
            buf = (ctypes.c_ubyte * data_or_len)()
            n = _lib.libusb_control_transfer(self._h, bmRequestType, bRequest, wValue,
                                             wIndex, buf, data_or_len, timeout)
            if n < 0:
                raise OSError(f"control transfer failed: {n}")
            return bytes(bytearray(buf[:n]))
        payload = bytes(data_or_len)
        buf = (ctypes.c_ubyte * max(len(payload), 1))(*payload)
        n = _lib.libusb_control_transfer(self._h, bmRequestType, bRequest, wValue,
                                         wIndex, buf, len(payload), timeout)
        if n < 0:
            raise OSError(f"control transfer failed: {n}")
        return n

    def bulk_write(self, endpoint, payload, timeout=1000):
        buf = (ctypes.c_ubyte * len(payload))(*bytes(payload))
        got = ctypes.c_int(0)
        rc = _lib.libusb_bulk_transfer(self._h, endpoint, buf, len(payload),
                                       ctypes.byref(got), timeout)
        if rc != 0:
            raise OSError(f"bulk write failed: {rc}")
        return got.value

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._running = False
        for t in self._transfers:
            _lib.libusb_cancel_transfer(t)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        for t in self._transfers:
            _lib.libusb_free_transfer(t)
        self._transfers = []
        if self._h:
            _lib.libusb_release_interface(self._h, self._iface)
            _lib.libusb_close(self._h)
            self._h = None
        _lib.libusb_exit(self._ctx)
