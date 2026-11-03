"""Offline regression checks for v3.37 migration preflight locking."""
from pathlib import Path

BASE = Path(__file__).resolve().parent
PLUGIN = (BASE / "RbxMigrator_Studio.plugin.lua").read_text(encoding="utf-8")
CORE = (BASE / "rbxmigrator_core.py").read_text(encoding="utf-8")


def migration_function():
    start = PLUGIN.index("local function migrateSelected()")
    end = PLUGIN.index("----------------------------------------------------------------\n-- Cache-only tools", start)
    return PLUGIN[start:end]


def test_preflight_lock_is_set_before_the_yielding_scan():
    function = migration_function()
    assert "if migrationPreflight then" in function
    assert function.index("migrationPreflight = true") < function.index("scanAllReferences(")
    assert "Validation already running" in function


def test_preflight_lock_is_released_on_all_early_exit_paths():
    function = migration_function()
    assert "local function stopPreflight(" in function
    assert "migrationPreflight = false" in function
    assert "stopPreflight(" in function
    assert "migrationPreflight = false\n\tsetBusy(true)" in function


def test_stale_preview_is_only_approved_after_successful_refresh():
    function = migration_function()
    assert "local previewOK = previewSelected()" in function
    assert "if previewOK then" in function
    assert "Preview refreshed successfully." in function
    assert "previewSignature = nil -- approval is single-use" in function


def test_preview_and_asset_type_controls_cannot_race_preflight():
    for control in (
        "animationTypeButton.MouseButton1Click",
        "soundTypeButton.MouseButton1Click",
        "meshTypeButton.MouseButton1Click",
        "forceButton.MouseButton1Click",
        "previewUiButton.MouseButton1Click",
        "refreshSelectionButton.MouseButton1Click",
    ):
        start = PLUGIN.index(control)
        end = PLUGIN.find("\nend)", start)
        assert end != -1
        assert "migrationPreflight" in PLUGIN[start:end]


def test_dashboard_version_matches_plugin_release():
    assert 'APP_TITLE = "RbxMigrator v3.37 — migration preflight lock"' in CORE


if __name__ == "__main__":
    tests = sorted(
        (name, fn)
        for name, fn in globals().items()
        if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print("PASS", name)
    print(f"All {len(tests)} v3.37 regression tests passed")
