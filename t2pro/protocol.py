"""Wire format of the InfiRay T2 Pro (MFi/iOS variant, USB 04b4:000a).

This is *not* the UVC variant most open-source tools target.  There is no video
interface at all; frames arrive on a vendor-specific bulk endpoint.

Enumeration
-----------
    interface 0  "com.infiRay.XthermProtocol"  class 0xFF / sub 0xF0 / proto 1
        alt 0 : no endpoints (zero-bandwidth default)
        alt 1 : bulk IN 0x82, bulk OUT 0x04     <- streaming + control live here
    interface 1  "iAP Interface"                MFi/iAP, unused here

Framing
-------
Every USB transfer is one message of 524 bytes: a 12-byte header followed by
512 bytes of payload.  Wire packetization splits it as 512 + 12, so the final
12 payload bytes arrive as a short packet that also terminates the transfer.

    offset  size  field
    0       1     0x0c   header length
    1       1     channel: 0x8c or 0x8d
    2       4     device timestamp, little-endian
    6       2     0x0001 constant
    8       4     row index within the frame, little-endian, 1..196

Each message carries exactly one row of 256 uint16 (little-endian).  A frame is
196 rows: 192 image rows plus 4 trailing metadata rows.  The device alternates
between channels 0x8c and 0x8d at 12.5 Hz each, giving the rated 25 Hz.

Control
-------
Commands are a bare 2-byte big-endian word written to bulk OUT 0x04: the high
byte selects a register, the low byte is the parameter.  This mirrors the
command word the UVC variant tunnels through its Zoom (Absolute) control.
Verified on hardware: 0x8000 fires the shutter (flat-field correction).
"""

VENDOR_ID = 0x04B4
PRODUCT_ID = 0x000A

INTERFACE = 0
ALT_SETTING = 1
EP_IN = 0x82
EP_OUT = 0x04

HEADER_LEN = 12
ROW_BYTES = 512
MESSAGE_LEN = HEADER_LEN + ROW_BYTES

WIDTH = 256           # uint16 per row
IMAGE_ROWS = 192
META_ROWS = 4
FRAME_ROWS = IMAGE_ROWS + META_ROWS      # 196
FRAME_BYTES = FRAME_ROWS * ROW_BYTES     # 100352

CHANNELS = (0x8C, 0x8D)

# --- command words: high byte selects the register, low byte the parameter --
REG_SHUTTER = 0x80

CMD_SHUTTER = 0x8000         # fire flat-field correction. Verified on hardware.

# Other parameters of register 0x80, taken from the UVC variants of this sensor
# family (diminDDL/IR-Py-Thermal, which drives them through CAP_PROP_ZOOM).
# NOT verified on this MFi variant -- listed so probing has somewhere to start.
# Send with T2Pro.send(); 0x80FF writes settings to flash, so treat it with care.
CMD_RAW_MODE = 0x8004        # 16-bit raw output mode
CMD_RANGE_NORMAL = 0x8020    # temperature range -20..120 C
CMD_RANGE_HIGH = 0x8021      # temperature range -20..450 C
CMD_SAVE_PARAMS = 0x80FF     # persist current settings

# There is no auto-shutter register known for this family: the vendor app times
# corrections in software, and so does t2pro.device.AutoFFC.  This device also
# reports no shutter or core temperature -- the UVC variants put those in the
# metadata rows, but here rows 193-195 are static.  See AutoFFC for the
# measurements behind that.


def command(word):
    """Encode a 16-bit command word for bulk OUT 0x04 (register byte first)."""
    return bytes(((word >> 8) & 0xFF, word & 0xFF))


def parse_header(msg):
    """Return (channel, row_index) for a 524-byte message, or None if not one."""
    if len(msg) < MESSAGE_LEN or msg[0] != HEADER_LEN:
        return None
    channel = msg[1]
    if channel not in CHANNELS:
        return None
    row = int.from_bytes(msg[8:12], "little")
    if not 1 <= row <= FRAME_ROWS:
        return None
    return channel, row
