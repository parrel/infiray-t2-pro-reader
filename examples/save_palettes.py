"""Library only: grab one calibrated frame and save it in every palette.

No HTTP, no Pillow beyond the PNG write -- this is the whole library surface
you need to get corrected pixels out of the camera.

    python examples/save_palettes.py
"""

from PIL import Image

from t2pro import T2Pro, palettes

with T2Pro() as cam:                 # auto-FFC calibrates on open
    cam.read(timeout=3.0)
    while cam.reference is None:     # wait for that first correction
        cam.read(timeout=2.0)
    frame = cam.read(timeout=2.0)
    print("serial/model:", frame.identity, "raw range:", frame.raw_range)

    image = cam.correct(frame.image)             # float32, 192x256
    for name in palettes.NAMES:
        rgb = palettes.render(image, name)       # uint8, 192x256x3
        path = f"frame_{name}.png"
        Image.fromarray(rgb).resize((768, 576), Image.NEAREST).save(path)
        print("wrote", path)
