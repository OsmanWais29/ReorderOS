"""Shared receipt-file validation + sanitization (Sprint 6, D-606-11 / D-606-20).

Called on EVERY ingestion path before the Spaces PUT (and re-run by the extraction
worker after the GET). The extension is never authoritative — type is decided by
the leading bytes. Three guarantees this module makes real:

  1. Magic-byte allowlist — only PDF / PNG / JPEG by signature; everything else,
     zero bytes, and oversize are rejected.
  2. Polyglot rejection — a file whose bytes contain a SECOND container signature
     after the first (e.g. a PDF that is also a ZIP, or a JPEG with an appended
     PNG/PDF) is rejected and flagged.
  3. EXIF strip — image bytes are re-encoded from raw pixels, dropping ALL metadata
     (including GPS). This only works because the mobile path is API-mediated
     (D-606-14): the server is in the byte path, so the cleaned object — not the
     original — is what reaches Spaces.

HEIC (iOS default) is rejected: the client transcodes HEIC→JPEG before upload.
"""

from __future__ import annotations

from io import BytesIO

# Hard size bounds (D-606-20). Zero-length and >50 MB are rejected outright.
MAX_BYTES = 50 * 1024 * 1024

# Leading-byte signatures (D-606-20).
_PDF = b"%PDF-"
_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"

# Foreign signatures that, if found AFTER offset 0, mark a polyglot.
_ZIP_LOCAL = b"PK\x03\x04"
_ZIP_EOCD = b"PK\x05\x06"

MIME_PDF = "application/pdf"
MIME_PNG = "image/png"
MIME_JPEG = "image/jpeg"

_EXT_FOR_MIME = {MIME_PDF: "pdf", MIME_PNG: "png", MIME_JPEG: "jpg"}


class ReceiptValidationError(Exception):
    """A receipt file failed validation. `code` is a stable machine code; `message`
    is operator-facing. Terminal — never retried (a corrupt/tampered/forbidden file
    will not validate on a second try)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def extension_for(mime_type: str) -> str:
    return _EXT_FOR_MIME[mime_type]


def _detect_type(data: bytes) -> str:
    if data.startswith(_PDF):
        return MIME_PDF
    if data.startswith(_PNG):
        return MIME_PNG
    if data.startswith(_JPEG):
        return MIME_JPEG
    # HEIC/HEIF: ISO-BMFF `ftyp` box with a heic-family brand at bytes 4..12.
    if (
        len(data) >= 12
        and data[4:8] == b"ftyp"
        and data[8:12]
        in (
            b"heic",
            b"heix",
            b"hevc",
            b"heim",
            b"heis",
            b"mif1",
            b"msf1",
        )
    ):
        raise ReceiptValidationError(
            "RECEIPT_HEIC_UNSUPPORTED",
            "HEIC images aren't supported — please retake or convert the photo to JPEG.",
        )
    raise ReceiptValidationError(
        "RECEIPT_UNSUPPORTED_TYPE",
        "Unsupported file — only PDF, JPEG, and PNG invoices are accepted.",
    )


def _reject_polyglot(data: bytes, primary: str) -> None:
    """Reject a file that carries a second container signature after offset 0.

    Detection is deliberately broad: scan everything past the first byte for any
    allowlisted image/PDF signature OTHER than the primary, plus ZIP markers (the
    classic PDF/ZIP polyglot). A legitimate single-format file contains none of
    these at a non-zero offset."""
    primary_sig = {MIME_PDF: _PDF, MIME_PNG: _PNG, MIME_JPEG: _JPEG}[primary]
    # Skip the legitimate signature at offset 0; scan the remainder for any OTHER
    # container signature (a second format = polyglot).
    rest = data[1:]
    for sig in (_PDF, _PNG, _JPEG, _ZIP_LOCAL, _ZIP_EOCD):
        if sig == primary_sig:
            continue
        if sig in rest:
            raise ReceiptValidationError(
                "RECEIPT_POLYGLOT_REJECTED",
                "File is corrupt or has been altered.",
            )


def _strip_exif(data: bytes, mime_type: str) -> bytes:
    """Re-encode an image from raw pixels so NO metadata (incl. GPS EXIF) survives.
    PDFs pass through unchanged (EXIF is an image concern; PDF page/metadata handling
    is the extraction worker's job)."""
    if mime_type == MIME_PDF:
        return data
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(BytesIO(data)) as img:
            img.load()
            # frombytes copies ONLY pixels — drops exif/icc/xmp/text chunks.
            clean = Image.frombytes(img.mode, img.size, img.tobytes())
            out = BytesIO()
            fmt = "PNG" if mime_type == MIME_PNG else "JPEG"
            save_kwargs = {} if fmt == "PNG" else {"quality": 90}
            clean.save(out, format=fmt, **save_kwargs)
            return out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ReceiptValidationError(
            "RECEIPT_CORRUPT_IMAGE",
            "File is corrupt or has been altered.",
        ) from exc


def validate_and_clean(data: bytes, *, filename: str | None = None) -> tuple[str, bytes]:
    """Validate `data` and return (mime_type, cleaned_bytes). Raises
    ReceiptValidationError (terminal) on any rejection. `filename` is advisory only —
    the type is decided by the leading bytes, never the extension."""
    if not data:
        raise ReceiptValidationError("RECEIPT_EMPTY", "The file is empty.")
    if len(data) > MAX_BYTES:
        raise ReceiptValidationError("RECEIPT_TOO_LARGE", "File is larger than the 50 MB limit.")
    mime_type = _detect_type(data)
    _reject_polyglot(data, mime_type)
    cleaned = _strip_exif(data, mime_type)
    return mime_type, cleaned
