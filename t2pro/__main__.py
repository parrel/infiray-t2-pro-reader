import argparse

from . import palettes
from .server import serve


def main():
    ap = argparse.ArgumentParser(prog="t2pro",
                                 description="InfiRay T2 Pro (iOS/MFi) MJPEG streamer")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8420)
    ap.add_argument("--palette", default=palettes.DEFAULT, choices=palettes.NAMES)
    ap.add_argument("--scale", type=int, default=2, help="integer upscale factor")
    ap.add_argument("--quality", type=int, default=85, help="JPEG quality")
    ap.add_argument("--floor", type=float, default=150.0,
                    help="narrowest autoscale window in raw counts; 0 disables")
    ap.add_argument("--interp", default="lanczos",
                    choices=("lanczos", "bicubic", "bilinear", "nearest"),
                    help="resampling filter for the upscale")
    a = ap.parse_args()
    serve(host=a.host, port=a.port, palette=a.palette, scale=a.scale, quality=a.quality,
          floor=a.floor, interp=a.interp)


if __name__ == "__main__":
    main()
