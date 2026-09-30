"""Draw glassmacro.ico - a faceted cyan gem on a dark rounded tile.

Drawn once at 1024px and scaled down, so the small sizes stay crisp. Also
writes icon_preview.png so it can be looked at without Explorer.
"""
import os

from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
S = 1024


def P(x, y):
    return (x * S, y * S)


def tile():
    """Dark rounded square with a faint top-down sheen."""
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    top, bot = (22, 34, 52), (7, 11, 17)
    for y in range(S):
        t = y / (S - 1)
        c = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        gd.line([(0, y), (S, y)], fill=c + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1],
                                           radius=int(S * 0.22), fill=255)
    base.paste(grad, (0, 0), mask)
    # hairline border so it reads on a dark taskbar
    ImageDraw.Draw(base).rounded_rectangle(
        [6, 6, S - 7, S - 7], radius=int(S * 0.215),
        outline=(40, 64, 92, 255), width=10)
    return base


def gem():
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    # silhouette: table, girdle, culet
    tl, tr = P(0.345, 0.255), P(0.655, 0.255)
    gl, gr = P(0.155, 0.415), P(0.845, 0.415)
    gm1, gm2 = P(0.385, 0.415), P(0.615, 0.415)
    cm = P(0.50, 0.415)
    culet = P(0.50, 0.815)

    # crown facets, light from the top left
    d.polygon([tl, tr, cm], fill=(196, 238, 255))          # table
    d.polygon([tl, gl, gm1], fill=(150, 222, 255))
    d.polygon([tl, gm1, cm], fill=(118, 208, 255))
    d.polygon([tr, cm, gm2], fill=(86, 188, 245))
    d.polygon([tr, gm2, gr], fill=(58, 160, 226))

    # pavilion facets, darker as they turn away
    d.polygon([gl, gm1, culet], fill=(70, 176, 240))
    d.polygon([gm1, cm, culet], fill=(46, 146, 214))
    d.polygon([cm, gm2, culet], fill=(31, 118, 186))
    d.polygon([gm2, gr, culet], fill=(20, 86, 146))

    # facet edges - thin, pale, like light catching glass
    edge = (225, 246, 255, 150)
    w = 7
    for a, b in [(tl, tr), (tl, gl), (tr, gr), (gl, gr), (gl, culet),
                 (gr, culet), (tl, gm1), (tl, cm), (tr, cm), (tr, gm2),
                 (gm1, culet), (cm, culet), (gm2, culet)]:
        d.line([a, b], fill=edge, width=w)
    return layer


def glow(shape):
    """Soft cyan halo behind the gem."""
    alpha = shape.split()[3]
    halo = Image.new("RGBA", (S, S), (94, 203, 255, 0))
    halo.putalpha(alpha.point(lambda a: int(a * 0.55)))
    return halo.filter(ImageFilter.GaussianBlur(S * 0.05))


def sparkle(img):
    d = ImageDraw.Draw(img)
    cx, cy, r = S * 0.745, S * 0.215, S * 0.07
    d.polygon([(cx, cy - r), (cx + r * 0.2, cy - r * 0.2), (cx + r, cy),
               (cx + r * 0.2, cy + r * 0.2), (cx, cy + r),
               (cx - r * 0.2, cy + r * 0.2), (cx - r, cy),
               (cx - r * 0.2, cy - r * 0.2)], fill=(235, 250, 255, 235))


def main():
    img = tile()
    g = gem()
    img.alpha_composite(glow(g))
    img.alpha_composite(g)
    sparkle(img)

    img.resize((256, 256), Image.LANCZOS).save(
        os.path.join(HERE, "icon_preview.png"))
    sizes = [(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)]
    img.resize((256, 256), Image.LANCZOS).save(
        os.path.join(HERE, "glassmacro.ico"), sizes=sizes)
    print("wrote glassmacro.ico")


if __name__ == "__main__":
    main()
