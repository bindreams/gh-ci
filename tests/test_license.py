"""Tests for the GPL-3.0 license file and its metadata across the project."""
from __future__ import annotations

import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GPL_3_0_SHA256 = "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"
GPL_3_0_BYTE_LENGTH = 35149


def test_license_file_matches_official_gplv3():
    license_path = REPO_ROOT / "LICENSE"
    assert license_path.exists(), "LICENSE file is missing"

    content = license_path.read_bytes()
    assert hashlib.sha256(content).hexdigest() == GPL_3_0_SHA256

    # Documentation-level assertions implied by (not independent of) the hash
    # match above -- see the plan's audit findings for why.
    assert len(content) == GPL_3_0_BYTE_LENGTH
    text = content.decode("utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 29 June 2007" in text
