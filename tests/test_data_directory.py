"""Where Vault keeps its data, and what happens when it cannot write there.

Two locations by design: a frozen build stores data beside the executable so it
can run from a USB stick with nothing installed, while a pip install uses the
platform-standard directory. Both are documented in the README.

The failure path matters at least as much as either location. Resolving the data
directory happens during startup, before any window exists, and a windowed build
has no console to print to - so an unwritable folder has to produce something an
interface can show the user, naming the path and what to do about it.
"""

import pathlib
import sys

import pytest

from primer_vault.utils import DataDirectoryError, get_app_dir


@pytest.fixture
def installed(monkeypatch, tmp_path):
    """A pip-style install whose standard directory is tmp_path."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr("primer_vault.utils.user_data_dir", lambda *a, **k: str(tmp_path))
    return tmp_path


@pytest.fixture
def portable(monkeypatch, tmp_path):
    """A frozen build living at tmp_path/Vault.exe."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Vault.exe"), raising=False)
    return tmp_path


class TestLocation:

    def test_frozen_build_keeps_data_beside_the_executable(self, portable):
        assert get_app_dir() == portable / "data"

    def test_pip_install_uses_the_platform_directory(self, installed):
        assert get_app_dir() == installed

    def test_the_write_probe_leaves_nothing_behind(self, installed):
        app_dir = get_app_dir()
        assert list(app_dir.iterdir()) == []


class TestUnwritable:

    def test_folder_cannot_be_created(self, monkeypatch, portable):
        monkeypatch.setattr(
            pathlib.Path, "mkdir",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "Access is denied")))

        with pytest.raises(DataDirectoryError) as exc:
            get_app_dir()

        assert exc.value.path == portable / "data"
        assert "Access is denied" in exc.value.user_message()

    def test_folder_exists_but_rejects_writes(self, monkeypatch, installed):
        """Creating the folder is not proof it can be written to."""
        monkeypatch.setattr(
            pathlib.Path, "write_text",
            lambda *a, **k: (_ for _ in ()).throw(OSError(30, "Read-only file system")))

        with pytest.raises(DataDirectoryError) as exc:
            get_app_dir()

        assert "Read-only file system" in exc.value.user_message()

    def test_the_message_names_the_folder_and_what_to_do(self, monkeypatch, portable):
        """A user with no console gets only this text, so it has to carry both."""
        monkeypatch.setattr(
            pathlib.Path, "mkdir",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "Access is denied")))

        with pytest.raises(DataDirectoryError) as exc:
            get_app_dir()
        message = exc.value.user_message()

        assert str(portable / "data") in message
        # Portable builds are told to move the app; that advice would be wrong
        # for an install, which cannot be relocated by the user.
        assert "USB" in message
        assert "Move Vault" in message

    def test_installed_builds_get_different_advice(self, monkeypatch, installed):
        monkeypatch.setattr(
            pathlib.Path, "write_text",
            lambda *a, **k: (_ for _ in ()).throw(OSError(30, "Read-only file system")))

        with pytest.raises(DataDirectoryError) as exc:
            get_app_dir()
        message = exc.value.user_message()

        assert "USB" not in message
        assert "permission" in message
