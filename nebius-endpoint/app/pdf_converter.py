import base64
import logging
from io import BytesIO
from pdf2image import convert_from_bytes
from app.config import Config

logger = logging.getLogger("app.pdf")


def pdf_to_single_page_image(pdf_bytes: bytes, page: int = 1) -> str:
    pages = convert_from_bytes(pdf_bytes, dpi=Config.PDF_DPI, first_page=page, last_page=page)
    if not pages:
        raise ValueError(f"Page {page} not found in PDF")
    buf = BytesIO()
    pages[0].save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    logger.info("PDF page %d converted: %d KB", page, len(b64) // 1024)
    return b64


def get_pdf_page_count(pdf_bytes: bytes) -> int:
    from pdf2image.pdf2image import pdfinfo_from_bytes
    info = pdfinfo_from_bytes(pdf_bytes)
    return info.get("Pages", 1)


def pdf_to_images(pdf_bytes: bytes) -> list:
    """Convert every PDF page to a base64 JPEG (packet mode, Req 16.1).

    Caller enforces the PDF_MAX_PAGES cap before invoking.
    """
    pages = convert_from_bytes(pdf_bytes, dpi=Config.PDF_DPI)
    images = []
    for p in pages:
        buf = BytesIO()
        p.save(buf, format="JPEG", quality=85)
        images.append(base64.b64encode(buf.getvalue()).decode("utf-8"))
    logger.info("PDF converted for packet mode: %d pages", len(images))
    return images
