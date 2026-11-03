# RbxMigrator v3.37

**RbxMigrator** is a Roblox asset migration tool. This release keeps the API-first downloader and bounded failed-asset retries, improves Studio plugin scan performance, and prevents overlapping migration preflight scans.

## Package contents

- `RbxMigrator.py` — local dashboard and helper server
- `rbxmigrator_core.py` — migration, downloader, and Roblox/RBXDL integration
- `RbxMigrator_Studio.plugin.lua` — Roblox Studio plugin source
- `Launch_RbxMigrator.bat` — Windows launcher
- `test_*.py` — offline regression tests
- `README_RbxMigrator.md` — Spanish instructions

## Run on Windows

1. Extract the ZIP into a folder you control.
2. Install Python 3.10+ if needed.
3. Open PowerShell or Command Prompt in that folder and install the base dependency:

   ```powershell
   py -m pip install requests
   ```

4. Double-click `Launch_RbxMigrator.bat`, or run:

   ```powershell
   py RbxMigrator.py
   ```

5. Open the local dashboard at `http://127.0.0.1:8765` if it does not open automatically.

The helper binds to `127.0.0.1` only; it is not exposed to other devices on the network by default.

## Roblox Studio plugin

Open `RbxMigrator_Studio.plugin.lua` in Studio as a plugin source and install it using your normal local plugin workflow. The toolbar and widget display as **RbxMigrator**. Existing internal plugin IDs are intentionally retained so updating the plugin does not create duplicate toolbar buttons or widgets.

## Studio plugin scan performance

The Studio plugin keeps folder selection lightweight: changing the selection only updates the selected path and invalidates an old preview. It does not scan the folder automatically. The explicit Preview / Dry Run command performs the inventory in cooperative batches, yielding to Studio between groups of instances and during large source scans. If the selection changes while a preview is running, that preview is cancelled. The scanner also reuses a single descendant snapshot, caches script source reads within each pass, indexes newline positions once per script, and reuses inventory results for dry-run validation and cache application. This reduces redundant tree walks, repeated source-prefix scans, and selection-click stalls; actual gains depend on the selected model and Studio environment.

## Automatic retries for failed assets

If a migration finishes with failed assets, the Studio plugin retries only the assets that remain unresolved, up to **2 additional attempts**. Assets that already migrated successfully are reused from the cache to avoid unnecessary duplicate uploads.

Each retry is announced in the plugin status area and in Roblox Studio's **Output** window. If assets still fail after the final retry, their IDs and available error details remain in the migration report and final status.

Retries cannot resolve missing permissions, inaccessible private assets, persistent Roblox/RBXDL rate limits, or other permanent errors.

## API-first downloading

The downloader tries the local cache, Roblox Asset Delivery where the current account can access the asset, and the authenticated RBXDL Creator Portal bulk backend. Browser-based RBXDL Single downloads are optional and disabled by default. To use the browser fallback, install Playwright and enable the **Allow browser fallback** option in the Downloader UI:

```powershell
py -m pip install playwright
```

The tool respects Roblox/RBXDL authentication, permissions, rate limits, and one-use download tokens. It does not bypass access restrictions for private assets. A successful API request is not guaranteed for every asset.

## Existing settings and compatibility

- The app keeps the legacy private settings directory name `RobloxTowerAnimationMigrator` so an existing installation can retain its saved settings and RBXDL login session. This is an internal storage path, not the public product name.
- Existing `TM_*` environment variable names are retained for backward compatibility with earlier releases.
- The migration workspace and `migrations` folder are stored beside the Python files. Keep that folder when upgrading if you need previous jobs, mappings, downloads, or cache entries.
- Each friend should run the tool under their own Windows account and authenticate their own Roblox/RBXDL session. Do not send anyone your settings folder, session profile, cookies, or credentials.

## Offline checks

Run from the extracted folder:

```powershell
py -m py_compile RbxMigrator.py rbxmigrator_core.py
py test_backend_first.py
py test_browser_fallback.py
py test_v3_32.py
py test_v3_33.py
py test_v3_34.py
py test_v3_35.py
```

The tests are offline regression checks; they do not prove live Roblox or RBXDL downloads will succeed. Test with a small set of assets you are authorized to migrate before running a large batch.


## Migration preflight reliability

The Studio plugin now takes a separate preflight lock while it revalidates the current selection and dry-run fingerprint. This prevents rapid repeated clicks from launching overlapping scans while those scans yield to Studio. Asset-type, cache, and preview controls are also blocked during that short validation phase so the approved preview cannot change underneath the migration check. If the preview is stale, the plugin refreshes it and tells you to review the report before clicking **Migrate Selected** again. Once validation passes, the lock is handed over to the normal migration state and uploads begin.
