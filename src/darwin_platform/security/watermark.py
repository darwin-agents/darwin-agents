"""KYC document watermarking (docs/engineering/16 §"KYC handling").

Doc 16: "Watermark them invisibly with the Darwin ID and timestamp at
retrieval, so leaks can be traced." The platform-side vault admin
already records a sidecar metadata blob (sha256, uploader, ts) per
upload — that's the *audit* trail. This module is the *forensic*
trail: an HMAC stamp embedded into the document bytes themselves, so a
leaked PDF or image is traceable back to (darwin_id, upload_ts, signer)
even when the audit chain is unreachable to the holder of the leak.

Two backends ship with the platform:

- :class:`NoOpWatermarker` — default. Returns bytes unchanged. The
  audit sidecar is still the primary forensic record.
- :class:`HmacWatermarker` — best-effort. For PDFs, appends an HMAC
  comment trailer between ``%%EOF`` markers (invisible to readers,
  preserved by most copy-paste flows). For images (PNG/JPEG header
  detected), appends an HMAC EXIF/text chunk. Other formats fall back
  to a sidecar HMAC stored next to the blob — better than nothing.

The HMAC binds (sha256(original_bytes), darwin_id, upload_ts) under a
platform-held signing key, so a leak's provenance survives any in-place
edit that doesn't strip the trailer.

This module's :class:`Watermarker` Protocol is the contract; production
deployers can wire pypdf / Pillow for richer steganography behind the
same surface.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


WATERMARK_MARKER = b"%%DARWIN-WATERMARK%%"
WATERMARK_VERSION = 1


@dataclass(frozen=True)
class WatermarkRecord:
    """One watermark stamp. Stored in the document's tail OR as a
    sidecar key alongside the vault entry."""

    darwin_id: str
    upload_ts: float
    sha256_of_original: str
    hmac_b64: str

    def to_dict(self) -> dict:
        return {
            "v": WATERMARK_VERSION,
            "darwin_id": self.darwin_id,
            "upload_ts": self.upload_ts,
            "sha256": self.sha256_of_original,
            "hmac": self.hmac_b64,
        }


@runtime_checkable
class Watermarker(Protocol):
    """Embed an invisible mark into the document bytes. Return
    (marked_bytes, record). ``record`` is the platform-side memo of what
    was stamped so a leak investigator can verify the HMAC against the
    same signing key."""

    def stamp(
        self,
        *,
        value: bytes,
        darwin_id: str,
        upload_ts: float | None = None,
        original_filename: str | None = None,
    ) -> tuple[bytes, WatermarkRecord]: ...


class NoOpWatermarker:
    """Returns bytes unchanged. Useful for tests, for non-document
    blobs (credentials, API tokens) where embedded watermarking makes
    no sense, and as the default for backwards-compatibility."""

    def stamp(
        self,
        *,
        value: bytes,
        darwin_id: str,
        upload_ts: float | None = None,
        original_filename: str | None = None,
    ) -> tuple[bytes, WatermarkRecord]:
        ts = float(upload_ts) if upload_ts is not None else time.time()
        sha = hashlib.sha256(value).hexdigest()
        return value, WatermarkRecord(
            darwin_id=darwin_id,
            upload_ts=ts,
            sha256_of_original=sha,
            hmac_b64="",
        )


class HmacWatermarker:
    """HMAC-stamps document bytes with a platform-held signing key. The
    stamp is embedded in a format-appropriate trailer so common
    readers display the document unchanged but the bytes carry the
    watermark.

    The signing key never enters the audit chain or the document's
    visible portion — only the HMAC output does. To verify a leaked
    document, a forensics team computes the same HMAC against the
    leaked bytes' prefix (everything before the marker) and compares.
    """

    def __init__(self, *, signing_key: bytes) -> None:
        if not signing_key:
            raise ValueError("HmacWatermarker requires a signing_key")
        self._key = bytes(signing_key)

    def stamp(
        self,
        *,
        value: bytes,
        darwin_id: str,
        upload_ts: float | None = None,
        original_filename: str | None = None,
    ) -> tuple[bytes, WatermarkRecord]:
        ts = float(upload_ts) if upload_ts is not None else time.time()
        sha = hashlib.sha256(value).hexdigest()
        # Bind sha + darwin_id + upload_ts together so swapping any
        # field invalidates the HMAC.
        msg = f"{sha}|{darwin_id}|{ts:.6f}".encode("utf-8")
        digest = hmac.new(self._key, msg, hashlib.sha256).digest()
        hmac_b64 = base64.b64encode(digest).decode("ascii")
        record = WatermarkRecord(
            darwin_id=darwin_id,
            upload_ts=ts,
            sha256_of_original=sha,
            hmac_b64=hmac_b64,
        )
        marked = self._embed(value, record, original_filename)
        return marked, record

    # -- format-aware embedding ------------------------------------------
    def _embed(
        self,
        value: bytes,
        record: WatermarkRecord,
        original_filename: str | None,
    ) -> bytes:
        trailer = self._build_trailer(record)
        # PDF: embed into the /Info dict via pypdf (when available) AND
        # append a trailer after %%EOF. The /Info embed survives PDF
        # re-saves that strip post-EOF garbage; the trailer is the
        # baseline for forensics when pypdf isn't around.
        if value.startswith(b"%PDF-") or _has_pdf_eof(value):
            with_meta = self._embed_pdf_metadata(value, record)
            return with_meta + b"\n" + trailer + b"\n"
        # PNG: append a tEXt chunk-like sentinel at the end. We don't
        # construct a real PNG chunk (which would require a CRC32 over
        # the chunk type + data) to avoid a Pillow dependency; an
        # appended trailer after IEND is still preserved by most
        # image-pipeline tooling that doesn't re-encode.
        if value.startswith(b"\x89PNG\r\n\x1a\n"):
            return value + b"\n" + trailer + b"\n"
        # JPEG: same shape — append after the EOI (0xFFD9) marker.
        if value.startswith(b"\xff\xd8"):
            return value + b"\n" + trailer + b"\n"
        # Unknown format — return unchanged. The record still gets
        # returned to the caller, who stores it as a sidecar (the
        # vault_admin module already writes a JSON sidecar; the HMAC
        # goes there too).
        return value

    @staticmethod
    def _embed_pdf_metadata(value: bytes, record: WatermarkRecord) -> bytes:
        """Inject the HMAC + darwin id into the PDF /Info dict. Failure
        is silent — the trailer path still works."""
        try:
            import io
            import pypdf
        except ImportError:
            return value
        try:
            reader = pypdf.PdfReader(io.BytesIO(value))
            writer = pypdf.PdfWriter(clone_from=reader)
            existing = dict(reader.metadata or {})
            existing.update({
                "/DarwinWatermarkHmac": record.hmac_b64,
                "/DarwinWatermarkDarwinId": record.darwin_id,
                "/DarwinWatermarkTs": f"{record.upload_ts:.6f}",
                "/DarwinWatermarkSha256": record.sha256_of_original,
            })
            writer.add_metadata(existing)
            buf = io.BytesIO()
            writer.write(buf)
            return buf.getvalue()
        except Exception:
            # Any pypdf failure (malformed PDF, unsupported feature)
            # falls back to the trailer path.
            return value

    def _build_trailer(self, record: WatermarkRecord) -> bytes:
        # The trailer is human-greppable enough for a forensics tool to
        # locate without parsing the host format. Format:
        #   %%DARWIN-WATERMARK%% v=1 darwin=X ts=N sha=H hmac=B64
        rec = record.to_dict()
        parts = [
            f"v={rec['v']}",
            f"darwin={rec['darwin_id']}",
            f"ts={rec['upload_ts']:.6f}",
            f"sha={rec['sha256']}",
            f"hmac={rec['hmac']}",
        ]
        return WATERMARK_MARKER + b" " + " ".join(parts).encode("utf-8")


def _has_pdf_eof(value: bytes) -> bool:
    """Probe for PDFs that may have leading garbage (rare but possible
    with form-data wrappers). Looks for the %%EOF trailer in the last
    1KB."""
    return b"%%EOF" in value[-1024:]


def verify(
    *,
    marked_bytes: bytes,
    signing_key: bytes,
    expected_darwin_id: str,
    expected_upload_ts: float,
    expected_sha256: str,
) -> bool:
    """Verify a leaked document's HMAC. Used by a forensics tool
    against the platform's signing key + the stored WatermarkRecord.

    Checks BOTH paths the watermarker may have used:
      1. The post-%%EOF trailer (always written when the formatter
         recognised the file as PDF/PNG/JPEG).
      2. The pypdf ``/Info`` metadata (only written when pypdf was
         available at stamp time AND the doc was a PDF)."""
    msg = (
        f"{expected_sha256}|{expected_darwin_id}|"
        f"{expected_upload_ts:.6f}"
    ).encode("utf-8")
    expected = base64.b64encode(
        hmac.new(signing_key, msg, hashlib.sha256).digest()
    ).decode("ascii")
    # Path 1: trailer.
    if WATERMARK_MARKER in marked_bytes:
        idx = marked_bytes.rfind(WATERMARK_MARKER)
        trailer = marked_bytes[idx:].decode("utf-8", errors="ignore")
        for token in trailer.split():
            if token.startswith("hmac="):
                if hmac.compare_digest(
                    token.split("=", 1)[1].strip(), expected
                ):
                    return True
    # Path 2: pypdf-embedded metadata (forensics tool path).
    if _verify_via_pypdf(
        marked_bytes=marked_bytes, expected_hmac_b64=expected,
    ):
        return True
    return False


def _verify_via_pypdf(*, marked_bytes: bytes, expected_hmac_b64: str) -> bool:
    """Probe the PDF /Info dictionary for the HMAC field we embed when
    pypdf is available at stamp time. Returns False on any error
    (file isn't a PDF, pypdf not installed, /Info missing, etc.)."""
    try:
        import io
        import pypdf
    except ImportError:
        return False
    try:
        reader = pypdf.PdfReader(io.BytesIO(marked_bytes))
        info = reader.metadata or {}
    except Exception:
        return False
    hmac_field = info.get("/DarwinWatermarkHmac") or info.get(
        "/Darwinwatermarkhmac"
    )
    if not hmac_field:
        return False
    return hmac.compare_digest(str(hmac_field), expected_hmac_b64)
