"""KYC watermarking embedded in document bytes (doc 16)."""

from __future__ import annotations

import pytest

from darwin_platform.vault_admin import VaultAdminService
from darwin_platform.watermark import (
    HmacWatermarker,
    NoOpWatermarker,
    WATERMARK_MARKER,
    Watermarker,
    verify,
)


class _Vault:
    def __init__(self):
        self.kv = {}

    def put(self, k, v): self.kv[k] = v
    def get(self, k): return self.kv.get(k)
    def delete(self, k): self.kv.pop(k, None)


# ---------------------------------------------------------------------------
# Watermarker primitives
# ---------------------------------------------------------------------------

def test_protocol_satisfied():
    assert isinstance(NoOpWatermarker(), Watermarker)
    assert isinstance(HmacWatermarker(signing_key=b"k"), Watermarker)


def test_noop_returns_bytes_unchanged():
    wm = NoOpWatermarker()
    marked, record = wm.stamp(
        value=b"hello", darwin_id="d-1", upload_ts=100.0,
    )
    assert marked == b"hello"
    assert record.darwin_id == "d-1"
    assert record.hmac_b64 == ""  # no HMAC for noop


def test_hmac_requires_key():
    with pytest.raises(ValueError):
        HmacWatermarker(signing_key=b"")


def test_pdf_gets_trailer_appended():
    wm = HmacWatermarker(signing_key=b"secret")
    pdf = b"%PDF-1.4\nstuff\n%%EOF\n"
    marked, record = wm.stamp(value=pdf, darwin_id="d-1", upload_ts=100.0)
    assert marked != pdf
    assert WATERMARK_MARKER in marked
    assert b"darwin=d-1" in marked
    assert record.hmac_b64  # non-empty for HMAC variant


def test_png_gets_trailer_appended():
    wm = HmacWatermarker(signing_key=b"secret")
    png = b"\x89PNG\r\n\x1a\n" + b"...IEND..."
    marked, record = wm.stamp(value=png, darwin_id="d-1", upload_ts=100.0)
    assert marked != png
    assert marked.startswith(b"\x89PNG\r\n\x1a\n")
    assert WATERMARK_MARKER in marked


def test_jpeg_gets_trailer_appended():
    wm = HmacWatermarker(signing_key=b"secret")
    jpeg = b"\xff\xd8" + b"...\xff\xd9"
    marked, record = wm.stamp(value=jpeg, darwin_id="d-1", upload_ts=100.0)
    assert marked != jpeg
    assert WATERMARK_MARKER in marked


def test_unknown_format_returns_unchanged():
    """Unknown formats return original bytes; the record is still
    produced so the sidecar metadata carries the HMAC for forensics."""
    wm = HmacWatermarker(signing_key=b"secret")
    txt = b"plain text credential"
    marked, record = wm.stamp(value=txt, darwin_id="d-1", upload_ts=100.0)
    assert marked == txt
    assert record.hmac_b64  # HMAC is still computed for the sidecar


def test_hmac_includes_all_fields():
    """Changing ANY of (sha, darwin_id, upload_ts) changes the HMAC."""
    wm = HmacWatermarker(signing_key=b"secret")
    _, r1 = wm.stamp(value=b"%PDF-x", darwin_id="d-1", upload_ts=100.0)
    _, r2 = wm.stamp(value=b"%PDF-y", darwin_id="d-1", upload_ts=100.0)  # different sha
    _, r3 = wm.stamp(value=b"%PDF-x", darwin_id="d-2", upload_ts=100.0)  # different id
    _, r4 = wm.stamp(value=b"%PDF-x", darwin_id="d-1", upload_ts=200.0)  # different ts
    assert len({r1.hmac_b64, r2.hmac_b64, r3.hmac_b64, r4.hmac_b64}) == 4


def test_verify_passes_for_genuine_watermark():
    wm = HmacWatermarker(signing_key=b"secret")
    pdf = b"%PDF-1.4 doc body %%EOF\n"
    marked, record = wm.stamp(value=pdf, darwin_id="d-1", upload_ts=100.0)
    assert verify(
        marked_bytes=marked, signing_key=b"secret",
        expected_darwin_id="d-1", expected_upload_ts=100.0,
        expected_sha256=record.sha256_of_original,
    )


def test_verify_fails_on_wrong_key():
    wm = HmacWatermarker(signing_key=b"real")
    pdf = b"%PDF-x %%EOF"
    marked, record = wm.stamp(value=pdf, darwin_id="d-1", upload_ts=100.0)
    assert not verify(
        marked_bytes=marked, signing_key=b"forged",
        expected_darwin_id="d-1", expected_upload_ts=100.0,
        expected_sha256=record.sha256_of_original,
    )


def test_verify_fails_on_wrong_darwin_id():
    wm = HmacWatermarker(signing_key=b"k")
    marked, record = wm.stamp(value=b"%PDF-x %%EOF", darwin_id="d-1", upload_ts=100.0)
    assert not verify(
        marked_bytes=marked, signing_key=b"k",
        expected_darwin_id="d-other", expected_upload_ts=100.0,
        expected_sha256=record.sha256_of_original,
    )


def test_verify_fails_on_missing_marker():
    """Verifying a doc that was never watermarked must fail closed."""
    plain = b"%PDF-x doc body %%EOF"
    assert not verify(
        marked_bytes=plain, signing_key=b"k",
        expected_darwin_id="d-1", expected_upload_ts=100.0,
        expected_sha256="sha",
    )


# ---------------------------------------------------------------------------
# VaultAdminService integration
# ---------------------------------------------------------------------------

def test_vault_admin_applies_watermark_to_sensitive():
    v = _Vault()
    wm = HmacWatermarker(signing_key=b"secret")
    svc = VaultAdminService(vault_resolver=lambda _: v, watermarker=wm)
    ref = svc.put(
        darwin_id="d-1", tier="sensitive", key="kyc.pdf",
        value="%PDF-1.4\nKYC content\n%%EOF\n",
    )
    stored = v.kv[ref.path]
    assert WATERMARK_MARKER.decode() in stored
    assert ref.watermarked is True


def test_vault_admin_skips_watermark_for_non_sensitive():
    """public + scoped uploads pass through unchanged (those tiers
    typically hold non-document credentials)."""
    v = _Vault()
    wm = HmacWatermarker(signing_key=b"secret")
    svc = VaultAdminService(vault_resolver=lambda _: v, watermarker=wm)
    ref = svc.put(
        darwin_id="d-1", tier="public", key="config",
        value="public config",
    )
    assert v.kv[ref.path] == "public config"
    assert ref.watermarked is False


def test_vault_admin_default_is_noop():
    """Without a custom watermarker, the default NoOp leaves bytes
    unchanged but the ref still reports watermarked=False."""
    v = _Vault()
    svc = VaultAdminService(vault_resolver=lambda _: v)
    ref = svc.put(
        darwin_id="d-1", tier="sensitive", key="kyc",
        value="%PDF-1.4 doc %%EOF",
    )
    assert v.kv[ref.path] == "%PDF-1.4 doc %%EOF"
    assert ref.watermarked is False


def test_vault_admin_records_watermark_in_sidecar():
    v = _Vault()
    wm = HmacWatermarker(signing_key=b"secret")
    svc = VaultAdminService(vault_resolver=lambda _: v, watermarker=wm)
    ref = svc.put(
        darwin_id="d-1", tier="sensitive", key="kyc.pdf",
        value="%PDF-x %%EOF", uploaded_by="alice",
    )
    sidecar_path = ref.path + ".__meta__"
    import json
    sidecar = json.loads(v.kv[sidecar_path])
    assert sidecar["watermark"] is not None
    assert sidecar["watermark"]["darwin_id"] == "d-1"
    assert sidecar["watermark"]["hmac"]


# ---------------------------------------------------------------------------
# Supervisor wiring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_supervisor_wires_hmac_watermarker():
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor(vault_watermark_key=b"platform-secret")
    status = await sup.spawn(opening_usdc=50.0)
    ref = sup.vault_put(
        status.darwin_id, tier="sensitive", key="kyc.pdf",
        value="%PDF-x KYC doc %%EOF",
        actor="alice",
    )
    assert ref.watermarked is True
    # The raw vault bytes contain the embedded marker.
    h = sup._darwins[status.darwin_id]
    stored = h.stack.vault.get(ref.path)
    assert WATERMARK_MARKER.decode() in stored
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_default_no_watermark():
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    ref = sup.vault_put(
        status.darwin_id, tier="sensitive", key="kyc",
        value="%PDF-x doc %%EOF", actor="alice",
    )
    # Without a key, the watermarker is no-op.
    assert ref.watermarked is False
    await sup.shutdown(kill_running=True)
