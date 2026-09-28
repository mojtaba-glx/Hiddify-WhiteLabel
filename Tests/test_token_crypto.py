"""Token crypto: roundtrip, fingerprint, failure modes, no/key leak in repr."""

from __future__ import annotations

import pytest

from Shared import crypto
from Shared.crypto import (
    FernetTokenCipher,
    TokenCipherError,
    fingerprint_token,
    generate_key,
    token_tail,
)
from Tests.conftest import make_fake_token


def test_roundtrip(cipher) -> None:
    plain = make_fake_token("roundtrip")
    sealed = cipher.encrypt(plain)
    assert sealed != plain
    assert plain not in sealed
    assert cipher.decrypt(sealed) == plain


def test_fingerprint_is_stable_and_unique() -> None:
    first = make_fake_token("fp")
    assert fingerprint_token(first) == fingerprint_token(first)
    assert len(fingerprint_token(first)) == 64
    assert fingerprint_token(first) != fingerprint_token(make_fake_token("other"))


def test_wrong_key_cannot_decrypt(enc_key) -> None:
    cipher = FernetTokenCipher(enc_key)
    sealed = cipher.encrypt(make_fake_token("wrongkey"))
    other = FernetTokenCipher(generate_key())
    with pytest.raises(TokenCipherError):
        other.decrypt(sealed)


def test_bad_inputs_rejected(cipher) -> None:
    with pytest.raises(TokenCipherError):
        cipher.encrypt("")
    with pytest.raises(TokenCipherError):
        cipher.encrypt("no-colon-here")
    with pytest.raises(TokenCipherError):
        cipher.decrypt("")
    with pytest.raises(TokenCipherError):
        FernetTokenCipher("not-a-valid-fernet-key")


def test_no_key_material_in_repr(enc_key, cipher) -> None:
    assert enc_key not in repr(cipher)
    assert "gAAAA" not in repr(cipher)


def test_tail_exposes_only_last_chars() -> None:
    plain = make_fake_token("tail")
    tail = token_tail(plain)
    assert tail == plain[-4:]
    assert plain not in tail
    assert crypto.TokenCipher is not None
