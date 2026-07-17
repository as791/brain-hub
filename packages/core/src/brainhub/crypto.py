"""Authenticated field encryption with production-safe key providers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import stat
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


KEY_BYTES = 32


class KeyProvider(Protocol):
    def get_key(self) -> bytes: ...


class KeyUnavailableError(RuntimeError):
    pass


class KeyringUnavailableError(KeyUnavailableError):
    """The operating-system keyring backend cannot currently be used."""


def _installation_digest(service: str, installation_id: str) -> str:
    return hashlib.sha256(f"{service}\0{installation_id}".encode("utf-8")).hexdigest()


def _validate_private_owner_and_mode(
    metadata: os.stat_result,
    *,
    expected_mode: int,
    label: str,
) -> None:
    if os.name != "posix":
        return
    getuid = getattr(os, "getuid", None)
    if getuid is not None and metadata.st_uid != getuid():
        raise KeyUnavailableError(f"{label} is not owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise KeyUnavailableError(
            f"{label} must have POSIX mode {expected_mode:04o}"
        )


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = path.lstat()
    except OSError as exc:
        raise KeyUnavailableError("private key directory is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise KeyUnavailableError("private key directory must not be a symbolic link")
    if not stat.S_ISDIR(metadata.st_mode):
        raise KeyUnavailableError("private key directory must be a directory")
    _validate_private_owner_and_mode(
        metadata,
        expected_mode=0o700,
        label="private key directory",
    )


def _validate_private_file(
    metadata: os.stat_result,
    *,
    label: str,
) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise KeyUnavailableError(f"{label} must not be a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise KeyUnavailableError(f"{label} must be a regular file")
    _validate_private_owner_and_mode(
        metadata,
        expected_mode=0o600,
        label=label,
    )


def _open_private_file(path: Path, flags: int, *, label: str) -> int:
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    except OSError as exc:
        raise KeyUnavailableError(f"{label} could not be inspected") from exc
    if before is not None:
        _validate_private_file(before, label=label)

    secure_flags = flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, secure_flags, 0o600)
    except OSError as exc:
        raise KeyUnavailableError(f"{label} is unavailable") from exc
    try:
        after = os.fstat(descriptor)
        _validate_private_file(after, label=label)
        if before is not None and (before.st_dev, before.st_ino) != (
            after.st_dev,
            after.st_ino,
        ):
            raise KeyUnavailableError(f"{label} changed while it was being opened")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_private_file(path: Path, *, label: str) -> bytes:
    try:
        path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise KeyUnavailableError(f"{label} could not be inspected") from exc
    descriptor = _open_private_file(path, os.O_RDONLY, label=label)
    with os.fdopen(descriptor, "rb") as handle:
        try:
            return handle.read()
        except OSError as exc:
            raise KeyUnavailableError(f"{label} could not be read") from exc


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise KeyUnavailableError("private key directory could not be synchronized") from exc


def _create_private_file_once(path: Path, payload: bytes, *, label: str) -> bytes:
    """Atomically create a private file, returning a concurrently created value."""

    _ensure_private_directory(path.parent)
    try:
        return _read_private_file(path, label=label)
    except FileNotFoundError:
        pass

    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
    except OSError as exc:
        raise KeyUnavailableError(f"{label} could not be staged") from exc
    temporary = Path(temporary_name)
    try:
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return _read_private_file(path, label=label)
        stored = _read_private_file(path, label=label)
        if stored != payload:
            raise KeyUnavailableError(f"{label} changed during creation")
        return stored
    except KeyUnavailableError:
        raise
    except OSError as exc:
        raise KeyUnavailableError(f"{label} could not be created") from exc
    finally:
        temporary.unlink(missing_ok=True)
        _fsync_directory(path.parent)


@contextmanager
def _exclusive_file_lock(path: Path):
    _ensure_private_directory(path.parent)
    descriptor = _open_private_file(
        path,
        os.O_CREAT | os.O_RDWR,
        label="private key lock",
    )
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
        lock_name = _installation_digest(service, installation_id)
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
        return self._get_key(create=True)

    def get_existing_key(self) -> bytes:
        """Read the selected keyring key without silently creating a replacement."""

        return self._get_key(create=False)

    def _get_key(self, *, create: bool) -> bytes:
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise KeyringUnavailableError("keyring support is not installed") from exc
        try:
            encoded = keyring.get_password(self.service, self.installation_id)
        except Exception as exc:  # pragma: no cover - backend/platform dependent
            raise KeyringUnavailableError("OS keychain is unavailable") from exc
        if not encoded:
            if not create:
                raise KeyUnavailableError("stored OS keychain master key is missing")
            try:
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
            except KeyUnavailableError:
                raise
            except Exception as exc:  # pragma: no cover - backend/platform dependent
                raise KeyringUnavailableError("OS keychain is unavailable") from exc
        try:
            key = base64.urlsafe_b64decode(encoded)
        except Exception as exc:  # pragma: no cover - corrupt backend value
            raise KeyUnavailableError("stored master key is not valid base64") from exc
        if len(key) != KEY_BYTES:
            raise KeyUnavailableError("stored master key has an invalid length")
        return key


class LocalFileKeyProvider:
    """Store one installation key in a hardened per-user file."""

    def __init__(
        self,
        installation_id: str,
        *,
        service: str = "brainhub",
        state_dir: str | Path | None = None,
    ) -> None:
        digest = _installation_digest(service, installation_id)
        self.state_dir = (
            Path(state_dir).expanduser()
            if state_dir is not None
            else Path.home() / ".local" / "share" / "brainhub" / "keys"
        )
        self.key_path = self.state_dir / f"{digest}.key"
        self.lock_path = self.state_dir / f"{digest}.key.lock"

    def get_key(self) -> bytes:
        """Read or atomically create the first local key for this installation."""

        with _exclusive_file_lock(self.lock_path):
            try:
                key = _read_private_file(self.key_path, label="local master key")
            except FileNotFoundError:
                key = _create_private_file_once(
                    self.key_path,
                    os.urandom(KEY_BYTES),
                    label="local master key",
                )
            if len(key) != KEY_BYTES:
                raise KeyUnavailableError("local master key has an invalid length")
            return key

    def get_existing_key(self) -> bytes:
        """Read the selected local key without silently replacing a missing key."""

        try:
            key = _read_private_file(self.key_path, label="local master key")
        except FileNotFoundError as exc:
            raise KeyUnavailableError("local master key is missing") from exc
        if len(key) != KEY_BYTES:
            raise KeyUnavailableError("local master key has an invalid length")
        return key


class DefaultKeyProvider:
    """Prefer an explicit override, then retain one automatic provider forever."""

    KEYRING_CHOICE = b"keyring-v1\n"
    LOCAL_FILE_CHOICE = b"local-file-v1\n"

    def __init__(
        self,
        installation_id: str,
        *,
        service: str = "brainhub",
        state_dir: str | Path | None = None,
        keyring_provider: KeyringKeyProvider | None = None,
    ) -> None:
        self.environment = EnvironmentKeyProvider()
        self._keyring_was_injected = keyring_provider is not None
        self.keyring = keyring_provider or KeyringKeyProvider(
            installation_id,
            service=service,
        )
        self.local_file = LocalFileKeyProvider(
            installation_id,
            service=service,
            state_dir=state_dir,
        )
        digest = _installation_digest(service, installation_id)
        self.state_dir = self.local_file.state_dir
        self.provider_path = self.state_dir / f"{digest}.provider"
        self.lock_path = self.state_dir / f"{digest}.provider.lock"

    @staticmethod
    def _interactive_stdin_available() -> bool:
        """Return whether first-use keychain interaction can reach a user."""

        try:
            return bool(sys.stdin is not None and sys.stdin.isatty())
        except (AttributeError, OSError, ValueError):
            return False

    def _provider_choice(self) -> bytes | None:
        try:
            choice = _read_private_file(
                self.provider_path,
                label="master key provider marker",
            )
        except FileNotFoundError:
            return None
        if choice not in {self.KEYRING_CHOICE, self.LOCAL_FILE_CHOICE}:
            raise KeyUnavailableError("master key provider marker is invalid")
        return choice

    def _persist_provider_choice(self, choice: bytes) -> None:
        stored = _create_private_file_once(
            self.provider_path,
            choice,
            label="master key provider marker",
        )
        if stored != choice:
            raise KeyUnavailableError("master key provider choice conflicts with existing state")

    def get_key(self) -> bytes:
        if os.environ.get(self.environment.variable):
            return self.environment.get_key()
        with _exclusive_file_lock(self.lock_path):
            choice = self._provider_choice()
            if choice == self.KEYRING_CHOICE:
                return self.keyring.get_existing_key()
            if choice == self.LOCAL_FILE_CHOICE:
                return self.local_file.get_existing_key()

            if (
                not self._keyring_was_injected
                and not self._interactive_stdin_available()
            ):
                key = self.local_file.get_key()
                selected = self.LOCAL_FILE_CHOICE
            else:
                try:
                    key = self.keyring.get_key()
                    selected = self.KEYRING_CHOICE
                except KeyringUnavailableError:
                    key = self.local_file.get_key()
                    selected = self.LOCAL_FILE_CHOICE
            self._persist_provider_choice(selected)
            return key


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
