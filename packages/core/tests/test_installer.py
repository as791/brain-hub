from __future__ import annotations

import base64
import io
import json
import os
import stat
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from scripts import install


def make_checkout(root: Path, version: str = "0.9.0") -> Path:
    (root / "adapters").mkdir(parents=True)
    (root / "plugins" / "brain-hub" / ".codex-plugin").mkdir(parents=True)
    (root / "plugins" / "brain-hub" / "hooks").mkdir(parents=True)
    (root / "plugins" / "brain-hub" / "scripts").mkdir(parents=True)
    (root / ".agents" / "plugins").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "brain-hub"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (root / "adapters" / "pyproject.toml").write_text(
        '[project]\nname = "brain-hub-adapters"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (root / "plugins" / "brain-hub" / ".codex-plugin" / "plugin.json").write_text(
        '{"name": "brain-hub", "version": "0.1.0"}\n',
        encoding="utf-8",
    )
    (root / "plugins" / "brain-hub" / ".mcp.json").write_text(
        '{"original": true}\n',
        encoding="utf-8",
    )
    (root / "plugins" / "brain-hub" / "hooks" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 capture_hook.py",
                                    "commandWindows": "py -3 capture_hook.py",
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (root / "plugins" / "brain-hub" / "scripts" / "capture_hook.py").write_text(
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    (root / ".agents" / "plugins" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "brain-hub",
                "plugins": [{"name": "brain-hub", "source": "./plugins/brain-hub"}],
            }
        ),
        encoding="utf-8",
    )
    return root


class SuccessfulRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")


def options_for(
    source: Path,
    install_root: Path,
    bin_dir: Path,
    **overrides: Any,
) -> install.InstallOptions:
    values: dict[str, Any] = {
        "source": str(source),
        "ref": None,
        "install_root": install_root,
        "bin_dir": bin_dir,
        "install_plugin": False,
        "modify_path": False,
        "dry_run": False,
    }
    values.update(overrides)
    return install.InstallOptions(**values)


def make_runtime_fixture(
    install_root: Path,
    runtime_id: str,
    *,
    windows: bool = False,
) -> tuple[Path, Path]:
    venv = install_root / "runtime" / "versions" / runtime_id / "venv"
    executable = install.venv_executable(venv, "brainhub", windows=windows)
    executable.parent.mkdir(parents=True)
    executable.write_text("managed executable\n", encoding="utf-8")
    executable.chmod(0o755)
    manifest = {
        "compatibility": install.runtime_compatibility(),
        "runtime_id": runtime_id,
        "source_fingerprint": f"fingerprint-{runtime_id}",
        "version": "0.9.0",
    }
    (venv.parent / install.RUNTIME_MANIFEST).write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return venv, executable


def test_github_repository_ref_becomes_https_archive() -> None:
    assert (
        install.github_archive_url(
            "https://github.com/example/brain-hub.git",
            "feature/installer",
        )
        == "https://github.com/example/brain-hub/archive/feature%2Finstaller.zip"
    )

    with pytest.raises(install.InstallerError, match="HTTPS"):
        install.github_archive_url("http://github.com/example/brain-hub", "main")
    with pytest.raises(install.InstallerError, match="GitHub"):
        install.github_archive_url("https://example.com/example/brain-hub", "main")
    with pytest.raises(install.InstallerError, match="required"):
        install.github_archive_url("https://github.com/example/brain-hub")


def test_download_rejects_redirect_away_from_github(tmp_path: Path) -> None:
    class Response(io.BytesIO):
        headers: dict[str, str] = {}

        def geturl(self) -> str:
            return "https://example.com/archive.zip"

    with pytest.raises(install.InstallerError, match="untrusted URL"):
        install.download_archive(
            "https://github.com/example/brain-hub/archive/main.zip",
            tmp_path / "archive.zip",
            opener=lambda *_args, **_kwargs: Response(b"not trusted"),
        )
    assert not (tmp_path / "archive.zip").exists()


def test_safe_zip_extraction_finds_checkout_and_rejects_traversal(
    tmp_path: Path,
) -> None:
    safe_archive = tmp_path / "safe.zip"
    with zipfile.ZipFile(safe_archive, "w") as archive:
        archive.writestr("brain-hub-main/pyproject.toml", "[project]\n")
        archive.writestr("brain-hub-main/adapters/pyproject.toml", "[project]\n")

    extracted = install.safe_extract_zip(safe_archive, tmp_path / "safe")
    assert extracted == (tmp_path / "safe" / "brain-hub-main").resolve()

    unsafe_archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe_archive, "w") as archive:
        archive.writestr("../outside.txt", "not allowed")
    with pytest.raises(install.InstallerError, match="unsafe archive member"):
        install.safe_extract_zip(unsafe_archive, tmp_path / "unsafe")
    assert not (tmp_path / "outside.txt").exists()


def test_safe_zip_extraction_rejects_symbolic_links(tmp_path: Path) -> None:
    archive_path = tmp_path / "link.zip"
    link = zipfile.ZipInfo("brain-hub-main/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(link, "target")

    with pytest.raises(install.InstallerError, match="symbolic link"):
        install.safe_extract_zip(archive_path, tmp_path / "extract")


@pytest.mark.parametrize(
    "member",
    ["brain-hub-main/CON.txt", "brain-hub-main/file:stream", "brain-hub-main/trailing."],
)
def test_safe_zip_extraction_rejects_windows_unsafe_names(
    tmp_path: Path,
    member: str,
) -> None:
    archive_path = tmp_path / "unsafe-name.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(member, "not allowed")

    with pytest.raises(install.InstallerError, match="unsafe archive member"):
        install.safe_extract_zip(archive_path, tmp_path / "extract")


def test_safe_zip_extraction_rejects_unicode_normalization_collisions(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "unicode-collision.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("brain-hub-main/caf\u00e9.txt", "first")
        archive.writestr("brain-hub-main/cafe\u0301.txt", "second")

    with pytest.raises(install.InstallerError, match="duplicate archive member"):
        install.safe_extract_zip(archive_path, tmp_path / "extract")


def test_versioned_runtime_is_idempotent_and_preserves_user_data(
    tmp_path: Path,
) -> None:
    source = make_checkout(tmp_path / "checkout")
    install_root = tmp_path / "state"
    bin_dir = tmp_path / "bin"
    install_root.mkdir()
    database = install_root / "brainhub.db"
    database.write_text("database sentinel", encoding="utf-8")
    spool = install_root / "spool"
    spool.mkdir()
    event = spool / "event.json"
    event.write_text("spool sentinel", encoding="utf-8")
    reporter = install.Reporter(io.StringIO())
    runner = SuccessfulRunner()
    options = options_for(source, install_root, bin_dir)

    first_venv = install.install_checkout(
        source,
        options,
        reporter,
        runner=runner,
        windows=False,
    )
    first_commands = list(runner.commands)
    second_venv = install.install_checkout(
        source,
        options,
        reporter,
        runner=runner,
        windows=False,
    )

    assert first_venv == second_venv
    runtime_id, fingerprint = install.runtime_identity(source, "0.9.0")
    assert first_venv == install_root / "runtime" / "versions" / runtime_id / "venv"
    assert len(first_commands) == 5
    assert first_commands[0][1:3] == ["-m", "venv"]
    assert first_commands[1][0] == str(first_venv / "bin" / "python")
    assert first_commands[1][-2:] == [str(source), str(source / "adapters")]
    assert first_commands[2][-1] == "--help"
    assert first_commands[3][-1] == "--help"
    assert first_commands[4][-2:] == ["ui", "--help"]
    assert len(runner.commands) == len(first_commands) + 3
    assert database.read_text("utf-8") == "database sentinel"
    assert event.read_text("utf-8") == "spool sentinel"
    current = json.loads((install_root / "runtime" / "current.json").read_text("utf-8"))
    assert current["brainhub"] == str(
        install.venv_executable(first_venv, "brainhub", windows=False)
    )
    assert current["source_fingerprint"] == fingerprint


def test_runtime_manifest_records_current_compatibility(tmp_path: Path) -> None:
    source = make_checkout(tmp_path / "checkout")
    install_root = tmp_path / "state"
    runner = SuccessfulRunner()

    venv = install.install_runtime(
        source,
        install_root,
        "0.9.0",
        install.Reporter(io.StringIO()),
        windows=False,
        runner=runner,
    )

    manifest = json.loads(
        (venv.parent / install.RUNTIME_MANIFEST).read_text("utf-8")
    )
    assert manifest["compatibility"] == install.runtime_compatibility()
    assert manifest["compatibility"] in manifest["runtime_id"]


def test_corrupt_installed_runtime_is_quarantined_and_rebuilt(
    tmp_path: Path,
) -> None:
    source = make_checkout(tmp_path / "checkout")
    install_root = tmp_path / "state"
    reporter = install.Reporter(io.StringIO())
    first = install.install_runtime(
        source,
        install_root,
        "0.9.0",
        reporter,
        windows=False,
        runner=SuccessfulRunner(),
    )
    (first.parent / install.RUNTIME_MANIFEST).write_text(
        "{invalid manifest",
        encoding="utf-8",
    )
    repair_runner = SuccessfulRunner()

    repaired = install.install_runtime(
        source,
        install_root,
        "0.9.0",
        reporter,
        windows=False,
        runner=repair_runner,
    )

    assert repaired == first
    assert len(repair_runner.commands) == 5
    repaired_manifest = json.loads(
        (repaired.parent / install.RUNTIME_MANIFEST).read_text("utf-8")
    )
    assert repaired_manifest["compatibility"] == install.runtime_compatibility()
    quarantined = list(
        (install_root / "runtime" / "versions").glob(".broken-*")
    )
    assert len(quarantined) == 1
    assert (
        quarantined[0] / install.RUNTIME_MANIFEST
    ).read_text("utf-8") == "{invalid manifest"


def test_changed_source_creates_a_new_runtime_without_deleting_old_one(
    tmp_path: Path,
) -> None:
    source = make_checkout(tmp_path / "checkout")
    install_root = tmp_path / "state"
    options = options_for(source, install_root, tmp_path / "bin")
    runner = SuccessfulRunner()
    reporter = install.Reporter(io.StringIO())

    first = install.install_checkout(
        source,
        options,
        reporter,
        runner=runner,
        windows=False,
    )
    (source / "packages" / "core" / "src" / "brainhub").mkdir(parents=True)
    (source / "packages" / "core" / "src" / "brainhub" / "feature.py").write_text(
        "FEATURE = True\n",
        encoding="utf-8",
    )
    second = install.install_checkout(
        source,
        options,
        reporter,
        runner=runner,
        windows=False,
    )

    assert second != first
    assert first.parent.exists()
    assert second.parent.exists()
    current = json.loads((install_root / "runtime" / "current.json").read_text("utf-8"))
    assert current["venv"] == str(second.resolve())


def test_runtime_failure_cleans_only_new_version_directory(tmp_path: Path) -> None:
    source = make_checkout(tmp_path / "checkout")
    install_root = tmp_path / "state"
    install_root.mkdir()
    database = install_root / "brainhub.db"
    database.write_text("keep", encoding="utf-8")
    calls = 0

    def failing_runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(command, 1, "", "failure")

    with pytest.raises(install.InstallerError, match="virtual environment"):
        install.install_runtime(
            source,
            install_root,
            "0.9.0",
            install.Reporter(io.StringIO()),
            windows=False,
            runner=failing_runner,
        )

    assert calls == 1
    assert database.read_text("utf-8") == "keep"
    versions = install_root / "runtime" / "versions"
    assert not versions.exists() or not any(versions.iterdir())


def test_upgrade_handoff_stops_only_the_previous_managed_runtime(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "state"
    old_venv, old_executable = make_runtime_fixture(install_root, "old-runtime")
    new_venv, _new_executable = make_runtime_fixture(install_root, "new-runtime")
    current_path = install_root / "runtime" / "current.json"
    current_path.write_text(
        json.dumps(
            {
                "brainhub": str(old_executable),
                "runtime_id": "old-runtime",
                "source_fingerprint": "fingerprint-old-runtime",
                "venv": str(old_venv),
                "version": "0.8.0",
            }
        ),
        encoding="utf-8",
    )
    runner = SuccessfulRunner()

    install.write_current_runtime(
        install_root,
        new_venv,
        "0.9.0",
        install.Reporter(io.StringIO()),
        windows=False,
        runner=runner,
    )

    assert runner.commands == [[str(old_executable.resolve()), "stop"]]
    assert old_venv.parent.is_dir()
    current = json.loads(current_path.read_text("utf-8"))
    assert current["runtime_id"] == "new-runtime"


def test_upgrade_handoff_never_invokes_an_unmanaged_pointer(tmp_path: Path) -> None:
    install_root = tmp_path / "state"
    old_venv, _old_executable = make_runtime_fixture(install_root, "old-runtime")
    new_venv, _new_executable = make_runtime_fixture(install_root, "new-runtime")
    unmanaged = tmp_path / "outside" / "brainhub"
    unmanaged.parent.mkdir()
    unmanaged.write_text("not managed\n", encoding="utf-8")
    unmanaged.chmod(0o755)
    current_path = install_root / "runtime" / "current.json"
    current_path.write_text(
        json.dumps(
            {
                "brainhub": str(unmanaged),
                "runtime_id": "old-runtime",
                "source_fingerprint": "fingerprint-old-runtime",
                "venv": str(old_venv),
                "version": "0.8.0",
            }
        ),
        encoding="utf-8",
    )
    runner = SuccessfulRunner()

    install.write_current_runtime(
        install_root,
        new_venv,
        "0.9.0",
        install.Reporter(io.StringIO()),
        windows=False,
        runner=runner,
    )

    assert runner.commands == []
    assert json.loads(current_path.read_text("utf-8"))["runtime_id"] == "new-runtime"


def test_installation_lock_rejects_a_concurrent_installer(tmp_path: Path) -> None:
    with install.installation_lock(tmp_path):
        with pytest.raises(install.InstallerError, match="already running"):
            with install.installation_lock(tmp_path):
                pass


def test_posix_shims_and_profile_are_idempotent(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin with space"
    venv = tmp_path / "runtime" / "venv"
    reporter = install.Reporter(io.StringIO())

    shims = install.write_shims(bin_dir, venv, reporter, windows=False)
    brainhub = bin_dir / "brainhub"
    expected_target = install.venv_executable(venv, "brainhub", windows=False)
    assert brainhub in shims
    assert str(expected_target) in brainhub.read_text("utf-8")
    assert os.access(brainhub, os.X_OK)

    profile = tmp_path / ".profile"
    profile.write_text("export EXISTING=value\n", encoding="utf-8")
    assert install.register_unix_path(profile, bin_dir)
    assert not install.register_unix_path(profile, bin_dir)
    contents = profile.read_text("utf-8")
    assert contents.count(install.PROFILE_MARKER_START) == 1
    assert contents.count(str(bin_dir)) == 2
    assert "export EXISTING=value" in contents


def test_path_registration_preserves_a_profile_symlink(tmp_path: Path) -> None:
    target = tmp_path / "profiles" / "real-profile"
    target.parent.mkdir()
    target.write_text("export EXISTING=value\n", encoding="utf-8")
    profile = tmp_path / ".profile"
    profile.symlink_to(target)
    original_link = os.readlink(profile)

    assert install.register_unix_path(profile, tmp_path / "bin")

    assert profile.is_symlink()
    assert os.readlink(profile) == original_link
    assert install.PROFILE_MARKER_START in target.read_text("utf-8")


class FakeRegistryKey:
    def __enter__(self) -> FakeRegistryKey:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


class FakeWinreg:
    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    def __init__(self) -> None:
        self.path: str | None = None
        self.value_type = self.REG_EXPAND_SZ

    def CreateKeyEx(self, *_args: Any) -> FakeRegistryKey:
        return FakeRegistryKey()

    def QueryValueEx(self, _key: FakeRegistryKey, _name: str) -> tuple[str, int]:
        if self.path is None:
            raise FileNotFoundError
        return self.path, self.value_type

    def SetValueEx(
        self,
        _key: FakeRegistryKey,
        _name: str,
        _reserved: int,
        value_type: int,
        value: str,
    ) -> None:
        self.value_type = value_type
        self.path = value


def test_windows_shims_and_hkcu_path_are_idempotent(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    venv = tmp_path / "runtime" / "venv"
    install.write_shims(
        bin_dir,
        venv,
        install.Reporter(io.StringIO()),
        windows=True,
    )
    contents = (bin_dir / "brainhub.cmd").read_text("utf-8")
    assert "Scripts" in contents
    assert "brainhub.exe" in contents
    assert contents.startswith("@echo off")

    registry = FakeWinreg()
    assert install.register_windows_path(
        bin_dir,
        winreg_module=registry,
        broadcast=False,
    )
    assert not install.register_windows_path(
        bin_dir,
        winreg_module=registry,
        broadcast=False,
    )
    assert registry.path == str(bin_dir)


def test_installed_plugin_uses_absolute_managed_launcher_without_mutating_source(
    tmp_path: Path,
) -> None:
    source = make_checkout(tmp_path / "checkout")
    source_config = source / "plugins" / "brain-hub" / ".mcp.json"
    original = source_config.read_text("utf-8")
    executable = tmp_path / "state" / "runtime" / "versions" / "0.9.0" / "venv"
    executable = executable / "bin" / "brainhub"

    marketplace = install.install_plugin_copy(
        source,
        tmp_path / "state",
        executable,
        install.Reporter(io.StringIO()),
        windows=False,
    )

    installed_config = json.loads(
        (marketplace / "plugins" / "brain-hub" / ".mcp.json").read_text("utf-8")
    )
    server = installed_config["mcpServers"]["brain-hub"]
    assert Path(server["command"]).is_absolute()
    assert server["command"] == str(executable.resolve())
    assert server["args"] == ["_plugin-mcp"]
    assert source_config.read_text("utf-8") == original
    installed_marketplace = json.loads(
        (marketplace / ".agents" / "plugins" / "marketplace.json").read_text("utf-8")
    )
    assert installed_marketplace["name"] == install.MANAGED_MARKETPLACE_NAME
    assert installed_marketplace["interface"]["displayName"] == "Brain Hub (Managed)"
    installed_manifest = json.loads(
        (
            marketplace
            / "plugins"
            / "brain-hub"
            / ".codex-plugin"
            / "plugin.json"
        ).read_text("utf-8")
    )
    assert installed_manifest["version"] == (
        "0.1.0+codex.src-" + install.source_fingerprint(source)[:16]
    )
    installed_hooks = json.loads(
        (marketplace / "plugins" / "brain-hub" / "hooks" / "hooks.json").read_text("utf-8")
    )
    hook_command = installed_hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert str(executable.parents[1] / "bin" / "python") in hook_command
    assert (
        str(marketplace / "plugins" / "brain-hub" / "scripts" / "capture_hook.py") in hook_command
    )


def test_installed_windows_plugin_uses_encoded_powershell_hook(
    tmp_path: Path,
) -> None:
    source = make_checkout(tmp_path / "checkout")
    install_root = tmp_path / "A&B %TEMP% O'Brien"
    executable = (
        install_root
        / "runtime"
        / "versions"
        / "0.9.0"
        / "venv"
        / "Scripts"
        / "brainhub.exe"
    )

    marketplace = install.install_plugin_copy(
        source,
        install_root,
        executable,
        install.Reporter(io.StringIO()),
        windows=True,
    )

    installed_hooks = json.loads(
        (marketplace / "plugins" / "brain-hub" / "hooks" / "hooks.json").read_text("utf-8")
    )
    command = installed_hooks["hooks"]["SessionStart"][0]["hooks"][0]["commandWindows"]
    prefix = "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand "
    assert command.startswith(prefix)
    decoded = base64.b64decode(command.removeprefix(prefix)).decode("utf-16-le")
    expected_python = str(executable.parents[1] / "Scripts" / "python.exe").replace(
        "'",
        "''",
    )
    expected_script = str(
        marketplace / "plugins" / "brain-hub" / "scripts" / "capture_hook.py"
    ).replace("'", "''")
    assert decoded == f"& '{expected_python}' '{expected_script}'\nexit $LASTEXITCODE"


def test_plugin_install_rejects_source_destination_overlap(tmp_path: Path) -> None:
    source = make_checkout(tmp_path / "checkout")
    source_plugin = source / "plugins" / "brain-hub"

    with pytest.raises(install.InstallerError, match="overlaps"):
        install.install_plugin_copy(
            source,
            source_plugin,
            tmp_path / "venv" / "bin" / "brainhub",
            install.Reporter(io.StringIO()),
            windows=False,
        )

    assert not (source_plugin / "marketplace").exists()


def test_plugin_staging_is_cleaned_after_configuration_failure(
    tmp_path: Path,
) -> None:
    source = make_checkout(tmp_path / "checkout")
    (source / "plugins" / "brain-hub" / "hooks" / "hooks.json").write_text(
        "{invalid hooks",
        encoding="utf-8",
    )
    install_root = tmp_path / "state"

    with pytest.raises(install.InstallerError, match="configure installed plugin hooks"):
        install.install_plugin_copy(
            source,
            install_root,
            tmp_path / "venv" / "bin" / "brainhub",
            install.Reporter(io.StringIO()),
            windows=False,
        )

    plugin_parent = install_root / "marketplace" / "plugins"
    assert not list(plugin_parent.glob(".brain-hub-plugin-staging-*"))


def test_plugin_staging_is_cleaned_and_previous_copy_restored_on_activation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_checkout(tmp_path / "checkout")
    install_root = tmp_path / "state"
    executable = tmp_path / "venv" / "bin" / "brainhub"
    reporter = install.Reporter(io.StringIO())
    marketplace = install.install_plugin_copy(
        source,
        install_root,
        executable,
        reporter,
        windows=False,
    )
    installed_script = (
        marketplace / "plugins" / "brain-hub" / "scripts" / "capture_hook.py"
    )
    previous_contents = installed_script.read_text("utf-8")
    (source / "plugins" / "brain-hub" / "scripts" / "capture_hook.py").write_text(
        "raise SystemExit(99)\n",
        encoding="utf-8",
    )
    installed_plugin = marketplace / "plugins" / "brain-hub"
    real_replace = install.os.replace

    def fail_staged_activation(source_path: os.PathLike[str], target_path: os.PathLike[str]) -> None:
        source_candidate = Path(source_path)
        target_candidate = Path(target_path)
        if (
            source_candidate.name == "brain-hub"
            and source_candidate.parent.name.startswith(".brain-hub-plugin-staging-")
            and target_candidate == installed_plugin
        ):
            raise OSError("simulated activation failure")
        real_replace(source_path, target_path)

    monkeypatch.setattr(install.os, "replace", fail_staged_activation)

    with pytest.raises(install.InstallerError, match="prepare or activate"):
        install.install_plugin_copy(
            source,
            install_root,
            executable,
            reporter,
            windows=False,
        )

    assert installed_script.read_text("utf-8") == previous_contents
    assert not list(installed_plugin.parent.glob(".brain-hub-plugin-staging-*"))
    assert not list(installed_plugin.parent.glob(".brain-hub-plugin-backup-*"))


def test_codex_registration_gracefully_skips_absence_and_conflict(
    tmp_path: Path,
) -> None:
    reporter = install.Reporter(io.StringIO())
    assert (
        install.register_codex_plugin(
            tmp_path,
            reporter,
            which=lambda _name: None,
        )
        == "codex-absent"
    )

    commands: list[list[str]] = []

    def conflict_runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "marketplace name conflict: different source",
        )

    assert (
        install.register_codex_plugin(
            tmp_path,
            reporter,
            runner=conflict_runner,
            which=lambda _name: "/usr/local/bin/codex",
        )
        == "marketplace-conflict"
    )
    assert len(commands) == 1


def test_codex_registration_does_not_trust_a_generic_existing_marketplace(
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def already_runner(
        command: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "Marketplace 'brain-hub' already exists",
        )

    result = install.register_codex_plugin(
        tmp_path,
        install.Reporter(io.StringIO()),
        runner=already_runner,
        which=lambda _name: "/usr/local/bin/codex",
    )

    assert result == "marketplace-unverified"
    assert commands == [
        [
            "/usr/local/bin/codex",
            "plugin",
            "marketplace",
            "list",
            "--json",
        ]
    ]


def test_codex_registration_adds_and_verifies_the_managed_plugin(
    tmp_path: Path,
) -> None:
    source = make_checkout(tmp_path / "checkout")
    executable = tmp_path / "state" / "runtime" / "versions" / "runtime" / "venv"
    executable = executable / "bin" / "brainhub"
    marketplace = install.install_plugin_copy(
        source,
        tmp_path / "state",
        executable,
        install.Reporter(io.StringIO()),
        windows=False,
    )
    manifest = json.loads(
        (
            marketplace
            / "plugins"
            / "brain-hub"
            / ".codex-plugin"
            / "plugin.json"
        ).read_text("utf-8")
    )
    commands: list[list[str]] = []

    def registration_runner(
        command: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[-3:] == ["marketplace", "list", "--json"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"marketplaces": []}),
                "",
            )
        if command[-2:] == ["list", "--json"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "installed": [
                            {
                                "name": "brain-hub",
                                "marketplaceName": install.MANAGED_MARKETPLACE_NAME,
                                "version": manifest["version"],
                                "installed": True,
                                "source": {
                                    "source": "local",
                                    "path": str(
                                        marketplace / "plugins" / "brain-hub"
                                    ),
                                },
                            }
                        ]
                    }
                ),
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    result = install.register_codex_plugin(
        marketplace,
        install.Reporter(io.StringIO()),
        runner=registration_runner,
        which=lambda _name: "/usr/local/bin/codex",
    )

    assert result == "registered"
    assert commands == [
        [
            "/usr/local/bin/codex",
            "plugin",
            "marketplace",
            "list",
            "--json",
        ],
        [
            "/usr/local/bin/codex",
            "plugin",
            "marketplace",
            "add",
            str(marketplace),
        ],
        [
            "/usr/local/bin/codex",
            "plugin",
            "add",
            f"brain-hub@{install.MANAGED_MARKETPLACE_NAME}",
        ],
        [
            "/usr/local/bin/codex",
            "plugin",
            "list",
            "--json",
        ],
    ]


def test_codex_registration_reuses_only_the_matching_managed_marketplace(
    tmp_path: Path,
) -> None:
    source = make_checkout(tmp_path / "checkout")
    executable = tmp_path / "state" / "runtime" / "versions" / "runtime" / "venv"
    executable = executable / "bin" / "brainhub"
    marketplace = install.install_plugin_copy(
        source,
        tmp_path / "state",
        executable,
        install.Reporter(io.StringIO()),
        windows=False,
    )
    manifest = json.loads(
        (
            marketplace
            / "plugins"
            / "brain-hub"
            / ".codex-plugin"
            / "plugin.json"
        ).read_text("utf-8")
    )
    commands: list[list[str]] = []

    def registration_runner(
        command: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[-3:] == ["marketplace", "list", "--json"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "marketplaces": [
                            {
                                "name": install.MANAGED_MARKETPLACE_NAME,
                                "root": str(marketplace),
                                "marketplaceSource": {
                                    "sourceType": "local",
                                    "source": str(marketplace),
                                },
                            }
                        ]
                    }
                ),
                "",
            )
        if command[-2:] == ["list", "--json"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "installed": [
                            {
                                "name": "brain-hub",
                                "marketplaceName": install.MANAGED_MARKETPLACE_NAME,
                                "version": manifest["version"],
                                "installed": True,
                                "source": {
                                    "source": "local",
                                    "path": str(
                                        marketplace / "plugins" / "brain-hub"
                                    ),
                                },
                            }
                        ]
                    }
                ),
                "",
            )
        if command[-2:] == [
            "add",
            f"brain-hub@{install.MANAGED_MARKETPLACE_NAME}",
        ]:
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "plugin already installed",
            )
        raise AssertionError(f"unexpected command: {command}")

    result = install.register_codex_plugin(
        marketplace,
        install.Reporter(io.StringIO()),
        runner=registration_runner,
        which=lambda _name: "/usr/local/bin/codex",
    )

    assert result == "already-installed"
    assert not any(command[-3:-1] == ["marketplace", "add"] for command in commands)


def test_codex_registration_rejects_a_different_managed_marketplace_path(
    tmp_path: Path,
) -> None:
    marketplace = tmp_path / "marketplace"
    commands: list[list[str]] = []

    def registration_runner(
        command: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "marketplaces": [
                        {
                            "name": install.MANAGED_MARKETPLACE_NAME,
                            "marketplaceSource": {
                                "sourceType": "local",
                                "source": str(tmp_path / "different"),
                            },
                        }
                    ]
                }
            ),
            "",
        )

    result = install.register_codex_plugin(
        marketplace,
        install.Reporter(io.StringIO()),
        runner=registration_runner,
        which=lambda _name: "/usr/local/bin/codex",
    )

    assert result == "marketplace-conflict"
    assert len(commands) == 1


def test_json_mcp_registration_preserves_other_servers_and_upgrades_managed_path(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "state"
    _old_venv, old_executable = make_runtime_fixture(install_root, "old-runtime")
    _new_venv, new_executable = make_runtime_fixture(install_root, "new-runtime")
    config = tmp_path / ".cursor" / "mcp.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "existing": {"command": "other-tool", "args": []},
                    "brain-hub": {
                        "type": "stdio",
                        "command": str(old_executable),
                        "args": ["_plugin-mcp"],
                    },
                },
                "unrelated": {"keep": True},
            }
        ),
        encoding="utf-8",
    )

    result = install.install_json_mcp_config(
        config,
        new_executable,
        install_root,
        install.Reporter(io.StringIO()),
        host_name="Cursor",
    )

    assert result == "registered"
    updated = json.loads(config.read_text("utf-8"))
    assert updated["mcpServers"]["existing"]["command"] == "other-tool"
    assert updated["unrelated"] == {"keep": True}
    assert updated["mcpServers"]["brain-hub"] == {
        "type": "stdio",
        "command": str(new_executable.resolve()),
        "args": ["_plugin-mcp"],
    }


def test_json_mcp_registration_never_overwrites_an_unmanaged_entry(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "state"
    _new_venv, new_executable = make_runtime_fixture(install_root, "new-runtime")
    config = tmp_path / "mcp.json"
    original = {
        "mcpServers": {
            "brain-hub": {
                "command": "/some/user/tool",
                "args": ["serve"],
            }
        }
    }
    config.write_text(json.dumps(original), encoding="utf-8")

    result = install.install_json_mcp_config(
        config,
        new_executable,
        install_root,
        install.Reporter(io.StringIO()),
        host_name="Cursor",
    )

    assert result == "config-conflict"
    assert json.loads(config.read_text("utf-8")) == original


def test_claude_registration_adds_a_user_scoped_absolute_mcp(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "state"
    _venv, executable = make_runtime_fixture(install_root, "runtime")
    commands: list[list[str]] = []

    def claude_runner(
        command: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[-3:] == ["mcp", "get", "brain-hub"]:
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                'No MCP server named "brain-hub".',
            )
        return subprocess.CompletedProcess(command, 0, "added", "")

    result = install.register_claude_mcp(
        executable,
        install_root,
        install.Reporter(io.StringIO()),
        runner=claude_runner,
        which=lambda _name: "/usr/local/bin/claude",
    )

    assert result == "registered"
    assert commands[0] == [
        "/usr/local/bin/claude",
        "mcp",
        "get",
        "brain-hub",
    ]
    assert commands[1][:6] == [
        "/usr/local/bin/claude",
        "mcp",
        "add-json",
        "--scope",
        "user",
        "brain-hub",
    ]
    payload = json.loads(commands[1][6])
    assert payload == {
        "type": "stdio",
        "command": str(executable.resolve()),
        "args": ["_plugin-mcp"],
    }


def test_claude_registration_upgrades_only_a_prior_managed_runtime(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "state"
    _old_venv, old_executable = make_runtime_fixture(install_root, "old-runtime")
    _new_venv, new_executable = make_runtime_fixture(install_root, "new-runtime")
    commands: list[list[str]] = []

    def claude_runner(
        command: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[-3:] == ["mcp", "get", "brain-hub"]:
            return subprocess.CompletedProcess(
                command,
                0,
                (
                    "brain-hub:\n"
                    "  Scope: User config\n"
                    f"  Command: {old_executable}\n"
                    "  Args: _plugin-mcp\n"
                ),
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    result = install.register_claude_mcp(
        new_executable,
        install_root,
        install.Reporter(io.StringIO()),
        runner=claude_runner,
        which=lambda _name: "/usr/local/bin/claude",
    )

    assert result == "registered"
    assert commands[1] == [
        "/usr/local/bin/claude",
        "mcp",
        "remove",
        "brain-hub",
        "--scope",
        "user",
    ]
    assert json.loads(commands[2][6])["command"] == str(new_executable.resolve())


def test_local_and_remote_dry_runs_make_no_changes(tmp_path: Path) -> None:
    source = make_checkout(tmp_path / "checkout")
    install_root = tmp_path / "state"
    bin_dir = tmp_path / "bin"

    def forbidden_runner(_command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("dry run must not execute commands")

    local_options = options_for(
        source,
        install_root,
        bin_dir,
        dry_run=True,
    )
    install.execute(
        local_options,
        install.Reporter(io.StringIO()),
        runner=forbidden_runner,
    )
    assert not install_root.exists()
    assert not bin_dir.exists()

    remote_options = install.InstallOptions(
        source="https://github.com/example/brain-hub",
        ref="main",
        install_root=install_root,
        bin_dir=bin_dir,
        dry_run=True,
    )
    install.execute(
        remote_options,
        install.Reporter(io.StringIO()),
        runner=forbidden_runner,
    )
    assert not install_root.exists()
    assert not bin_dir.exists()
