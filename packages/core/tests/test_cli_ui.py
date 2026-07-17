import ctypes
import io
import json
import os
import signal
import subprocess
from pathlib import Path
from threading import Event

import pytest
from typer.testing import CliRunner

from brainhub.api import PRODUCT_ID
from brainhub.cli import (
    SERVICE_START_TIMEOUT_SECONDS,
    ManagedServiceState,
    _WebConsoleHandler,
    _atomic_write_service_state,
    _background_process_options,
    _configuration_matches,
    _ensure_service,
    _flush_spool_directly,
    _process_exists,
    _python_executable_identity,
    _read_service_state,
    _service_config_fingerprint,
    _service_is_healthy,
    _service_lock,
    _service_state_path,
    _start_adapter_watcher,
    _terminate_and_reap_child,
    _terminate_service_process,
    _web_asset_dir,
    _windows_process_exists,
    app,
)
from brainhub_adapters.normalize import normalize_capture
from brainhub_adapters.spool import BoundedSpool


runner = CliRunner()


def managed_state(
    *,
    pid: int | None = None,
    instance_id: str = "instance-a",
    python_executable: str | None = None,
    api_port: int = 8420,
    ui_port: int = 4173,
    config_fingerprint: str | None = None,
    schema_version: int = 2,
) -> ManagedServiceState:
    return ManagedServiceState(
        pid=pid or os.getpid(),
        instance_id=instance_id,
        control_token="control-token-" + ("x" * 32),
        api_port=api_port,
        ui_port=ui_port,
        python_executable=python_executable or _python_executable_identity(),
        config_fingerprint=(
            _service_config_fingerprint()
            if config_fingerprint is None
            else config_fingerprint
        ),
        schema_version=schema_version,
    )


def test_web_console_assets_are_packaged() -> None:
    assets = _web_asset_dir()

    assert (assets / "index.html").is_file()
    assert any((assets / "assets").iterdir())


def test_service_state_is_atomic_private_and_round_trips(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("brainhub.cli._runtime_dir", lambda: tmp_path)
    state = managed_state()

    _atomic_write_service_state(state)

    assert json.loads(_service_state_path().read_text("utf-8"))["instance_id"] == "instance-a"
    if os.name != "nt":
        assert _service_state_path().stat().st_mode & 0o077 == 0
    assert _read_service_state(require_live=False) == state
    assert not list(tmp_path.glob(".service.*.json"))


def test_service_config_fingerprint_covers_effective_authority_without_secrets(
    monkeypatch, tmp_path: Path
) -> None:
    for variable in (
        "BRAINHUB_ALLOW_RAW_CONTENT",
        "BRAINHUB_API_TOKEN",
        "BRAINHUB_MASTER_KEY",
        "BRAINHUB_SEMANTIC",
        "BRAINHUB_SPOOL",
    ):
        monkeypatch.delenv(variable, raising=False)
    database = tmp_path / "authority.db"
    monkeypatch.setenv("BRAINHUB_DB_PATH", str(database))
    baseline = _service_config_fingerprint()

    monkeypatch.setenv(
        "BRAINHUB_DB_PATH",
        str(tmp_path / "not-created" / ".." / "authority.db"),
    )
    assert _service_config_fingerprint() == baseline

    monkeypatch.setenv("BRAINHUB_SEMANTIC", "false")
    semantic = _service_config_fingerprint()
    assert semantic != baseline

    monkeypatch.setenv("BRAINHUB_API_TOKEN", "do-not-store-this-api-token")
    api_token = _service_config_fingerprint()
    assert api_token != semantic

    monkeypatch.setenv("BRAINHUB_MASTER_KEY", "do-not-store-this-master-key")
    master_key = _service_config_fingerprint()
    assert master_key != api_token

    monkeypatch.setenv("BRAINHUB_ALLOW_RAW_CONTENT", "true")
    assert _service_config_fingerprint() != master_key


def test_service_state_stores_only_secret_safe_config_digest(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("brainhub.cli._runtime_dir", lambda: tmp_path)
    monkeypatch.setenv("BRAINHUB_API_TOKEN", "private-api-token-value")
    monkeypatch.setenv("BRAINHUB_MASTER_KEY", "private-master-key-value")

    _atomic_write_service_state(managed_state())

    raw = _service_state_path().read_text("utf-8")
    assert "private-api-token-value" not in raw
    assert "private-master-key-value" not in raw
    assert len(json.loads(raw)["config_fingerprint"]) == 64


def test_legacy_v1_state_is_parseable_but_never_reusable(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("brainhub.cli._runtime_dir", lambda: tmp_path)
    legacy = managed_state(config_fingerprint="", schema_version=1)
    _atomic_write_service_state(legacy)

    parsed = _read_service_state(require_live=False)

    assert parsed == legacy
    assert not _configuration_matches(parsed, _service_config_fingerprint())


def test_service_state_requires_the_live_managed_lock(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("brainhub.cli._runtime_dir", lambda: tmp_path)
    state = managed_state()
    _atomic_write_service_state(state)

    assert _read_service_state() is None
    assert not _service_state_path().exists()

    with _service_lock():
        _atomic_write_service_state(state)
        assert _read_service_state() == state


def test_health_requires_matching_api_and_ui_instance(monkeypatch) -> None:
    state = managed_state()

    monkeypatch.setattr(
        "brainhub.cli._endpoint_identity",
        lambda _url: {
            "instance_id": state.instance_id,
            "product": PRODUCT_ID,
            "status": "ok",
        },
    )
    assert _service_is_healthy(state)

    monkeypatch.setattr(
        "brainhub.cli._endpoint_identity",
        lambda _url: {
            "instance_id": "unrelated-process",
            "product": PRODUCT_ID,
            "status": "ok",
        },
    )
    assert not _service_is_healthy(state)


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [
        (259, True),
        (0, False),
    ],
)
def test_windows_process_exists_reads_exit_code(
    monkeypatch, exit_code: int, expected: bool
) -> None:
    class Function:
        argtypes = None
        restype = None

        def __init__(self, implementation):
            self.implementation = implementation

        def __call__(self, *args):
            return self.implementation(*args)

    handles = []

    def get_exit_code(_handle, output):
        output._obj.value = exit_code
        return True

    class Kernel32:
        OpenProcess = Function(lambda _access, _inherit, _pid: 321)
        GetExitCodeProcess = Function(get_exit_code)
        CloseHandle = Function(lambda handle: handles.append(handle) or True)

    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda _name, **_kwargs: Kernel32(),
        raising=False,
    )

    assert _windows_process_exists(123) is expected
    assert handles == [321]


@pytest.mark.parametrize(
    ("last_error", "expected"),
    [
        (5, True),
        (87, False),
        (8, True),
    ],
)
def test_windows_process_exists_handles_open_failures(
    monkeypatch, last_error: int, expected: bool
) -> None:
    class Function:
        argtypes = None
        restype = None

        def __init__(self, result):
            self.result = result

        def __call__(self, *_args):
            return self.result

    class Kernel32:
        OpenProcess = Function(0)
        GetExitCodeProcess = Function(False)
        CloseHandle = Function(True)

    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda _name, **_kwargs: Kernel32(),
        raising=False,
    )
    monkeypatch.setattr(
        ctypes,
        "get_last_error",
        lambda: last_error,
        raising=False,
    )

    assert _windows_process_exists(123) is expected


def test_process_exists_uses_windows_probe_instead_of_os_kill(monkeypatch) -> None:
    checked = []

    monkeypatch.setattr("brainhub.cli.os.name", "nt")
    monkeypatch.setattr(
        "brainhub.cli._windows_process_exists",
        lambda pid: checked.append(pid) or True,
    )

    assert _process_exists(123)
    assert checked == [123]


def test_bundled_ui_health_exposes_product_and_instance() -> None:
    handler = object.__new__(_WebConsoleHandler)
    handler.instance_id = "ui-instance"
    handler.path = "/healthz"
    handler.wfile = io.BytesIO()
    response_codes = []
    response_headers = []
    handler.send_response = response_codes.append
    handler.send_header = lambda key, value: response_headers.append((key, value))
    handler.end_headers = lambda: None

    handler.do_GET()

    assert response_codes == [200]
    assert ("Cache-Control", "no-store") in response_headers
    assert json.loads(handler.wfile.getvalue()) == {
        "instance_id": "ui-instance",
        "product": PRODUCT_ID,
        "status": "ok",
        "version": "0.1.0",
    }


def test_runtime_python_mismatch_stops_and_restarts(monkeypatch, tmp_path: Path) -> None:
    old = managed_state(pid=123, python_executable="/old/python")
    new = managed_state(pid=456, instance_id="instance-new", python_executable="/new/python")
    states = iter([old, new])
    stopped = []

    class Process:
        pid = 456

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr("brainhub.cli._python_executable_identity", lambda: "/new/python")
    monkeypatch.setattr("brainhub.cli._read_service_state", lambda: next(states))
    monkeypatch.setattr("brainhub.cli._terminate_service_process", stopped.append)
    monkeypatch.setattr("brainhub.cli._launch_service", lambda *_args: Process())
    monkeypatch.setattr("brainhub.cli._service_is_healthy", lambda state: state is new)
    monkeypatch.setattr("brainhub.cli._runtime_dir", lambda: tmp_path)

    assert _ensure_service() == (456, True)
    assert stopped == [old]


def test_identity_mismatch_stops_and_restarts(monkeypatch, tmp_path: Path) -> None:
    old = managed_state(pid=123, instance_id="old-instance")
    new = managed_state(pid=456, instance_id="new-instance")
    states = iter([old, new])
    stopped = []

    class Process:
        pid = 456

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr("brainhub.cli._read_service_state", lambda: next(states))
    monkeypatch.setattr("brainhub.cli._terminate_service_process", stopped.append)
    monkeypatch.setattr("brainhub.cli._launch_service", lambda *_args: Process())
    monkeypatch.setattr("brainhub.cli._service_is_healthy", lambda state: state is new)
    monkeypatch.setattr("brainhub.cli._runtime_dir", lambda: tmp_path)

    assert _ensure_service() == (456, True)
    assert stopped == [old]


def test_graph_authority_mismatch_stops_and_restarts(
    monkeypatch, tmp_path: Path
) -> None:
    expected = "a" * 64
    old = managed_state(pid=123, config_fingerprint="b" * 64)
    new = managed_state(
        pid=456,
        instance_id="new-instance",
        config_fingerprint=expected,
    )
    states = iter([old, new])
    stopped = []

    class Process:
        pid = 456

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr("brainhub.cli._service_config_fingerprint", lambda: expected)
    monkeypatch.setattr("brainhub.cli._read_service_state", lambda: next(states))
    monkeypatch.setattr("brainhub.cli._terminate_service_process", stopped.append)
    monkeypatch.setattr("brainhub.cli._launch_service", lambda *_args: Process())
    monkeypatch.setattr("brainhub.cli._service_is_healthy", lambda _state: True)
    monkeypatch.setattr("brainhub.cli._runtime_dir", lambda: tmp_path)

    assert _ensure_service() == (456, True)
    assert stopped == [old]


def test_failed_startup_terminates_and_reaps_child_without_state(
    monkeypatch, tmp_path: Path
) -> None:
    actions = []

    class Process:
        pid = 789

        @staticmethod
        def poll():
            return None

        @staticmethod
        def terminate():
            actions.append("terminate")

        @staticmethod
        def kill():
            actions.append("kill")

        @staticmethod
        def wait(*, timeout):
            actions.append(("wait", timeout))
            return 1

    moments = iter([0.0, SERVICE_START_TIMEOUT_SECONDS + 1.0])
    monkeypatch.setattr("brainhub.cli._read_service_state", lambda: None)
    monkeypatch.setattr("brainhub.cli._launch_service", lambda *_args: Process())
    monkeypatch.setattr("brainhub.cli._runtime_dir", lambda: tmp_path)
    monkeypatch.setattr("brainhub.cli.time.monotonic", lambda: next(moments))

    with pytest.raises(Exception, match="failed to start"):
        _ensure_service()

    assert actions == ["terminate", ("wait", 2)]


def test_child_cleanup_kills_after_timeout_and_reaps() -> None:
    actions = []

    class Process:
        @staticmethod
        def poll():
            return None

        @staticmethod
        def terminate():
            actions.append("terminate")

        @staticmethod
        def kill():
            actions.append("kill")

        @staticmethod
        def wait(*, timeout):
            actions.append(("wait", timeout))
            if actions.count(("wait", timeout)) == 1:
                raise subprocess.TimeoutExpired("brainhub", timeout)
            return -9

    _terminate_and_reap_child(Process(), timeout=0.25)

    assert actions == [
        "terminate",
        ("wait", 0.25),
        "kill",
        ("wait", 0.25),
    ]


def test_background_process_options_are_platform_specific() -> None:
    windows = _background_process_options(windows=True)
    posix = _background_process_options(windows=False)

    assert windows["creationflags"]
    assert "start_new_session" not in windows
    assert posix["start_new_session"] is True
    assert "creationflags" not in posix


def test_ui_command_is_exposed() -> None:
    result = runner.invoke(app, ["ui", "--help"])

    assert result.exit_code == 0
    assert "Open the self-managed Brain Hub web console." in result.stdout


def test_start_is_idempotent_when_managed_service_is_running(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "brainhub.cli._ensure_service",
        lambda: calls.append(True) or (321, False),
    )

    first = runner.invoke(app, ["start"])
    second = runner.invoke(app, ["start"])

    assert first.exit_code == second.exit_code == 0
    assert "already running" in first.stdout
    assert "already running" in second.stdout
    assert calls == [True, True]


def test_status_is_idempotent_for_matching_managed_identity(monkeypatch) -> None:
    state = managed_state(pid=321)
    monkeypatch.setattr("brainhub.cli._read_service_state", lambda: state)
    monkeypatch.setattr(
        "brainhub.cli._endpoint_identity",
        lambda _url: {
            "instance_id": state.instance_id,
            "product": PRODUCT_ID,
            "status": "ok",
        },
    )

    first = runner.invoke(app, ["status"])
    second = runner.invoke(app, ["status"])

    assert first.exit_code == second.exit_code == 0
    assert json.loads(first.stdout)["managed"] is True
    assert json.loads(second.stdout)["instance_id"] == state.instance_id


def test_stop_is_idempotent_when_service_is_absent(monkeypatch) -> None:
    monkeypatch.setattr("brainhub.cli._read_service_state", lambda: None)

    first = runner.invoke(app, ["stop"])
    second = runner.invoke(app, ["stop"])

    assert first.exit_code == second.exit_code == 0
    assert first.stdout == second.stdout == "Brain Hub is not running.\n"


def test_adapter_watcher_uses_the_service_api_port(monkeypatch) -> None:
    started = Event()
    captured = []

    def fake_watch(_spool, settings, *, stop_event) -> None:
        captured.append(settings)
        started.set()
        stop_event.wait(1)

    monkeypatch.setattr("brainhub_adapters.watch.watch_spool", fake_watch)

    stop, thread = _start_adapter_watcher("token", api_port=18420)
    assert started.wait(1)
    assert captured[0].endpoint == "http://127.0.0.1:18420/v1/events"
    assert captured[0].api_token == "token"
    assert stop is not None
    assert thread is not None
    stop.set()
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_adapter_watcher_failure_does_not_block_service(monkeypatch) -> None:
    def fail_spool(_path):
        raise PermissionError("private path")

    monkeypatch.setattr("brainhub_adapters.spool.BoundedSpool", fail_spool)

    assert _start_adapter_watcher(None, api_port=8420) == (None, None)


def test_direct_mcp_watcher_records_and_acknowledges_hook_events(
    tmp_path: Path,
) -> None:
    spool = BoundedSpool(tmp_path / "spool")
    payload = normalize_capture(
        "codex",
        {
            "hook_event_name": "Stop",
            "session_id": "direct-mcp-session",
            "workspace_id": "workspace",
            "event_id": "direct-mcp-event",
        },
        mode="hook",
    ).as_dict()
    spool.enqueue(payload)
    recorded = []

    class Service:
        @staticmethod
        def record(event) -> None:
            recorded.append(event)

    result = _flush_spool_directly(spool, Service())

    assert result.delivered == 1
    assert result.remaining == 0
    assert result.error is None
    assert recorded[0].id == payload["id"]


def test_direct_mcp_watcher_quarantines_permanent_rejections(
    tmp_path: Path,
) -> None:
    spool = BoundedSpool(tmp_path / "spool")
    payload = normalize_capture(
        "codex",
        {
            "hook_event_name": "Stop",
            "session_id": "rejected-mcp-session",
            "workspace_id": "workspace",
            "event_id": "rejected-mcp-event",
        },
        mode="hook",
    ).as_dict()
    spool.enqueue(payload)

    class RejectingService:
        @staticmethod
        def record(_event) -> None:
            raise ValueError("permanent rejection")

    result = _flush_spool_directly(spool, RejectingService())

    assert result.quarantined == 1
    assert result.remaining == 0
    assert result.error is None
    assert result.quarantine_path == str(spool.root / "quarantine")


def test_ui_reports_missing_assets(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("brainhub.cli._web_asset_dir", lambda: tmp_path)

    result = runner.invoke(app, ["_service"])

    assert result.exit_code != 0
    assert "web console assets are missing" in result.stderr


def test_ui_ensures_service_then_opens_browser(monkeypatch) -> None:
    ensured = []
    opened = []
    monkeypatch.setattr("brainhub.cli._ensure_service", lambda: ensured.append(True))
    monkeypatch.setattr("brainhub.cli.webbrowser.open", lambda url: opened.append(url) or True)

    result = runner.invoke(app, ["ui"])

    assert result.exit_code == 0
    assert ensured == [True]
    assert opened == ["http://127.0.0.1:4173"]


def test_cooperative_shutdown_precedes_signals(monkeypatch, tmp_path: Path) -> None:
    state = managed_state(pid=123)
    cooperative = []
    monkeypatch.setattr("brainhub.cli._process_exists", lambda _pid: True)
    monkeypatch.setattr(
        "brainhub.cli._request_cooperative_shutdown",
        lambda supplied: cooperative.append(supplied) or True,
    )
    monkeypatch.setattr("brainhub.cli._wait_for_process_exit", lambda _pid, _timeout: True)
    monkeypatch.setattr(
        "brainhub.cli.os.kill",
        lambda *_args: pytest.fail("signal fallback must not run"),
    )
    monkeypatch.setattr("brainhub.cli._runtime_dir", lambda: tmp_path)

    _terminate_service_process(state)

    assert cooperative == [state]


def test_service_shutdown_escalates_after_grace_period(monkeypatch) -> None:
    signals = []
    waits = iter([False, True])
    monkeypatch.setattr("brainhub.cli._process_exists", lambda _pid: True)
    monkeypatch.setattr("brainhub.cli.os.kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(
        "brainhub.cli._wait_for_process_exit",
        lambda _pid, _timeout: next(waits),
    )

    _terminate_service_process(123, timeout=1)

    force_signal = signal.SIGTERM if os.name == "nt" else signal.SIGKILL
    assert signals == [(123, signal.SIGTERM), (123, force_signal)]


def test_plugin_mcp_does_not_require_http_or_ui(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(
        "brainhub.cli._ensure_service",
        lambda: pytest.fail("plugin MCP must not start the managed service"),
    )
    monkeypatch.setattr("brainhub.cli.mcp_command", lambda: called.append(True))

    result = runner.invoke(app, ["_plugin-mcp"])

    assert result.exit_code == 0
    assert called == [True]
