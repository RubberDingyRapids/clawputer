"""Draw the Clawputer icon: dark tile, orange >_ prompt, three claw scratches."""
import sys
from PIL import Image, ImageDraw, ImageFont

BG = (24, 24, 32)
ORANGE = (217, 119, 87)
LIGHT = (240, 200, 180)


def make(size, font_path):
    s = 4                                  # supersample for smooth edges
    W = size * s
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(W * 0.22)
    d.rounded_rectangle((0, 0, W - 1, W - 1), radius=r, fill=BG)
    d.rounded_rectangle((int(W*0.03), int(W*0.03), W - 1 - int(W*0.03), W - 1 - int(W*0.03)),
                        radius=int(r*0.85), outline=(60, 60, 75), width=max(2, W // 64))
    # claw scratches, top right
    lw = int(W * 0.055)
    for i in range(3):
        x0 = W * (0.56 + i * 0.11)
        d.line((x0, W * 0.16, x0 + W * 0.10, W * 0.40), fill=LIGHT, width=lw)
    # >_ prompt
    font = ImageFont.truetype(font_path, int(W * 0.46))
    d.text((W * 0.13, W * 0.40), ">_", font=font, fill=ORANGE)
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    font = sys.argv[1]
    for size, name in ((100, "clawputer.png"), (256, "store-icon.png")):
        make(size, font).save(sys.argv[2] + "/" + name)
    print("icons written")
