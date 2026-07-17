#!/usr/bin/env python3
"""Cross-platform installer for Brain Hub.

The installer intentionally uses only the Python standard library so a clean
Python 3.11+ installation is enough to bootstrap Brain Hub.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import datetime as dt
import hashlib
import json
import ntpath
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unicodedata
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, TextIO

MINIMUM_PYTHON = (3, 11)
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 50_000
MAX_COMPRESSION_RATIO = 1_000
FINGERPRINT_PATHS = (
    Path("pyproject.toml"),
    Path("packages/core/src/brainhub"),
    Path("adapters/pyproject.toml"),
    Path("adapters/src/brainhub_adapters"),
    Path("plugins/brain-hub"),
    Path(".agents/plugins/marketplace.json"),
)
FINGERPRINT_IGNORED_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
RUNTIME_COMMANDS = (
    "brainhub",
    "brainhub-adapter",
    "brainhub-codex-hook",
    "brainhub-claude-hook",
    "brainhub-cursor-hook",
    "brainhub-antigravity-hook",
)
PROFILE_MARKER_START = "# >>> brainhub installer >>>"
PROFILE_MARKER_END = "# <<< brainhub installer <<<"
RUNTIME_MANIFEST = ".brainhub-runtime.json"
MANAGED_MARKETPLACE_NAME = "brain-hub-managed"
PLUGIN_NAME = "brain-hub"

Runner = Callable[..., subprocess.CompletedProcess[str]]


class InstallerError(RuntimeError):
    """An expected, user-actionable installer failure."""


@dataclass(frozen=True)
class InstallOptions:
    """Resolved command-line options."""

    source: str
    ref: str | None
    install_root: Path
    bin_dir: Path
    install_plugin: bool = True
    modify_path: bool = True
    dry_run: bool = False


class Reporter:
    """Small status printer that keeps installer output consistent."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout

    def _write(self, kind: str, message: str) -> None:
        print(f"[{kind}] {message}", file=self.stream)

    def plan(self, message: str) -> None:
        self._write("plan", message)

    def ok(self, message: str) -> None:
        self._write("ok", message)

    def skip(self, message: str) -> None:
        self._write("skip", message)

    def warn(self, message: str) -> None:
        self._write("warn", message)


def _is_https_source(source: str) -> bool:
    return "://" in source


def github_archive_url(source: str, ref: str | None = None) -> str:
    """Validate a GitHub HTTPS source and return a downloadable archive URL."""

    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme.lower() != "https":
        raise InstallerError("remote --source must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise InstallerError("remote --source must not contain credentials, query, or fragment")

    host = (parsed.hostname or "").lower()
    if host not in {"github.com", "www.github.com", "codeload.github.com"}:
        raise InstallerError("remote --source must be a GitHub archive or repository URL")

    clean_path = parsed.path.rstrip("/")
    if host == "codeload.github.com":
        if ref:
            raise InstallerError("--ref cannot be combined with a direct archive URL")
        if not clean_path:
            raise InstallerError("invalid GitHub archive URL")
        return urllib.parse.urlunsplit(("https", host, clean_path, "", ""))

    if clean_path.lower().endswith(".zip") or "/archive/" in clean_path:
        if ref:
            raise InstallerError("--ref cannot be combined with a direct archive URL")
        return urllib.parse.urlunsplit(("https", "github.com", clean_path, "", ""))

    parts = [part for part in clean_path.split("/") if part]
    if len(parts) != 2:
        raise InstallerError(
            "GitHub repository --source must look like https://github.com/OWNER/REPO"
        )
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository:
        raise InstallerError("invalid GitHub repository URL")
    if not ref:
        raise InstallerError("--ref is required with a GitHub repository URL")
    if any(character in ref for character in "\x00\r\n"):
        raise InstallerError("--ref contains invalid characters")

    encoded_ref = urllib.parse.quote(ref, safe="")
    return f"https://github.com/{owner}/{repository}/archive/{encoded_ref}.zip"


def _copy_limited(source: BinaryIO, destination: BinaryIO, limit: int) -> int:
    copied = 0
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            return copied
        copied += len(chunk)
        if copied > limit:
            raise InstallerError("download exceeded the archive size limit")
        destination.write(chunk)


def download_archive(
    url: str,
    destination: Path,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> None:
    """Download an archive with a fixed upper size bound."""

    request = urllib.request.Request(url, headers={"User-Agent": "brainhub-installer/1"})
    try:
        response_context = opener(request, timeout=60)
        with contextlib.closing(response_context) as response:
            final_url = response.geturl()
            final = urllib.parse.urlsplit(final_url)
            if final.scheme.lower() != "https" or (final.hostname or "").lower() not in {
                "github.com",
                "www.github.com",
                "codeload.github.com",
            }:
                raise InstallerError(f"GitHub download redirected to an untrusted URL: {final_url}")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_ARCHIVE_BYTES:
                raise InstallerError("GitHub archive is larger than the allowed limit")
            with destination.open("wb") as output:
                _copy_limited(response, output, MAX_ARCHIVE_BYTES)
    except InstallerError:
        raise
    except (OSError, ValueError) as error:
        raise InstallerError(f"could not download GitHub archive: {error}") from error


def _safe_member_parts(name: str) -> tuple[str, ...]:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or normalized.startswith("/") or path.is_absolute():
        raise InstallerError(f"unsafe archive member: {name!r}")
    if ntpath.splitdrive(normalized)[0]:
        raise InstallerError(f"unsafe archive member: {name!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise InstallerError(f"unsafe archive member: {name!r}")
    for part in path.parts:
        if ":" in part or any(ord(character) < 32 for character in part):
            raise InstallerError(f"unsafe archive member: {name!r}")
        if part.endswith((" ", ".")):
            raise InstallerError(f"unsafe archive member: {name!r}")
        device_name = part.split(".", 1)[0].rstrip(" .").upper()
        if device_name in WINDOWS_RESERVED_NAMES:
            raise InstallerError(f"unsafe archive member: {name!r}")
    return path.parts


def safe_extract_zip(archive_path: Path, destination: Path) -> Path:
    """Extract a ZIP without traversal, links, special files, or zip bombs."""

    destination.mkdir(parents=True, exist_ok=True)
    destination_resolved = destination.resolve()
    extracted_size = 0
    seen: set[str] = set()

    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as error:
        raise InstallerError(f"invalid ZIP archive: {error}") from error

    with archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise InstallerError("ZIP archive contains too many files")

        for member in members:
            parts = _safe_member_parts(member.filename)
            collision_key = unicodedata.normalize("NFC", "/".join(parts)).casefold()
            if collision_key in seen:
                raise InstallerError(f"duplicate archive member: {member.filename!r}")
            seen.add(collision_key)

            if member.flag_bits & 0x1:
                raise InstallerError("encrypted ZIP archives are not supported")

            unix_mode = (member.external_attr >> 16) & 0o177777
            file_type = stat.S_IFMT(unix_mode)
            if stat.S_ISLNK(unix_mode):
                raise InstallerError(f"symbolic link in ZIP archive: {member.filename!r}")
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise InstallerError(f"special file in ZIP archive: {member.filename!r}")

            extracted_size += member.file_size
            if extracted_size > MAX_EXTRACTED_BYTES:
                raise InstallerError("ZIP expands beyond the allowed size limit")
            if member.file_size > 1024 * 1024 and (
                member.compress_size == 0
                or member.file_size / member.compress_size > MAX_COMPRESSION_RATIO
            ):
                raise InstallerError(
                    f"ZIP member has an unsafe compression ratio: {member.filename!r}"
                )

            target = destination.joinpath(*parts)
            try:
                common = os.path.commonpath((str(destination_resolved), str(target.resolve())))
            except ValueError as error:
                raise InstallerError(f"unsafe archive member: {member.filename!r}") from error
            if common != str(destination_resolved):
                raise InstallerError(f"unsafe archive member: {member.filename!r}")

            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)

    return find_checkout_root(destination)


def _looks_like_checkout(path: Path) -> bool:
    return (path / "pyproject.toml").is_file() and (path / "adapters" / "pyproject.toml").is_file()


def find_checkout_root(extracted: Path) -> Path:
    """Locate the single Brain Hub checkout root in an extracted archive."""

    if _looks_like_checkout(extracted):
        return extracted.resolve()
    candidates = [
        child.resolve()
        for child in extracted.iterdir()
        if child.is_dir() and _looks_like_checkout(child)
    ]
    if len(candidates) != 1:
        raise InstallerError("archive does not contain one recognizable Brain Hub checkout")
    return candidates[0]


def validate_checkout(source_root: Path, *, require_plugin: bool) -> Path:
    root = source_root.expanduser().resolve()
    if not _looks_like_checkout(root):
        raise InstallerError(f"{root} is not a Brain Hub checkout (missing project metadata)")
    if require_plugin:
        required = (
            root / "plugins" / "brain-hub" / ".codex-plugin" / "plugin.json",
            root / "plugins" / "brain-hub" / "hooks" / "hooks.json",
            root / "plugins" / "brain-hub" / "scripts" / "capture_hook.py",
            root / ".agents" / "plugins" / "marketplace.json",
        )
        missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
        if missing:
            raise InstallerError("checkout is missing plugin files: " + ", ".join(missing))
    return root


@contextlib.contextmanager
def materialize_source(
    source: str,
    ref: str | None,
    reporter: Reporter,
) -> Iterator[Path]:
    """Yield a local checkout for either a path or a GitHub archive source."""

    if not _is_https_source(source):
        if ref:
            raise InstallerError("--ref is only valid with a GitHub repository URL")
        yield Path(source).expanduser().resolve()
        return

    url = github_archive_url(source, ref)
    reporter.plan(f"download {url}")
    with tempfile.TemporaryDirectory(prefix="brainhub-source-") as temporary:
        temporary_path = Path(temporary)
        archive_path = temporary_path / "source.zip"
        extracted_path = temporary_path / "source"
        download_archive(url, archive_path)
        reporter.ok("downloaded GitHub source archive")
        yield safe_extract_zip(archive_path, extracted_path)


def project_version(source_root: Path) -> str:
    try:
        metadata = tomllib.loads((source_root / "pyproject.toml").read_text("utf-8"))
        raw_version = metadata["project"]["version"]
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError) as error:
        raise InstallerError(f"could not read project version: {error}") from error
    if not isinstance(raw_version, str) or not raw_version.strip():
        raise InstallerError("project version must be a non-empty string")
    version = re.sub(r"[^A-Za-z0-9._+-]+", "-", raw_version.strip()).strip(".-")
    if not version or version in {".", ".."}:
        raise InstallerError("project version cannot form a safe runtime directory")
    return version


def source_fingerprint(source_root: Path) -> str:
    """Hash install-relevant source so mutable refs never reuse stale code."""

    digest = hashlib.sha256()
    files: list[Path] = []
    for relative in FINGERPRINT_PATHS:
        candidate = source_root / relative
        if candidate.is_file():
            files.append(candidate)
        elif candidate.is_dir():
            files.extend(
                path
                for path in candidate.rglob("*")
                if path.is_file()
                and not any(
                    part in FINGERPRINT_IGNORED_NAMES
                    for part in path.relative_to(source_root).parts
                )
            )

    if not files:
        raise InstallerError("checkout contains no installable source files")

    for path in sorted(files, key=lambda item: item.relative_to(source_root).as_posix()):
        relative = path.relative_to(source_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        try:
            digest.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as error:
            raise InstallerError(f"could not fingerprint {path}: {error}") from error
    return digest.hexdigest()


def runtime_compatibility() -> str:
    implementation = sys.implementation.name
    cache_tag = sys.implementation.cache_tag or "unknown-abi"
    system = platform.system().lower() or "unknown-os"
    machine = re.sub(r"[^A-Za-z0-9._-]+", "-", platform.machine().lower())
    return f"{implementation}-{cache_tag}-{system}-{machine or 'unknown-arch'}"


def runtime_identity(source_root: Path, version: str) -> tuple[str, str]:
    fingerprint = source_fingerprint(source_root)
    return f"{version}-{runtime_compatibility()}-{fingerprint[:16]}", fingerprint


def venv_executable(venv_dir: Path, command: str, *, windows: bool) -> Path:
    if windows:
        return venv_dir / "Scripts" / f"{command}.exe"
    return venv_dir / "bin" / command


def venv_python(venv_dir: Path, *, windows: bool) -> Path:
    executable = "python.exe" if windows else "python"
    directory = "Scripts" if windows else "bin"
    # A POSIX venv's Python is normally a symlink to the base interpreter.
    # Resolving it would escape the venv and install packages into the base Python.
    return venv_dir / directory / executable


def _run_checked(runner: Runner, command: Sequence[str], description: str) -> None:
    environment = os.environ.copy()
    for variable in (
        "BASH_ENV",
        "ENV",
        "PIP_CONFIG_FILE",
        "PIP_PREFIX",
        "PIP_REQUIRE_VIRTUALENV",
        "PIP_TARGET",
        "PIP_USER",
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    ):
        environment.pop(variable, None)
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    try:
        result = runner(
            list(command),
            check=False,
            capture_output=True,
            env=environment,
            text=True,
        )
    except OSError as error:
        raise InstallerError(f"{description} failed to start: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if len(detail) > 1_500:
            detail = detail[-1_500:]
        suffix = f": {detail}" if detail else ""
        raise InstallerError(f"{description} failed{suffix}")


def smoke_test_runtime(
    venv_dir: Path,
    *,
    windows: bool,
    runner: Runner = subprocess.run,
) -> None:
    brainhub = str(venv_executable(venv_dir, "brainhub", windows=windows))
    adapter = str(venv_executable(venv_dir, "brainhub-adapter", windows=windows))
    checks = (
        ([brainhub, "--help"], "brainhub smoke test"),
        ([adapter, "--help"], "adapter smoke test"),
        ([brainhub, "ui", "--help"], "UI smoke test"),
    )
    for command, description in checks:
        _run_checked(runner, command, description)


def _atomic_write_text(path: Path, content: str, *, mode: int | None = None) -> bool:
    encoded = content.encode("utf-8")
    existing_mode: int | None = None
    try:
        if path.read_bytes() == encoded:
            if mode is not None:
                path.chmod(mode)
            return False
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        pass

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        selected_mode = mode if mode is not None else existing_mode
        if selected_mode is not None:
            temporary.chmod(selected_mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


@contextlib.contextmanager
def installation_lock(install_root: Path) -> Iterator[None]:
    """Serialize installs so two launchers cannot mutate runtime state together."""

    runtime_dir = install_root / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = runtime_dir / "install.lock"
    with lock_path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise InstallerError("another Brain Hub installation is already running") from error

        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _quarantine_runtime(
    version_dir: Path,
    runtime_id: str,
    reporter: Reporter,
    reason: str,
) -> Path:
    """Move an installer-owned but unusable runtime aside for diagnosis."""

    if version_dir.is_symlink() or not version_dir.is_dir():
        raise InstallerError(f"refusing unexpected runtime path: {version_dir}")
    quarantine = version_dir.with_name(
        f".broken-{runtime_id}-{dt.datetime.now(dt.UTC).strftime('%Y%m%d%H%M%S%f')}"
    )
    try:
        os.replace(version_dir, quarantine)
    except OSError as error:
        raise InstallerError(f"could not quarantine failed runtime at {version_dir}") from error
    reporter.warn(f"quarantined {reason} runtime at {quarantine}")
    return quarantine


def install_runtime(
    source_root: Path,
    install_root: Path,
    version: str,
    reporter: Reporter,
    *,
    windows: bool,
    runner: Runner = subprocess.run,
) -> Path:
    """Create or reuse the isolated, versioned Brain Hub virtual environment."""

    runtime_id, fingerprint = runtime_identity(source_root, version)
    versions_dir = install_root / "runtime" / "versions"
    version_dir = versions_dir / runtime_id
    venv_dir = version_dir / "venv"
    manifest_path = version_dir / RUNTIME_MANIFEST

    if version_dir.is_symlink():
        raise InstallerError(f"refusing unexpected runtime path: {version_dir}")
    if version_dir.exists():
        failure_reason: str | None = None
        manifest: dict[str, Any] | None = None
        if manifest_path.is_symlink() or not manifest_path.is_file():
            failure_reason = "incomplete"
        else:
            try:
                loaded_manifest = json.loads(manifest_path.read_text("utf-8"))
                if not isinstance(loaded_manifest, dict):
                    raise TypeError("runtime manifest must be an object")
                manifest = loaded_manifest
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
                failure_reason = "invalid-manifest"

        if manifest is not None:
            expected_manifest = {
                "compatibility": runtime_compatibility(),
                "runtime_id": runtime_id,
                "source_fingerprint": fingerprint,
                "version": version,
            }
            if any(manifest.get(key) != value for key, value in expected_manifest.items()):
                failure_reason = "incompatible"

        if failure_reason is None:
            reporter.skip(f"runtime {runtime_id} is already installed")
            try:
                smoke_test_runtime(venv_dir, windows=windows, runner=runner)
            except InstallerError:
                failure_reason = "failed"
            else:
                reporter.ok("existing runtime passed smoke tests")
                return venv_dir

        _quarantine_runtime(
            version_dir,
            runtime_id,
            reporter,
            failure_reason or "invalid",
        )

    version_dir.mkdir(parents=True)
    try:
        reporter.plan(f"create Python environment at {venv_dir}")
        _run_checked(
            runner,
            [sys.executable, "-m", "venv", str(venv_dir)],
            "virtual environment creation",
        )
        python = str(venv_python(venv_dir, windows=windows))
        reporter.plan("install Brain Hub core and adapters")
        _run_checked(
            runner,
            [
                python,
                "-m",
                "pip",
                "install",
                str(source_root),
                str(source_root / "adapters"),
            ],
            "package installation",
        )
        smoke_test_runtime(venv_dir, windows=windows, runner=runner)
        manifest = {
            "compatibility": runtime_compatibility(),
            "installed_at": dt.datetime.now(dt.UTC).isoformat(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "runtime_id": runtime_id,
            "source_fingerprint": fingerprint,
            "version": version,
        }
        _atomic_write_text(
            manifest_path,
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        )
    except BaseException:
        shutil.rmtree(version_dir, ignore_errors=True)
        raise

    reporter.ok(f"installed and verified runtime {runtime_id}")
    return venv_dir


def _managed_previous_brainhub(
    install_root: Path,
    current: dict[str, Any],
    *,
    windows: bool,
) -> Path | None:
    """Resolve a current pointer only when it names the expected managed executable."""

    runtime_id = current.get("runtime_id")
    configured_value = current.get("brainhub")
    if (
        not isinstance(runtime_id, str)
        or not re.fullmatch(r"[A-Za-z0-9._+-]+", runtime_id)
        or not isinstance(configured_value, str)
    ):
        return None

    configured = Path(configured_value)
    if not configured.is_absolute():
        return None

    versions_dir = install_root / "runtime" / "versions"
    version_dir = versions_dir / runtime_id
    expected = venv_executable(version_dir / "venv", "brainhub", windows=windows)
    manifest_path = version_dir / RUNTIME_MANIFEST
    if version_dir.is_symlink() or manifest_path.is_symlink():
        return None
    try:
        install_root_resolved = install_root.resolve(strict=True)
        versions_resolved = versions_dir.resolve(strict=True)
        configured_resolved = configured.resolve(strict=True)
        expected_resolved = expected.resolve(strict=True)
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not versions_resolved.is_relative_to(install_root_resolved)
        or configured_resolved != expected_resolved
        or not configured_resolved.is_relative_to(versions_resolved)
        or not configured_resolved.is_file()
        or not os.access(configured_resolved, os.X_OK)
        or not isinstance(manifest, dict)
        or manifest.get("runtime_id") != runtime_id
    ):
        return None
    return configured_resolved


def stop_previous_runtime(
    install_root: Path,
    new_runtime_id: str,
    reporter: Reporter,
    *,
    windows: bool,
    runner: Runner = subprocess.run,
) -> None:
    """Best-effort stop of the previous service before changing the active pointer."""

    current_path = install_root / "runtime" / "current.json"
    if not current_path.is_file() or current_path.is_symlink():
        return
    try:
        current = json.loads(current_path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        reporter.warn("could not verify the previous runtime; service stop was skipped")
        return
    if not isinstance(current, dict) or current.get("runtime_id") == new_runtime_id:
        return

    executable = _managed_previous_brainhub(
        install_root,
        current,
        windows=windows,
    )
    if executable is None:
        reporter.warn("previous runtime was not a verified managed executable; stop was skipped")
        return
    try:
        result = runner(
            [str(executable), "stop"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        reporter.warn(f"could not stop the previous Brain Hub runtime: {error}")
        return
    if result.returncode != 0:
        reporter.warn("previous Brain Hub runtime did not stop cleanly; upgrade will continue")
        return
    reporter.ok(f"stopped previous Brain Hub runtime {current['runtime_id']}")


def write_current_runtime(
    install_root: Path,
    venv_dir: Path,
    version: str,
    reporter: Reporter,
    *,
    windows: bool,
    runner: Runner = subprocess.run,
) -> Path:
    """Atomically point plugin discovery and diagnostics at the active runtime."""

    manifest_path = venv_dir.parent / RUNTIME_MANIFEST
    try:
        runtime_manifest = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InstallerError(f"could not read installed runtime manifest: {error}") from error
    current = {
        "brainhub": str(venv_executable(venv_dir, "brainhub", windows=windows)),
        "runtime_id": runtime_manifest["runtime_id"],
        "source_fingerprint": runtime_manifest["source_fingerprint"],
        "venv": str(venv_dir.resolve()),
        "version": version,
    }
    stop_previous_runtime(
        install_root,
        current["runtime_id"],
        reporter,
        windows=windows,
        runner=runner,
    )
    path = install_root / "runtime" / "current.json"
    changed = _atomic_write_text(
        path,
        json.dumps(current, indent=2, sort_keys=True) + "\n",
    )
    reporter.ok(f"{'updated' if changed else 'kept'} active runtime pointer {path}")
    return path


def _posix_shim(target: Path) -> str:
    return f'#!/bin/sh\nexec {shlex.quote(str(target))} "$@"\n'


def _windows_shim(target: Path) -> str:
    escaped = str(target).replace("%", "%%")
    return f'@echo off\r\n"{escaped}" %*\r\n'


def write_shims(
    bin_dir: Path,
    venv_dir: Path,
    reporter: Reporter,
    *,
    windows: bool,
) -> list[Path]:
    """Write stable launchers that target the selected versioned runtime."""

    written: list[Path] = []
    for command in RUNTIME_COMMANDS:
        target = venv_executable(venv_dir, command, windows=windows)
        if windows:
            shim = bin_dir / f"{command}.cmd"
            content = _windows_shim(target)
            changed = _atomic_write_text(shim, content)
        else:
            shim = bin_dir / command
            content = _posix_shim(target)
            changed = _atomic_write_text(shim, content, mode=0o755)
        reporter.ok(f"{'wrote' if changed else 'kept'} launcher {shim}")
        written.append(shim)
    return written


def _profile_block(bin_dir: Path) -> str:
    quoted = shlex.quote(str(bin_dir))
    return (
        f"{PROFILE_MARKER_START}\n"
        f'case ":${{PATH}}:" in\n'
        f"  *:{quoted}:*) ;;\n"
        f'  *) export PATH={quoted}:"${{PATH}}" ;;\n'
        f"esac\n"
        f"{PROFILE_MARKER_END}"
    )


def choose_unix_profile(home: Path, environment: dict[str, str] | None = None) -> Path:
    environment = environment or os.environ
    shell = Path(environment.get("SHELL", "")).name
    if shell == "zsh":
        return home / ".zprofile"
    if shell == "bash" and (home / ".bash_profile").exists():
        return home / ".bash_profile"
    return home / ".profile"


def register_unix_path(profile: Path, bin_dir: Path) -> bool:
    """Add or replace one managed PATH block in a shell profile."""

    write_target = profile.resolve() if profile.is_symlink() else profile
    try:
        current = write_target.read_text("utf-8")
    except FileNotFoundError:
        current = ""

    block = _profile_block(bin_dir)
    pattern = re.compile(
        rf"{re.escape(PROFILE_MARKER_START)}.*?{re.escape(PROFILE_MARKER_END)}",
        flags=re.DOTALL,
    )
    if pattern.search(current):
        unmanaged = pattern.sub("", current).rstrip()
        separator = "\n" if unmanaged else ""
        updated = f"{unmanaged}{separator}{block}\n"
    else:
        separator = "" if not current or current.endswith("\n") else "\n"
        updated = f"{current}{separator}{block}\n"
    return _atomic_write_text(write_target, updated)


def _normalized_windows_path(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(os.path.expandvars(path.strip().strip('"'))))


def _broadcast_windows_environment_change() -> None:
    try:
        ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
            0xFFFF,
            0x001A,
            0,
            "Environment",
            0x0002,
            5_000,
            None,
        )
    except (AttributeError, OSError):
        pass


def register_windows_path(
    bin_dir: Path,
    *,
    winreg_module: Any | None = None,
    broadcast: bool = True,
) -> bool:
    """Idempotently append the launcher directory to the HKCU user PATH."""

    if winreg_module is None:
        try:
            import winreg as winreg_module  # type: ignore[import-not-found,no-redef]
        except ImportError as error:
            raise InstallerError("Windows registry support is unavailable") from error

    access = getattr(winreg_module, "KEY_QUERY_VALUE", 0) | getattr(
        winreg_module, "KEY_SET_VALUE", 0
    )
    with winreg_module.CreateKeyEx(
        winreg_module.HKEY_CURRENT_USER,
        "Environment",
        0,
        access,
    ) as key:
        try:
            current, value_type = winreg_module.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current = ""
            value_type = winreg_module.REG_EXPAND_SZ

        entries = [entry for entry in str(current).split(";") if entry.strip()]
        desired = _normalized_windows_path(str(bin_dir))
        if any(_normalized_windows_path(entry) == desired for entry in entries):
            return False

        entries.append(str(bin_dir))
        new_value = ";".join(entries)
        valid_types = {
            getattr(winreg_module, "REG_SZ", value_type),
            getattr(winreg_module, "REG_EXPAND_SZ", value_type),
        }
        if value_type not in valid_types:
            value_type = winreg_module.REG_EXPAND_SZ
        winreg_module.SetValueEx(key, "Path", 0, value_type, new_value)

    if broadcast:
        _broadcast_windows_environment_change()
    return True


def install_plugin_copy(
    source_root: Path,
    install_root: Path,
    brainhub_executable: Path,
    reporter: Reporter,
    *,
    windows: bool,
    source_digest: str | None = None,
) -> Path:
    """Install a marketplace copy and generate its managed MCP configuration."""

    marketplace_root = install_root / "marketplace"
    source_plugin = source_root / "plugins" / "brain-hub"
    installed_plugin = marketplace_root / "plugins" / "brain-hub"
    source_marketplace = source_root / ".agents" / "plugins" / "marketplace.json"
    installed_marketplace = marketplace_root / ".agents" / "plugins" / "marketplace.json"
    source_resolved = source_plugin.resolve()
    destination_resolved = installed_plugin.resolve()
    if destination_resolved.is_relative_to(
        source_resolved
    ) or source_resolved.is_relative_to(destination_resolved):
        raise InstallerError("plugin install destination overlaps the source plugin")

    installed_plugin.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(
            prefix=".brain-hub-plugin-staging-",
            dir=installed_plugin.parent,
        )
    )
    staging_plugin = staging_root / "brain-hub"
    backup_root: Path | None = None
    backup_plugin: Path | None = None
    previous_moved = False
    activated = False
    try:
        shutil.copytree(
            source_plugin,
            staging_plugin,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

        manifest_path = staging_plugin / ".codex-plugin" / "plugin.json"
        try:
            plugin_manifest = json.loads(manifest_path.read_text("utf-8"))
            plugin_version = plugin_manifest["version"]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise InstallerError(f"could not configure installed plugin manifest: {error}") from error
        if not isinstance(plugin_manifest, dict) or not isinstance(plugin_version, str):
            raise InstallerError("installed plugin manifest must contain a string version")
        version_prefix = plugin_version.split("+", 1)[0]
        fingerprint = source_digest or source_fingerprint(source_root)
        plugin_manifest["version"] = (
            f"{version_prefix}+codex.src-{fingerprint[:16]}"
        )
        _atomic_write_text(
            manifest_path,
            json.dumps(plugin_manifest, indent=2, ensure_ascii=False) + "\n",
        )

        configuration = {
            "mcpServers": {
                PLUGIN_NAME: {
                    "type": "stdio",
                    "command": str(brainhub_executable.resolve()),
                    "args": ["_plugin-mcp"],
                }
            }
        }
        _atomic_write_text(
            staging_plugin / ".mcp.json",
            json.dumps(configuration, indent=2, sort_keys=True) + "\n",
        )

        hooks_path = staging_plugin / "hooks" / "hooks.json"
        try:
            hooks_configuration = json.loads(hooks_path.read_text("utf-8"))
            hook_events = hooks_configuration["hooks"]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise InstallerError(f"could not configure installed plugin hooks: {error}") from error
        hook_python = venv_python(
            brainhub_executable.parent.parent,
            windows=windows,
        )
        hook_script = installed_plugin / "scripts" / "capture_hook.py"
        posix_command = shlex.join((str(hook_python), str(hook_script)))
        escaped_python = str(hook_python).replace("'", "''")
        escaped_script = str(hook_script).replace("'", "''")
        powershell_script = f"& '{escaped_python}' '{escaped_script}'\nexit $LASTEXITCODE"
        encoded_script = base64.b64encode(powershell_script.encode("utf-16-le")).decode("ascii")
        windows_command = (
            "powershell.exe -NoLogo -NoProfile -NonInteractive "
            f"-EncodedCommand {encoded_script}"
        )
        for groups in hook_events.values():
            if not isinstance(groups, list):
                raise InstallerError("installed plugin hook groups must be arrays")
            for group in groups:
                handlers = group.get("hooks") if isinstance(group, dict) else None
                if not isinstance(handlers, list):
                    raise InstallerError("installed plugin hook handlers must be arrays")
                for handler in handlers:
                    if not isinstance(handler, dict) or handler.get("type") != "command":
                        raise InstallerError(
                            "installed plugin contains an unsupported hook handler"
                        )
                    if windows:
                        handler["commandWindows"] = windows_command
                    else:
                        handler["command"] = posix_command
        _atomic_write_text(
            hooks_path,
            json.dumps(hooks_configuration, indent=2, sort_keys=True) + "\n",
        )

        try:
            marketplace_configuration = json.loads(source_marketplace.read_text("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise InstallerError(f"could not read marketplace metadata: {error}") from error
        if not isinstance(marketplace_configuration, dict):
            raise InstallerError("marketplace metadata must contain a JSON object")
        marketplace_configuration["name"] = MANAGED_MARKETPLACE_NAME
        interface = marketplace_configuration.setdefault("interface", {})
        if not isinstance(interface, dict):
            raise InstallerError("marketplace interface metadata must contain a JSON object")
        interface["displayName"] = "Brain Hub (Managed)"
        _atomic_write_text(
            installed_marketplace,
            json.dumps(
                marketplace_configuration,
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
        )

        if installed_plugin.exists() or installed_plugin.is_symlink():
            if installed_plugin.is_symlink() or not installed_plugin.is_dir():
                raise InstallerError(
                    f"refusing unexpected installed plugin path: {installed_plugin}"
                )
            backup_root = Path(
                tempfile.mkdtemp(
                    prefix=".brain-hub-plugin-backup-",
                    dir=installed_plugin.parent,
                )
            )
            backup_plugin = backup_root / "brain-hub"
            os.replace(installed_plugin, backup_plugin)
            previous_moved = True

        os.replace(staging_plugin, installed_plugin)
        activated = True
        reporter.ok(f"installed plugin marketplace copy at {marketplace_root}")
        return marketplace_root
    except BaseException as error:
        if previous_moved and not activated and backup_plugin is not None:
            try:
                os.replace(backup_plugin, installed_plugin)
                previous_moved = False
            except OSError:
                raise InstallerError(
                    f"plugin activation failed; previous copy remains at {backup_plugin}"
                ) from error
        if isinstance(error, OSError):
            raise InstallerError(f"could not prepare or activate installed plugin: {error}") from error
        raise
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
        if backup_root is not None and (activated or not previous_moved):
            shutil.rmtree(backup_root, ignore_errors=True)


def _command_output(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stdout or ''}\n{result.stderr or ''}".strip().lower()


def _run_optional(
    runner: Runner,
    command: Sequence[str],
    *,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    options: dict[str, Any] = {
        "check": False,
        "capture_output": True,
        "text": True,
    }
    if timeout is not None:
        options["timeout"] = timeout
    return runner(list(command), **options)


def _json_output(result: subprocess.CompletedProcess[str]) -> dict[str, Any] | None:
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "")
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _installed_plugin_identity(marketplace_root: Path) -> tuple[str, str | None, Path]:
    marketplace_name = MANAGED_MARKETPLACE_NAME
    marketplace_path = marketplace_root / ".agents" / "plugins" / "marketplace.json"
    plugin_root = marketplace_root / "plugins" / PLUGIN_NAME
    try:
        marketplace = json.loads(marketplace_path.read_text("utf-8"))
        configured_name = marketplace.get("name")
        if isinstance(configured_name, str) and configured_name.strip():
            marketplace_name = configured_name
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        pass

    version: str | None = None
    try:
        manifest = json.loads(
            (plugin_root / ".codex-plugin" / "plugin.json").read_text("utf-8")
        )
        configured_version = manifest.get("version")
        if isinstance(configured_version, str) and configured_version.strip():
            version = configured_version
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        pass
    return marketplace_name, version, plugin_root


def _local_marketplace_source(entry: dict[str, Any]) -> str | None:
    source = entry.get("marketplaceSource")
    if isinstance(source, dict) and source.get("sourceType") == "local":
        configured = source.get("source")
        if isinstance(configured, str):
            return configured
    root = entry.get("root")
    return root if isinstance(root, str) else None


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except (OSError, RuntimeError):
        return False


def _marketplace_registration_state(
    payload: dict[str, Any],
    marketplace_name: str,
    marketplace_root: Path,
) -> str:
    entries = payload.get("marketplaces")
    if not isinstance(entries, list):
        return "unknown"
    matches = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("name") == marketplace_name
    ]
    if not matches:
        return "absent"
    if len(matches) != 1:
        return "conflict"
    configured_source = _local_marketplace_source(matches[0])
    if configured_source is None:
        return "conflict"
    return "verified" if _same_path(configured_source, marketplace_root) else "conflict"


def _plugin_registration_matches(
    payload: dict[str, Any],
    *,
    marketplace_name: str,
    plugin_version: str,
    plugin_root: Path,
) -> bool:
    entries = payload.get("installed")
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if (
            entry.get("name") != PLUGIN_NAME
            or entry.get("marketplaceName") != marketplace_name
            or entry.get("version") != plugin_version
            or entry.get("installed") is not True
        ):
            continue
        source = entry.get("source")
        if not isinstance(source, dict):
            return False
        configured_path = source.get("path")
        return isinstance(configured_path, str) and _same_path(configured_path, plugin_root)
    return False


def _managed_mcp_executable(
    configured: str,
    install_root: Path,
) -> bool:
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        return False
    versions = install_root / "runtime" / "versions"
    try:
        resolved = candidate.resolve(strict=True)
        versions_resolved = versions.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return (
        resolved.is_file()
        and resolved.is_relative_to(versions_resolved)
        and resolved.name.casefold() in {"brainhub", "brainhub.exe"}
    )


def install_json_mcp_config(
    config_path: Path,
    brainhub_executable: Path,
    install_root: Path,
    reporter: Reporter,
    *,
    host_name: str,
) -> str:
    """Add or upgrade one installer-owned stdio entry in a host JSON config."""

    write_target = config_path
    if config_path.is_symlink():
        try:
            write_target = config_path.resolve(strict=True)
        except (OSError, RuntimeError):
            reporter.warn(f"{host_name} MCP config is a dangling symlink; registration skipped")
            return "config-error"

    configuration: dict[str, Any]
    if write_target.exists():
        if not write_target.is_file():
            reporter.warn(f"{host_name} MCP config is not a regular file; registration skipped")
            return "config-error"
        try:
            loaded = json.loads(write_target.read_text("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            reporter.warn(f"{host_name} MCP config could not be read: {error}")
            return "config-error"
        if not isinstance(loaded, dict):
            reporter.warn(f"{host_name} MCP config is not a JSON object; registration skipped")
            return "config-error"
        configuration = loaded
    else:
        configuration = {}

    servers = configuration.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        reporter.warn(f"{host_name} mcpServers is not a JSON object; registration skipped")
        return "config-error"
    desired = {
        "type": "stdio",
        "command": str(brainhub_executable.resolve()),
        "args": ["_plugin-mcp"],
    }
    existing = servers.get(PLUGIN_NAME)
    if existing == desired:
        reporter.skip(f"{host_name} already uses the current Brain Hub MCP runtime")
        return "already-registered"
    if existing is not None:
        if (
            not isinstance(existing, dict)
            or existing.get("args") != ["_plugin-mcp"]
            or not isinstance(existing.get("command"), str)
            or not _managed_mcp_executable(existing["command"], install_root)
        ):
            reporter.warn(
                f"{host_name} already has a different brain-hub MCP entry; "
                "registration was left unchanged"
            )
            return "config-conflict"

    servers[PLUGIN_NAME] = desired
    try:
        _atomic_write_text(
            write_target,
            json.dumps(configuration, indent=2, ensure_ascii=False) + "\n",
            mode=0o600 if os.name != "nt" else None,
        )
    except OSError as error:
        reporter.warn(f"{host_name} MCP config could not be updated: {error}")
        return "config-error"
    reporter.ok(f"registered Brain Hub MCP with {host_name}")
    return "registered"


def _parse_claude_mcp_details(output: str) -> tuple[str, list[str]] | None:
    command_match = re.search(r"(?m)^\s*Command:\s*(.+?)\s*$", output)
    args_match = re.search(r"(?m)^\s*Args:\s*(.*?)\s*$", output)
    if command_match is None or args_match is None:
        return None
    command = command_match.group(1)
    rendered_args = args_match.group(1)
    args = shlex.split(rendered_args, posix=os.name != "nt") if rendered_args else []
    return command, args


def register_claude_mcp(
    brainhub_executable: Path,
    install_root: Path,
    reporter: Reporter,
    *,
    runner: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> str:
    """Register a user-scoped Claude Code MCP, upgrading only our prior path."""

    claude = which("claude")
    if not claude:
        reporter.skip("Claude Code CLI not found; MCP registration was skipped")
        return "claude-absent"
    desired_command = str(brainhub_executable.resolve())
    desired = {
        "type": "stdio",
        "command": desired_command,
        "args": ["_plugin-mcp"],
    }
    try:
        current = _run_optional(
            runner,
            [claude, "mcp", "get", PLUGIN_NAME],
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        reporter.warn(f"Claude Code MCP inspection was skipped: {error}")
        return "config-error"

    previous: tuple[str, list[str]] | None = None
    if current.returncode == 0:
        previous = _parse_claude_mcp_details(
            f"{current.stdout or ''}\n{current.stderr or ''}"
        )
        if previous == (desired_command, ["_plugin-mcp"]):
            reporter.skip("Claude Code already uses the current Brain Hub MCP runtime")
            return "already-registered"
        if (
            previous is None
            or previous[1] != ["_plugin-mcp"]
            or not _managed_mcp_executable(previous[0], install_root)
        ):
            reporter.warn(
                "Claude Code already has a different brain-hub MCP entry; "
                "registration was left unchanged"
            )
            return "config-conflict"
        try:
            removed = _run_optional(
                runner,
                [claude, "mcp", "remove", PLUGIN_NAME, "--scope", "user"],
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            reporter.warn(f"Claude Code MCP upgrade was skipped: {error}")
            return "config-error"
        if removed.returncode != 0:
            reporter.warn("Claude Code could not remove its prior managed MCP entry")
            return "config-error"
    elif not any(
        marker in _command_output(current)
        for marker in ("no mcp server", "not found", "does not exist")
    ):
        reporter.warn("Claude Code MCP inspection failed; registration was skipped")
        return "config-error"

    payload = json.dumps(desired, separators=(",", ":"))
    try:
        added = _run_optional(
            runner,
            [
                claude,
                "mcp",
                "add-json",
                "--scope",
                "user",
                PLUGIN_NAME,
                payload,
            ],
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        added = subprocess.CompletedProcess([], 1, "", str(error))
    if added.returncode == 0:
        reporter.ok("registered Brain Hub MCP with Claude Code")
        return "registered"

    if previous is not None:
        rollback = json.dumps(
            {
                "type": "stdio",
                "command": previous[0],
                "args": previous[1],
            },
            separators=(",", ":"),
        )
        try:
            _run_optional(
                runner,
                [
                    claude,
                    "mcp",
                    "add-json",
                    "--scope",
                    "user",
                    PLUGIN_NAME,
                    rollback,
                ],
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    reporter.warn("Claude Code MCP registration failed")
    return "config-error"


def register_agent_mcps(
    brainhub_executable: Path,
    install_root: Path,
    home: Path,
    reporter: Reporter,
    *,
    runner: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, str]:
    """Register supported global MCP surfaces without overwriting user entries."""

    results = {
        "claude": register_claude_mcp(
            brainhub_executable,
            install_root,
            reporter,
            runner=runner,
            which=which,
        )
    }
    cursor_config = home / ".cursor" / "mcp.json"
    if which("cursor-agent") or which("cursor") or cursor_config.is_file():
        results["cursor"] = install_json_mcp_config(
            cursor_config,
            brainhub_executable,
            install_root,
            reporter,
            host_name="Cursor",
        )
    else:
        reporter.skip("Cursor not found; MCP registration was skipped")
        results["cursor"] = "cursor-absent"

    antigravity_config = home / ".gemini" / "config" / "mcp_config.json"
    if (
        which("agy")
        or which("agy-ide")
        or antigravity_config.is_file()
    ):
        results["antigravity"] = install_json_mcp_config(
            antigravity_config,
            brainhub_executable,
            install_root,
            reporter,
            host_name="Antigravity",
        )
    else:
        reporter.skip("Antigravity not found; MCP registration was skipped")
        results["antigravity"] = "antigravity-absent"
    return results


def register_codex_plugin(
    marketplace_root: Path,
    reporter: Reporter,
    *,
    runner: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> str:
    """Best-effort Codex registration; it never makes runtime install fail."""

    codex = which("codex")
    if not codex:
        reporter.skip("Codex CLI not found; plugin registration was skipped")
        return "codex-absent"

    marketplace_name, plugin_version, plugin_root = _installed_plugin_identity(
        marketplace_root
    )
    selector = f"{PLUGIN_NAME}@{marketplace_name}"
    marketplace_verified = False

    try:
        marketplace_list = _run_optional(
            runner,
            [codex, "plugin", "marketplace", "list", "--json"],
        )
    except OSError as error:
        reporter.warn(f"Codex marketplace inspection was skipped: {error}")
        return "marketplace-error"

    marketplace_payload = _json_output(marketplace_list)
    if marketplace_payload is not None:
        state = _marketplace_registration_state(
            marketplace_payload,
            marketplace_name,
            marketplace_root,
        )
        if state == "conflict":
            reporter.skip(
                f"a different {marketplace_name} marketplace is already registered; "
                "Codex registration was left unchanged"
            )
            return "marketplace-conflict"
        marketplace_verified = state == "verified"
    elif marketplace_list.returncode != 0:
        output = _command_output(marketplace_list)
        conflict_markers = (
            "conflict",
            "different source",
            "already registered from",
            "marketplace name",
        )
        if any(marker in output for marker in conflict_markers):
            reporter.skip(
                "a conflicting Brain Hub marketplace is already registered; "
                "Codex registration was left unchanged"
            )
            return "marketplace-conflict"
        if "already" in output or "exists" in output:
            reporter.skip(
                "an existing marketplace could not be verified; "
                "Codex plugin registration was left unchanged"
            )
            return "marketplace-unverified"

    if not marketplace_verified:
        try:
            marketplace = _run_optional(
                runner,
                [codex, "plugin", "marketplace", "add", str(marketplace_root)],
            )
        except OSError as error:
            reporter.warn(f"Codex marketplace registration was skipped: {error}")
            return "marketplace-error"
        if marketplace.returncode != 0:
            output = _command_output(marketplace)
            if any(
                marker in output
                for marker in (
                    "conflict",
                    "different source",
                    "already registered from",
                    "marketplace name",
                )
            ):
                reporter.skip(
                    "a conflicting Brain Hub marketplace is already registered; "
                    "Codex registration was left unchanged"
                )
                return "marketplace-conflict"
            if "already" in output or "exists" in output:
                reporter.skip(
                    "an existing marketplace could not be verified; "
                    "Codex plugin registration was left unchanged"
                )
                return "marketplace-unverified"
            reporter.warn("Codex marketplace registration failed; runtime is installed")
            return "marketplace-error"

    try:
        plugin = _run_optional(
            runner,
            [codex, "plugin", "add", selector],
        )
    except OSError as error:
        reporter.warn(f"Codex plugin registration was skipped: {error}")
        return "plugin-error"

    add_reported_existing = False
    if plugin.returncode != 0:
        output = _command_output(plugin)
        if "already" in output or "exists" in output:
            add_reported_existing = True
        else:
            reporter.warn("Codex plugin registration failed; runtime is installed")
            return "plugin-error"

    if plugin_version is None:
        reporter.warn(
            "Codex plugin was registered but its installed version could not be verified"
        )
        return "plugin-unverified"

    try:
        plugin_list = _run_optional(
            runner,
            [codex, "plugin", "list", "--json"],
        )
    except OSError as error:
        reporter.warn(f"Codex plugin verification was skipped: {error}")
        return "plugin-unverified"
    plugin_payload = _json_output(plugin_list)
    if plugin_payload is None or not _plugin_registration_matches(
        plugin_payload,
        marketplace_name=marketplace_name,
        plugin_version=plugin_version,
        plugin_root=plugin_root,
    ):
        reporter.warn(
            "Codex did not report the expected Brain Hub plugin version; "
            "restart Codex and rerun the installer"
        )
        return "plugin-unverified"

    if add_reported_existing:
        reporter.ok("verified the current brain-hub plugin in Codex")
        return "already-installed"
    reporter.ok("registered and verified the brain-hub plugin with Codex")
    return "registered"


def install_checkout(
    source_root: Path,
    options: InstallOptions,
    reporter: Reporter,
    *,
    runner: Runner = subprocess.run,
    windows: bool | None = None,
    home: Path | None = None,
    environment: dict[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Path:
    """Install from an already materialized checkout and return its venv."""

    root = validate_checkout(source_root, require_plugin=options.install_plugin)
    version = project_version(root)
    windows = os.name == "nt" if windows is None else windows
    home = Path.home() if home is None else home
    runtime_id, fingerprint = runtime_identity(root, version)
    version_dir = options.install_root / "runtime" / "versions" / runtime_id
    venv_dir = version_dir / "venv"

    reporter.plan(f"source: {root}")
    reporter.plan(f"runtime: {version_dir}")
    reporter.plan(f"launchers: {options.bin_dir}")
    if options.dry_run:
        if options.install_plugin:
            reporter.plan(f"plugin marketplace: {options.install_root / 'marketplace'}")
            reporter.plan("register detected Codex, Claude Code, Cursor, and Antigravity hosts")
        reporter.skip("dry run requested; no files or registrations were changed")
        return venv_dir

    with installation_lock(options.install_root):
        installed_venv = install_runtime(
            root,
            options.install_root,
            version,
            reporter,
            windows=windows,
            runner=runner,
        )
        write_current_runtime(
            options.install_root,
            installed_venv,
            version,
            reporter,
            windows=windows,
            runner=runner,
        )
        write_shims(
            options.bin_dir,
            installed_venv,
            reporter,
            windows=windows,
        )

        if options.modify_path:
            if windows:
                changed = register_windows_path(options.bin_dir)
                reporter.ok(f"{'updated' if changed else 'kept'} the Windows user PATH")
            else:
                profile = choose_unix_profile(home, environment)
                changed = register_unix_path(profile, options.bin_dir)
                reporter.ok(f"{'updated' if changed else 'kept'} PATH block in {profile}")
        else:
            reporter.skip("PATH modification disabled by --no-modify-path")

        if options.install_plugin:
            brainhub = venv_executable(installed_venv, "brainhub", windows=windows)
            marketplace_root = install_plugin_copy(
                root,
                options.install_root,
                brainhub,
                reporter,
                windows=windows,
                source_digest=fingerprint,
            )
            register_codex_plugin(
                marketplace_root,
                reporter,
                runner=runner,
                which=which,
            )
            register_agent_mcps(
                brainhub,
                options.install_root,
                home,
                reporter,
                runner=runner,
                which=which,
            )
        else:
            reporter.skip("plugin installation disabled by --no-plugin")

    reporter.ok("Brain Hub installation complete")
    if str(options.bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        reporter.warn("open a new terminal before using the brainhub command")
    reporter.plan("next: brainhub ui")
    return installed_venv


def execute(
    options: InstallOptions,
    reporter: Reporter,
    *,
    runner: Runner = subprocess.run,
) -> Path | None:
    """Resolve a source and perform one installation."""

    if _is_https_source(options.source) and options.dry_run:
        url = github_archive_url(options.source, options.ref)
        reporter.plan(f"download {url}")
        reporter.plan(
            f"stage a versioned runtime under {options.install_root / 'runtime' / 'versions'}"
        )
        reporter.plan(f"write launchers under {options.bin_dir}")
        if options.install_plugin:
            reporter.plan(f"install plugin under {options.install_root / 'marketplace'}")
            reporter.plan("register detected Codex, Claude Code, Cursor, and Antigravity hosts")
        reporter.skip("dry run requested; no download, files, or registrations changed")
        return None

    with materialize_source(options.source, options.ref, reporter) as source_root:
        return install_checkout(source_root, options, reporter, runner=runner)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install Brain Hub from a checkout or GitHub archive.",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="local checkout or HTTPS GitHub repository/archive URL",
    )
    parser.add_argument(
        "--ref",
        help="Git ref for a GitHub repository URL (for example v0.1.0 or main)",
    )
    parser.add_argument(
        "--install-root",
        type=Path,
        default=Path.home() / ".local" / "share" / "brainhub",
        help="managed installation root",
    )
    parser.add_argument(
        "--bin-dir",
        type=Path,
        default=Path.home() / ".local" / "bin",
        help="directory for stable command launchers",
    )
    parser.add_argument(
        "--no-plugin",
        action="store_true",
        help="do not copy or register the Codex plugin",
    )
    parser.add_argument(
        "--no-modify-path",
        action="store_true",
        help="do not add the launcher directory to the user PATH",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the installation plan without downloading or changing files",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if sys.version_info < MINIMUM_PYTHON:
        print("[error] Brain Hub requires Python 3.11 or newer", file=sys.stderr)
        return 1

    arguments = build_parser().parse_args(argv)
    options = InstallOptions(
        source=arguments.source,
        ref=arguments.ref,
        install_root=arguments.install_root.expanduser().resolve(),
        bin_dir=arguments.bin_dir.expanduser().resolve(),
        install_plugin=not arguments.no_plugin,
        modify_path=not arguments.no_modify_path,
        dry_run=arguments.dry_run,
    )
    reporter = Reporter()
    try:
        execute(options, reporter)
    except InstallerError as error:
        print(f"[error] {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("[error] installation interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
