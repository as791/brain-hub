"""Brain Hub administration and local daemon CLI."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import webbrowser
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from threading import Event, Thread
from typing import Annotated, BinaryIO, Iterator
from urllib import error, request

import typer
from pydantic import ValidationError

from .api import PRODUCT_ID, PRODUCT_VERSION, ApiSettings, create_app
from .crypto import ContentCipher, DefaultKeyProvider
from .demo import demo_event, seed_demo
from .graph import EvidenceGraph
from .mcp_server import run_mcp
from .models import BrainEvent, FeedbackRequest, stable_id
from .policy import CapturePolicyError
from .service import BrainHubService
from .store import (
    DemoResetRefused,
    EventIntegrityError,
    EventStore,
    ProjectionIntegrityError,
)


app = typer.Typer(
    name="brainhub",
    no_args_is_help=True,
    help="Local-first evidence-backed graph memory for AI agent workstreams.",
)

SERVICE_START_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ManagedServiceState:
    """Private authority tying lifecycle commands to one exact service instance."""

    pid: int
    instance_id: str
    control_token: str
    api_port: int
    ui_port: int
    python_executable: str
    config_fingerprint: str
    product: str = PRODUCT_ID
    schema_version: int = 2

    @classmethod
    def from_json(cls, value: object) -> ManagedServiceState:
        if not isinstance(value, dict):
            raise ValueError("service state must be an object")
        state = cls(
            pid=int(value["pid"]),
            instance_id=str(value["instance_id"]),
            control_token=str(value["control_token"]),
            api_port=int(value["api_port"]),
            ui_port=int(value["ui_port"]),
            python_executable=str(value["python_executable"]),
            config_fingerprint=str(value.get("config_fingerprint", "")),
            product=str(value.get("product", "")),
            # v1 had every control field required to retire it safely, but no
            # authority fingerprint. Parse it as stale rather than orphaning it.
            schema_version=int(value.get("schema_version", 1)),
        )
        if (
            state.pid <= 0
            or not state.instance_id
            or len(state.control_token) < 32
            or not 1 <= state.api_port <= 65535
            or not 1 <= state.ui_port <= 65535
            or not state.python_executable
            or state.product != PRODUCT_ID
            or state.schema_version not in {1, 2}
            or (
                state.schema_version == 2
                and (
                    len(state.config_fingerprint) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in state.config_fingerprint
                    )
                )
            )
        ):
            raise ValueError("invalid service state")
        return state


def default_db_path() -> Path:
    configured = os.environ.get("BRAINHUB_DB_PATH") or os.environ.get("BRAINHUB_DB")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "share" / "brainhub" / "brainhub.db"


def _normalized_path_identity(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve(strict=False)))


def _semantic_enabled() -> bool:
    return os.environ.get("BRAINHUB_SEMANTIC", "true").casefold() not in {
        "0",
        "false",
        "no",
    }


def _raw_content_enabled() -> bool:
    return os.environ.get("BRAINHUB_ALLOW_RAW_CONTENT", "").casefold() in {
        "1",
        "true",
        "yes",
    }


def _secret_config_identity(variable: str) -> dict[str, object]:
    value = os.environ.get(variable)
    if value is None:
        return {"present": False}
    digest = hashlib.sha256(
        b"brainhub-service-config\0"
        + variable.encode("utf-8")
        + b"\0"
        + value.encode("utf-8")
    ).hexdigest()
    return {"digest": digest, "present": True}


def _service_config_fingerprint() -> str:
    configured_spool = os.environ.get("BRAINHUB_SPOOL")
    spool_path = (
        Path(configured_spool)
        if configured_spool
        else Path.home() / ".brainhub" / "spool"
    )
    effective = {
        "allow_raw_content": _raw_content_enabled(),
        "api_token": _secret_config_identity("BRAINHUB_API_TOKEN"),
        "db_path": _normalized_path_identity(default_db_path()),
        "master_key": _secret_config_identity("BRAINHUB_MASTER_KEY"),
        "semantic": _semantic_enabled(),
        "spool_path": _normalized_path_identity(spool_path),
    }
    encoded = json.dumps(
        effective,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_service(
    db_path: Path | None = None,
    *,
    semantic: bool | None = None,
) -> BrainHubService:
    selected = (db_path or default_db_path()).expanduser()
    key_account = stable_id("installation", str(selected.resolve()))
    cipher = ContentCipher(DefaultKeyProvider(key_account))
    store = EventStore(selected, cipher)
    enabled = _semantic_enabled() if semantic is None else semantic
    return BrainHubService(
        store,
        enable_semantic=enabled,
        allow_raw_content=_raw_content_enabled(),
    )


def _write(value) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    typer.echo(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _web_asset_dir() -> Path:
    return Path(__file__).with_name("web_dist")


def _runtime_dir() -> Path:
    return Path.home() / ".local" / "share" / "brainhub" / "run"


def _service_state_path() -> Path:
    return _runtime_dir() / "service.json"


def _legacy_service_pid_path() -> Path:
    return _runtime_dir() / "service.pid"


def _service_log_path() -> Path:
    return _runtime_dir() / "service.log"


def _service_lock_path() -> Path:
    return _runtime_dir() / "service.lock"


def _windows_service_mutex_name() -> str:
    authority = _normalized_path_identity(_runtime_dir()).encode("utf-8")
    digest = hashlib.sha256(authority).hexdigest()
    return f"Local\\BrainHub.Service.{digest}"


def _python_executable_identity() -> str:
    """Return a stable, exact identity for the interpreter hosting this command."""

    # Do not resolve a POSIX venv's Python symlink: distinct managed runtimes
    # commonly target the same base interpreter and must still be distinguishable.
    return os.path.normcase(os.path.abspath(sys.executable))


def _start_adapter_watcher(
    token: str | None,
    *,
    api_port: int,
) -> tuple[Event | None, Thread | None]:
    """Drain hook events with the supervised service when adapters are installed."""

    try:
        from brainhub_adapters.hook import default_spool_path
        from brainhub_adapters.spool import BoundedSpool
        from brainhub_adapters.watch import WatchSettings, watch_spool
    except ImportError:
        return None, None

    try:
        stop = Event()
        spool = BoundedSpool(default_spool_path())
        settings = WatchSettings(
            endpoint=f"http://127.0.0.1:{api_port}/v1/events",
            api_token=token,
        )
        thread = Thread(
            target=watch_spool,
            args=(spool, settings),
            kwargs={"stop_event": stop},
            name="brainhub-adapter-watcher",
            daemon=True,
        )
        thread.start()
        return stop, thread
    except Exception as exc:
        print(
            f"Brain Hub adapter watcher disabled ({type(exc).__name__})",
            file=sys.stderr,
        )
        return None, None


def _flush_spool_directly(
    spool,
    service: BrainHubService,
    *,
    limit: int = 100,
):
    """Drain adapter events into the MCP process without an HTTP sidecar."""

    from brainhub_adapters.deliver import FlushResult, default_quarantine

    delivered = 0
    quarantined = 0
    failure: str | None = None
    rejected = default_quarantine(spool)
    permanent_failures = (
        CapturePolicyError,
        EventIntegrityError,
        ProjectionIntegrityError,
        ValidationError,
        ValueError,
    )
    for path, payload in spool.pending(limit=limit):
        try:
            service.record(BrainEvent.model_validate(payload))
        except permanent_failures as exc:
            status = 409 if isinstance(exc, EventIntegrityError) else 422
            try:
                rejected.add(
                    payload,
                    http_status=status,
                    original_spool_file=path.name,
                )
                spool.acknowledge(path)
            except (OSError, ValueError) as quarantine_error:
                failure = (
                    "could not quarantine rejected event: "
                    f"{type(quarantine_error).__name__}"
                )
                break
            quarantined += 1
            continue
        except Exception as exc:
            # Keep event content and exception text out of the stdio MCP log.
            failure = f"direct spool delivery failed: {type(exc).__name__}"
            break
        spool.acknowledge(path)
        delivered += 1
    remaining = sum(1 for _ in spool.pending())
    return FlushResult(
        delivered,
        remaining,
        failure,
        quarantined,
        str(rejected.root) if quarantined else None,
    )


def _start_direct_adapter_watcher(
    service: BrainHubService,
) -> tuple[Event | None, Thread | None]:
    """Continuously ingest passive hooks while an MCP host keeps stdio open."""

    try:
        from brainhub_adapters.hook import default_spool_path
        from brainhub_adapters.spool import BoundedSpool
        from brainhub_adapters.watch import WatchSettings, watch_spool
    except ImportError:
        return None, None

    try:
        stop = Event()
        spool = BoundedSpool(default_spool_path())

        def direct_flusher(selected_spool, **_kwargs):
            return _flush_spool_directly(selected_spool, service)

        thread = Thread(
            target=watch_spool,
            args=(spool, WatchSettings()),
            kwargs={
                "flusher": direct_flusher,
                "stop_event": stop,
            },
            name="brainhub-mcp-adapter-watcher",
            daemon=True,
        )
        thread.start()
        return stop, thread
    except Exception as exc:
        print(
            f"Brain Hub direct adapter watcher disabled ({type(exc).__name__})",
            file=sys.stderr,
        )
        return None, None


def _lock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _windows_mutex_api():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    create_mutex.restype = wintypes.HANDLE
    open_mutex = kernel32.OpenMutexW
    open_mutex.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    open_mutex.restype = wintypes.HANDLE
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait_for_single_object.restype = wintypes.DWORD
    release_mutex = kernel32.ReleaseMutex
    release_mutex.argtypes = (wintypes.HANDLE,)
    release_mutex.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    return (
        ctypes,
        create_mutex,
        open_mutex,
        wait_for_single_object,
        release_mutex,
        close_handle,
    )


def _acquire_windows_service_mutex():
    (
        ctypes,
        create_mutex,
        _open_mutex,
        wait_for_single_object,
        release_mutex,
        close_handle,
    ) = _windows_mutex_api()
    handle = create_mutex(None, False, _windows_service_mutex_name())
    if not handle:
        raise OSError(
            ctypes.get_last_error(),
            "could not create the Brain Hub service mutex",
        )
    wait_result = wait_for_single_object(handle, 0)
    if wait_result in {0x00000000, 0x00000080}:
        return handle, release_mutex, close_handle
    close_handle(handle)
    if wait_result == 0x00000102:
        raise BlockingIOError("Brain Hub service mutex is already held")
    raise OSError(
        ctypes.get_last_error(),
        "could not acquire the Brain Hub service mutex",
    )


def _windows_service_mutex_is_held() -> bool:
    (
        ctypes,
        _create_mutex,
        open_mutex,
        _wait_for_single_object,
        _release_mutex,
        close_handle,
    ) = _windows_mutex_api()
    synchronize = 0x00100000
    error_file_not_found = 2
    handle = open_mutex(synchronize, False, _windows_service_mutex_name())
    if not handle:
        # Access denial or an indeterminate query must not allow a duplicate
        # daemon to start. A missing named object is the only definitive miss.
        return ctypes.get_last_error() != error_file_not_found
    close_handle(handle)
    return True


@contextmanager
def _service_lock() -> Iterator[None]:
    if os.name == "nt":
        try:
            handle, release_mutex, close_handle = _acquire_windows_service_mutex()
        except OSError as exc:
            raise typer.BadParameter(
                "Brain Hub service is already starting or running"
            ) from exc
        try:
            yield
        finally:
            release_mutex(handle)
            close_handle(handle)
        return

    runtime = _runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    handle = _service_lock_path().open("a+b")
    try:
        try:
            _lock_file(handle)
        except OSError as exc:
            raise typer.BadParameter("Brain Hub service is already starting or running") from exc
        yield
    finally:
        try:
            _unlock_file(handle)
        except OSError:
            pass
        handle.close()


def _service_lock_is_held() -> bool:
    if os.name == "nt":
        return _windows_service_mutex_is_held()

    runtime = _runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    with _service_lock_path().open("a+b") as handle:
        try:
            _lock_file(handle)
        except OSError:
            return True
        _unlock_file(handle)
        return False


def _atomic_write_service_state(state: ManagedServiceState) -> None:
    runtime = _runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        runtime.chmod(0o700)
    except OSError:
        pass
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".service.",
        suffix=".json",
        dir=runtime,
    )
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(asdict(state), output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, _service_state_path())
        try:
            _service_state_path().chmod(0o600)
        except OSError:
            pass
        _legacy_service_pid_path().unlink(missing_ok=True)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)


def _read_service_state(*, require_live: bool = True) -> ManagedServiceState | None:
    try:
        state = ManagedServiceState.from_json(
            json.loads(_service_state_path().read_text(encoding="utf-8"))
        )
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        _service_state_path().unlink(missing_ok=True)
        _legacy_service_pid_path().unlink(missing_ok=True)
        return None
    if require_live and (
        not _process_exists(state.pid) or not _service_lock_is_held()
    ):
        _service_state_path().unlink(missing_ok=True)
        return None
    return state


def _read_service_pid() -> int | None:
    state = _read_service_state()
    return state.pid if state is not None else None


def _endpoint_identity(url: str, *, timeout: float = 0.3) -> dict[str, object] | None:
    check = request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with request.urlopen(check, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                return None
            payload = response.read(4097)
            if len(payload) > 4096:
                return None
            value = json.loads(payload)
    except (
        error.URLError,
        TimeoutError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return None
    return value if isinstance(value, dict) else None


def _identity_matches(value: dict[str, object] | None, state: ManagedServiceState) -> bool:
    return bool(
        value
        and value.get("product") == PRODUCT_ID
        and value.get("instance_id") == state.instance_id
        and value.get("status") == "ok"
    )


def _service_is_healthy(state: ManagedServiceState) -> bool:
    return _identity_matches(
        _endpoint_identity(f"http://127.0.0.1:{state.api_port}/healthz"),
        state,
    ) and _identity_matches(
        _endpoint_identity(f"http://127.0.0.1:{state.ui_port}/healthz"),
        state,
    )


def _windows_process_exists(pid: int) -> bool:
    """Check process liveness without relying on unsupported Windows signals."""

    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    error_access_denied = 5
    error_invalid_parameter = 87
    still_active = 259

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    open_process.restype = wintypes.HANDLE
    get_exit_code_process = kernel32.GetExitCodeProcess
    get_exit_code_process.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    get_exit_code_process.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = open_process(process_query_limited_information, False, pid)
    if not handle:
        error_code = ctypes.get_last_error()
        if error_code == error_access_denied:
            return True
        # Other failures are treated conservatively so a transient query
        # failure cannot spawn a duplicate managed service.
        return error_code != error_invalid_parameter

    try:
        exit_code = wintypes.DWORD()
        if not get_exit_code_process(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        close_handle(handle)


def _process_exists(pid: int) -> bool:
    if os.name == "nt":
        return _windows_process_exists(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _request_cooperative_shutdown(
    state: ManagedServiceState,
    *,
    timeout: float = 1.0,
) -> bool:
    shutdown = request.Request(
        f"http://127.0.0.1:{state.api_port}/_brainhub/control/shutdown",
        data=b"",
        headers={
            "Accept": "application/json",
            "X-BrainHub-Control": state.control_token,
        },
        method="POST",
    )
    try:
        with request.urlopen(shutdown, timeout=timeout) as response:
            payload = response.read(4097)
            if response.status != 202 or len(payload) > 4096:
                return False
            value = json.loads(payload)
            return bool(
                isinstance(value, dict)
                and value.get("product") == PRODUCT_ID
                and value.get("instance_id") == state.instance_id
                and value.get("status") == "stopping"
            )
    except (
        error.URLError,
        TimeoutError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return False


def _wait_for_process_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return True
        time.sleep(0.1)
    return not _process_exists(pid)


def _remove_service_state(instance_id: str | None = None) -> None:
    if instance_id is not None:
        current = _read_service_state(require_live=False)
        if current is not None and current.instance_id != instance_id:
            return
    _service_state_path().unlink(missing_ok=True)
    _legacy_service_pid_path().unlink(missing_ok=True)


def _terminate_service_process(
    target: ManagedServiceState | int,
    *,
    timeout: float = 5,
) -> None:
    state = target if isinstance(target, ManagedServiceState) else None
    pid = state.pid if state is not None else target
    if not _process_exists(pid):
        if state is not None:
            _remove_service_state(state.instance_id)
        return
    if state is not None:
        _request_cooperative_shutdown(state)
        if _wait_for_process_exit(pid, timeout):
            _remove_service_state(state.instance_id)
            return
    os.kill(pid, signal.SIGTERM)
    if _wait_for_process_exit(pid, timeout):
        if state is not None:
            _remove_service_state(state.instance_id)
        return
    # The PID comes from Brain Hub's private runtime file. Escalation prevents
    # open WebSocket tasks from leaving the UI listener orphaned indefinitely.
    force_signal = signal.SIGTERM if os.name == "nt" else signal.SIGKILL
    os.kill(pid, force_signal)
    if _wait_for_process_exit(pid, 2):
        if state is not None:
            _remove_service_state(state.instance_id)
        return
    raise RuntimeError(f"Brain Hub process {pid} did not exit")


def _background_process_options(*, windows: bool | None = None) -> dict[str, object]:
    selected_windows = os.name == "nt" if windows is None else windows
    if selected_windows:
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        return {"close_fds": True, "creationflags": creationflags}
    return {"close_fds": True, "start_new_session": True}


def _launch_service(api_port: int, ui_port: int, log_path: Path) -> subprocess.Popen:
    command = [
        sys.executable,
        "-m",
        "brainhub.cli",
        "_service",
        "--api-port",
        str(api_port),
        "--ui-port",
        str(ui_port),
    ]
    with log_path.open("ab") as log:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            **_background_process_options(),
        )


def _terminate_and_reap_child(
    process: subprocess.Popen,
    *,
    timeout: float = 2,
) -> None:
    """Own cleanup of a direct child that failed to become the managed service."""

    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:
        pass
    process.wait(timeout=timeout)


def _configuration_matches(
    state: ManagedServiceState,
    expected_fingerprint: str,
) -> bool:
    return (
        state.schema_version == 2
        and len(state.config_fingerprint) == 64
        and secrets.compare_digest(state.config_fingerprint, expected_fingerprint)
    )


def _ensure_service(*, api_port: int = 8420, ui_port: int = 4173) -> tuple[int, bool]:
    expected_config = _service_config_fingerprint()
    existing = _read_service_state()
    if existing is not None:
        expected_runtime = _python_executable_identity()
        ports_match = existing.api_port == api_port and existing.ui_port == ui_port
        runtime_matches = existing.python_executable == expected_runtime
        config_matches = _configuration_matches(existing, expected_config)
        if (
            ports_match
            and runtime_matches
            and config_matches
            and _service_is_healthy(existing)
        ):
            return existing.pid, False
        try:
            _terminate_service_process(existing)
        except (OSError, RuntimeError) as exc:
            raise typer.BadParameter(
                f"could not replace managed Brain Hub service {existing.pid}: {exc}"
            ) from exc

    runtime = _runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = _service_log_path()
    process = _launch_service(api_port, ui_port, log_path)
    # First access to an interactive OS keychain can be noticeably slower than
    # ordinary restarts. Keep the child bounded, but allow enough time for that
    # one-time platform initialization before cleanup treats startup as failed.
    deadline = time.monotonic() + SERVICE_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        state = _read_service_state()
        if (
            state is not None
            and state.api_port == api_port
            and state.ui_port == ui_port
            and state.python_executable == _python_executable_identity()
            and _configuration_matches(state, expected_config)
            and _service_is_healthy(state)
        ):
            # A Windows venv's python.exe is a redirector: Popen owns the
            # redirector PID while the child interpreter writes its own PID to
            # service.json. Terminating that apparently "different" process is
            # destructive because the redirector keeps its child in a
            # kill-on-close Job object. The healthy state plus the named mutex
            # is the service authority on Windows; the launcher PID is not.
            if state.pid != process.pid and os.name != "nt":
                _terminate_and_reap_child(process)
            return state.pid, state.pid == process.pid or os.name == "nt"
        if process.poll() is not None and state is None:
            break
        time.sleep(0.1)
    failed_state = _read_service_state()
    if failed_state is not None and failed_state.pid == process.pid:
        try:
            _terminate_service_process(failed_state, timeout=1)
        except (OSError, RuntimeError):
            pass
    try:
        _terminate_and_reap_child(process)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise typer.BadParameter(
            f"Brain Hub service failed to start and child cleanup failed: {exc}; "
            f"see {log_path}"
        ) from exc
    raise typer.BadParameter(f"Brain Hub service failed to start; see {log_path}")


class _WebConsoleHandler(SimpleHTTPRequestHandler):
    """Serve the bundled single-page application without logging private URLs."""

    def __init__(
        self,
        *args,
        instance_id: str,
        **kwargs,
    ) -> None:
        self.instance_id = instance_id
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        requested = self.path.partition("?")[0].partition("#")[0]
        if requested == "/healthz":
            body = json.dumps(
                {
                    "instance_id": self.instance_id,
                    "product": PRODUCT_ID,
                    "status": "ok",
                    "version": PRODUCT_VERSION,
                },
                sort_keys=True,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        target = Path(self.directory) / requested.lstrip("/")
        if requested != "/" and not target.exists() and not Path(requested).suffix:
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, format: str, *args: object) -> None:
        return


class _BrainHubWebServer(ThreadingHTTPServer):
    """Bind the loopback UI without HTTPServer's blocking reverse-DNS lookup."""

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


@app.command()
def serve(
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8420,
    allow_non_loopback: Annotated[
        bool, typer.Option(help="Acknowledge exposure beyond the local machine.")
    ] = False,
    no_semantic: Annotated[bool, typer.Option(help="Use explicit lexical degraded mode.")] = False,
) -> None:
    """Run the REST/WebSocket daemon on 127.0.0.1:8420 by default."""

    non_loopback = host not in {"127.0.0.1", "localhost", "::1"}
    if non_loopback and not allow_non_loopback:
        raise typer.BadParameter("non-loopback bind requires --allow-non-loopback")
    token = os.environ.get("BRAINHUB_API_TOKEN")
    if non_loopback and (token is None or not token.strip()):
        raise typer.BadParameter("non-loopback bind requires a nonempty BRAINHUB_API_TOKEN")
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise typer.BadParameter("uvicorn is not installed") from exc
    service = build_service(db, semantic=not no_semantic)
    # Uvicorn access logs include full URLs; keeping them off by default prevents
    # GET query text and opaque node identifiers from leaking into terminal logs.
    uvicorn.run(
        create_app(service, settings=ApiSettings(token=token)),
        host=host,
        port=port,
        access_log=False,
    )


def _build_web_server(
    host: str,
    port: int,
    *,
    instance_id: str,
) -> ThreadingHTTPServer:
    assets = _web_asset_dir()
    if not (assets / "index.html").is_file():
        raise typer.BadParameter(
            "web console assets are missing; reinstall Brain Hub from a release package"
        )
    handler = partial(
        _WebConsoleHandler,
        directory=str(assets),
        instance_id=instance_id,
    )
    try:
        return _BrainHubWebServer((host, port), handler)
    except OSError as exc:
        raise typer.BadParameter(f"cannot start web console on {host}:{port}: {exc}") from exc


@app.command("start")
def start_command() -> None:
    """Start the API and web console as one background service."""

    pid, started = _ensure_service()
    state = f"started in background (PID {pid})" if started else "already running"
    typer.echo(f"Brain Hub {state}")
    typer.echo("UI: http://127.0.0.1:4173")


@app.command("status")
def status_command() -> None:
    """Show background service health."""

    state = _read_service_state()
    if state is None:
        _write(
            {
                "api": "stopped",
                "managed": False,
                "pid": None,
                "product": PRODUCT_ID,
                "ui": "stopped",
            }
        )
        raise typer.Exit(1)
    api = _identity_matches(
        _endpoint_identity(f"http://127.0.0.1:{state.api_port}/healthz"),
        state,
    )
    ui = _identity_matches(
        _endpoint_identity(f"http://127.0.0.1:{state.ui_port}/healthz"),
        state,
    )
    runtime_matches = state.python_executable == _python_executable_identity()
    config_matches = _configuration_matches(
        state,
        _service_config_fingerprint(),
    )
    _write(
        {
            "api": "healthy" if api else "identity-mismatch",
            "api_port": state.api_port,
            "instance_id": state.instance_id,
            "configuration": "current" if config_matches else "restart-required",
            "managed": api and ui and runtime_matches and config_matches,
            "pid": state.pid,
            "product": PRODUCT_ID,
            "runtime": "current" if runtime_matches else "restart-required",
            "ui": "healthy" if ui else "identity-mismatch",
            "ui_port": state.ui_port,
        }
    )
    if not (api and ui and runtime_matches and config_matches):
        raise typer.Exit(1)


@app.command("stop")
def stop_command() -> None:
    """Stop the background API and web console service."""

    state = _read_service_state()
    if state is None:
        typer.echo("Brain Hub is not running.")
        return
    try:
        _terminate_service_process(state)
    except (OSError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo("Brain Hub stopped.")


@app.command("ui")
def ui_command() -> None:
    """Open the self-managed Brain Hub web console."""

    _ensure_service()
    url = "http://127.0.0.1:4173"
    typer.echo(f"Brain Hub UI: {url}")
    if not webbrowser.open(url):
        typer.echo("Could not open a browser automatically; open the URL above.")


@app.command("_service", hidden=True)
def service_command(
    api_port: Annotated[int, typer.Option(min=1, max=65535)] = 8420,
    ui_port: Annotated[int, typer.Option(min=1, max=65535)] = 4173,
) -> None:
    """Run the supervised API and bundled UI in one foreground process."""

    token = os.environ.get("BRAINHUB_API_TOKEN")
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise typer.BadParameter("uvicorn is not installed") from exc
    instance_id = uuid.uuid4().hex
    control_token = secrets.token_urlsafe(32)
    state = ManagedServiceState(
        pid=os.getpid(),
        instance_id=instance_id,
        control_token=control_token,
        api_port=api_port,
        ui_port=ui_port,
        python_executable=_python_executable_identity(),
        config_fingerprint=_service_config_fingerprint(),
    )
    web_server = _build_web_server(
        "127.0.0.1",
        ui_port,
        instance_id=instance_id,
    )
    try:
        service = build_service()
    except BaseException:
        web_server.server_close()
        raise
    with _service_lock():
        web_thread: Thread | None = None
        watcher_stop: Event | None = None
        watcher_thread: Thread | None = None
        try:
            _atomic_write_service_state(state)
            candidate_web_thread = Thread(
                target=web_server.serve_forever,
                name="brainhub-ui",
                daemon=True,
            )
            candidate_web_thread.start()
            web_thread = candidate_web_thread
            watcher_stop, watcher_thread = _start_adapter_watcher(
                token,
                api_port=api_port,
            )
            server_holder: dict[str, object] = {}

            def request_shutdown() -> None:
                server = server_holder.get("server")
                if server is not None:
                    server.should_exit = True

            api_app = create_app(
                service,
                settings=ApiSettings(
                    token=token,
                    instance_id=instance_id,
                    control_token=control_token,
                    shutdown_callback=request_shutdown,
                ),
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    api_app,
                    host="127.0.0.1",
                    port=api_port,
                    access_log=False,
                    timeout_graceful_shutdown=2,
                )
            )
            server_holder["server"] = server
            server.run()
        finally:
            if watcher_stop is not None:
                watcher_stop.set()
            if watcher_thread is not None:
                watcher_thread.join(timeout=2)
            if web_thread is not None:
                web_server.shutdown()
                web_thread.join(timeout=2)
            web_server.server_close()
            service.close()
            _remove_service_state(instance_id)


@app.command("_plugin-mcp", hidden=True)
def plugin_mcp_command() -> None:
    """Run plugin MCP directly without requiring an HTTP or UI sidecar."""

    mcp_command()


@app.command("mcp")
def mcp_command(
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    """Run the local MCP server over stdio (keeps stdout protocol-clean)."""

    service = build_service(db)
    watcher_stop, watcher_thread = _start_direct_adapter_watcher(service)
    try:
        run_mcp(service)
    finally:
        if watcher_stop is not None:
            watcher_stop.set()
        if watcher_thread is not None:
            watcher_thread.join(timeout=2)
        service.close()


@app.command("record")
def record_command(
    input_path: Annotated[str, typer.Argument(help="CloudEvents JSON file, or '-' for stdin.")],
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    raw = sys.stdin.read() if input_path == "-" else Path(input_path).read_text(encoding="utf-8")
    event = BrainEvent.model_validate_json(raw)
    service = build_service(db)
    try:
        _write(service.record(event))
    finally:
        service.close()


@app.command("search")
def search_command(
    query: Annotated[str, typer.Argument(min=1)],
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    anchor: Annotated[str | None, typer.Option(help="Strict neighborhood anchor.")] = None,
    hops: Annotated[int, typer.Option(min=0, max=EvidenceGraph.MAX_HOPS)] = 2,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    global_scope: Annotated[
        bool, typer.Option("--global", help="Explicitly search the entire graph.")
    ] = False,
) -> None:
    if anchor is None and not global_scope:
        raise typer.BadParameter("pass --anchor for scoped search or --global explicitly")
    service = build_service(db)
    try:
        _write(
            service.search(
                query,
                anchor_id=anchor,
                hops=hops,
                limit=limit,
                global_scope=global_scope,
            )
        )
    finally:
        service.close()


@app.command("get-node")
def get_node_command(
    node_id: str,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    service = build_service(db)
    try:
        node = service.get_node(node_id)
        if node is None:
            raise typer.BadParameter(f"node not found: {node_id}")
        _write(node)
    finally:
        service.close()


@app.command("expand")
def expand_command(
    node_id: str,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    hops: Annotated[int, typer.Option(min=0, max=EvidenceGraph.MAX_HOPS)] = 1,
) -> None:
    service = build_service(db)
    try:
        _write(service.expand(node_id, hops=hops))
    finally:
        service.close()


@app.command("path")
def path_command(
    source_id: str,
    target_id: str,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    directed: bool = False,
    max_length: Annotated[int, typer.Option(min=1, max=EvidenceGraph.MAX_PATH_LENGTH)] = 8,
) -> None:
    service = build_service(db)
    try:
        _write(service.path(source_id, target_id, directed=directed, max_length=max_length))
    finally:
        service.close()


@app.command("feedback")
def feedback_command(
    target_id: str,
    verdict: Annotated[
        str, typer.Option(help="accept, reject, needs_review, incorrect, or duplicate")
    ],
    note: Annotated[str | None, typer.Option(max=2000)] = None,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
) -> None:
    service = build_service(db)
    try:
        _write(service.feedback(FeedbackRequest(target_id=target_id, verdict=verdict, note=note)))
    finally:
        service.close()


@app.command("import-graphify")
def import_graphify_command(
    graph_json: Annotated[Path, typer.Argument(exists=True, readable=True)],
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    workspace_id: Annotated[str, typer.Option(max=256)] = "graphify-import",
) -> None:
    service = build_service(db)
    try:
        _write(service.import_graphify(graph_json, workspace_id=workspace_id))
    finally:
        service.close()


@app.command("demo")
def demo_command(
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    reset: Annotated[
        bool,
        typer.Option(
            "--reset",
            help="Reset only an empty/demo-only DB; refuses databases with personal events.",
        ),
    ] = False,
) -> None:
    """Seed the same deterministic graph shown by the offline web console."""

    service = build_service(db)
    try:
        if reset:
            try:
                service.store.reset_if_only_events({demo_event().id})
            except DemoResetRefused as exc:
                raise typer.BadParameter(str(exc)) from exc
        _write(seed_demo(service))
    finally:
        service.close()


@app.command("sync-batch")
def sync_batch_command(
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 500,
) -> None:
    service = build_service(db, semantic=False)
    try:
        batch = service.next_sync_batch(limit=limit)
        _write(batch if batch is not None else {"events": []})
    finally:
        service.close()


if __name__ == "__main__":  # pragma: no cover
    app()
