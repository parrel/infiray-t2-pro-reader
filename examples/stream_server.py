"""The bundled MJPEG server, driven from Python instead of the CLI.

Everything here is in `t2pro.server`, which sits on top of the library and is
the only part that knows about HTTP.  Equivalent to:

    python -m t2pro --palette lava --scale 3

    python examples/stream_server.py     # then open http://127.0.0.1:8420/
"""

from t2pro.server import serve

serve(host="127.0.0.1", port=8420, palette="lava", scale=3, quality=85,
      floor=150.0, interp="lanczos")
