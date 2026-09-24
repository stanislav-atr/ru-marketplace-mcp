"""Compose product photos into one labelled contact sheet.

Some MCP clients render a tool's images but drop the text blocks between them
(seen 2026-09-24 in Claude Desktop), so a label sent as its own text block does
not reliably reach the model next to its photo. Burning the label into the
image does. Labels stay ASCII (`#n SKU`): Pillow's bundled font has no
Cyrillic, and brand and colour live in the JSON index keyed by the same `n`.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

_LABEL_H = 22
_MAX_COLUMNS = 4


def compose(tiles: list[tuple[str, bytes]], *, quality: int = 82) -> bytes:
    """Return a JPEG grid of ``tiles`` (label, image bytes), label above each tile.

    Every tile is resized to the first decodable image's size, so a mixed batch
    still lines up. Raises ValueError when no tile decodes.
    """
    decoded: list[tuple[str, Image.Image]] = []
    for label, data in tiles:
        try:
            with Image.open(io.BytesIO(data)) as img:
                decoded.append((label, img.convert("RGB")))
        except Exception:
            continue
    if not decoded:
        raise ValueError("no photo could be decoded")
    width, height = decoded[0][1].size
    columns = min(_MAX_COLUMNS, len(decoded))
    rows = (len(decoded) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * width, rows * (height + _LABEL_H)), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=15)
    for i, (label, photo) in enumerate(decoded):
        x, y = (i % columns) * width, (i // columns) * (height + _LABEL_H)
        draw.text((x + 4, y + 3), label, fill="black", font=font)
        tile = photo if photo.size == (width, height) else photo.resize((width, height))
        sheet.paste(tile, (x, y + _LABEL_H))
    out = io.BytesIO()
    sheet.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()
