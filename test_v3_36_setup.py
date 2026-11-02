"""Offline static checks for the RbxMigrator first-run setup launchers."""
from pathlib import Path

base = Path(__file__).resolve().parent
ps1 = (base / "Launch_RbxMigrator.ps1").read_text(encoding="utf-8")
bat = (base / "Launch_RbxMigrator.bat").read_text(encoding="utf-8")
readme_en = (base / "README_EN.md").read_text(encoding="utf-8")
readme_es = (base / "README_RbxMigrator.md").read_text(encoding="utf-8")
requirements = (base / "requirements.txt").read_text(encoding="utf-8")
plugin = (base / "RbxMigrator_Studio.plugin.lua").read_text(encoding="utf-8")


def test_setup_reuses_or_installs_python_without_admin():
    assert "function Find-Python" in ps1
    assert "Python.Python.3.13" in ps1
    assert "https://www.python.org/ftp/python/$pythonVersion/$installerName" in ps1
    assert "InstallAllUsers=0" in ps1
    assert "Python 3.10 or newer" in ps1


def test_setup_creates_venv_and_installs_requirements():
    assert "-m venv $venvPath" in ps1
    assert "requirements.txt" in ps1
    assert "requests>=2.31,<3" in requirements


def test_setup_installs_plugin_if_missing_then_launches_batch():
    assert "Roblox\\Plugins" in ps1
    assert "Copy-Item -LiteralPath $pluginSource" in ps1
    assert "RbxMigrator.lua" in ps1
    assert "& $batchLauncher" in ps1
    assert "$env:RBXMIGRATOR_PYTHON = $venvPython" in ps1
    assert '"%RBXMIGRATOR_PYTHON%" RbxMigrator.py' in bat


def test_batch_uses_venv_for_subsequent_runs():
    assert '.venv\\Scripts\\python.exe' in bat
    assert "Launch_RbxMigrator.ps1" in bat


def test_both_readmes_explain_automatic_setup_and_plugin_install():
    assert "first-run setup" in readme_en.lower()
    assert "%LOCALAPPDATA%" in readme_en
    assert "configuración inicial" in readme_es.lower()
    assert "%LOCALAPPDATA%" in readme_es
    assert "Launch_RbxMigrator.bat" in readme_en
    assert "Launch_RbxMigrator.bat" in readme_es


def test_plugin_source_is_present():
    assert 'assert(plugin, "This script must run as a Roblox Studio plugin.")' in plugin


if __name__ == "__main__":
    tests = sorted(
        (name, fn)
        for name, fn in globals().items()
        if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print("PASS", name)
    print(f"All {len(tests)} setup tests passed")
