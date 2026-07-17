"""Authenticated field encryption with production-safe key providers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


KEY_BYTES = 32


class KeyProvider(Protocol):
    def get_key(self) -> bytes: ...


class KeyUnavailableError(RuntimeError):
    pass


@contextmanager
def _exclusive_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a+b", buffering=0) as handle:
        if os.name == "nt":  # pragma: no cover - exercised on Windows
            import msvcrt

            if path.stat().st_size == 0:
                handle.write(b"\0")
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(slots=True)
class MemoryKeyProvider:
    """Explicit test/ephemeral provider; callers must never use it as a silent fallback."""

    key: bytes

    def __post_init__(self) -> None:
        if len(self.key) != KEY_BYTES:
            raise ValueError("XChaCha20-Poly1305 keys must be 32 bytes")

    @classmethod
    def random(cls) -> "MemoryKeyProvider":
        return cls(os.urandom(KEY_BYTES))

    def get_key(self) -> bytes:
        return self.key


class EnvironmentKeyProvider:
    def __init__(self, variable: str = "BRAINHUB_MASTER_KEY") -> None:
        self.variable = variable

    def get_key(self) -> bytes:
        encoded = os.environ.get(self.variable)
        if not encoded:
            raise KeyUnavailableError(f"{self.variable} is not set")
        try:
            key = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except Exception as exc:  # pragma: no cover - defensive boundary
            raise KeyUnavailableError(f"{self.variable} is not valid URL-safe base64") from exc
        if len(key) != KEY_BYTES:
            raise KeyUnavailableError(f"{self.variable} must decode to {KEY_BYTES} bytes")
        return key


class KeyringKeyProvider:
    """Retrieve or create a master key in the operating-system keychain."""

    def __init__(
        self,
        installation_id: str,
        *,
        service: str = "brainhub",
        lock_path: str | Path | None = None,
    ) -> None:
        self.installation_id = installation_id
        self.service = service
        lock_name = hashlib.sha256(
            f"{service}\0{installation_id}".encode("utf-8")
        ).hexdigest()
        self.lock_path = (
            Path(lock_path).expanduser()
            if lock_path is not None
            else Path.home()
            / ".local"
            / "share"
            / "brainhub"
            / "locks"
            / f"keyring-{lock_name}.lock"
        )

    def get_key(self) -> bytes:
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise KeyUnavailableError("keyring support is not installed") from exc
        try:
            encoded = keyring.get_password(self.service, self.installation_id)
            if not encoded:
                with _exclusive_file_lock(self.lock_path):
                    encoded = keyring.get_password(
                        self.service, self.installation_id
                    )
                    if not encoded:
                        key = os.urandom(KEY_BYTES)
                        encoded = base64.urlsafe_b64encode(key).decode("ascii")
                        keyring.set_password(
                            self.service, self.installation_id, encoded
                        )
        except Exception as exc:  # pragma: no cover - backend/platform dependent
            raise KeyUnavailableError("OS keychain is unavailable; set BRAINHUB_MASTER_KEY") from exc
        try:
            key = base64.urlsafe_b64decode(encoded)
        except Exception as exc:  # pragma: no cover - corrupt backend value
            raise KeyUnavailableError("stored master key is not valid base64") from exc
        if len(key) != KEY_BYTES:
            raise KeyUnavailableError("stored master key has an invalid length")
        return key


class DefaultKeyProvider:
    """Prefer an explicit environment override, otherwise require the OS keychain."""

    def __init__(self, installation_id: str) -> None:
        self.environment = EnvironmentKeyProvider()
        self.keyring = KeyringKeyProvider(installation_id)

    def get_key(self) -> bytes:
        if os.environ.get(self.environment.variable):
            return self.environment.get_key()
        return self.keyring.get_key()


class ContentCipher:
    VERSION = b"BHE1"
    _PSEUDONYM_KEY_CONTEXT = b"brainhub:cloud-pseudonym-key:v1"

    def __init__(self, provider: KeyProvider) -> None:
        self._key = provider.get_key()
        if len(self._key) != KEY_BYTES:
            raise ValueError("XChaCha20-Poly1305 keys must be 32 bytes")
        self._pseudonym_key = hmac.new(
            self._key,
            self._PSEUDONYM_KEY_CONTEXT,
            hashlib.sha256,
        ).digest()

    def encrypt(self, plaintext: bytes, *, context: bytes) -> bytes:
        try:
            from nacl.bindings import (
                crypto_aead_xchacha20poly1305_ietf_NPUBBYTES,
                crypto_aead_xchacha20poly1305_ietf_encrypt,
            )
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("PyNaCl is required for encrypted storage") from exc
        nonce = os.urandom(crypto_aead_xchacha20poly1305_ietf_NPUBBYTES)
        ciphertext = crypto_aead_xchacha20poly1305_ietf_encrypt(
            plaintext, context, nonce, self._key
        )
        return self.VERSION + nonce + ciphertext

    def decrypt(self, envelope: bytes, *, context: bytes) -> bytes:
        try:
            from nacl.bindings import (
                crypto_aead_xchacha20poly1305_ietf_NPUBBYTES,
                crypto_aead_xchacha20poly1305_ietf_decrypt,
            )
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("PyNaCl is required for encrypted storage") from exc
        if not envelope.startswith(self.VERSION):
            raise ValueError("unknown ciphertext envelope version")
        offset = len(self.VERSION)
        nonce = envelope[offset : offset + crypto_aead_xchacha20poly1305_ietf_NPUBBYTES]
        ciphertext = envelope[offset + crypto_aead_xchacha20poly1305_ietf_NPUBBYTES :]
        return crypto_aead_xchacha20poly1305_ietf_decrypt(
            ciphertext, context, nonce, self._key
        )

    def pseudonymize(self, namespace: str, value: str | bytes) -> str:
        """Create an installation-scoped, domain-separated opaque identifier."""

        if not namespace:
            raise ValueError("pseudonym namespace must not be empty")
        rendered = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        message = namespace.encode("utf-8") + b"\x00" + rendered
        return hmac.new(self._pseudonym_key, message, hashlib.sha256).hexdigest()
