@echo off
setlocal
cd /d "%~dp0"
echo Starting RbxMigrator v3.35...
where py >nul 2>&1
if not errorlevel 1 (
    py RbxMigrator.py
) else (
    where python >nul 2>&1
    if not errorlevel 1 (
        python RbxMigrator.py
    ) else (
        echo Python was not found. Install Python 3.10+ and enable it on PATH.
    )
)
echo.
echo RbxMigrator has closed. If an error appeared above, check it before retrying.
pause
