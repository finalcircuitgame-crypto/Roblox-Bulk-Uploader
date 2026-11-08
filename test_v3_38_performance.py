"""Offline regression checks for v3.38 report safety and parallel uploads."""
from pathlib import Path

BASE = Path(__file__).resolve().parent
PLUGIN = (BASE / "RbxMigrator_Studio.plugin.lua").read_text(encoding="utf-8")
CORE = (BASE / "rbxmigrator_core.py").read_text(encoding="utf-8")


def test_report_limit_has_margin_below_roblox_maximum():
    assert "local MAX_REPORT_CHARS = 180000" in PLUGIN
    assert "if #report >= MAX_REPORT_CHARS then" in PLUGIN
    assert "report = report:sub(1, MAX_REPORT_CHARS - 1)" in PLUGIN


def test_saved_report_is_sanitized_before_initial_text_assignment():
    assert "if #savedReport >= MAX_REPORT_CHARS then" in PLUGIN
    assert "reportBox.Text = savedReport" in PLUGIN


def test_source_reporting_uses_larger_batches_and_constant_time_queue():
    assert "local STUDIO_REPORT_BATCH_SIZE = 16" in PLUGIN
    assert "local reportQueueHead = 1" in PLUGIN
    assert "local reportQueueTail = 0" in PLUGIN
    assert "table.remove(reportQueue, 1)" not in PLUGIN


def test_migration_uploads_use_a_bounded_worker_pool():
    assert "MIGRATION_UPLOAD_WORKERS" in CORE
    assert 'os.environ.get("TM_MIGRATION_UPLOAD_WORKERS", "4")' in CORE
    start = CORE.index("# Pass 3: upload successfully prepared files concurrently.")
    end = CORE.index("with self.source_cache_lock:", start)
    upload_stage = CORE[start:end]
    assert "ThreadPoolExecutor(max_workers=worker_count)" in upload_stage
    assert "as_completed(future_to_asset)" in upload_stage
    assert "future.result()" in upload_stage
    assert "errors[old_id]" in upload_stage


if __name__ == "__main__":
    tests = sorted(
        (name, fn)
        for name, fn in globals().items()
        if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print("PASS", name)
    print(f"All {len(tests)} v3.38 regression tests passed")
