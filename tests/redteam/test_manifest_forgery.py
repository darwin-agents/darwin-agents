"""Genome manifest signature forgery red-team (docs/engineering/02, 24).

Every Darwin boots through ``verify_manifest`` against the platform's
public key. An attacker who re-signs the manifest with their own key
must not be able to convince a fresh boot that the genome is authentic.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization

from darwin_dna.manifest import verify_manifest
from darwin_fakes.fake_platform import SigningService, mint_genesis


def test_redteam_manifest_signature_forgery_detected():
    """A manifest re-signed by an attacker key must NOT verify against
    the platform's public key. The genome's `verify_manifest` path
    catches this on boot."""
    platform = SigningService()
    attacker = SigningService()
    manifest, blobs, names, identity = mint_genesis(platform)
    platform_pub = serialization.load_pem_public_key(
        platform.public_key_pem(),
    )
    attacker_pub = serialization.load_pem_public_key(
        attacker.public_key_pem(),
    )
    # Genuine manifest verifies.
    verify_manifest(manifest, blobs, platform_public_key=platform_pub)
    # Same manifest against the attacker's key — must fail.
    with pytest.raises(Exception):
        verify_manifest(manifest, blobs, platform_public_key=attacker_pub)
