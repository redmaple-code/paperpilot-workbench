"""Bounded local SVG/PDF conversion worker. No remote resources are permitted."""
from __future__ import annotations

import argparse
import math
import os
from pathlib import Path

MAX_PIXELS = 16_000_000


def ensure_cjk_fonts(drawing) -> None:
    """Embed an installed font for CJK labels; never silently emit missing glyphs."""
    from reportlab.graphics.shapes import String
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont, TTFError

    def strings(node):
        if isinstance(node, String):
            yield node
        for child in getattr(node, "contents", []):
            yield from strings(child)

    texts = [(node, {ord(c) for c in node.text if '\u2e80' <= c <= '\u9fff' or '\uac00' <= c <= '\ud7af'}) for node in strings(drawing)]
    texts = [(node, chars) for node, chars in texts if chars]
    if not texts:
        return
    candidates = [os.environ.get("PWA_VISUAL_CJK_FONT", ""), str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/simhei.ttf"),
                  "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "/usr/share/fonts/truetype/arphic/uming.ttc"]
    for index, filename in enumerate(candidates):
        if not filename or not Path(filename).is_file():
            continue
        try:
            font = TTFont("PaperPilotCJK" + str(index), filename, subfontIndex=0)
            if not all(chars.issubset(font.face.charToGlyph) for _, chars in texts):
                continue
            pdfmetrics.registerFont(font)
            for node, _ in texts:
                node.fontName = font.fontName
            return
        except (OSError, ValueError, TTFError):
            continue
    raise ValueError("cjk_font_unavailable: configure PWA_VISUAL_CJK_FONT with a compatible installed TrueType font")


def convert(source: Path, pdf: Path, preview: Path, kind: str, page: int = 1) -> None:
    import pypdfium2

    if kind == "svg":
        from svglib.svglib import svg2rlg
        from reportlab.graphics import renderPDF

        drawing = svg2rlg(str(source))
        if drawing is None or drawing.width <= 0 or drawing.height <= 0:
            raise ValueError("invalid_svg_dimensions")
        if drawing.width * drawing.height > MAX_PIXELS:
            raise ValueError("image_too_large")
        ensure_cjk_fonts(drawing)
        renderPDF.drawToFile(drawing, str(pdf))
    else:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(str(source))
        if reader.is_encrypted or not 1 <= page <= len(reader.pages) or len(reader.pages) > 200:
            raise ValueError("invalid_page")
        writer = PdfWriter()
        writer.add_page(reader.pages[page - 1])
        with pdf.open("wb") as stream:
            writer.write(stream)
    with pypdfium2.PdfDocument(str(pdf)) as doc:
        pdf_page = doc[0]
        try:
            width, height = pdf_page.get_size()
            if width <= 0 or height <= 0:
                raise ValueError("invalid_dimensions")
            scale = min(2.0, 2048 / max(width, height), math.sqrt(MAX_PIXELS / (width * height)))
            bitmap = pdf_page.render(scale=scale)
            try:
                bitmap.to_pil().save(preview, "PNG")
            finally:
                bitmap.close()
        finally:
            pdf_page.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["svg", "pdf"])
    parser.add_argument("source", type=Path)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("preview", type=Path)
    parser.add_argument("--page", type=int, default=1)
    args = parser.parse_args()
    convert(args.source, args.pdf, args.preview, args.kind, args.page)
