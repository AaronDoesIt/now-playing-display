"""Cut the four sunset photos to squares for the idle slideshow, and make a contact sheet to look at.

    python crop_slides.py <project folder> <contact sheet png>

Each entry: source file, output name, x of the left edge of the square crop (the crop is as tall
as the photo, so its width equals the photo's height). Chosen by eye for each picture.
"""
import sys
from pathlib import Path

from PIL import Image, ImageOps

project = Path(sys.argv[1])
out_dir = project / "idle_images"
out_dir.mkdir(exist_ok=True)
JOBS = [
    ("1W6A2215.jpg", "01-ocean-sunset.jpg", 460),    # sun in the middle of the frame
    ("1W6A2944.jpg", "02-palm.jpg", 367),            # palm centred
    ("1W6A3005.jpg", "03-cactus-sunset.jpg", 345),   # keeps the sun; trims the cactus's far arm
    ("1W6A3027.jpg", "04-three-cacti.jpg", 527),     # the middle cactus, flanked by the other two
]
tiles = []
for source, name, left in JOBS:
    im = ImageOps.exif_transpose(Image.open(project / source)).convert("RGB")
    w, h = im.size
    left = max(0, min(left, w - h))
    square = im.crop((left, 0, left + h, h)).resize((1000, 1000), Image.Resampling.LANCZOS)
    square.save(out_dir / name, "JPEG", quality=92)
    print(f"{source} {w}x{h} -> {name}: columns {left}..{left + h}")
    tiles.append(square.resize((420, 420), Image.Resampling.LANCZOS))

sheet = Image.new("RGB", (2 * 420 + 30, 2 * 420 + 30), (20, 20, 20))
for i, tile in enumerate(tiles):
    sheet.paste(tile, (10 + (i % 2) * 430, 10 + (i // 2) * 430))
sheet.save(sys.argv[2])
print("contact sheet:", sys.argv[2])
