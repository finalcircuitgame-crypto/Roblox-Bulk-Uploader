"""Offline static regression checks for RbxMigrator v3.35 retry notices."""
from pathlib import Path

base = Path(__file__).resolve().parent
plugin = (base / "RbxMigrator_Studio.plugin.lua").read_text(encoding="utf-8")
readme = (base / "README_RbxMigrator.md").read_text(encoding="utf-8")
core = (base / "rbxmigrator_core.py").read_text(encoding="utf-8")


def test_bounded_failed_asset_retries_are_configured():
    assert "MAX_FAILED_ASSET_RETRIES = 2" in plugin
    assert "while failed > 0 and retriesUsed < MAX_FAILED_ASSET_RETRIES do" in plugin


def test_retry_notifies_plugin_status_and_studio_output():
    assert 'setStatus("Retrying failed migration", retryNotice)' in plugin
    assert 'warn("[RbxMigrator] " .. retryNotice)' in plugin
    assert "unique asset(s) still failing after " in plugin


def test_retry_reuses_successful_mappings_even_in_force_reupload_mode():
    assert "local canReuseCache = not forceReupload or (retryAttempt or 0) > 0" in plugin
    assert "AUTOMATIC RETRY SUMMARY" in plugin


def test_readme_is_spanish_and_documents_retry_behavior():
    assert "## Reintentos automáticos de recursos fallidos" in readme
    assert "README_EN.md" in readme
    assert "2 reintentos adicionales" in readme


def test_visible_app_version_is_bumped():
    assert 'APP_TITLE = "RbxMigrator v3.35 — API-first + failed-asset retries"' in core


if __name__ == "__main__":
    tests = sorted((name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn))
    for name, fn in tests:
        fn()
        print("PASS", name)
    print(f"All {len(tests)} v3.35 tests passed")
