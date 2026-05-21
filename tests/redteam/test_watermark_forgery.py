"""KYC watermark forensics red-team (docs/engineering/16, 24).

The HMAC watermark is the platform's leak-tracing surface. Two attacks the
operator must detect: someone claiming a document came from a *different*
Darwin (forgery), and someone stripping the trailer and replaying the
clean blob (removal). Both must produce a hard refusal from ``verify``.
"""

from __future__ import annotations

from darwin_platform.security.watermark import HmacWatermarker, WATERMARK_MARKER, verify


def test_redteam_watermark_forgery_via_wrong_key_detectable():
    wm = HmacWatermarker(signing_key=b"platform-key")
    pdf = b"%PDF-1.4\nKYC content\n%%EOF\n"
    marked, record = wm.stamp(value=pdf, darwin_id="d-1", upload_ts=100.0)
    # An attacker who tries to verify against a forged darwin id /
    # upload ts must not pass.
    assert verify(
        marked_bytes=marked, signing_key=b"platform-key",
        expected_darwin_id="d-other",  # claims a different Darwin
        expected_upload_ts=100.0,
        expected_sha256=record.sha256_of_original,
    ) is False
    # The genuine claim still verifies.
    assert verify(
        marked_bytes=marked, signing_key=b"platform-key",
        expected_darwin_id="d-1", expected_upload_ts=100.0,
        expected_sha256=record.sha256_of_original,
    ) is True


def test_redteam_watermark_marker_removal_breaks_verify():
    """If an attacker strips the watermark trailer entirely, verify
    must refuse rather than silently pass."""
    wm = HmacWatermarker(signing_key=b"k")
    pdf = b"%PDF-1.4\n%%EOF\n"
    marked, record = wm.stamp(value=pdf, darwin_id="d-1", upload_ts=100.0)
    stripped = marked.split(WATERMARK_MARKER, 1)[0]
    assert verify(
        marked_bytes=stripped, signing_key=b"k",
        expected_darwin_id="d-1", expected_upload_ts=100.0,
        expected_sha256=record.sha256_of_original,
    ) is False
