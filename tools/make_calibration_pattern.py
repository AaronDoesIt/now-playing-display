"""A 480x480 measuring pattern for the panel: shows how much of the picture the case's mask hides.

Nested frames, each 4 px wide, alternating white and dark, from the very edge inward to 80 px.
Every 20 px the frame is coloured and numbered: 20 red, 40 green, 60 blue, 80 yellow. Counting the
frames you can see on a side gives the hidden margin on that side. Usage: python make_cal.py out.jpg
"""
import sys

from PIL import Image, ImageDraw, ImageFont

S = 480
im = Image.new("RGB", (S, S), (24, 24, 28))
d = ImageDraw.Draw(im)
marks = {20: (255, 40, 40), 40: (40, 230, 60), 60: (70, 120, 255), 80: (255, 220, 0)}
for inset in range(0, 84, 4):
    colour = marks.get(inset, (255, 255, 255) if (inset // 4) % 2 == 0 else (24, 24, 28))
    for k in range(4):
        i = inset + k
        d.rectangle([i, i, S - 1 - i, S - 1 - i], outline=colour)

try:
    big = ImageFont.load_default(26)
    mid = ImageFont.load_default(18)
    small = ImageFont.load_default(14)
except Exception:
    big = mid = small = ImageFont.load_default()

# numbers beside each coloured frame, at the middle of every side
for inset, colour in marks.items():
    t = str(inset)
    w = d.textlength(t, font=small)
    d.text((S / 2 - w / 2, inset + 5), t, font=small, fill=colour)            # top
    d.text((S / 2 - w / 2, S - inset - 21), t, font=small, fill=colour)        # bottom
    d.text((inset + 6, S / 2 - 8), t, font=small, fill=colour)                 # left
    d.text((S - inset - 6 - w, S / 2 - 8), t, font=small, fill=colour)         # right

# centre cross, a circle (shows squashing) and labels for orientation
c = S // 2
d.line([(c, 110), (c, S - 110)], fill=(120, 120, 130))
d.line([(110, c), (S - 110, c)], fill=(120, 120, 130))
d.ellipse([c - 100, c - 100, c + 100, c + 100], outline=(120, 120, 130))
for text, xy in (("TOP", (c, 120)), ("BOTTOM", (c, S - 146)), ("LEFT", (140, c - 40)), ("RIGHT", (S - 140, c - 40))):
    w = d.textlength(text, font=big)
    d.text((xy[0] - w / 2, xy[1]), text, font=big, fill=(235, 235, 240))
for i, line in enumerate(("each stripe = 4 pixels", "red 20   green 40", "blue 60   yellow 80")):
    w = d.textlength(line, font=mid)
    d.text((c - w / 2, c + 14 + i * 24), line, font=mid, fill=(200, 200, 210))

im.save(sys.argv[1], "JPEG", quality=95, progressive=False, optimize=False, subsampling=0)
print("wrote", sys.argv[1], im.size)
