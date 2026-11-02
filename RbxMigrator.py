from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
import webbrowser
import zipfile
import csv
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs

import rbxmigrator_core as core


HOST = "127.0.0.1"
DEFAULT_PORT = 8765
WEB_HISTORY_FILE = core.APP_DIR / "web-history.json"
RESUME_CACHE_FILE = core.MIGRATIONS_DIR / "_resume-cache.json"

JOB_STATE_FILENAME = "job.json"
JOB_MANIFEST_FILENAME = "manifest.json"
JOB_MAPPING_FILENAME = "mapping.json"
JOB_FAILURES_FILENAME = "failures.json"

MAX_HISTORY = 100

DOWNLOADS_DIR = core.MIGRATIONS_DIR / "_downloads"
DOWNLOAD_CACHE_DIR = core.MIGRATIONS_DIR / "_download-cache"
MAX_DOWNLOADER_IDS = 2000
DOWNLOAD_PRIORITY = {"Low": 0, "Normal": 1, "High": 2}

OFFICIAL_SAMPLE_SIZE = 20
OFFICIAL_WORKERS = 12
OFFICIAL_SKIP_THRESHOLD = 0.75

# Roblox Open Cloud's Create Asset API creates one asset per request.
# We make several independent requests concurrently for "batch-like" uploads.
# Four workers is a conservative default that is much faster than serial
# uploading without being obnoxious to Open Cloud rate limits.
ROBLOX_UPLOAD_WORKERS = 4
ROBLOX_UPLOAD_RETRIES = 3


def json_copy(value):
    return json.loads(json.dumps(value))


def now_ms():
    return int(time.time() * 1000)



def parse_downloader_asset_ids(source):
    text = str(source or "")
    ids = []
    seen = set()

    patterns = (
        r"rbxassetid://(\d{5,20})",
        r"roblox\.com/(?:catalog|library|asset)/(\d{5,20})",
        r"[?&]id=(\d{5,20})",
        r"(?<!\d)(\d{5,20})(?!\d)",
    )

    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = match.group(1)
            if value not in seen:
                seen.add(value)
                ids.append(value)

    return ids


def infer_downloader_name(source):
    match = re.search(
        r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{",
        str(source or ""),
    )
    return match.group(1) if match else "AssetDownload"

class WebMigratorState:
    def __init__(self):
        core.APP_DIR.mkdir(parents=True, exist_ok=True)
        core.MIGRATIONS_DIR.mkdir(parents=True, exist_ok=True)

        self.jobs = {}
        self.jobs_lock = threading.RLock()

        self.source_cache = {}
        self.source_cache_lock = threading.RLock()

        self.config_lock = threading.RLock()
        self.config = self._load_runtime_config()

        self.payloads = {}
        self.history = self._load_history()

        self.resume_lock = threading.RLock()
        self.resume_cache = self._load_resume_cache()

        self.download_jobs = {}
        self.download_jobs_lock = threading.RLock()
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        DOWNLOAD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.download_queue_condition = threading.Condition(self.download_jobs_lock)
        self._restore_download_jobs()
        # Rehydrate completed jobs/payloads so "Retry failed" still works
        # after the helper is closed and restarted.
        self._restore_persisted_jobs()

        self.live_condition = threading.Condition()
        self.live_revision = 0
        self.rbxdl_status = {
            "state": "idle",
            "message": "Session not checked yet.",
            "updatedAt": now_ms(),
        }
        self.download_runner = threading.Thread(
            target=self._download_queue_loop, name="RbxMigrator-Intelligent-Scheduler", daemon=True,
        )
        self.download_runner.start()

    # --------------------------------------------------------
    # Live events
    # --------------------------------------------------------

    def emit_change(self):
        with self.live_condition:
            self.live_revision += 1
            self.live_condition.notify_all()

    def wait_for_change(
        self,
        last_revision,
        timeout=15.0,
    ):
        with self.live_condition:
            if self.live_revision == last_revision:
                self.live_condition.wait(
                    timeout=timeout
                )
            return self.live_revision

    def snapshot(self):
        return {
            "revision": self.live_revision,
            "jobs": self.list_jobs(),
            "downloads": self.list_download_jobs(),
            "scheduler": self.scheduler_snapshot(),
            "config": self.get_config_public(),
            "rbxdl": dict(self.rbxdl_status),
        }

    def set_rbxdl_status(
        self,
        state,
        message,
    ):
        self.rbxdl_status = {
            "state": state,
            "message": str(message),
            "updatedAt": now_ms(),
        }
        self.emit_change()

    # --------------------------------------------------------
    # Config
    # --------------------------------------------------------

    def _load_runtime_config(self):
        settings = core.load_settings()

        creator_type = settings.get("creator_type", "Group")
        if creator_type not in {"User", "Group"}:
            creator_type = "Group"

        api_key = ""
        encrypted = settings.get("api_key_dpapi")

        if isinstance(encrypted, str) and encrypted:
            try:
                api_key = core.unprotect_secret(encrypted)
            except Exception:
                api_key = ""

        rolhub_key = ""
        encrypted_rolhub = settings.get("rolhub_key_dpapi")

        if isinstance(encrypted_rolhub, str) and encrypted_rolhub:
            try:
                rolhub_key = core.unprotect_secret(encrypted_rolhub)
            except Exception:
                rolhub_key = ""

        return {
            "api_key": api_key,
            "rolhub_key": rolhub_key,
            "creator_type": creator_type,
            "creator_id": str(settings.get("creator_id", "")),
            "description": str(
                settings.get(
                    "description",
                    "Migrated Roblox asset",
                )
            ),
        }

    def get_config_public(self):
        with self.config_lock:
            config = dict(self.config)

        return {
            "creatorType": config["creator_type"],
            "creatorId": config["creator_id"],
            "description": config["description"],
            "hasApiKey": bool(config["api_key"]),
            "hasRolhubKey": bool(config.get("rolhub_key")),
            "configured": bool(
                config["api_key"]
                and re.fullmatch(r"\d+", config["creator_id"])
            ),
        }

    def get_runtime_config(self):
        with self.config_lock:
            return dict(self.config)

    def save_config(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Invalid settings payload.")

        with self.config_lock:
            current = dict(self.config)

            creator_type = str(
                payload.get(
                    "creatorType",
                    current["creator_type"],
                )
            )

            if creator_type not in {"User", "Group"}:
                raise ValueError(
                    "Creator type must be User or Group."
                )

            creator_id = str(
                payload.get(
                    "creatorId",
                    current["creator_id"],
                )
            ).strip()

            if creator_id and not re.fullmatch(
                r"\d+",
                creator_id,
            ):
                raise ValueError(
                    "Creator ID must contain digits only."
                )

            description = str(
                payload.get(
                    "description",
                    current["description"],
                )
            )

            api_key = current["api_key"]

            if payload.get("clearApiKey") is True:
                api_key = ""
            else:
                incoming_key = payload.get("apiKey")
                if isinstance(incoming_key, str) and incoming_key.strip():
                    api_key = incoming_key.strip()

            rolhub_key = current.get("rolhub_key", "")

            if payload.get("clearRolhubKey") is True:
                rolhub_key = ""
            else:
                incoming_rolhub = payload.get("rolhubKey")
                if isinstance(incoming_rolhub, str) and incoming_rolhub.strip():
                    rolhub_key = incoming_rolhub.strip()

            self.config = {
                "api_key": api_key,
                "rolhub_key": rolhub_key,
                "creator_type": creator_type,
                "creator_id": creator_id,
                "description": description,
            }

            settings = {
                "creator_type": creator_type,
                "creator_id": creator_id,
                "description": description,
                "port": DEFAULT_PORT,
            }

            if api_key:
                settings["api_key_dpapi"] = (
                    core.protect_secret(api_key)
                )

            if rolhub_key:
                settings["rolhub_key_dpapi"] = (
                    core.protect_secret(rolhub_key)
                )

            core.save_settings_file(settings)

        self.emit_change()
        return self.get_config_public()

    # --------------------------------------------------------
    # Resume cache
    # --------------------------------------------------------

    def _load_resume_cache(self):
        try:
            if RESUME_CACHE_FILE.is_file():
                value = json.loads(
                    RESUME_CACHE_FILE.read_text(
                        encoding="utf-8"
                    )
                )

                if isinstance(value, dict):
                    return value
        except Exception:
            pass

        return {}

    def _save_resume_cache(self):
        try:
            RESUME_CACHE_FILE.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            temp = RESUME_CACHE_FILE.with_suffix(
                ".json.tmp"
            )

            temp.write_text(
                json.dumps(
                    self.resume_cache,
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            temp.replace(
                RESUME_CACHE_FILE
            )
        except Exception:
            pass

    @staticmethod
    def _resume_creator_key(config):
        return (
            str(config.get("creator_type") or "")
            + ":"
            + str(config.get("creator_id") or "")
        )

    def get_resume_mapping(
        self,
        config,
        asset_ids,
        asset_type="Animation",
    ):
        creator_key = self._resume_creator_key(config)
        typed_key = creator_key + ":" + asset_type

        with self.resume_lock:
            creator_cache = self.resume_cache.get(typed_key, {})
            if not creator_cache and asset_type in {"Animation", "Audio"}:
                # Compatibility with v3.22 legacy untyped migration cache.
                creator_cache = self.resume_cache.get(creator_key, {})

            if not isinstance(
                creator_cache,
                dict,
            ):
                return {}

            result = {}

            for asset_id in asset_ids:
                new_id = creator_cache.get(
                    str(asset_id)
                )

                if (
                    isinstance(new_id, str)
                    and re.fullmatch(
                        r"\d+",
                        new_id,
                    )
                ):
                    result[
                        str(asset_id)
                    ] = new_id

            return result

    def save_resume_mapping(
        self,
        config,
        old_id,
        new_id,
        asset_type="Animation",
    ):
        creator_key = self._resume_creator_key(config) + ":" + asset_type

        with self.resume_lock:
            creator_cache = self.resume_cache.setdefault(
                creator_key,
                {},
            )

            creator_cache[
                str(old_id)
            ] = str(new_id)

            self._save_resume_cache()

    # --------------------------------------------------------
    # Per-job persistence
    # --------------------------------------------------------

    @staticmethod
    def _job_folder_for(
        tower,
        job_id,
    ):
        return (
            core.MIGRATIONS_DIR
            / core.safe_filename(
                str(tower)
            )
            / str(job_id)
        )

    @staticmethod
    def _write_json_atomic(
        path,
        value,
    ):
        path = Path(path)
        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temp = path.with_suffix(
            path.suffix + ".tmp"
        )

        temp.write_text(
            json.dumps(
                value,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        temp.replace(path)

    def _persist_manifest(
        self,
        job_id,
    ):
        with self.jobs_lock:
            job = self.jobs.get(
                job_id
            )
            payload = self.payloads.get(
                job_id
            )

            if not job or not payload:
                return

            manifest = {
                "jobId": job_id,
                "tower":
                    payload.get(
                        "tower",
                        job.get(
                            "tower",
                            "",
                        ),
                    ),
                "rootPath":
                    payload.get(
                        "rootPath"
                    ),
                "assetType":
                    payload.get(
                        "assetType",
                        "Animation",
                    ),
                "assets":
                    payload.get(
                        "assets",
                        payload.get(
                            "animations",
                            [],
                        ),
                    ),
                "forceReupload":
                    bool(
                        payload.get(
                            "forceReupload",
                            False,
                        )
                    ),
                "createdAt":
                    job.get(
                        "createdAt"
                    ),
            }

            folder = (
                self._job_folder_for(
                    job.get(
                        "tower",
                        manifest["tower"],
                    ),
                    job_id,
                )
            )

        try:
            self._write_json_atomic(
                folder
                / JOB_MANIFEST_FILENAME,
                manifest,
            )
        except Exception:
            pass

    def _persist_finished_job(
        self,
        job,
    ):
        try:
            folder = (
                self._job_folder_for(
                    job.get(
                        "tower",
                        "Migration",
                    ),
                    job.get(
                        "jobId",
                        "unknown",
                    ),
                )
            )

            folder.mkdir(
                parents=True,
                exist_ok=True,
            )

            self._write_json_atomic(
                folder
                / JOB_STATE_FILENAME,
                job,
            )

            self._write_json_atomic(
                folder
                / JOB_MAPPING_FILENAME,
                job.get(
                    "mapping",
                    {},
                ),
            )

            self._write_json_atomic(
                folder
                / JOB_FAILURES_FILENAME,
                job.get(
                    "errors",
                    {},
                ),
            )

        except Exception:
            pass

    @staticmethod
    def _unique_from_assets(
        assets,
    ):
        unique = {}

        if not isinstance(
            assets,
            list,
        ):
            return unique

        for entry in assets:
            if not isinstance(
                entry,
                dict,
            ):
                continue

            match = re.search(
                r"\d+",
                str(
                    entry.get(
                        "assetId"
                    )
                    or ""
                ),
            )

            if not match:
                continue

            asset_id = match.group(0)

            if asset_id not in unique:
                unique[
                    asset_id
                ] = dict(entry)

        return unique

    _unique_from_animations = _unique_from_assets

    def _payload_from_manifest(
        self,
        manifest,
    ):
        if not isinstance(
            manifest,
            dict,
        ):
            return None

        asset_type = str(
            manifest.get(
                "assetType"
            )
            or "Animation"
        )

        assets = manifest.get(
            "assets"
        )

        # v3.14 compatibility.
        if not isinstance(assets, list):
            assets = manifest.get(
                "animations"
            )

        if not isinstance(
            assets,
            list,
        ):
            return None

        return {
            "tower":
                str(
                    manifest.get(
                        "tower"
                    )
                    or "Migration"
                ),
            "rootPath":
                manifest.get(
                    "rootPath"
                ),
            "assetType": asset_type,
            "assets": assets,
            "animations": assets if asset_type == "Animation" else [],
            "unique":
                self._unique_from_assets(
                    assets
                ),
            "forceReupload":
                bool(
                    manifest.get(
                        "forceReupload",
                        False,
                    )
                ),
        }

    def _history_job_stub(
        self,
        item,
    ):
        total = int(
            item.get(
                "total",
                0,
            )
            or 0
        )

        state = str(
            item.get(
                "state"
            )
            or "done"
        )

        return {
            "ok": True,
            "jobId":
                str(
                    item.get(
                        "jobId"
                    )
                    or ""
                ),
            "tower":
                str(
                    item.get(
                        "tower"
                    )
                    or "Migration"
                ),
            "assetType":
                str(
                    item.get(
                        "assetType"
                    )
                    or "Animation"
                ),
            "state": state,
            "phase": "complete",
            "phaseCompleted": total,
            "phaseTotal": total,
            "phaseStartedAt":
                item.get(
                    "createdAt"
                ),
            "overallProgress": 1.0,
            "etaSeconds": 0.0,
            "ratePerMinute": 0.0,
            "elapsedSeconds": 0.0,
            "uploadWorkers": 0,
            "total": total,
            "completed": total,
            "migrated": int(
                item.get(
                    "migrated",
                    0,
                )
                or 0
            ),
            "failed": int(
                item.get(
                    "failed",
                    0,
                )
                or 0
            ),
            "message":
                str(
                    item.get(
                        "message"
                    )
                    or state
                ),
            "mapping":
                dict(
                    item.get(
                        "mapping",
                        {},
                    )
                    or {}
                ),
            "errors":
                dict(
                    item.get(
                        "errors",
                        {},
                    )
                    or {}
                ),
            "fetchSources":
                dict(
                    item.get(
                        "fetchSources",
                        {},
                    )
                    or {}
                ),
            # Old v3.13 history does not contain per-asset rows. That is fine:
            # retry can still be reconstructed from errors.keys().
            "assets": {},
            "createdAt":
                item.get(
                    "createdAt"
                )
                or now_ms(),
            "finishedAt":
                item.get(
                    "finishedAt"
                ),
        }

    def _restore_persisted_jobs(
        self,
    ):
        restored_ids = set()

        # First restore every history entry as a lightweight job. This is also
        # the compatibility path for jobs completed by v3.13 and earlier.
        for item in self.history:
            if not isinstance(
                item,
                dict,
            ):
                continue

            job_id = str(
                item.get(
                    "jobId"
                )
                or ""
            )

            if not job_id:
                continue

            stub = self._history_job_stub(
                item
            )

            self.jobs[
                job_id
            ] = stub

            restored_ids.add(
                job_id
            )

            retry_assets = item.get(
                "retryAssets"
            )

            if not isinstance(retry_assets, list):
                retry_assets = item.get(
                    "retryAnimations"
                )

            if isinstance(
                retry_assets,
                list,
            ):
                asset_type = str(
                    item.get(
                        "assetType"
                    )
                    or "Animation"
                )

                self.payloads[
                    job_id
                ] = {
                    "tower":
                        stub["tower"],
                    "rootPath":
                        item.get(
                            "rootPath"
                        ),
                    "assetType": asset_type,
                    "assets": retry_assets,
                    "animations": retry_assets if asset_type == "Animation" else [],
                    "unique":
                        self._unique_from_assets(
                            retry_assets
                        ),
                    "forceReupload":
                        False,
                }

        # Then upgrade lightweight history rows with full job/manifest data
        # wherever v3.14+ persisted it.
        try:
            state_files = sorted(
                core.MIGRATIONS_DIR.rglob(
                    JOB_STATE_FILENAME
                ),
                key=lambda path:
                    path.stat().st_mtime,
                reverse=True,
            )
        except Exception:
            state_files = []

        for state_path in state_files[
            :MAX_HISTORY
        ]:
            try:
                value = json.loads(
                    state_path.read_text(
                        encoding="utf-8"
                    )
                )

                if not isinstance(
                    value,
                    dict,
                ):
                    continue

                job_id = str(
                    value.get(
                        "jobId"
                    )
                    or ""
                )

                if not job_id:
                    continue

                self.jobs[
                    job_id
                ] = value

                restored_ids.add(
                    job_id
                )

                manifest_path = (
                    state_path.parent
                    / JOB_MANIFEST_FILENAME
                )

                if manifest_path.is_file():
                    manifest = json.loads(
                        manifest_path.read_text(
                            encoding="utf-8"
                        )
                    )

                    payload = (
                        self._payload_from_manifest(
                            manifest
                        )
                    )

                    if payload:
                        self.payloads[
                            job_id
                        ] = payload

            except Exception:
                continue

        # A manifest can exist even if the helper died before final job.json
        # was written. Load it for retry/recovery purposes.
        try:
            manifest_files = sorted(
                core.MIGRATIONS_DIR.rglob(
                    JOB_MANIFEST_FILENAME
                ),
                key=lambda path:
                    path.stat().st_mtime,
                reverse=True,
            )
        except Exception:
            manifest_files = []

        for manifest_path in manifest_files[
            :MAX_HISTORY
        ]:
            try:
                manifest = json.loads(
                    manifest_path.read_text(
                        encoding="utf-8"
                    )
                )

                job_id = str(
                    manifest.get(
                        "jobId"
                    )
                    or ""
                )

                if (
                    not job_id
                    or job_id
                    in self.payloads
                ):
                    continue

                payload = (
                    self._payload_from_manifest(
                        manifest
                    )
                )

                if payload:
                    self.payloads[
                        job_id
                    ] = payload

            except Exception:
                continue

    # --------------------------------------------------------
    # History
    # --------------------------------------------------------

    def _load_history(self):
        try:
            if WEB_HISTORY_FILE.is_file():
                value = json.loads(
                    WEB_HISTORY_FILE.read_text(
                        encoding="utf-8"
                    )
                )
                if isinstance(value, list):
                    return value[:MAX_HISTORY]
        except Exception:
            pass

        return []

    def _save_history(self):
        try:
            WEB_HISTORY_FILE.write_text(
                json.dumps(
                    self.history[:MAX_HISTORY],
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    def add_to_history(self, job):
        payload = self.payloads.get(
            job["jobId"],
            {},
        )

        failed_ids = set(
            str(asset_id)
            for asset_id in (
                job.get(
                    "errors",
                    {}
                )
                or {}
            ).keys()
        )

        asset_type = str(
            job.get(
                "assetType"
            )
            or payload.get(
                "assetType",
                "Animation",
            )
        )

        retry_assets = []

        for entry in (
            payload.get(
                "assets",
                payload.get(
                    "animations",
                    [],
                ),
            )
            if isinstance(
                payload,
                dict,
            )
            else []
        ):
            if not isinstance(
                entry,
                dict,
            ):
                continue

            match = re.search(
                r"\d+",
                str(
                    entry.get(
                        "assetId"
                    )
                    or ""
                ),
            )

            if (
                match
                and match.group(0)
                in failed_ids
            ):
                retry_assets.append(
                    dict(entry)
                )

        summary = {
            "jobId": job["jobId"],
            "tower": job["tower"],
            "assetType": asset_type,
            "state": job["state"],
            "total": job["total"],
            "migrated": job["migrated"],
            "failed": job["failed"],
            "message": job["message"],
            "createdAt": job["createdAt"],
            "finishedAt": job.get("finishedAt"),
            "mapping": job.get("mapping", {}),
            "errors": job.get("errors", {}),
            "fetchSources": job.get("fetchSources", {}),
            "rootPath":
                payload.get(
                    "rootPath"
                )
                if isinstance(
                    payload,
                    dict,
                )
                else None,
            "retryAssets": retry_assets,
            # Keep the legacy field for old dashboards/scripts.
            "retryAnimations": retry_assets if asset_type == "Animation" else [],
        }

        self._persist_finished_job(
            job
        )

        self.history = [
            item
            for item in self.history
            if item.get("jobId") != job["jobId"]
        ]

        self.history.insert(0, summary)
        self.history = self.history[:MAX_HISTORY]
        self._save_history()
        self.emit_change()

    # --------------------------------------------------------
    # Jobs
    # --------------------------------------------------------

    def _normalize_job_payload(self, payload):
        if not isinstance(payload, dict):
            raise ValueError(
                "Invalid migration request."
            )

        tower = str(
            payload.get("tower") or ""
        ).strip()

        asset_type = str(
            payload.get(
                "assetType"
            )
            or "Animation"
        )

        if asset_type not in {
            "Animation",
            "Audio",
            "Mesh",
        }:
            raise ValueError(
                f"Unsupported asset type: {asset_type}"
            )

        assets = payload.get(
            "assets"
        )

        if not isinstance(assets, list):
            assets = payload.get(
                "animations"
            )

        if not tower:
            raise ValueError(
                "Selection name is missing."
            )

        if not isinstance(
            assets,
            list,
        ):
            raise ValueError(
                f"{asset_type} manifest is missing."
            )

        config = self.get_runtime_config()

        if not config.get("api_key"):
            raise ValueError(
                "Open Cloud API key is not configured."
            )

        if not re.fullmatch(
            r"\d+",
            config.get(
                "creator_id",
                "",
            ),
        ):
            raise ValueError(
                "Creator ID is not configured."
            )

        unique = self._unique_from_assets(
            assets
        )

        if not unique:
            raise ValueError(
                f"No valid {asset_type} asset IDs were found."
            )

        return (
            tower,
            asset_type,
            assets,
            unique,
            config,
        )

    def prepare_job(self, payload):
        (
            tower,
            asset_type,
            assets_manifest,
            unique,
            config,
        ) = self._normalize_job_payload(
            payload
        )

        job_id = str(
            uuid.uuid4()
        )

        assets = {}

        for old_id, entry in unique.items():
            assets[
                old_id
            ] = {
                "assetId": old_id,
                "name":
                    core.suggested_asset_name(
                        entry,
                        asset_type,
                    ),
                "status":
                    "queued",
                "source": "",
                "newId": "",
                "error": "",
            }

        job = {
            "ok": True,
            "jobId": job_id,
            "tower": tower,
            "assetType": asset_type,
            "state": "preparing",
            "phase": (
                "studio-prep"
                if asset_type == "Animation"
                else "prepared"
            ),
            "phaseCompleted": 0,
            "phaseTotal": len(unique),
            "phaseStartedAt": now_ms(),
            "overallProgress": 0.0,
            "etaSeconds": None,
            "ratePerMinute": 0.0,
            "elapsedSeconds": 0.0,
            "uploadWorkers": 0,
            "total": len(unique),
            "completed": 0,
            "migrated": 0,
            "failed": 0,
            "message": (
                "Waiting for Studio source preparation..."
                if asset_type == "Animation"
                else f"{asset_type} manifest ready; start the migration."
            ),
            "mapping": {},
            "errors": {},
            "fetchSources": {},
            "assets": assets,
            "createdAt": now_ms(),
            "finishedAt": None,
        }

        with self.jobs_lock:
            self.jobs[
                job_id
            ] = job

            self.payloads[
                job_id
            ] = {
                "tower": tower,
                "rootPath":
                    payload.get(
                        "rootPath"
                    ),
                "assetType": asset_type,
                "assets": assets_manifest,
                "animations": assets_manifest if asset_type == "Animation" else [],
                "unique": unique,
                "forceReupload":
                    bool(
                        payload.get(
                            "forceReupload",
                            False,
                        )
                    ),
            }

        # Save the original manifest immediately. Even if the helper is
        # closed later, the exact asset entries needed for retry survive.
        self._persist_manifest(
            job_id
        )

        self.emit_change()

        return job_id

    def mark_prepare_item(
        self,
        job_id,
        asset_id,
        success,
        error="",
    ):
        with self.jobs_lock:
            job = self.jobs.get(
                job_id
            )

            if not job:
                raise ValueError(
                    "Unknown migration job."
                )

            asset = job[
                "assets"
            ].get(
                str(asset_id)
            )

            if asset is None:
                raise ValueError(
                    "Unknown asset for this job."
                )

            # Do not double-count if Studio retries an item.
            already_prepared = bool(
                asset.get(
                    "_studioPrepReported"
                )
            )

            asset[
                "_studioPrepReported"
            ] = True

            if success:
                asset[
                    "status"
                ] = "studio-ready"
                asset[
                    "source"
                ] = (
                    "Roblox Studio "
                    "AnimationClipProvider"
                )
                asset[
                    "error"
                ] = ""
            else:
                asset[
                    "status"
                ] = "queued"

                if error:
                    asset[
                        "studioError"
                    ] = str(error)

            if not already_prepared:
                job[
                    "phaseCompleted"
                ] = min(
                    int(
                        job.get(
                            "phaseCompleted",
                            0,
                        )
                    )
                    + 1,
                    int(
                        job.get(
                            "phaseTotal",
                            0,
                        )
                    ),
                )

            phase_completed = int(
                job.get(
                    "phaseCompleted",
                    0,
                )
            )
            phase_total = max(
                int(
                    job.get(
                        "phaseTotal",
                        1,
                    )
                ),
                1,
            )

            job[
                "overallProgress"
            ] = (
                0.18
                * (
                    phase_completed
                    / phase_total
                )
            )

            phase_started_at = int(
                job.get(
                    "phaseStartedAt",
                    job.get("createdAt", now_ms()),
                )
            )
            elapsed_seconds = max(
                (now_ms() - phase_started_at) / 1000.0,
                0.001,
            )
            rate_per_second = (
                phase_completed / elapsed_seconds
                if phase_completed > 0
                else 0.0
            )
            remaining = max(
                phase_total - phase_completed,
                0,
            )

            job["elapsedSeconds"] = elapsed_seconds
            job["ratePerMinute"] = rate_per_second * 60.0
            job["etaSeconds"] = (
                remaining / rate_per_second
                if rate_per_second > 0
                else None
            )

            job[
                "message"
            ] = (
                "Preparing Studio sources "
                f"{phase_completed}/"
                f"{phase_total}"
            )

        self.emit_change()

    def start_prepared_job(
        self,
        job_id,
    ):
        with self.jobs_lock:
            job = self.jobs.get(
                job_id
            )
            payload = self.payloads.get(
                job_id
            )

            if not job or not payload:
                raise ValueError(
                    "Unknown migration job."
                )

            if job.get(
                "_started"
            ):
                return

            job[
                "_started"
            ] = True
            job[
                "state"
            ] = "queued"
            job[
                "phase"
            ] = "fetching"
            job["phaseStartedAt"] = now_ms()
            job["phaseCompleted"] = 0
            job["phaseTotal"] = len(
                payload["unique"]
            )
            job["etaSeconds"] = None
            job["ratePerMinute"] = 0.0
            job["elapsedSeconds"] = 0.0
            job["uploadWorkers"] = 0
            job[
                "overallProgress"
            ] = max(
                float(
                    job.get(
                        "overallProgress",
                        0.0,
                    )
                ),
                0.18,
            )
            job[
                "message"
            ] = (
                "Studio preparation complete; "
                "starting migration..."
            )

            tower = payload[
                "tower"
            ]
            asset_type = str(
                payload.get(
                    "assetType"
                )
                or "Animation"
            )
            unique = payload[
                "unique"
            ]

        config = self.get_runtime_config()

        self.emit_change()

        threading.Thread(
            target=self.run_job,
            args=(
                job_id,
                tower,
                unique,
                config,
                asset_type,
            ),
            daemon=True,
        ).start()

    def create_job(self, payload):
        # Backwards-compatible endpoint:
        # create a job and immediately start it.
        job_id = self.prepare_job(
            payload
        )

        self.start_prepared_job(
            job_id
        )

        return job_id

    def get_job(self, job_id):
        with self.jobs_lock:
            value = self.jobs.get(job_id)
            return (
                json_copy(value)
                if value is not None
                else None
            )

    def list_jobs(self):
        with self.jobs_lock:
            values = [
                json_copy(job)
                for job in self.jobs.values()
            ]

        values.sort(
            key=lambda job:
                job.get("createdAt", 0),
            reverse=True,
        )

        return values

    def update_job(self, job_id, **changes):
        with self.jobs_lock:
            job = self.jobs.get(job_id)

            if not job:
                return

            job.update(changes)

        self.emit_change()

    def update_asset(
        self,
        job_id,
        asset_id,
        **changes,
    ):
        with self.jobs_lock:
            job = self.jobs.get(job_id)

            if not job:
                return

            asset = job["assets"].get(
                asset_id
            )

            if not asset:
                return

            asset.update(changes)

        self.emit_change()

    def retry_failed(self, job_id):
        with self.jobs_lock:
            job = self.jobs.get(
                job_id
            )

            payload = self.payloads.get(
                job_id
            )

            history_item = next(
                (
                    item
                    for item in self.history
                    if str(
                        item.get(
                            "jobId"
                        )
                        or ""
                    )
                    == str(job_id)
                ),
                None,
            )

            if not job and history_item:
                job = self._history_job_stub(
                    history_item
                )

            if not job:
                raise ValueError(
                    "Unknown migration job."
                )

            failed_ids = set(
                str(asset_id)
                for asset_id in (
                    job.get(
                        "errors",
                        {}
                    )
                    or {}
                ).keys()
            )

            if not failed_ids:
                raise ValueError(
                    "This job has no failed assets."
                )

            animations = []

            # Best path: exact original manifest/payload.
            if payload:
                for entry in payload.get(
                    "assets",
                    payload.get(
                        "animations",
                        [],
                    ),
                ):
                    if not isinstance(
                        entry,
                        dict,
                    ):
                        continue

                    match = re.search(
                        r"\d+",
                        str(
                            entry.get(
                                "assetId"
                            )
                            or ""
                        ),
                    )

                    if (
                        match
                        and match.group(0)
                        in failed_ids
                    ):
                        animations.append(
                            dict(entry)
                        )

            # Second-best path: failed entries saved directly in web-history.
            if (
                not animations
                and history_item
                and isinstance(
                    history_item.get(
                        "retryAssets",
                        history_item.get(
                            "retryAnimations"
                        ),
                    ),
                    list,
                )
            ):
                source_retry_assets = history_item.get(
                    "retryAssets",
                    history_item.get(
                        "retryAnimations",
                        [],
                    ),
                )
                animations = [
                    dict(entry)
                    for entry in source_retry_assets
                    if isinstance(
                        entry,
                        dict,
                    )
                ]

            # Compatibility path for jobs completed by v3.13 or earlier:
            # those history entries did not persist the original manifest.
            # We can still retry the exact failed source IDs. The upload names
            # will be generic, but no successful source ID is reprocessed.
            if not animations:
                assets = (
                    job.get(
                        "assets",
                        {}
                    )
                    or {}
                )

                for asset_id in sorted(
                    failed_ids
                ):
                    asset = assets.get(
                        asset_id,
                        {}
                    )

                    animations.append(
                        {
                            "assetId":
                                asset_id,
                            "name":
                                str(
                                    asset.get(
                                        "name"
                                    )
                                    or (
                                        "Animation_"
                                        + asset_id
                                    )
                                ),
                        }
                    )

            asset_type = str(
                job.get(
                    "assetType"
                )
                or (
                    payload.get(
                        "assetType"
                    )
                    if payload
                    else (
                        history_item.get(
                            "assetType"
                        )
                        if history_item
                        else "Animation"
                    )
                )
                or "Animation"
            )

            retry_payload = {
                "tower":
                    str(
                        job.get(
                            "tower"
                        )
                        or "Migration"
                    )
                    + " (retry)",
                "rootPath":
                    (
                        payload.get(
                            "rootPath"
                        )
                        if payload
                        else (
                            history_item.get(
                                "rootPath"
                            )
                            if history_item
                            else None
                        )
                    ),
                "assetType": asset_type,
                "assets": animations,
                "animations": animations if asset_type == "Animation" else [],
                "forceReupload":
                    False,
            }

        return self.create_job(
            retry_payload
        )

    # --------------------------------------------------------
    # Migration pipeline
    # --------------------------------------------------------

    @staticmethod
    def _format_short_duration(
        seconds,
    ):
        if seconds is None:
            return "calculating"

        seconds = max(
            int(round(seconds)),
            0,
        )

        hours, seconds = divmod(
            seconds,
            3600,
        )

        minutes, seconds = divmod(
            seconds,
            60,
        )

        if hours:
            return (
                f"{hours}h "
                f"{minutes:02d}m"
            )

        if minutes:
            return (
                f"{minutes}m "
                f"{seconds:02d}s"
            )

        return f"{seconds}s"

    def portal_status(
        self,
        job_id,
        message,
    ):
        message = str(message)

        state = "working"
        lowered = message.lower()

        if "parallel single" in lowered or "single fallback" in lowered:
            state = "single"
        elif "without opening a browser" in lowered or "session valid" in lowered:
            state = "browserless"
        elif "opening edge" in lowered or "login" in lowered or "key access" in lowered:
            state = "auth"
        elif "ready" in lowered:
            state = "ready"

        self.set_rbxdl_status(
            state,
            message,
        )

        self.update_job(
            job_id,
            state="portal",
            message=message,
        )

    def portal_item(
        self,
        job_id,
        asset_id,
        status,
        metadata,
    ):
        changes = {
            "status": status,
            "source": str(
                metadata.get("source")
                or "RBXDL Portal native API"
            ),
        }

        if metadata.get("error"):
            changes["error"] = str(
                metadata.get("error")
            )
        elif status in {
            "queued",
            "fallback_queued",
            "fetching",
            "retrying",
            "ready",
            "prepared",
        }:
            # Clear stale bulk/Portal errors when the asset is being retried
            # through the single downloader or has already succeeded.
            changes["error"] = ""

        if metadata.get("filename"):
            changes["filename"] = str(
                metadata.get("filename")
            )

        if metadata.get("size") is not None:
            changes["size"] = int(
                metadata.get("size") or 0
            )

        if metadata.get("privacy"):
            changes["privacy"] = str(
                metadata.get("privacy")
            )

        if metadata.get("retries") is not None:
            changes["retries"] = int(
                metadata.get("retries") or 0
            )

        now = now_ms()

        with self.jobs_lock:
            job = self.jobs.get(
                job_id
            )

            if not job:
                return

            asset = job.get(
                "assets",
                {},
            ).get(
                str(asset_id)
            )

            if asset is not None:
                # Streaming uploads may complete between a source-file-ready
                # notification and the downloader's final `ready` event.
                # Never regress `uploading`/`done` to a download-only state.
                if asset.get("status") in {"uploading", "done"}:
                    changes.pop("status", None)
                    changes.pop("error", None)
                asset.update(
                    changes
                )

            portal_ids = set(
                str(value)
                for value in (
                    job.get(
                        "_portalAssetIds"
                    )
                    or []
                )
            )

            # Only count items belonging to the current portal phase.
            if str(asset_id) in portal_ids:
                done_ids = job.setdefault(
                    "_portalDoneIds",
                    {},
                )

                terminal = (
                    status
                    in {
                        "ready",
                        "skipped",
                        "failed",
                    }
                )

                if terminal:
                    done_ids[
                        str(asset_id)
                    ] = status
                else:
                    # A failed bulk item can later be retried by the single
                    # fallback. If it becomes fetching/retrying again, remove
                    # the temporary terminal mark so ETA stays honest.
                    done_ids.pop(
                        str(asset_id),
                        None,
                    )

                completed = len(
                    done_ids
                )

                total = max(
                    int(
                        job.get(
                            "portalTotal",
                            len(portal_ids),
                        )
                        or len(portal_ids)
                    ),
                    1,
                )

                started_at = int(
                    job.get(
                        "phaseStartedAt",
                        now,
                    )
                    or now
                )

                elapsed = max(
                    (now - started_at)
                    / 1000.0,
                    0.001,
                )

                rate_per_second = (
                    completed / elapsed
                    if completed > 0
                    else 0.0
                )

                remaining = max(
                    total - completed,
                    0,
                )

                eta_seconds = (
                    remaining
                    / rate_per_second
                    if rate_per_second > 0
                    else None
                )

                fraction = min(
                    max(
                        completed / total,
                        0.0,
                    ),
                    1.0,
                )

                job.update(
                    {
                        "state": "portal",
                        "phase": "portal",
                        "portalCompleted":
                            completed,
                        "portalTotal":
                            total,
                        "phaseCompleted":
                            completed,
                        "phaseTotal":
                            total,
                        "elapsedSeconds":
                            elapsed,
                        "ratePerMinute":
                            rate_per_second
                            * 60.0,
                        "etaSeconds":
                            eta_seconds,
                        # Streaming uploads may advance farther than the
                        # old portal-only fraction. Never regress progress.
                        "overallProgress": max(
                            float(job.get("overallProgress") or 0.0),
                            0.26 + 0.09 * fraction,
                        ),
                    }
                )

                if completed > 0:
                    if eta_seconds is None:
                        eta_text = (
                            "calculating"
                        )
                    else:
                        eta_text = (
                            self._format_short_duration(
                                eta_seconds
                            )
                        )

                    job[
                        "message"
                    ] = (
                        f"RBXDL fetch "
                        f"{completed}/{total} "
                        f"• "
                        f"{rate_per_second * 60.0:.1f}/min "
                        f"• ETA {eta_text}"
                    )

        self.emit_change()

    def run_job(
        self,
        job_id,
        tower,
        unique,
        config,
        asset_type="Animation",
    ):
        job_folder = (
            self._job_folder_for(
                tower,
                job_id,
            )
        )

        job_folder.mkdir(
            parents=True,
            exist_ok=True,
        )

        payload = self.payloads.get(
            job_id,
            {},
        )

        force_reupload = bool(
            payload.get(
                "forceReupload",
                False,
            )
        )

        mapping = {}
        errors = {}
        prepared_files = {}
        fetch_sources = {}
        official_errors = {}
        unresolved = []

        # ----------------------------------------------------
        # Resume previously uploaded assets from interrupted jobs.
        # ----------------------------------------------------

        if not force_reupload:
            resumed = self.get_resume_mapping(
                config,
                unique.keys(),
                asset_type,
            )

            for old_id, new_id in resumed.items():
                if old_id not in unique:
                    continue

                mapping[
                    old_id
                ] = new_id

                fetch_sources[
                    old_id
                ] = "Resume cache"

                self.update_asset(
                    job_id,
                    old_id,
                    status="done",
                    source="Resume cache",
                    newId=new_id,
                    error="",
                )

            if resumed:
                self.update_job(
                    job_id,
                    migrated=len(mapping),
                    completed=len(mapping),
                    mapping=dict(mapping),
                    fetchSources=dict(
                        fetch_sources
                    ),
                    message=(
                        f"Resumed {len(mapping)} previously uploaded "
                        f"{asset_type.lower()} asset(s)."
                    ),
                )

        # ----------------------------------------------------
        # Streaming migration uploads (v3.28)
        # ----------------------------------------------------
        # This pool begins as soon as source files are ready. Source producers
        # submit a completed file exactly once; all mapping writes/checkpoints
        # happen inside a lock to avoid races between upload workers.
        upload_pool = ThreadPoolExecutor(
            max_workers=max(1, ROBLOX_UPLOAD_WORKERS)
        )
        upload_lock = threading.RLock()
        upload_futures = {}
        upload_finished = 0
        upload_failed = 0
        upload_accepted = 0
        upload_confirmed = 0
        download_failed = 0
        upload_started_monotonic = time.monotonic()
        upload_started_ms = now_ms()
        resumed_count = len(mapping)
        total_assets = len(unique)

        def upload_one(
            old_id,
            entry,
            file_path,
        ):
            nonlocal upload_accepted
            name = (
                core.suggested_asset_name(
                    entry,
                    asset_type,
                )
            )

            if (
                file_path.stat().st_size
                > core.MAX_FILE_BYTES
            ):
                raise RuntimeError(
                    f"Prepared {asset_type.lower()} asset exceeds Roblox's "
                    "20 MB upload limit."
                )

            self.update_asset(
                job_id,
                old_id,
                status="uploading",
            )

            def retryable_error(exc):
                lowered = str(exc).lower()

                return (
                    "429" in lowered
                    or "too many" in lowered
                    or "rate limit" in lowered
                    or "500" in lowered
                    or "502" in lowered
                    or "503" in lowered
                    or "504" in lowered
                    or "timed out" in lowered
                    or "network error" in lowered
                )

            # Retry creation only until Roblox returns an operation path.
            # After that point we NEVER create the asset again on a polling
            # failure; we keep polling the same operation so retries cannot
            # accidentally create duplicate animation assets.
            operation = None
            create_error = None

            for attempt in range(
                1,
                ROBLOX_UPLOAD_RETRIES + 1,
            ):
                try:
                    operation = (
                        core.create_asset(
                            config["api_key"],
                            file_path,
                            name,
                            config[
                                "creator_type"
                            ],
                            config[
                                "creator_id"
                            ],
                            config[
                                "description"
                            ],
                            asset_type,
                        )
                    )
                    break

                except Exception as exc:
                    create_error = exc

                    if (
                        not retryable_error(exc)
                        or attempt
                        >= ROBLOX_UPLOAD_RETRIES
                    ):
                        break

                    time.sleep(
                        min(
                            2.0 ** attempt,
                            8.0,
                        )
                    )

            if operation is None:
                raise RuntimeError(
                    str(create_error)
                    if create_error is not None
                    else "Roblox did not start the upload."
                )

            # Create Asset accepted the request; now Roblox is processing it.
            # This is NOT a claim that Studio can load the animation yet.
            with upload_lock:
                upload_accepted += 1
                self.update_asset(job_id, old_id, status="confirming")
                refresh_stream_progress("Roblox processing uploaded assets")

            poll_error = None

            for attempt in range(
                1,
                ROBLOX_UPLOAD_RETRIES + 1,
            ):
                try:
                    new_id = (
                        core.wait_for_operation(
                            config["api_key"],
                            operation,
                        )
                    )

                    return {
                        "oldId": old_id,
                        "newId": new_id,
                        "name": name,
                        "attempts": attempt,
                    }

                except Exception as exc:
                    poll_error = exc

                    if (
                        not retryable_error(exc)
                        or attempt
                        >= ROBLOX_UPLOAD_RETRIES
                    ):
                        break

                    time.sleep(
                        min(
                            2.0 ** attempt,
                            8.0,
                        )
                    )

            raise RuntimeError(
                str(poll_error)
                if poll_error is not None
                else "Roblox upload operation did not complete."
            )

        def refresh_stream_progress(message=None, finished=False):
            # Caller holds upload_lock; snapshot is safe for async callbacks.
            fetched = len(prepared_files) + resumed_count + download_failed
            queued = len(upload_futures)
            uploaded = upload_finished
            accounted = len(mapping) + len(errors)
            elapsed = max(time.monotonic() - upload_started_monotonic, .001)
            upload_speed = (uploaded / elapsed * 60.0) if uploaded else 0.0
            data = dict(
                downloadCompleted=min(fetched, total_assets),
                downloadTotal=total_assets,
                uploadCompleted=uploaded,
                uploadTotal=queued,
                uploadAccepted=upload_accepted,
                uploadConfirmed=upload_confirmed,
                uploadWorkers=min(ROBLOX_UPLOAD_WORKERS, max(1, queued)),
                migrated=len(mapping),
                failed=len(errors),
                completed=accounted,
                mapping=dict(mapping),
                errors=dict(errors),
                fetchSources=dict(fetch_sources),
                overallProgress=(
                    0.12 + 0.44 * min(1, fetched/max(1,total_assets))
                    + 0.44 * min(1, accounted/max(1,total_assets))
                ) if not finished else 1.0,
            )
            if message:
                data["message"] = (
                    f"{message} • {min(fetched,total_assets)}/{total_assets} fetched "
                    f"• {len(mapping)} migrated • {len(errors)} failed "
                    f"• {uploaded}/{queued} upload tasks complete"
                )
            if finished:
                data.update(phase="uploading", phaseCompleted=uploaded,
                            phaseTotal=queued, etaSeconds=0.0)
            self.update_job(job_id, **data)

        def finish_one_upload(old_id, future):
            nonlocal upload_finished, upload_failed, upload_confirmed
            with upload_lock:
                try:
                    result = future.result()
                    new_id = str(result["newId"])
                    mapping[old_id] = new_id
                    errors.pop(old_id, None)
                    upload_confirmed += 1
                    # Save immediately; the Studio plugin can apply mappings
                    # from an interrupted job without uploading these twice.
                    if not force_reupload:
                        try:
                            self.save_resume_mapping(config, old_id, new_id, asset_type)
                        except Exception as cache_exc:
                            # A cache I/O failure must never turn a real,
                            # successful remote upload into a failed asset.
                            print(f"TM resume cache warning for {old_id}: {cache_exc}")
                    self.update_asset(
                        job_id, old_id, status="done",
                        source=fetch_sources.get(old_id, ""),
                        newId=new_id, error="",
                    )
                except Exception as exc:
                    upload_failed += 1
                    error = (
                        "Upload failed after fetching via "
                        f"{fetch_sources.get(old_id, 'unknown')}: {exc}"
                    )
                    errors[old_id] = error
                    self.update_asset(job_id, old_id, status="failed", error=error)
                finally:
                    upload_finished += 1
                    refresh_stream_progress("Downloading + uploading in parallel")

        def queue_ready_file(old_id, file_path, source):
            old_id = str(old_id)
            file_path = Path(file_path)
            with upload_lock:
                if old_id not in unique or old_id in mapping or old_id in upload_futures:
                    return
                if not file_path.is_file() or file_path.stat().st_size == 0:
                    raise RuntimeError(f"Asset {old_id} was reported ready but the file is missing/empty: {file_path}")
                # A native Roblox mesh can arrive with the generic `.bin`
                # suffix. Normalize BEFORE early upload, not after the whole
                # RBXDL batch returns (which was the v3.27 behavior).
                if asset_type == "Mesh" and file_path.suffix.lower() == ".bin":
                    detected = core._guess_asset_extension(
                        file_path.read_bytes()[:64], asset_type="Mesh"
                    )
                    if detected == ".mesh":
                        normalized = file_path.with_suffix(".mesh")
                        normalized.write_bytes(file_path.read_bytes())
                        file_path = normalized
                prepared_files[old_id] = file_path
                fetch_sources[old_id] = str(source)
                # Schedule before reporting ready so dashboard always reflects
                # a queued upload even when browser workers are busy.
                future = upload_pool.submit(
                    upload_one, old_id, unique[old_id], file_path
                )
                upload_futures[old_id] = future
                future.add_done_callback(
                    lambda completed_future, aid=old_id:
                    finish_one_upload(aid, completed_future)
                )
                refresh_stream_progress("Downloading + uploading in parallel")

        # Initial resumed mapping contributes to total without requiring new
        # downloads. Pending sources and upload workers can now run together.
        with upload_lock:
            refresh_stream_progress("Starting streaming migration")

        # ----------------------------------------------------
        # Studio-provided sources.
        # ----------------------------------------------------

        if asset_type == "Animation":
            with self.source_cache_lock:
                studio_sources = {
                    old_id:
                        self.source_cache.get(
                            old_id
                        )
                    for old_id in unique.keys()
                    if self.source_cache.get(
                        old_id
                    )
                }
        else:
            studio_sources = {}

        for old_id, studio_rbxmx in studio_sources.items():
            if old_id in mapping:
                continue

            entry = unique[
                old_id
            ]

            name = (
                core.suggested_asset_name(
                    entry,
                    asset_type,
                )
            )

            file_path = (
                job_folder
                / (
                    f"{core.safe_filename(name)}"
                    f"_{old_id}.rbxmx"
                )
            )

            file_path.write_text(
                studio_rbxmx,
                encoding="utf-8",
            )

            prepared_files[
                old_id
            ] = file_path

            fetch_sources[
                old_id
            ] = (
                "Roblox Studio "
                "AnimationClipProvider"
            )

            self.update_asset(
                job_id,
                old_id,
                status="prepared",
                source=fetch_sources[
                    old_id
                ],
            )
            queue_ready_file(old_id, file_path, fetch_sources[old_id])

        official_candidates = [
            old_id
            for old_id in unique.keys()
            if (
                old_id
                not in mapping
                and old_id
                not in prepared_files
            )
        ]

        # ----------------------------------------------------
        # Fast public Asset Delivery probe.
        #
        # Large folders no longer do hundreds of serial 403s.
        # We sample at most 20 IDs in parallel. If >=75% are
        # denied/unavailable, the remaining IDs go straight to
        # RBXDL. Otherwise, we check the rest in parallel.
        # ----------------------------------------------------

        def is_access_denied(error_text):
            lowered = str(
                error_text
            ).lower()

            return (
                "403" in lowered
                or "not authorized" in lowered
                or "unauthorized" in lowered
                or "no downloadable location"
                    in lowered
            )

        def official_fetch(
            asset_id,
        ):
            return (
                asset_id,
                core.fetch_asset_roblox(
                    asset_id,
                    asset_type,
                ),
            )

        def handle_official_result(
            asset_id,
            result,
            error,
        ):
            entry = unique[
                asset_id
            ]

            name = (
                core.suggested_asset_name(
                    entry,
                    asset_type,
                )
            )

            if error is not None:
                official_errors[
                    asset_id
                ] = str(error)

                unresolved.append(
                    asset_id
                )

                self.update_asset(
                    job_id,
                    asset_id,
                    status="portal",
                    source="RBXDL Portal",
                )

                return

            (
                data,
                extension,
                source,
            ) = result

            file_path = (
                job_folder
                / (
                    f"{core.safe_filename(name)}"
                    f"_{asset_id}"
                    f"{extension}"
                )
            )

            file_path.write_bytes(
                data
            )

            prepared_files[
                asset_id
            ] = file_path

            fetch_sources[
                asset_id
            ] = source

            self.update_asset(
                job_id,
                asset_id,
                status="prepared",
                source=source,
            )
            queue_ready_file(asset_id, file_path, source)

        def run_official_parallel(
            ids,
            label,
        ):
            if not ids:
                return []

            denied = []
            finished = 0

            workers = min(
                OFFICIAL_WORKERS,
                len(ids),
            )

            with ThreadPoolExecutor(
                max_workers=workers
            ) as executor:
                future_map = {
                    executor.submit(
                        official_fetch,
                        asset_id,
                    ):
                        asset_id
                    for asset_id in ids
                }

                for future in as_completed(
                    future_map
                ):
                    asset_id = future_map[
                        future
                    ]

                    finished += 1

                    self.update_job(
                        job_id,
                        state="fetching",
                        phase="official-probe",
                        message=(
                            f"{label} "
                            f"{finished}/"
                            f"{len(ids)}"
                        ),
                    )

                    self.update_asset(
                        job_id,
                        asset_id,
                        status="fetching",
                    )

                    try:
                        (
                            _asset_id,
                            result,
                        ) = future.result()

                        handle_official_result(
                            asset_id,
                            result,
                            None,
                        )

                    except Exception as exc:
                        if is_access_denied(
                            exc
                        ):
                            denied.append(
                                asset_id
                            )

                        handle_official_result(
                            asset_id,
                            None,
                            exc,
                        )

            return denied

        if official_candidates:
            sample_size = min(
                OFFICIAL_SAMPLE_SIZE,
                len(
                    official_candidates
                ),
            )

            sample = (
                official_candidates[
                    :sample_size
                ]
            )

            self.update_job(
                job_id,
                state="fetching",
                phase="official-probe",
                message=(
                    f"Fast Roblox access probe: "
                    f"{sample_size} sample asset(s)..."
                ),
                overallProgress=0.20,
            )

            sample_denied = (
                run_official_parallel(
                    sample,
                    "Roblox access probe",
                )
            )

            denied_ratio = (
                len(
                    sample_denied
                )
                / max(
                    len(sample),
                    1,
                )
            )

            remaining = (
                official_candidates[
                    sample_size:
                ]
            )

            if (
                remaining
                and denied_ratio
                >= OFFICIAL_SKIP_THRESHOLD
            ):
                self.update_job(
                    job_id,
                    state="portal",
                    phase="portal",
                    message=(
                        f"Roblox denied "
                        f"{len(sample_denied)}/"
                        f"{len(sample)} sampled assets; "
                        f"skipping {len(remaining)} pointless "
                        f"Asset Delivery checks and going straight "
                        f"to RBXDL."
                    ),
                    overallProgress=0.26,
                )

                for asset_id in remaining:
                    official_errors[
                        asset_id
                    ] = (
                        "Official Asset Delivery skipped after "
                        "sample showed access was unavailable."
                    )

                    unresolved.append(
                        asset_id
                    )

                    self.update_asset(
                        job_id,
                        asset_id,
                        status="portal",
                        source="RBXDL Portal",
                    )

            elif remaining:
                run_official_parallel(
                    remaining,
                    "Parallel Roblox check",
                )

        # Remove duplicates while preserving order.
        unresolved = list(
            dict.fromkeys(
                unresolved
            )
        )

        rolhub_key = config.get("rolhub_key") or os.environ.get("ROLHUB_KEY", "")

        print(f"[Rolhub] unresolved={len(unresolved)} key_present={bool(rolhub_key)}", flush=True)

        if unresolved and not rolhub_key:
            self.update_job(
                job_id,
                state="portal",
                message="Rolhub API skipped: no Rolhub key saved in Settings.",
            )

        if unresolved and rolhub_key:
            rolhub = None

            try:
                rolhub = core.RolhubApiClient(
                    rolhub_key,
                    status_callback=lambda message:
                        self.portal_status(job_id, message),
                    item_callback=lambda asset_id, status, metadata:
                        self.portal_item(job_id, asset_id, status, metadata),
                    file_ready_callback=queue_ready_file,
                )

                self.update_job(
                    job_id,
                    state="portal",
                    phase="portal",
                    message=f"Rolhub API: fetching {len(unresolved)} asset(s)...",
                )

                rolhub_files, rolhub_errors = rolhub.download_batch(
                    unresolved,
                    job_folder / "rolhub-api",
                )

                print(f"[Rolhub] got={len(rolhub_files)} errors={len(rolhub_errors)}", flush=True)
                for failed_id, failed_detail in list(rolhub_errors.items())[:5]:
                    print(f"[Rolhub] {failed_id}: {failed_detail}", flush=True)

                for old_id, detail in rolhub_errors.items():
                    official_errors[old_id] = (
                        official_errors.get(old_id, "") + " | " + detail
                    ).strip(" |")

                unresolved = [
                    asset_id
                    for asset_id in unresolved
                    if asset_id not in rolhub_files
                ]

            except Exception as exc:
                print(f"[Rolhub] stage crashed: {exc}", flush=True)
                for old_id in unresolved:
                    official_errors[old_id] = (
                        official_errors.get(old_id, "")
                        + f" | Rolhub API failed: {exc}"
                    )

            finally:
                if rolhub is not None:
                    rolhub.close()

        # ----------------------------------------------------
        # RBXDL portal.
        # ----------------------------------------------------

        if unresolved:
            portal = None

            try:
                portal = (
                    core.RBXDLPortalDownloader(
                        status_callback=
                            lambda message:
                            self.portal_status(
                                job_id,
                                message,
                            ),
                        item_callback=
                            lambda asset_id, status, metadata:
                            self.portal_item(
                                job_id,
                                asset_id,
                                status,
                                metadata,
                            ),
                        file_ready_callback=queue_ready_file,
                    )
                )

                portal_started_at = now_ms()

                self.update_job(
                    job_id,
                    state="portal",
                    phase="portal",
                    phaseStartedAt=portal_started_at,
                    phaseCompleted=0,
                    phaseTotal=len(unresolved),
                    portalCompleted=0,
                    portalTotal=len(unresolved),
                    etaSeconds=None,
                    ratePerMinute=0.0,
                    elapsedSeconds=0.0,
                    overallProgress=0.26,
                    _portalAssetIds=list(unresolved),
                    _portalDoneIds={},
                    message=(
                        f"Sending "
                        f"{len(unresolved)} "
                        f"asset(s) to RBXDL "
                        f"(Creator Portal bulk HTTP; Single browser only if enabled)..."
                    ),
                )

                (
                    portal_files,
                    portal_errors,
                ) = (
                    portal.download_batch(
                        unresolved,
                        job_folder
                        / "rbxdl-portal",
                        retry_rate_limits_forever=False,
                        allow_interactive_auth=os.environ.get("TM_RBXDL_INTERACTIVE_AUTH", "").lower() in {"1", "true", "yes"},
                        allow_single_fallback=os.environ.get("TM_RBXDL_DISABLE_BROWSER_FALLBACK", "").lower() not in {"1", "true", "yes"},
                    )
                )

                for (
                    old_id,
                    portal_file,
                ) in portal_files.items():
                    if asset_type == "Mesh":
                        portal_file = Path(portal_file)
                        if portal_file.suffix.lower() == ".bin":
                            detected = core._guess_asset_extension(
                                portal_file.read_bytes()[:64],
                                asset_type="Mesh",
                            )
                            if detected == ".mesh":
                                mesh_file = portal_file.with_suffix(".mesh")
                                if mesh_file != portal_file:
                                    mesh_file.write_bytes(portal_file.read_bytes())
                                portal_file = mesh_file

                    prepared_files[
                        old_id
                    ] = portal_file

                    current_job = self.get_job(
                        job_id
                    ) or {}

                    current_asset = (
                        current_job
                        .get(
                            "assets",
                            {},
                        )
                        .get(
                            old_id,
                            {},
                        )
                    )

                    resolved_source = str(
                        current_asset.get(
                            "source"
                        )
                        or (
                            "RBXDL Portal "
                            "native API"
                        )
                    )

                    fetch_sources[
                        old_id
                    ] = resolved_source

                    # If file-ready notifications were not emitted by a
                    # particular fetch provider, schedule this path now.
                    queue_ready_file(old_id, portal_file, resolved_source)

                for old_id in unresolved:
                    if (
                        old_id in prepared_files
                        or old_id in upload_futures
                        or old_id in mapping
                    ):
                        continue

                    portal_error = (
                        portal_errors.get(
                            old_id
                        )
                        or (
                            "RBXDL Portal "
                            "did not return "
                            "this asset."
                        )
                    )

                    error = (
                        "Official Roblox "
                        "fetch failed/skipped: "
                        + official_errors.get(
                            old_id,
                            "unknown error",
                        )
                        + " | RBXDL: "
                        + portal_error
                    )

                    errors[
                        old_id
                    ] = error

                    self.update_asset(
                        job_id,
                        old_id,
                        status="failed",
                        error=error,
                    )

            except Exception as exc:
                portal_failure = str(
                    exc
                )

                for old_id in unresolved:
                    if (
                        old_id in prepared_files
                        or old_id in upload_futures
                        or old_id in mapping
                    ):
                        continue

                    error = (
                        "Official Roblox "
                        "fetch failed/skipped: "
                        + official_errors.get(
                            old_id,
                            "unknown error",
                        )
                        + (
                            " | RBXDL Portal "
                            "bulk job failed: "
                        )
                        + portal_failure
                    )

                    errors[
                        old_id
                    ] = error

                    self.update_asset(
                        job_id,
                        old_id,
                        status="failed",
                        error=error,
                    )

            finally:
                if portal is not None:
                    try:
                        portal.close()
                    except Exception:
                        pass

        with upload_lock:
            download_failed = sum(
                1 for asset_id in unique
                if asset_id in errors and asset_id not in upload_futures
            )
            refresh_stream_progress("Finished fetching; uploading remaining assets")

        # Download producers are finished. Drain the upload futures before we
        # write final mapping/diagnostics. Successful uploads were committed and
        # checkpointed individually while downloads were still underway.
        self.update_job(
            job_id,
            state="uploading",
            phase="uploading",
            message="All download attempts finished; finishing pending uploads...",
        )
        upload_pool.shutdown(wait=True)

        with upload_lock:
            for old_id in unique:
                if old_id in mapping or old_id in errors:
                    continue
                error = "No downloadable asset was returned by any source."
                errors[old_id] = error
                self.update_asset(job_id, old_id, status="failed", error=error)
            refresh_stream_progress("Upload stage finished", finished=True)

        with self.source_cache_lock:
            for old_id in unique.keys():
                self.source_cache.pop(
                    old_id,
                    None,
                )

        if mapping and errors:
            state = "partial"
            message = (
                f"Finished with "
                f"{len(mapping)} migrated "
                f"and {len(errors)} failed."
            )

        elif mapping:
            state = "done"
            message = (
                f"Successfully migrated "
                f"{len(mapping)} unique "
                f"{asset_type.lower()} asset(s)."
            )

        else:
            state = "failed"

            first_error = next(
                iter(
                    errors.values()
                ),
                (
                    "Unknown fetch/upload "
                    "error."
                ),
            )

            message = (
                f"No {asset_type.lower()} assets could be "
                "migrated. "
                + first_error
            )

        diagnostics = {
            "tower": tower,
            "assetType": asset_type,
            "mapping": mapping,
            "errors": errors,
            "fetchSources":
                fetch_sources,
            "resumable": True,
            "forceReupload":
                force_reupload,
        }

        try:
            (
                job_folder
                / "migration-diagnostics.json"
            ).write_text(
                json.dumps(
                    diagnostics,
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

        self.update_job(
            job_id,
            state=state,
            phase="complete",
            mapping=dict(mapping),
            errors=dict(errors),
            fetchSources=dict(
                fetch_sources
            ),
            migrated=len(mapping),
            failed=len(errors),
            completed=len(unique),
            overallProgress=1.0,
            etaSeconds=0.0,
            ratePerMinute=0.0,
            elapsedSeconds=(
                max(
                    (
                        now_ms()
                        - int(
                            (
                                self.get_job(job_id)
                                or {}
                            ).get(
                                "createdAt",
                                now_ms(),
                            )
                        )
                    )
                    / 1000.0,
                    0.0,
                )
            ),
            message=message,
            finishedAt=now_ms(),
        )

        final_job = self.get_job(
            job_id
        )

        if final_job:
            self.add_to_history(
                final_job
            )

    # --------------------------------------------------------
    # Standalone ZIP downloader
    # --------------------------------------------------------

    def scheduler_snapshot(self):
        gate = core._RBXDL_SINGLE_RATE_GATE.snapshot()
        with self.download_jobs_lock:
            pending = sorted(
                (j for j in self.download_jobs.values() if j.get("state") == "queued"),
                key=lambda j: (-DOWNLOAD_PRIORITY.get(j.get("priority"), 1), j.get("createdAt", 0)),
            )
            active = next((j.get("jobId") for j in self.download_jobs.values() if j.get("state") == "downloading"), None)
        return {"gate": gate, "queued": len(pending), "activeJobId": active,
                "nextJob": pending[0].get("name") if pending else None,
                "priorityOrder": [j.get("jobId") for j in pending[:20]]}

    def _download_job_directory(self, job):
        return DOWNLOADS_DIR / core.safe_filename(job.get("name") or "AssetDownload") / str(job["jobId"])

    def _persist_download_job(self, job):
        """Atomic JSON snapshot; stores no Roblox API keys or RBXDL cookies."""
        directory = self._download_job_directory(job)
        directory.mkdir(parents=True, exist_ok=True)
        dest = directory / "job-state.json"
        temp = directory / "job-state.json.tmp"
        temp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, dest)

    def _restore_download_jobs(self):
        # On restart the work queue is reconstructed from exact source ID lists.
        # In-flight files already saved under _rbxdl/ will be reused by the job.
        for path in DOWNLOADS_DIR.glob("*/*/job-state.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(job, dict) or not re.fullmatch(r"[0-9a-f-]{36}", str(job.get("jobId", ""))):
                    continue
                if not isinstance(job.get("_ids"), list) or not isinstance(job.get("assets"), dict):
                    continue
                if job.get("priority") not in DOWNLOAD_PRIORITY:
                    job["priority"] = "Normal"
                if job.get("state") not in ("done", "partial", "failed", "paused"):
                    job["state"] = "queued"
                    job["message"] = "Recovered after helper restart; unfinished assets will resume."
                    for asset in job["assets"].values():
                        if asset.get("status") not in ("done", "cached"):
                            asset["status"] = "queued"
                            asset["error"] = ""
                self._recount_download_job(job)
                self.download_jobs[job["jobId"]] = job
            except (OSError, ValueError, TypeError, KeyError):
                continue

    def _download_queue_loop(self):
        """One active ZIP job at a time; queued jobs execute by priority then FIFO."""
        while True:
            with self.download_queue_condition:
                waiting = [j for j in self.download_jobs.values() if j.get("state") == "queued"]
                if not waiting:
                    self.download_queue_condition.wait(timeout=2.0)
                    continue
                job = min(waiting, key=lambda j: (-DOWNLOAD_PRIORITY.get(j.get("priority"), 1), j.get("createdAt", 0)))
                job_id = job["jobId"]
                job["state"] = "downloading"
                job["message"] = "Selected by intelligent scheduler; checking existing files..."
                self._persist_download_job(job)
            self.emit_change()
            try:
                self._run_download_job(job_id)
            except Exception as exc:
                self._update_download_job(job_id, state="failed", message=f"Scheduler error: {exc}")

    def set_download_priority(self, job_id, priority):
        if priority not in DOWNLOAD_PRIORITY:
            raise ValueError("Priority must be Low, Normal, or High.")
        with self.download_queue_condition:
            job = self.download_jobs.get(str(job_id))
            if job is None:
                raise ValueError("Unknown downloader job.")
            if job.get("state") not in ("queued", "paused"):
                raise ValueError("Priority can only be changed before a job starts.")
            job["priority"] = priority
            self._persist_download_job(job)
            self.download_queue_condition.notify_all()
        self.emit_change()
        return self.get_download_job(job_id)

    def pause_download_job(self, job_id, pause):
        with self.download_queue_condition:
            job = self.download_jobs.get(str(job_id))
            if job is None:
                raise ValueError("Unknown downloader job.")
            expected = "queued" if pause else "paused"
            if job.get("state") != expected:
                raise ValueError("Only queued jobs can be paused; only paused jobs can resume.")
            job["state"] = "paused" if pause else "queued"
            job["message"] = "Paused before starting." if pause else "Resumed and queued."
            self._persist_download_job(job)
            self.download_queue_condition.notify_all()
        self.emit_change()
        return self.get_download_job(job_id)

    def list_download_jobs(self):
        with self.download_jobs_lock:
            values = [json_copy(v) for v in self.download_jobs.values()]
        values.sort(key=lambda v: int(v.get("createdAt") or 0), reverse=True)
        return values[:50]

    def get_download_job(self, job_id):
        with self.download_jobs_lock:
            value = self.download_jobs.get(str(job_id))
            return json_copy(value) if value is not None else None

    def _update_download_job(self, job_id, **changes):
        with self.download_jobs_lock:
            job = self.download_jobs.get(str(job_id))
            if job is None:
                return
            job.update(changes)
            self._persist_download_job(job)
        self.emit_change()

    def _recount_download_job(self, job):
        assets = list((job.get("assets") or {}).values())
        job["completed"] = sum(1 for a in assets if a.get("status") in {"done","cached","failed"})
        job["downloaded"] = sum(1 for a in assets if a.get("status") in {"done","cached"})
        job["failed"] = sum(1 for a in assets if a.get("status") == "failed")
        started = int(job.get("startedAt") or job.get("createdAt") or now_ms())
        elapsed = max((now_ms()-started)/1000.0, 0.0)
        job["elapsedSeconds"] = elapsed
        completed = int(job.get("completed") or 0)
        total = int(job.get("total") or 0)
        if completed > 0:
            rate = completed / max(elapsed, 0.001)
            job["ratePerMinute"] = rate * 60.0
            job["etaSeconds"] = max(total-completed,0) / max(rate, 0.000001)
        if completed >= total:
            job["etaSeconds"] = 0

    def _update_download_asset(self, job_id, asset_id, **changes):
        asset_id = str(asset_id)
        with self.download_jobs_lock:
            job = self.download_jobs.get(str(job_id))
            if job is None:
                return
            asset = (job.setdefault("assets", {})).setdefault(asset_id, {
                "assetId": asset_id,
                "status": "queued",
                "source": "",
                "filename": "",
                "error": "",
            })
            asset.update(changes)
            self._recount_download_job(job)
            self._persist_download_job(job)
        self.emit_change()

    def create_download_job(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Invalid downloader payload.")

        source = str(payload.get("source") or "")
        ids = parse_downloader_asset_ids(source)
        if not ids:
            raise ValueError("No Roblox asset IDs were found.")
        if len(ids) > MAX_DOWNLOADER_IDS:
            raise ValueError(f"Too many asset IDs. Maximum is {MAX_DOWNLOADER_IDS}.")

        name = str(payload.get("name") or "").strip() or infer_downloader_name(source)
        name = core.safe_filename(name) or "AssetDownload"
        job_id = str(uuid.uuid4())
        created_at = now_ms()
        priority = str(payload.get("priority") or "Normal")
        if priority not in DOWNLOAD_PRIORITY:
            raise ValueError("Priority must be Low, Normal, or High.")

        job = {
            "jobId": job_id,
            "name": name,
            "createdAt": created_at,
            "startedAt": created_at,
            "state": "queued",
            "priority": priority,
            "message": f"Queued {len(ids)} asset(s) for API-first download ({priority}).",
            "allowBrowserFallback": payload.get("allowBrowserFallback") is True,
            "total": len(ids),
            "completed": 0,
            "downloaded": 0,
            "failed": 0,
            "ratePerMinute": 0.0,
            "etaSeconds": None,
            "elapsedSeconds": 0.0,
            "zipReady": False,
            "zipName": "",
            "assets": {asset_id: {
                "assetId": asset_id,
                "status": "queued",
                "source": "",
                "filename": "",
                "error": "",
            } for asset_id in ids},
            "_source": source,
            "_ids": ids,
        }

        with self.download_queue_condition:
            self.download_jobs[job_id] = job
            self._persist_download_job(job)
            self.download_queue_condition.notify_all()
        self.emit_change()
        return job_id

    def retry_download_job(self, job_id, overrides=None):
        """Retry a completed/partial batch without redownloading cached files.

        Create a NEW job rather than mutating history. Reuse all original IDs:
        previously successful files are found by _run_download_job's cache
        before it tries any network route. This also recreates a full ZIP.
        """
        job = self.get_download_job(job_id)
        if not job:
            raise ValueError("Unknown downloader job.")
        if job.get("state") not in {"partial", "failed", "done"}:
            raise ValueError("Wait for the current download job to finish.")
        if not int(job.get("failed") or 0):
            raise ValueError("This job has no failed assets to retry.")
        ids = [str(asset_id) for asset_id in job.get("_ids") or []]
        if not ids:
            raise ValueError("The original download ID list is unavailable.")
        return self.create_download_job({
            "name": str(job.get("name") or "AssetDownload") + " Retry",
            "priority": str(job.get("priority") or "Normal"),
            "source": "\n".join(ids),
            "allowBrowserFallback": (
                overrides["allowBrowserFallback"] is True
                if isinstance(overrides, dict) and "allowBrowserFallback" in overrides
                else job.get("allowBrowserFallback") is True
            ),
        })

    def _cache_file_for_asset(self, asset_id):
        for path in sorted(DOWNLOAD_CACHE_DIR.glob(f"{asset_id}.*")):
            if path.is_file() and not path.name.endswith(".part"):
                return path
        return None

    def _run_download_job(self, job_id):
        job = self.get_download_job(job_id)
        if not job:
            return

        ids = list(job.get("_ids") or [])
        name = str(job.get("name") or "AssetDownload")
        source = str(job.get("_source") or "")
        job_dir = DOWNLOADS_DIR / core.safe_filename(name) / job_id
        asset_dir = job_dir / "assets"
        raw_dir = job_dir / "_rbxdl"
        asset_dir.mkdir(parents=True, exist_ok=True)
        raw_dir.mkdir(parents=True, exist_ok=True)

        self._update_download_job(job_id, state="downloading", startedAt=now_ms(), message=f"Checking cache for {len(ids)} asset(s)...")

        files = {}
        unresolved = []
        for asset_id in ids:
            cached = self._cache_file_for_asset(asset_id)
            if cached is None:
                cached = next((f for f in sorted(raw_dir.glob(f"{asset_id}.*"))
                               if f.is_file() and f.suffix.lower() in {".mp3", ".ogg", ".wav", ".flac", ".mesh", ".rbxm", ".rbxmx", ".bin"} and f.stat().st_size > 0), None)
            if cached is None:
                unresolved.append(asset_id)
                continue
            target = asset_dir / cached.name
            shutil.copy2(cached, target)
            files[asset_id] = target
            self._update_download_asset(job_id, asset_id, status="cached", source="Downloader cache", filename=target.name, error="")

        if unresolved:
            self._update_download_job(job_id, state="downloading", message=f"API-first: {len(unresolved)} uncached asset(s) remaining.")

            def status_callback(message):
                self._update_download_job(job_id, state="downloading", message=str(message))

            def item_callback(asset_id, status, metadata):
                metadata = dict(metadata or {})
                changes = {
                    "status": status,
                    "source": metadata.get("source") or "RBXDL Single Downloader",
                }
                if metadata.get("filename"):
                    changes["filename"] = str(metadata.get("filename"))
                if metadata.get("error"):
                    changes["error"] = str(metadata.get("error"))
                elif status in {
                    "queued",
                    "fallback_queued",
                    "fetching",
                    "rate_limited",
                    "ready",
                    "done",
                }:
                    changes["error"] = ""
                self._update_download_asset(job_id, asset_id, **changes)

            downloader = None
            try:
                downloader = core.RBXDLPortalDownloader(
                    status_callback=status_callback,
                    item_callback=item_callback,
                )
                downloaded, errors = downloader.download_single_batch(
                    unresolved,
                    raw_dir,
                    "Standalone ZIP downloader, official + Portal HTTP first.",
                    priority=DOWNLOAD_PRIORITY.get(job.get("priority"), 1),
                    allow_browser_fallback=job.get("allowBrowserFallback") is True,
                )

                for asset_id, source_path in downloaded.items():
                    source_path = Path(source_path)
                    ext = source_path.suffix or ".bin"
                    target = asset_dir / f"{asset_id}{ext}"
                    if source_path.resolve() != target.resolve():
                        shutil.copy2(source_path, target)
                    files[asset_id] = target
                    try:
                        shutil.copy2(target, DOWNLOAD_CACHE_DIR / target.name)
                    except Exception:
                        pass
                    latest_asset = ((self.get_download_job(job_id) or {}).get("assets") or {}).get(asset_id) or {}
                    resolved_source = str(latest_asset.get("source") or "RBXDL Single Downloader")
                    self._update_download_asset(job_id, asset_id, status="done", source=resolved_source, filename=target.name, error="")

                for asset_id, error in errors.items():
                    self._update_download_asset(job_id, asset_id, status="failed", source="RBXDL Single Downloader", error=str(error))

            except Exception as exc:
                message = str(exc)
                latest = self.get_download_job(job_id) or {}
                for asset_id in unresolved:
                    status = ((latest.get("assets") or {}).get(asset_id) or {}).get("status")
                    if status not in {"done","cached"}:
                        self._update_download_asset(job_id, asset_id, status="failed", source="RBXDL Single Downloader", error=message)
            finally:
                if downloader is not None:
                    try:
                        downloader.close()
                    except Exception:
                        pass

        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "source.txt").write_text(source, encoding="utf-8")
        latest = self.get_download_job(job_id) or {}
        manifest = {
            "name": name,
            "jobId": job_id,
            "createdAt": latest.get("createdAt"),
            "total": latest.get("total", len(ids)),
            "downloaded": latest.get("downloaded", len(files)),
            "failed": latest.get("failed", 0),
            "assets": [((latest.get("assets") or {}).get(asset_id) or {"assetId":asset_id}) for asset_id in ids],
        }
        (job_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        with (job_dir / "manifest.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["assetId","status","source","filename","error"])
            writer.writeheader()
            for item in manifest["assets"]:
                writer.writerow({k:item.get(k,"") for k in writer.fieldnames})

        zip_name = f"{core.safe_filename(name) or 'AssetDownload'}.zip"
        zip_path = job_dir / zip_name
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(job_dir / "source.txt", "source.txt")
            archive.write(job_dir / "manifest.json", "manifest.json")
            archive.write(job_dir / "manifest.csv", "manifest.csv")
            for asset_id in ids:
                path = files.get(asset_id)
                if path and Path(path).is_file():
                    archive.write(path, "assets/" + Path(path).name)

        final = self.get_download_job(job_id) or {}
        failed = int(final.get("failed") or 0)
        downloaded = int(final.get("downloaded") or 0)
        total = int(final.get("total") or len(ids))
        if failed == 0:
            state, message = "done", f"Downloaded {downloaded}/{total} asset(s). ZIP ready."
        elif downloaded > 0:
            state, message = "partial", f"Downloaded {downloaded}/{total}; {failed} failed. Partial ZIP ready."
        else:
            state, message = "failed", f"No assets downloaded; {failed} failed."

        self._update_download_job(
            job_id,
            state=state,
            message=message,
            completed=total,
            etaSeconds=0,
            zipReady=zip_path.is_file(),
            zipName=zip_name,
            zipPath=str(zip_path),
            folderPath=str(job_dir),
            elapsedSeconds=max((now_ms()-int(final.get("startedAt") or final.get("createdAt") or now_ms()))/1000.0, 0.0),
        )

    def open_download_root(self):
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(DOWNLOADS_DIR)
        return str(DOWNLOADS_DIR)

    def open_download_job_folder(self, job_id):
        job = self.get_download_job(job_id)
        if not job:
            raise ValueError("Unknown downloader job.")
        path = Path(job.get("folderPath") or (DOWNLOADS_DIR / core.safe_filename(job.get("name") or "AssetDownload") / str(job_id)))
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)
        return str(path)

    # --------------------------------------------------------
    # OS helpers
    # --------------------------------------------------------

    def open_root_folder(self):
        path = core.MIGRATIONS_DIR
        path.mkdir(
            parents=True,
            exist_ok=True,
        )

        if os.name == "nt":
            os.startfile(path)
        return str(path)

    def open_job_folder(self, job_id):
        job = self.get_job(job_id)

        if not job:
            raise ValueError(
                "Unknown migration job."
            )

        path = (
            core.MIGRATIONS_DIR
            / core.safe_filename(
                job["tower"]
            )
            / job_id
        )

        path.mkdir(
            parents=True,
            exist_ok=True,
        )

        if os.name == "nt":
            os.startfile(path)

        return str(path)


STATE = WebMigratorState()


# ============================================================
# Web UI
# ============================================================

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0c1017">
<title>RbxMigrator</title>
<style>
/* TM 3.31 — Backend-first RBXDL downloads. */
:root{
  color-scheme:dark;
  --bg:#080b12; --sidebar:#0c111b; --surface:#111824; --surface2:#151e2c;
  --surface3:#1b2534; --surface-hover:#202e42; --border:#253246;
  --border-bright:#34455c; --text:#edf3fb; --muted:#91a1b7; --subtle:#65778e;
  --blue:#6598ff; --blue-strong:#477df2; --cyan:#4bd4c6; --green:#63d9a5;
  --yellow:#f4c46c; --red:#f37c8b; --shadow:0 16px 40px rgba(0,0,0,.18);
  --glow:rgba(84,127,245,.16); --input:#0d1420; --radius:16px;
}
html[data-theme="light"]{
  color-scheme:light;
  --bg:#f3f5fa; --sidebar:#fff; --surface:#fff; --surface2:#f8fafe;
  --surface3:#edf2f9; --surface-hover:#e9f0fc; --border:#e1e7f0;
  --border-bright:#bccbdc; --text:#18243a; --muted:#65768e; --subtle:#8290a3;
  --blue:#386ce3; --blue-strong:#295bd1; --cyan:#088f85; --green:#15976a;
  --yellow:#a16b1d; --red:#cb4c60; --shadow:0 12px 30px rgba(20,42,75,.055);
  --glow:rgba(65,111,222,.10); --input:#f6f8fc;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,"Segoe UI",system-ui,-apple-system,sans-serif;line-height:1.48;-webkit-font-smoothing:antialiased}
button,input,select,textarea{font:inherit}button{cursor:pointer}
button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
button:disabled{opacity:.55;cursor:not-allowed}
::selection{background:rgba(101,152,255,.35)}
a{color:var(--blue)}
.shell{display:grid;grid-template-columns:252px minmax(0,1fr);min-height:100vh}
.sidebar{position:sticky;top:0;display:flex;flex-direction:column;height:100vh;padding:27px 16px 18px;background:var(--sidebar);border-right:1px solid var(--border);z-index:12}
.brand{display:flex;align-items:center;gap:12px;padding:0 11px 36px}
.logo{flex:0 0 44px;width:44px;height:44px;border-radius:13px;display:grid;place-items:center;font-size:16px;font-weight:850;letter-spacing:-.1em;color:#fff;background:linear-gradient(145deg,#477ef4,#4ad0c4);box-shadow:0 6px 16px rgba(64,118,233,.19)}
.brand h1{font-size:14px;font-weight:750;letter-spacing:-.03em;line-height:1.25;margin:0}
.brand p{font-size:11px;color:var(--muted);margin:4px 0 0}
.nav-group-label{font-size:10px;letter-spacing:.17em;color:var(--subtle);font-weight:800;padding:0 14px;margin-bottom:10px}
.nav{display:grid;gap:4px}
.nav button{border:1px solid transparent;display:flex;align-items:center;gap:12px;width:100%;min-height:44px;border-radius:10px;text-align:left;padding:9px 12px;background:transparent;color:var(--muted);font-size:13px;font-weight:560;transition:background .15s,color .15s,border-color .15s}
.nav button:hover{color:var(--text);background:var(--surface2)}
.nav button.active{color:var(--text);background:linear-gradient(90deg,var(--glow),transparent);border-color:var(--border);font-weight:750}
.nav button.active .nav-icon{color:var(--blue)}
.nav-icon{width:18px;height:18px;display:grid;place-items:center;flex:none;color:var(--subtle)}
.nav-icon svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:1.75;stroke-linecap:round;stroke-linejoin:round}
.nav-count{font-size:10px;margin-left:auto;border:1px solid var(--border);color:var(--muted);border-radius:6px;padding:1px 6px;min-width:23px;text-align:center}
.sidebar-foot{margin-top:auto;padding:14px 10px 2px;border-top:1px solid var(--border);font-size:11px;color:var(--muted)}
.sidebar-status{display:flex;gap:8px;align-items:center;font-size:12px;color:var(--text);font-weight:650;margin-bottom:4px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;background:var(--yellow);box-shadow:0 0 0 4px rgba(244,196,108,.08)}
.dot.online{background:var(--green);box-shadow:0 0 0 4px rgba(99,217,165,.10)}
.dot.offline{background:var(--red)}
.sidebar-version{color:var(--subtle);display:flex;justify-content:space-between;margin-top:16px}
.main{min-width:0;padding:31px clamp(20px,3vw,46px) 70px;max-width:1850px;width:100%;margin:0 auto}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:22px;margin-bottom:27px}
.eyebrow{font-size:10px;color:var(--blue);font-weight:800;letter-spacing:.16em;text-transform:uppercase}
.topbar .eyebrow{margin-bottom:5px;color:var(--subtle)}
.topbar h2{font-size:29px;line-height:1.14;letter-spacing:-.045em;margin:0;font-weight:780}
.topbar .sub{margin-top:7px;color:var(--muted);font-size:12px}
.top-actions{display:flex;align-items:center;justify-content:flex-end;gap:9px;flex-wrap:wrap}
.connection-pill{display:flex;align-items:center;gap:8px;border:1px solid var(--border);background:var(--surface);padding:9px 11px;border-radius:9px;color:var(--muted);font-size:11px;white-space:nowrap}
.connection-pill[data-state="online"]{color:var(--green)}
.connection-pill[data-state="offline"]{color:var(--red)}
.connection-pill .dot{width:6px;height:6px;box-shadow:none}
.actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;border:1px solid var(--border);background:var(--surface2);color:var(--text);padding:9px 13px;border-radius:9px;font-size:12px;font-weight:650;min-height:37px;white-space:nowrap;transition:border-color .16s,background .16s,transform .16s}
.btn:hover{border-color:var(--border-bright);background:var(--surface-hover)}
.btn:active{transform:translateY(1px)}
.btn.primary{background:var(--blue-strong);border-color:var(--blue-strong);color:white;box-shadow:0 5px 18px var(--glow)}
.btn.primary:hover{background:#648ff0;border-color:#648ff0}
.btn.danger{background:rgba(243,124,139,.08);color:var(--red);border-color:rgba(243,124,139,.28)}
.btn.ghost{border-color:transparent;background:transparent;color:var(--muted)}
.btn.ghost:hover{background:var(--surface2);color:var(--text)}
.icon-button{height:37px;min-width:37px;padding:6px 10px;font-size:16px;line-height:1}
.grid{display:grid;gap:16px}
.cards{grid-template-columns:repeat(5,minmax(0,1fr));margin-bottom:17px}
.card{min-width:0;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:21px;box-shadow:var(--shadow)}
.card-hover:hover{border-color:var(--border-bright)}
.metric-card{position:relative;min-height:148px;overflow:hidden;padding:19px 20px}
.metric-card::before{position:absolute;content:"";top:0;left:20px;right:20px;height:2px;background:var(--metric-accent);opacity:.8}
.metric-card[data-accent="blue"]{--metric-accent:var(--blue)}
.metric-card[data-accent="teal"]{--metric-accent:var(--cyan)}
.metric-card[data-accent="green"]{--metric-accent:var(--green)}
.metric-card[data-accent="red"]{--metric-accent:var(--red)}
.metric-card[data-accent="gold"]{--metric-accent:var(--yellow)}
.metric-label{font-size:11px;color:var(--muted);font-weight:650;letter-spacing:.025em}
.metric{font-size:29px;font-weight:790;letter-spacing:-.06em;line-height:1.25;margin:15px 0 5px;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.metric-note{font-size:11px;line-height:1.4;color:var(--subtle);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.metric-icon{position:absolute;right:17px;top:17px;width:28px;height:28px;border-radius:8px;display:grid;place-items:center;color:var(--metric-accent);background:color-mix(in srgb,var(--metric-accent) 9%,transparent);font-size:15px}
.dashboard-hero{display:flex;align-items:stretch;justify-content:space-between;gap:20px;position:relative;isolation:isolate;overflow:hidden;min-height:194px;padding:31px 35px;margin-bottom:17px;border-radius:18px;background:linear-gradient(115deg,#182a4d,#142241 55%,#10233d);border:1px solid #2c4466}
.dashboard-hero::before{position:absolute;content:"";inset:0;background:radial-gradient(550px 240px at 87% 40%,rgba(57,124,248,.21),transparent 75%);z-index:-1}
.hero-content{position:relative;z-index:2;max-width:650px}
.dashboard-hero .eyebrow{color:#96baff}
.dashboard-hero h3{font-size:clamp(24px,2.4vw,34px);line-height:1.13;letter-spacing:-.045em;margin:8px 0 10px;color:#f5f8ff}
.dashboard-hero p{max-width:510px;margin:0 0 19px;color:#afc2db;font-size:13px}
.dashboard-hero .btn{border-color:rgba(182,210,255,.25);background:rgba(255,255,255,.10);color:#fff}
.dashboard-hero .btn.primary{background:#fff;color:#20345c;border-color:#fff}
.hero-orbit{align-self:center;position:relative;width:215px;height:155px;flex:none;opacity:.85}
.hero-orbit .ring{position:absolute;border:1px solid rgba(159,196,250,.25);border-radius:50%;width:230px;height:230px;top:50%;left:50%;transform:translate(-50%,-50%)}
.hero-orbit .ring:nth-child(2){width:160px;height:160px;border-style:dashed}
.hero-orbit .ring:nth-child(3){width:93px;height:93px}
.hero-orbit .center{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:55px;height:55px;display:grid;place-items:center;border-radius:16px;background:linear-gradient(135deg,#6b99ff,#50d3dc);box-shadow:0 0 32px rgba(85,158,239,.24);font-weight:900;color:#10233a}
.hero-orbit .orbit-dot{position:absolute;border-radius:50%;width:11px;height:11px;background:#7cacf7;box-shadow:0 0 18px #77aef7}
.hero-orbit .orbit-dot.a{top:6px;left:78px}.hero-orbit .orbit-dot.b{top:115px;left:183px;background:#55d5c8}.hero-orbit .orbit-dot.c{top:122px;left:20px;width:7px;height:7px}
.section-head{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:11px;margin-bottom:16px}
.section-head h3{font-size:15px;font-weight:760;letter-spacing:-.02em;margin:0}
.section-caption{font-size:11px;color:var(--muted);margin:3px 0 0}
.section-head .badge{margin-left:auto}
.table-wrap{max-width:100%;overflow:auto;border:1px solid var(--border);border-radius:10px;background:var(--surface)}
table{border-collapse:separate;border-spacing:0;width:100%;min-width:700px}
th,td{text-align:left;vertical-align:middle;padding:12px 14px;border-bottom:1px solid var(--border);font-size:12px}
th{position:sticky;top:0;z-index:2;background:var(--surface2);text-transform:uppercase;letter-spacing:.095em;font-weight:700;font-size:10px;color:var(--subtle);white-space:nowrap}
td{color:var(--muted)}
td strong,.asset-name{color:var(--text)}
tr:last-child td{border-bottom:0}tbody tr{cursor:pointer;transition:background .12s}tbody tr:hover{background:var(--surface2)}
tbody tr.is-selected{background:var(--glow);box-shadow:inset 3px 0 0 var(--blue)}
tr[hidden]{display:none!important}
.badge{display:inline-flex;max-width:100%;align-items:center;gap:5px;border:1px solid var(--border);background:var(--surface2);color:var(--muted);padding:4px 9px;border-radius:7px;font-size:10px;font-weight:750;letter-spacing:.02em;text-transform:capitalize;white-space:nowrap}
.badge::before{content:"";width:5px;height:5px;border-radius:50%;background:currentColor;opacity:.65;flex:none}
.badge.done,.badge.cached,.badge.ready{color:var(--green);background:rgba(99,217,165,.07);border-color:rgba(99,217,165,.2)}
.badge.failed,.badge.error{color:var(--red);background:rgba(243,124,139,.07);border-color:rgba(243,124,139,.2)}
.badge.partial,.badge.rate_limited,.badge.paused{color:var(--yellow);background:rgba(244,196,108,.07);border-color:rgba(244,196,108,.2)}
.badge.portal,.badge.fetching,.badge.uploading,.badge.confirming,.badge.downloading,.badge.queued,.badge.preparing,.badge.working,.badge.studio-ready{color:var(--blue);background:rgba(101,152,255,.07);border-color:rgba(101,152,255,.2)}
.progress{height:6px;background:var(--surface3);border-radius:99px;overflow:hidden}
.progress>div{height:100%;min-width:0;border-radius:99px;background:linear-gradient(90deg,var(--blue-strong),var(--cyan));transition:width .3s ease}
.two{grid-template-columns:minmax(0,1.55fr) minmax(300px,1fr)}
.form{display:grid;gap:15px}.field{display:grid;gap:7px}.field label{font-size:11px;font-weight:730;color:var(--muted)}
.field input,.field select,.field textarea,.toolbar-input,.toolbar-select,.actions select{border:1px solid var(--border);background:var(--input);color:var(--text);border-radius:9px;padding:10px 12px;outline:none;font-size:12px;width:100%;min-height:39px;transition:border-color .15s,box-shadow .15s}
.field input:focus,.field select:focus,.field textarea:focus,.toolbar-input:focus,.toolbar-select:focus{border-color:var(--blue);box-shadow:0 0 0 3px var(--glow)}
.field textarea{resize:vertical;min-height:150px;font-family:Consolas,"Cascadia Code",monospace;line-height:1.65;font-size:12px}
.inline{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.hint{font-size:11px;line-height:1.6;color:var(--muted)}
.empty{padding:43px 22px;color:var(--muted);text-align:center;font-size:12px}
.asset-name{font-size:12px;font-weight:750;max-width:400px;overflow-wrap:anywhere}
.asset-id{font-family:"Cascadia Code",Consolas,monospace;color:var(--blue);font-size:11px}
.error-text{white-space:normal;color:var(--red);max-width:520px;overflow-wrap:anywhere}
.details{display:grid;gap:15px}
.job-title{display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:14px}
.job-title h3{font-size:18px;letter-spacing:-.025em;margin:0}
.job-title p{color:var(--subtle);font-size:11px;margin:5px 0}
.code{padding:15px;border:1px solid var(--border);border-radius:10px;background:var(--input);font-size:11px;color:var(--muted);font-family:Consolas,monospace;white-space:pre-wrap;overflow:auto;max-height:270px}
.view{display:none}.view.active{display:block;animation:fadein .17s ease-out both}
.toast{position:fixed;z-index:50;bottom:24px;right:24px;padding:13px 17px;border:1px solid var(--border-bright);background:var(--surface2);color:var(--text);box-shadow:0 18px 55px rgba(0,0,0,.3);border-radius:11px;font-size:12px;opacity:0;transform:translateY(12px);pointer-events:none;transition:opacity .2s,transform .2s}
.toast.show{opacity:1;transform:translateY(0)}
.live-stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-top:16px}
.live-stats>div{display:grid;gap:5px;padding:11px 13px;background:var(--surface2);border:1px solid var(--border);border-radius:9px}
.live-stats span{color:var(--subtle);font-size:10px;font-weight:650;text-transform:uppercase;letter-spacing:.075em}
.live-stats strong{color:var(--text);font-size:13px;font-weight:760;font-variant-numeric:tabular-nums}
.live-tower{font-size:17px;font-weight:770;letter-spacing:-.025em;color:var(--text)}
#liveCard{margin-bottom:17px}#liveCard.is-running{border-color:rgba(101,152,255,.34)}
#liveAssets{display:flex;flex-wrap:wrap;gap:6px;max-height:145px;overflow:auto;margin-top:14px}
#liveAssets.expanded{max-height:320px}
#liveAssets .badge{max-width:270px;overflow:hidden;text-overflow:ellipsis;display:inline-block}
#liveAssets .badge::before{display:none}
#liveAssetToggle{margin-top:10px;display:none}
.pipeline-steps{display:grid;gap:0;margin-top:14px}
.pipeline-step{position:relative;display:flex;gap:13px;padding:11px 0;color:var(--muted);font-size:12px}
.pipeline-step+.pipeline-step{border-top:1px solid var(--border)}
.pipeline-num{display:grid;place-items:center;background:var(--surface3);border:1px solid var(--border);width:27px;height:27px;border-radius:8px;color:var(--blue);font-weight:800;font-size:11px;flex:none}
.pipeline-step strong{display:block;color:var(--text);font-size:12px;margin-bottom:1px}
.toolbar{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.toolbar-tools{display:flex;align-items:center;gap:8px;flex-wrap:wrap;flex:1;justify-content:flex-end}
.toolbar-input{width:min(340px,100%);background:var(--surface2)}
.toolbar-select{width:155px;background:var(--surface2)}
.toolbar-counter{color:var(--subtle);font-size:11px;white-space:nowrap}
.heading-label{font-size:10px;letter-spacing:.15em;color:var(--subtle);text-transform:uppercase;font-weight:790;margin-bottom:6px}
.info-strip{display:flex;gap:9px;align-items:flex-start;padding:12px 14px;border-radius:10px;background:var(--surface2);border:1px solid var(--border);font-size:11px;color:var(--muted);line-height:1.6}
.info-strip strong{color:var(--text)}
.stats-inline{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.stats-inline>span{font-size:11px;color:var(--subtle)}
.stats-inline b{font-weight:780;color:var(--text)}
#jobDetails .table-wrap,#downloadDetails .table-wrap{max-height:520px;overflow:auto}
#jobDetails .details>.hint,#downloadDetails .details>.hint{overflow-wrap:anywhere}
#jobDetails .actions,#downloadDetails .actions{margin-bottom:2px}
#jobsTable td:nth-child(7){max-width:355px;min-width:180px;overflow-wrap:anywhere;color:var(--subtle)}
#downloadJobsTable .actions select{width:98px;min-height:34px;padding:6px 8px}
.scheduler-panel{display:grid;grid-template-columns:1fr auto;align-items:center;gap:18px}
.scheduler-signal{display:flex;align-items:center;gap:11px}
.scheduler-signal-icon{width:38px;height:38px;border-radius:10px;display:grid;place-items:center;background:var(--surface3);color:var(--cyan);font-size:20px}
.scheduler-details{display:grid;gap:3px}
.download-form-note{margin-bottom:15px}
.code-label{display:flex;align-items:center;justify-content:space-between;gap:10px}
#downloadDetected{color:var(--cyan);font-weight:780}
#jobDetailsToolbar{display:none;margin-top:16px}
#jobDetailsToolbar.visible{display:flex}
#jobAssetsSearch{max-width:360px}
.compact-label{font-size:10px;color:var(--subtle);text-transform:uppercase;letter-spacing:.08em}
html[data-density="compact"] th,html[data-density="compact"] td{padding:8px 11px}
html[data-density="compact"] .card{padding:15px}
html[data-density="compact"] .metric-card{min-height:124px}
@keyframes fadein{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
@media(max-width:1300px){.cards{grid-template-columns:repeat(3,minmax(0,1fr))}.two{grid-template-columns:minmax(0,1.3fr) minmax(280px,1fr)}}
@media(max-width:1050px){.shell{grid-template-columns:76px minmax(0,1fr)}.sidebar{padding:20px 12px}.sidebar .brand{padding:0 3px 25px}.sidebar .brand>div:last-child,.nav-group-label,.nav button .nav-text,.nav-count,.sidebar-foot{display:none}.nav button{justify-content:center;padding:10px}.sidebar .nav-icon{width:21px;height:21px}.sidebar .nav-icon svg{width:21px;height:21px}}
@media(max-width:780px){.shell{display:block}.sidebar{height:auto;position:sticky;top:0;padding:8px 12px;flex-direction:row;align-items:center;gap:12px;border-right:0;border-bottom:1px solid var(--border)}.sidebar .brand{padding:0}.sidebar .logo{height:35px;width:35px;font-size:13px}.nav{display:flex;flex:1;justify-content:flex-end;gap:4px}.nav button{width:40px;min-height:38px}.main{padding:22px 16px 50px}.topbar{align-items:flex-start;flex-direction:column;gap:14px}.top-actions{width:100%;justify-content:flex-start}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.two{grid-template-columns:1fr}.dashboard-hero{padding:23px}.hero-orbit{position:absolute;opacity:.28;right:-15px;top:20px;z-index:0}.hero-content{z-index:1}}
@media(max-width:510px){.cards{grid-template-columns:1fr 1fr;gap:9px}.metric-card{padding:13px;min-height:124px}.metric{font-size:23px}.metric-note{white-space:normal}.card{padding:16px}.topbar h2{font-size:25px}.live-stats{grid-template-columns:repeat(2,minmax(0,1fr))}.inline{grid-template-columns:1fr}.toolbar-tools{justify-content:stretch}.toolbar-input,.toolbar-select{width:100%}.connection-pill{display:none}.hero-orbit{display:none}.dashboard-hero h3{font-size:25px}.btn{white-space:normal}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}

</style>
<link rel="manifest" href="/manifest.webmanifest">
</head>
<body>
<div class="shell">
  <aside class="sidebar" aria-label="Main navigation">
    <div class="brand"><div class="logo" aria-hidden="true">RBX</div><div><h1>RbxMigrator</h1><p>Control center</p></div></div>
    <div class="nav-group-label">WORKSPACE</div>
    <nav class="nav"><button class="active" data-view="dashboard" aria-label="Overview"><span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg></span><span class="nav-text">Overview</span></button>
<button class="" data-view="jobs" aria-label="Migration jobs"><span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 9h8M8 13h8M8 17h5"/></svg></span><span class="nav-text">Migration jobs</span><span class="nav-count" id="navJobsCount">0</span></button>
<button class="" data-view="downloader" aria-label="Downloader"><span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12m0 0 4-4m-4 4-4-4M4 17v3h16v-3"/></svg></span><span class="nav-text">Downloader</span></button>
<button class="" data-view="studio" aria-label="Studio bridge"><span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7l9-4 9 4v10l-9 4-9-4zM3 7l9 5 9-5M12 12v9"/></svg></span><span class="nav-text">Studio bridge</span></button>
<button class="" data-view="settings" aria-label="Settings"><span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 17h16M9 4v6M15 14v6"/></svg></span><span class="nav-text">Settings</span></button></nav>
    <div class="sidebar-foot">
      <div class="sidebar-status"><span class="dot" id="sidebarDot"></span><span id="sidebarConnection">Connecting to helper</span></div>
      <div>127.0.0.1:8765 · Local only</div>
      <div class="sidebar-version"><span>TM Workspace</span><span>v3.32</span></div>
    </div>
  </aside>
  <main class="main">
    <div class="topbar">
      <div><div class="eyebrow">TM / WORKSPACE</div><h2 id="pageTitle">Dashboard</h2><div class="sub" id="pageSub">Your asset operations at a glance.</div></div>
      <div class="top-actions">
        <div class="connection-pill" id="connectionPill" data-state="checking"><span class="dot" id="headerDot"></span><span id="connectionText">Connecting…</span></div>
        <button class="btn ghost icon-button" id="densityToggle" onclick="toggleDensity()" title="Toggle compact density" aria-label="Toggle compact density">≡</button>
        <button class="btn ghost icon-button" id="themeToggle" onclick="toggleTheme()" title="Toggle light and dark theme" aria-label="Toggle light and dark theme">☾</button>
        <button class="btn" onclick="openMigrations()">Open folder</button>
        <button class="btn primary" onclick="refreshAll()">↻ &nbsp; Refresh</button>
      </div>
    </div>

    <section id="dashboard" class="view active">
      <div class="dashboard-hero">
        <div class="hero-content">
          <div class="eyebrow">Asset operations / unified</div>
          <h3>Build faster. Migrate smarter.</h3>
          <p>Track active migrations, inspect asset diagnostics, and manage your RBXDL download queue from one local workspace.</p>
          <div class="actions"><button class="btn primary" onclick="showView('jobs')">View migrations →</button><button class="btn" onclick="showView('downloader')">Bulk downloader</button></div>
        </div>
        <div class="hero-orbit" aria-hidden="true"><div class="ring"></div><div class="ring"></div><div class="ring"></div><div class="center">TM</div><div class="orbit-dot a"></div><div class="orbit-dot b"></div><div class="orbit-dot c"></div></div>
      </div>
      <div class="grid cards">
        <div class="card metric-card" data-accent="green"><div class="metric-label">Helper status</div><span class="metric-icon">◈</span><div class="metric" id="mHelper">—</div><div class="metric-note" id="mHelperNote">Checking configuration…</div></div>
        <div class="card metric-card" data-accent="blue"><div class="metric-label">Active jobs</div><span class="metric-icon">▤</span><div class="metric" id="mActive">0</div><div class="metric-note">In-progress migrations</div></div>
        <div class="card metric-card" data-accent="teal"><div class="metric-label">Migrated assets</div><span class="metric-icon">↗</span><div class="metric" id="mMigrated">0</div><div class="metric-note">Across loaded job history</div></div>
        <div class="card metric-card" data-accent="red"><div class="metric-label">Failed assets</div><span class="metric-icon">!</span><div class="metric" id="mFailed">0</div><div class="metric-note">May need a retry</div></div>
        <div class="card metric-card" data-accent="gold"><div class="metric-label">RBXDL pipeline</div><span class="metric-icon">◉</span><div class="metric" id="mRbxdl">Idle</div><div class="metric-note" id="mRbxdlNote">Session not checked</div></div>
      </div>

      <div class="card" id="liveCard" style="margin-bottom:16px">
        <div class="section-head"><div><div class="heading-label">OPERATIONS / CURRENT</div><h3>Live migration</h3></div><div class="actions"><button class="btn ghost" onclick="openActiveJob()">Inspect job →</button><span class="badge" id="liveState">idle</span></div></div>
        <div id="liveEmpty" class="hint">Waiting for a Studio migration…</div>
        <div id="liveContent" style="display:none">
          <div style="display:flex;justify-content:space-between;gap:12px;align-items:end"><div><div class="live-tower" id="liveTower">—</div><div class="hint" id="liveMessage">—</div></div><div class="asset-id" id="liveCount">0 / 0</div></div>
          <div class="progress" style="margin-top:10px"><div id="liveProgress"></div></div>
          <div class="live-stats">
            <div><span>Elapsed</span><strong id="liveElapsed">—</strong></div>
            <div><span>ETA</span><strong id="liveEta">Calculating…</strong></div>
            <div><span>Speed</span><strong id="liveRate">—</strong></div>
            <div><span>Workers</span><strong id="liveWorkers">—</strong></div>
          </div>
          <div id="liveAssets" aria-label="Recently updated assets"></div><button class="btn ghost" id="liveAssetToggle" onclick="toggleLiveAssets()">Show all assets</button>
        </div>
      </div>

      <div class="grid two">
        <div class="card">
          <div class="section-head"><div><div class="heading-label">HISTORY</div><h3>Recent migrations</h3></div><button class="btn ghost" onclick="showView('jobs')">View all →</button></div>
          <div class="table-wrap"><table><thead><tr><th>Tower</th><th>State</th><th>Progress</th><th>Migrated</th><th>Failed</th></tr></thead><tbody id="recentJobs"></tbody></table></div>
        </div>
        <div class="card">
          <div class="section-head"><h3>Pipeline</h3></div>
          <p class="hint">The migration pipeline prioritizes direct sources, then falls back when needed.</p>
          <div class="pipeline-steps">
            <div class="pipeline-step"><span class="pipeline-num">01</span><div><strong>Studio source preparation</strong>AnimationClipProvider and asset scanning</div></div>
            <div class="pipeline-step"><span class="pipeline-num">02</span><div><strong>Source download</strong>Roblox Asset Delivery / RBXDL fallback</div></div>
            <div class="pipeline-step"><span class="pipeline-num">03</span><div><strong>Parallel upload</strong>Open Cloud asset creation</div></div>
            <div class="pipeline-step"><span class="pipeline-num">04</span><div><strong>Studio reference patch</strong>Apply mapped IDs after review</div></div>
          </div>
          <div class="info-strip" style="margin-top:16px"><span>ⓘ</span><span>Use <strong>Preview / Dry Run</strong> in Studio before a large migration.</span></div>
        </div>
      </div>
    </section>

    <section id="jobs" class="view">
      <div class="card">
        <div class="section-head"><div><div class="heading-label">ASSET OPERATIONS</div><h3>Migration jobs</h3><p class="section-caption">Search, filter, and inspect jobs without scrolling through everything.</p></div><span class="badge" id="jobsTotalBadge">0 jobs</span></div>
        <div class="toolbar"><span class="toolbar-counter" id="jobsShown">0 jobs</span><div class="toolbar-tools"><input id="jobsSearch" class="toolbar-input" type="search" placeholder="Search names, IDs, messages…" aria-label="Search migration jobs"><select id="jobsFilter" class="toolbar-select" aria-label="Filter migration jobs"><option value="all">All statuses</option><option value="active">In progress</option><option value="done">Completed</option><option value="partial">Partial</option><option value="failed">Failed</option></select></div></div>
        <div class="table-wrap"><table><thead><tr><th>Tower</th><th>State</th><th>Progress</th><th>ETA</th><th>Migrated</th><th>Failed</th><th>Message</th></tr></thead><tbody id="jobsTable"></tbody></table></div>
      </div>
      <div class="toolbar" id="jobDetailsToolbar"><span class="toolbar-counter">PER-ASSET INSPECTOR</span><input type="search" id="jobAssetsSearch" class="toolbar-input" placeholder="Filter assets by name, ID or error…" aria-label="Filter selected job assets"></div>
      <div class="card" id="jobDetails" style="margin-top:14px"><div class="empty">Select a migration job to inspect it.</div></div>
    </section>

    <section id="downloader" class="view">
      <div class="grid two">
        <div class="card">
          <div class="section-head"><div><div class="heading-label">ASSET EXTRACTION</div><h3>Bulk Asset Downloader</h3></div><span class="badge">API → ZIP</span></div>
          <div class="info-strip download-form-note"><span>↓</span><span>Paste Roblox IDs, URLs or Lua tables. <strong>Downloads only</strong> — this tool never uploads assets or changes Studio.</span></div>
          <div class="form">
            <div class="field"><label>ZIP / job name</label><input id="downloadName" placeholder="Auto-detected from Lua table name"></div>
            <div class="field"><label>Asset IDs / Lua</label><textarea id="downloadSource" rows="14" spellcheck="false" placeholder="WaveIntros = {
    10981954022,
    10981954769,
    10981955568,
}"></textarea><div class="hint"><span id="downloadDetected">0</span> unique asset ID(s) detected.</div></div>
            <div class="field"><label><input type="checkbox" id="downloadAllowBrowserFallback" style="width:auto;vertical-align:middle;margin-right:9px">Allow browser fallback (optional)</label><div class="hint">OFF by default: cache → Roblox Asset Delivery → authenticated RBXDL Portal bulk HTTP. Enable for public RBXDL Single only if direct endpoints cannot finish. Existing site tokens, login and rate limits still apply.</div></div>
            <div class="field"><label>Queue priority</label><select id="downloadPriority"><option value="Normal" selected>Normal</option><option value="High">High — run next</option><option value="Low">Low — background</option></select></div>
            <div class="hint">The scheduler picks the highest-priority waiting ZIP job. Currently running jobs finish before the next starts.</div>
            <div class="actions"><button class="btn primary" onclick="startDownloader()">Start API-first ZIP</button><button class="btn" onclick="authenticateRbxdl()">Refresh RBXDL Portal login</button><button class="btn" onclick="openDownloads()">Open downloads folder</button><button class="btn" onclick="clearDownloaderInput()">Clear</button></div>
          </div>
        </div>
        <div class="card" id="downloadLiveCard">
          <div class="section-head"><h3>Current download</h3><span class="badge" id="downloadLiveState">idle</span></div>
          <div id="downloadLiveEmpty" class="hint">Paste a list and start a download.</div>
          <div id="downloadLiveContent" style="display:none">
            <div class="asset-name" id="downloadLiveName">—</div>
            <div class="hint" id="downloadLiveMessage">—</div>
            <div class="progress" style="margin-top:12px"><div id="downloadLiveProgress"></div></div>
            <div class="live-stats"><div><span>Progress</span><strong id="downloadLiveCount">—</strong></div><div><span>Elapsed</span><strong id="downloadLiveElapsed">—</strong></div><div><span>ETA</span><strong id="downloadLiveEta">—</strong></div><div><span>Speed</span><strong id="downloadLiveRate">—</strong></div></div>
            <div class="actions" id="downloadLiveActions" style="margin-top:14px"></div>
          </div>
        </div>
      </div>
      <div class="card" style="margin-top:14px">
        <div class="section-head"><div class="heading-label">COORDINATED DOWNLOADS</div><span class="badge" id="schedulerQueueCount">Queue: 0</span></div><div class="scheduler-signal"><span class="scheduler-signal-icon">◷</span><div class="scheduler-details"><h3 style="margin:0;font-size:15px">Intelligent Scheduler</h3><span class="hint">Priority queue with shared RBXDL pacing</span></div></div>
        <div class="hint" id="schedulerStatus">Waiting for status…</div>
        <div class="hint" style="margin-top:6px" id="schedulerNext">—</div>
      </div>
      <div class="card" style="margin-top:14px">
        <div class="section-head"><div><div class="heading-label">ZIP ARCHIVE</div><h3>Downloader jobs</h3></div><span class="hint">Successful files are cached.</span></div>
        <div class="toolbar"><span class="toolbar-counter" id="downloadsShown">0 jobs</span><div class="toolbar-tools"><input type="search" id="downloadJobsSearch" class="toolbar-input" placeholder="Find a download job…" aria-label="Search download jobs"><select id="downloadJobsFilter" class="toolbar-select" aria-label="Filter downloader jobs"><option value="all">All statuses</option><option value="active">Queued / running</option><option value="done">Completed</option><option value="partial">Partial</option><option value="failed">Failed</option></select></div></div>
        <div class="table-wrap"><table><thead><tr><th>Name</th><th>State</th><th>Progress</th><th>Downloaded</th><th>Failed</th><th>Actions</th></tr></thead><tbody id="downloadJobsTable"></tbody></table></div>
      </div>
      <div class="card" id="downloadDetails" style="margin-top:14px"><div class="empty">Select a downloader job to inspect its assets.</div></div>
    </section>

    <section id="studio" class="view">
      <div class="grid two">
        <div class="card">
          <div class="section-head"><div><div class="heading-label">CONNECTOR</div><h3>Studio workflow</h3></div></div>
          <div class="hint" style="font-size:14px;line-height:1.8">
            1. Keep this helper running.<br>
            2. Open Roblox Studio and select exactly one folder/model/Animation/Sound.<br>
            3. Open the <b>RbxMigrator</b> plugin.<br>
            4. Select Animations, Sounds, and/or Meshes, then click <b>Preview / Dry Run</b>.<br>
            5. Review the inventory, then confirm with <b>Migrate Selected</b>. The plugin sends the selected asset manifests here, the helper migrates them, and Studio applies the returned mappings.<br><br>
            The plugin can also reuse its local old→new mapping cache on duplicate tower copies with <b>Apply Cache</b>.
          </div>
        </div>
        <div class="card">
          <div class="section-head"><div><div class="heading-label">DIAGNOSTICS</div><h3>Connection test</h3></div></div>
          <div class="hint" id="studioStatus">Checking helper…</div>
          <div class="actions" style="margin-top:14px"><button class="btn primary" onclick="testHelper()">Test helper</button><button class="btn" onclick="authenticateRbxdl()">Refresh RBXDL login/key</button><button class="btn" onclick="downloadPlugin()">Download plugin source</button></div>
          <div class="code" style="margin-top:14px">http://127.0.0.1:8765
GET /ping
POST /source
POST /migrate
GET /jobs/&lt;job-id&gt;</div>
        </div>
      </div>
    </section>

    <section id="settings" class="view">
      <div class="grid two">
        <div class="card">
          <div class="section-head"><div><div class="heading-label">CREATOR ACCESS</div><h3>Open Cloud configuration</h3></div></div>
          <div class="form">
            <div class="field"><label>API key</label><input id="apiKey" type="password" placeholder="Leave blank to keep saved key"><div class="hint" id="keyHint">Your key is encrypted with Windows DPAPI before it is saved.</div></div>
            <div class="field"><label>Rolhub API key</label><input id="rolhubKey" type="password" placeholder="Leave blank to keep saved key"><div class="hint" id="rolhubHint">No Rolhub key saved.</div></div>
            <div class="inline">
              <div class="field"><label>Creator type</label><select id="creatorType"><option>Group</option><option>User</option></select></div>
              <div class="field"><label>Creator ID</label><input id="creatorId" inputmode="numeric" placeholder="Group or user ID"></div>
            </div>
            <div class="field"><label>Description</label><input id="description" placeholder="Migrated Roblox asset"></div>
            <div class="actions"><button class="btn primary" onclick="saveSettings()">Save settings</button><button class="btn danger" onclick="clearKey()">Clear saved API key</button></div>
          </div>
        </div>
        <div class="card">
          <div class="section-head"><div><div class="heading-label">DESKTOP MODE</div><h3>Install as an app</h3></div></div>
          <div class="hint" style="font-size:14px;line-height:1.7">
            This UI is local-only and runs on your PC. In Edge you can install it as an app using <b>⋯ → Apps → Install this site as an app</b>. That gives it its own window, taskbar entry, and app-like feel while the Python backend keeps doing the stuff a normal webpage cannot do: DPAPI, filesystem access, Playwright, Roblox uploads, and the Studio localhost API.
          </div>
          <div class="actions" style="margin-top:14px"><button class="btn" onclick="openMigrations()">Open data folder</button></div>
        </div>
      </div>
    </section>
  </main>
</div>
<div class="toast" id="toast"></div>
<script>
let jobs = [];
let downloads = [];
let selectedJob = null;
let selectedDownloadJob = null;
let config = null;
let liveAssetExpanded = false;
let connectionState = 'checking';
function setConnection(state){
  connectionState=state;
  const pill=$('#connectionPill');
  if(pill)pill.dataset.state=state;
  const text=$('#connectionText');
  if(text)text.textContent=state==='online'?'Local helper connected':state==='offline'?'Helper disconnected':'Connecting…';
  const side=$('#sidebarConnection');if(side)side.textContent=state==='online'?'Local helper online':state==='offline'?'Helper offline':'Connecting to helper';
  ['#sidebarDot','#headerDot'].forEach(s=>{const d=$(s);if(d)d.className='dot '+(state==='online'?'online':state==='offline'?'offline':'')});
}
function toggleTheme(){
  const light=document.documentElement.dataset.theme==='light';
  document.documentElement.dataset.theme=light?'dark':'light';
  try{localStorage.setItem('tm-theme',document.documentElement.dataset.theme)}catch{}
  updateThemeToggle();
}
function updateThemeToggle(){const b=$('#themeToggle');if(b)b.textContent=document.documentElement.dataset.theme==='light'?'☀':'☾'}
function toggleDensity(){const next=document.documentElement.dataset.density==='compact'?'comfortable':'compact';document.documentElement.dataset.density=next;try{localStorage.setItem('tm-density',next)}catch{};toast(next==='compact'?'Compact mode enabled':'Comfortable mode enabled')}
function toggleLiveAssets(){liveAssetExpanded=!liveAssetExpanded;renderLive()}
function openActiveJob(){const j=jobs.find(x=>!['done','failed','partial'].includes(x.state));if(j){showView('jobs');selectJob(j.jobId)}else{showView('jobs')}}
try{document.documentElement.dataset.theme=localStorage.getItem('tm-theme')||'dark';document.documentElement.dataset.density=localStorage.getItem('tm-density')||'comfortable'}catch{}

const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(msg){const t=$('#toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2200)}
function badge(state){const s=String(state||'queued');return `<span class="badge ${esc(s)}">${esc(s)}</span>`}
function progress(job){
  if(Number.isFinite(Number(job.overallProgress))){
    return Math.max(0,Math.min(100,Math.round(Number(job.overallProgress)*100)));
  }
  const total=Math.max(Number(job.total||0),1);
  return Math.round(Number(job.completed||0)/total*100);
}
function formatDuration(seconds){
  const n=Number(seconds);
  if(!Number.isFinite(n)||n<0)return '—';
  const s=Math.max(0,Math.round(n));
  const h=Math.floor(s/3600);
  const m=Math.floor((s%3600)/60);
  const sec=s%60;
  if(h>0)return `${h}h ${String(m).padStart(2,'0')}m`;
  if(m>0)return `${m}m ${String(sec).padStart(2,'0')}s`;
  return `${sec}s`;
}
function elapsedFor(job){
  const start=Number(job.createdAt||0);
  if(!start)return Number(job.elapsedSeconds||0);
  const end=Number(job.finishedAt||Date.now());
  return Math.max(0,(end-start)/1000);
}
function phaseCountText(job){
  const phase=String(job.phase||'');
  if(Number(job.downloadTotal||0)>0 && !['done','failed','partial'].includes(String(job.state||''))){
    return `${Number(job.downloadCompleted||0)}/${Number(job.downloadTotal||0)} fetched · ${Number(job.uploadAccepted||0)} accepted · ${Number(job.uploadConfirmed||0)} confirmed`;
  }

  if(phase==='portal' && Number(job.portalTotal||0)>0){
    return `${Number(job.portalCompleted||0)}/${Number(job.portalTotal||0)} fetched`;
  }

  if(phase==='uploading' && Number(job.phaseTotal||0)>0){
    return `${Number(job.phaseCompleted||0)}/${Number(job.phaseTotal||0)} uploaded`;
  }

  if(phase==='studio-prep' && Number(job.phaseTotal||0)>0){
    return `${Number(job.phaseCompleted||0)}/${Number(job.phaseTotal||0)} prepared`;
  }

  return `${Number(job.completed||0)}/${Number(job.total||0)} processed`;
}

function phaseRateLabel(job){
  const rate=Number(job.ratePerMinute||0);
  if(!(rate>0))return '';
  if(String(job.phase||'')==='portal')return `${rate.toFixed(1)} fetched/min`;
  if(String(job.phase||'')==='uploading')return `${rate.toFixed(1)} uploads/min`;
  if(String(job.phase||'')==='studio-prep')return `${rate.toFixed(1)} prepared/min`;
  return `${rate.toFixed(1)}/min`;
}

function etaFor(job){
  const serverEta=Number(job.etaSeconds);
  const phaseDone=Number(job.phaseCompleted||0);
  const phaseTotal=Number(job.phaseTotal||0);
  const activePhase=!['done','failed','partial','complete'].includes(String(job.state||''));

  // A 0-second ETA is only meaningful when the active phase is actually done.
  // This prevents stale "ETA 0s" values from a previous phase.
  if(Number.isFinite(serverEta)&&serverEta>=0){
    if(serverEta>0 || !activePhase || (phaseTotal>0 && phaseDone>=phaseTotal)){
      return serverEta;
    }
  }

  const done=phaseDone;
  const total=Number(job.phaseTotal||0);
  const phaseStart=Number(job.phaseStartedAt||0);

  if(done>0&&total>done&&phaseStart>0){
    const elapsed=Math.max((Date.now()-phaseStart)/1000,.001);
    const rate=done/elapsed;
    if(rate>0)return (total-done)/rate;
  }
  return NaN;
}
function setMetric(id,value){
  const el=$(id);
  const next=String(value);
  if(!el||el.textContent===next)return;
  el.textContent=next;
  el.classList.remove('metric-pop');
  void el.offsetWidth;
  el.classList.add('metric-pop');
}
function setTitles(view){const map={dashboard:['Dashboard','Your asset operations at a glance.'],jobs:['Migration jobs','Track uploads, retries, and per-asset results.'],downloader:['Downloader','API-first downloads, with optional RBXDL browser fallback.'],studio:['Studio bridge','Use the Studio plugin with your local helper.'],settings:['Settings','Manage your Roblox Open Cloud credentials.']};$('#pageTitle').textContent=map[view][0];$('#pageSub').textContent=map[view][1]}
function showView(view){
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
  const next=$('#'+view);
  next.classList.add('active');
  next.style.animation='none';
  void next.offsetWidth;
  next.style.animation='';
  document.querySelectorAll('.nav button').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
  setTitles(view);
}
document.querySelectorAll('.nav button').forEach(b=>b.onclick=()=>showView(b.dataset.view));

async function api(path, opts={}){
  const res=await fetch(path,{headers:{'Content-Type':'application/json',...(opts.headers||{})},...opts});
  const text=await res.text(); let data={}; try{data=text?JSON.parse(text):{}}catch{data={error:text}}
  if(!res.ok) throw new Error(data.error||`${res.status} ${res.statusText}`);
  return data;
}

function renderLive(){
  const active=jobs.find(j=>!['done','failed','partial'].includes(j.state));
  const card=$('#liveCard');

  if(!active){
    card?.classList.remove('is-running');
    $('#liveEmpty').style.display='block';
    $('#liveContent').style.display='none';
    $('#liveAssetToggle').style.display='none';
    $('#liveState').textContent='idle';
    $('#liveState').className='badge';
    $('#liveElapsed').textContent='—';
    $('#liveEta').textContent='—';
    $('#liveRate').textContent='—';
    $('#liveWorkers').textContent='—';
    document.title='RbxMigrator';
    return;
  }

  card?.classList.add('is-running');
  $('#liveEmpty').style.display='none';
  $('#liveContent').style.display='block';
  $('#liveState').textContent=active.state;
  $('#liveState').className='badge '+active.state;
  $('#liveTower').textContent=active.tower;
  $('#liveMessage').textContent=active.message||active.state;
  $('#liveCount').textContent=phaseCountText(active);
  $('#liveProgress').style.width=`${progress(active)}%`;

  const eta=etaFor(active);
  const rate=Number(active.ratePerMinute||0);
  const workers=Number(active.uploadWorkers||0);

  $('#liveElapsed').textContent=formatDuration(elapsedFor(active));
  $('#liveEta').textContent=Number.isFinite(eta)?formatDuration(eta):'Calculating…';
  $('#liveRate').textContent=rate>0?`${rate.toFixed(1)}/min`:'—';
  $('#liveWorkers').textContent=workers>0?String(workers):(active.phase==='studio-prep'?'12':'—');

  document.title=`${progress(active)}% · ${active.tower} · RbxMigrator`;

  const assets=Object.values(active.assets||{});
  const visible=liveAssetExpanded?assets:assets.filter(a=>['uploading','confirming','failed','rate_limited'].includes(a.status)).slice(0,8).concat(assets.filter(a=>!['uploading','confirming','failed','rate_limited'].includes(a.status)).slice(0,Math.max(0,12-Math.min(8,assets.filter(a=>['uploading','confirming','failed','rate_limited'].includes(a.status)).length))));
  $('#liveAssets').classList.toggle('expanded',liveAssetExpanded);
  $('#liveAssets').innerHTML=visible.map(a=>`<span class="badge ${esc(a.status)}" title="${esc(a.error||a.filename||'')}">${esc(a.name||a.assetId)} · ${esc(a.status)}</span>`).join('');
  const toggle=$('#liveAssetToggle');
  toggle.style.display=assets.length>visible.length||liveAssetExpanded?'inline-flex':'none';
  toggle.textContent=liveAssetExpanded?`Collapse asset list`:`View all ${assets.length} assets →`;
}

function applySnapshot(data){
  if(data.config){config=data.config;if(document.activeElement!==$('#creatorType'))$('#creatorType').value=config.creatorType||'Group';if(document.activeElement!==$('#creatorId'))$('#creatorId').value=config.creatorId||'';if(document.activeElement!==$('#description'))$('#description').value=config.description||'Migrated Roblox asset';$('#keyHint').textContent=config.hasApiKey?'A Windows-encrypted API key is saved. Leave blank to keep it.':'No API key is currently saved.';setMetric('#mHelper',config.configured?'Ready':'Setup');$('#mHelperNote').textContent=config.configured?`${config.creatorType} ${config.creatorId}`:'Configure API key + creator ID';$('#studioStatus').innerHTML=config.configured?`<span style="color:var(--green)">● Helper is configured and ready.</span>`:`<span style="color:var(--yellow)">● Helper is running, but settings are incomplete.</span>`;}
  if(data.rbxdl){setMetric('#mRbxdl',(data.rbxdl.state||'idle').replace(/^./,c=>c.toUpperCase()));$('#mRbxdlNote').textContent=data.rbxdl.message||'';}
  if(data.jobs){jobs=data.jobs;renderJobsFromState();}
  if(data.scheduler){const s=data.scheduler;const g=s.gate||{};$('#schedulerQueueCount').textContent=`Queue: ${s.queued||0}`;$('#schedulerStatus').textContent=`RBXDL pacing: ${Number(g.spacing||0).toFixed(1)}s minimum · cooldown remaining: ${Number(g.wait||0).toFixed(1)}s · ${g.waitingWorkers||0} worker(s) waiting · ${g.rateLimitEvents||0} rate limits learned`;$('#schedulerNext').textContent=s.nextJob?`Next ZIP: ${s.nextJob}`:'No queued ZIP jobs.';}
  if(data.downloads){downloads=data.downloads;renderDownloader();}
}

function renderJobsFromState(){
  const active=jobs.filter(j=>!['done','failed','partial'].includes(j.state)).length;
  const migrated=jobs.reduce((n,j)=>n+Number(j.migrated||0),0);
  const failed=jobs.reduce((n,j)=>n+Number(j.failed||0),0);
  setMetric('#mActive',active);setMetric('#mMigrated',migrated);setMetric('#mFailed',failed);
  const query=($('#jobsSearch')?.value||'').trim().toLowerCase();
  const filter=$('#jobsFilter')?.value||'all';
  const visibleJobs=jobs.filter(j=>{
    const state=String(j.state||'');
    if(filter==='active'&&['done','failed','partial'].includes(state))return false;
    if(!['all','active'].includes(filter)&&state!==filter)return false;
    return !query||[j.tower,j.assetType,j.jobId,j.message].some(v=>String(v||'').toLowerCase().includes(query));
  });
  $('#jobsShown').textContent=`Showing ${visibleJobs.length} of ${jobs.length} jobs`;
  $('#jobsTotalBadge').textContent=`${jobs.length} total`;
  const navCount=$('#navJobsCount');if(navCount)navCount.textContent=String(active);
  const rows=visibleJobs.map(j=>`<tr class="${selectedJob===j.jobId?'is-selected':''}" onclick="selectJob('${esc(j.jobId)}')"><td><div class="asset-name">${esc(j.tower)}</div><div class="hint">${esc(j.assetType||'Animation')} · ${new Date(j.createdAt).toLocaleString()}</div></td><td>${badge(j.state)}</td><td><div class="progress"><div style="width:${progress(j)}%"></div></div><div class="hint" style="margin-top:6px">${esc(phaseCountText(j))}</div></td><td><div class="asset-name">${Number.isFinite(etaFor(j))?formatDuration(etaFor(j)):'—'}</div><div class="hint">${formatDuration(elapsedFor(j))} elapsed</div></td><td><strong>${esc(j.migrated)}</strong></td><td>${esc(j.failed)}</td><td title="${esc(j.message||'')}">${esc(j.message)}</td></tr>`).join('');
  $('#jobsTable').innerHTML=rows||`<tr><td colspan="7"><div class="empty">No jobs match this filter.</div></td></tr>`;
  $('#recentJobs').innerHTML=(jobs.slice(0,5).map(j=>`<tr onclick="showView('jobs');selectJob('${esc(j.jobId)}')"><td>${esc(j.tower)}</td><td>${badge(j.state)}</td><td>${esc(phaseCountText(j))}</td><td>${esc(j.migrated)}</td><td>${esc(j.failed)}</td></tr>`).join(''))||`<tr><td colspan="5"><div class="empty">No migrations yet.</div></td></tr>`;
  if(selectedJob){const updated=jobs.find(j=>j.jobId===selectedJob);if(updated)renderJob(updated)}
  renderLive();
}

function filterJobAssets(){
  const q=($('#jobAssetsSearch')?.value||'').trim().toLowerCase();
  document.querySelectorAll('#jobDetails tbody tr').forEach(tr=>{tr.hidden=!!q&&!tr.textContent.toLowerCase().includes(q)});
}
$('#jobsSearch')?.addEventListener('input',()=>renderJobsFromState());
$('#jobsFilter')?.addEventListener('change',()=>renderJobsFromState());
$('#jobAssetsSearch')?.addEventListener('input',filterJobAssets);
$('#downloadJobsSearch')?.addEventListener('input',()=>renderDownloader());
$('#downloadJobsFilter')?.addEventListener('change',()=>renderDownloader());

async function refreshConfig(){
  config=await api('/api/config');
  $('#creatorType').value=config.creatorType||'Group';
  $('#creatorId').value=config.creatorId||'';
  $('#description').value=config.description||'Migrated Roblox asset';
  $('#keyHint').textContent=config.hasApiKey?'A Windows-encrypted API key is saved. Leave blank to keep it.':'No API key is currently saved.';
  $('#rolhubHint').textContent=config.hasRolhubKey?'A Windows-encrypted Rolhub key is saved. Leave blank to keep it.':'No Rolhub key saved.';
  setMetric('#mHelper',config.configured?'Ready':'Setup');
  $('#mHelperNote').textContent=config.configured?`${config.creatorType} ${config.creatorId}`:'Configure API key + creator ID';
  $('#studioStatus').innerHTML=config.configured?`<span style="color:var(--green)">● Helper is configured and ready.</span>`:`<span style="color:var(--yellow)">● Helper is running, but settings are incomplete.</span>`;
}

async function refreshJobs(){
  const data=await api('/api/snapshot');
  applySnapshot(data);
}


window.selectJob=id=>{selectedJob=id;const job=jobs.find(j=>j.jobId===id);if(job)renderJob(job);document.querySelectorAll('#jobsTable tbody tr').forEach(tr=>tr.classList.toggle('is-selected',tr.getAttribute('onclick')===`selectJob('${id}')`))}
function renderJob(job){
  const assets=Object.values(job.assets||{});
  const assetRows=assets.map(a=>`<tr><td><div class="asset-name">${esc(a.name)}</div><div class="asset-id">${esc(a.assetId)}</div></td><td>${badge(a.status)}</td><td>${esc(a.source||'—')}</td><td class="asset-id">${esc(a.newId||'—')}</td><td class="error-text">${esc(a.error||'')}</td></tr>`).join('');
  const pct=progress(job);
  $('#jobDetails').innerHTML=`<div class="details"><div class="job-title"><div><h3>${esc(job.tower)}</h3><p>${esc(job.assetType||'Animation')} • ${esc(job.jobId)} • ${new Date(job.createdAt).toLocaleString()}</p></div><div>${badge(job.state)}</div></div><div class="progress"><div style="width:${pct}%"></div></div><div class="hint">${esc(job.message)} • ${esc(phaseCountText(job))} • ${job.migrated} migrated • ${job.failed} failed • ${formatDuration(elapsedFor(job))} elapsed • ETA ${Number.isFinite(etaFor(job))?formatDuration(etaFor(job)):'calculating'}${phaseRateLabel(job)?` • ${esc(phaseRateLabel(job))}`:''}${Number(job.uploadWorkers||0)>0?` • ${Number(job.uploadWorkers)} upload workers`:''}</div><div class="actions"><button class="btn primary" onclick="retryJob('${esc(job.jobId)}')">Retry failed</button><button class="btn" onclick="openJob('${esc(job.jobId)}')">Open job folder</button><button class="btn" onclick="downloadDiagnostics('${esc(job.jobId)}')">Download diagnostics</button></div><div class="table-wrap"><table><thead><tr><th>Asset</th><th>Status</th><th>Source</th><th>New ID</th><th>Error</th></tr></thead><tbody>${assetRows||'<tr><td colspan="5"><div class="empty">No asset details.</div></td></tr>'}</tbody></table></div></div>`;
  const panel=$('#jobDetails');
  $('#jobDetailsToolbar').classList.add('visible');
  filterJobAssets();
  panel.classList.remove('details-animate');
  void panel.offsetWidth;
  panel.classList.add('details-animate');
}


function extractDownloaderIds(text){
  const values=[],seen=new Set();
  const push=v=>{if(v&&!seen.has(v)){seen.add(v);values.push(v)}};
  for(const m of String(text||'').matchAll(/rbxassetid:\/\/(\d{5,20})/gi))push(m[1]);
  for(const m of String(text||'').matchAll(/roblox\.com\/(?:catalog|library|asset)\/(\d{5,20})/gi))push(m[1]);
  for(const m of String(text||'').matchAll(/[?&]id=(\d{5,20})/gi))push(m[1]);
  for(const m of String(text||'').matchAll(/(?<!\d)(\d{5,20})(?!\d)/g))push(m[1]);
  return values;
}
function updateDownloaderDetected(){const ids=extractDownloaderIds($('#downloadSource')?.value||'');const el=$('#downloadDetected');if(el)el.textContent=String(ids.length)}
setTimeout(()=>{$('#downloadSource')?.addEventListener('input',updateDownloaderDetected)},0);
function downloadJobProgress(job){const total=Math.max(Number(job.total||0),1);return Math.max(0,Math.min(100,Math.round(Number(job.completed||0)/total*100)))}
function renderDownloader(){
  const table=$('#downloadJobsTable'); if(!table)return;
  const dq=($('#downloadJobsSearch')?.value||'').trim().toLowerCase();
  const df=$('#downloadJobsFilter')?.value||'all';
  const filteredDownloads=downloads.filter(job=>{
    if(df==='active'&&['done','partial','failed'].includes(job.state))return false;
    if(!['all','active'].includes(df)&&job.state!==df)return false;
    return !dq||[job.name,job.priority,job.jobId,job.message].some(x=>String(x||'').toLowerCase().includes(dq));
  });
  $('#downloadsShown').textContent=`Showing ${filteredDownloads.length} of ${downloads.length} jobs`;
  table.innerHTML=filteredDownloads.map(job=>`<tr class="${selectedDownloadJob===job.jobId?'is-selected':''}" onclick="selectDownloadJob('${esc(job.jobId)}')"><td><div class="asset-name">${esc(job.name)}</div><div class="hint">${new Date(job.createdAt).toLocaleString()} · ${esc(job.priority||'Normal')} priority</div></td><td>${badge(job.state)}</td><td><div class="progress"><div style="width:${downloadJobProgress(job)}%"></div></div><div class="hint" style="margin-top:4px">${esc(job.completed||0)}/${esc(job.total||0)}</div></td><td>${esc(job.downloaded||0)}</td><td>${esc(job.failed||0)}</td><td><div class="actions" onclick="event.stopPropagation()">${job.zipReady?`<button class="btn primary" onclick="downloadZip('${esc(job.jobId)}')">ZIP</button>`:''}${Number(job.failed||0)>0&&['partial','failed','done'].includes(job.state)?`<button class="btn" onclick="retryDownloadJob('${esc(job.jobId)}')">Retry failed</button>`:''}${['queued','paused'].includes(job.state)?`<select aria-label="Job priority" onclick="event.stopPropagation()" onchange="event.stopPropagation();changeDownloadPriority('${esc(job.jobId)}',this.value)">${['High','Normal','Low'].map(p=>`<option value="${p}" ${job.priority===p?'selected':''}>${p}</option>`).join('')}</select>`:''}${job.state==='queued'?`<button class="btn" onclick="controlDownloadJob('${esc(job.jobId)}','pause')">Pause</button>`:''}${job.state==='paused'?`<button class="btn" onclick="controlDownloadJob('${esc(job.jobId)}','resume')">Resume</button>`:''}<button class="btn" onclick="openDownloadJob('${esc(job.jobId)}')">Folder</button></div></td></tr>`).join('')||`<tr><td colspan="6"><div class="empty">No downloader jobs yet.</div></td></tr>`;
  const active=downloads.find(job=>!['done','partial','failed'].includes(String(job.state||'')))||downloads[0];
  if(!active){$('#downloadLiveEmpty').style.display='block';$('#downloadLiveContent').style.display='none';$('#downloadLiveState').textContent='idle';return}
  $('#downloadLiveEmpty').style.display='none';$('#downloadLiveContent').style.display='block';$('#downloadLiveState').textContent=active.state||'idle';$('#downloadLiveState').className='badge '+esc(active.state||'');$('#downloadLiveName').textContent=active.name||'AssetDownload';$('#downloadLiveMessage').textContent=active.message||'';$('#downloadLiveProgress').style.width=`${downloadJobProgress(active)}%`;$('#downloadLiveCount').textContent=`${active.completed||0} / ${active.total||0}`;$('#downloadLiveElapsed').textContent=formatDuration(Number(active.elapsedSeconds||0));$('#downloadLiveEta').textContent=Number.isFinite(Number(active.etaSeconds))?formatDuration(Number(active.etaSeconds)):'Calculating…';$('#downloadLiveRate').textContent=Number(active.ratePerMinute||0)>0?`${Number(active.ratePerMinute).toFixed(1)}/min`:'—';$('#downloadLiveActions').innerHTML=`${active.zipReady?`<button class="btn primary" onclick="downloadZip('${esc(active.jobId)}')">Download ZIP</button>`:''}<button class="btn" onclick="openDownloadJob('${esc(active.jobId)}')">Open folder</button>`;
  if(selectedDownloadJob){const selected=downloads.find(j=>j.jobId===selectedDownloadJob);if(selected)renderDownloadDetails(selected)}
}
window.selectDownloadJob=id=>{selectedDownloadJob=id;const job=downloads.find(j=>j.jobId===id);if(job)renderDownloadDetails(job);document.querySelectorAll('#downloadJobsTable tbody tr').forEach(tr=>tr.classList.toggle('is-selected',tr.getAttribute('onclick')===`selectDownloadJob('${id}')`))};
function renderDownloadDetails(job){const assets=Object.values(job.assets||{});const rows=assets.map(a=>`<tr><td class="asset-id">${esc(a.assetId)}</td><td>${badge(a.status)}</td><td>${esc(a.source||'—')}</td><td>${esc(a.filename||'—')}</td><td class="error-text">${esc(a.error||'')}</td></tr>`).join('');$('#downloadDetails').innerHTML=`<div class="details"><div class="job-title"><div><h3>${esc(job.name)}</h3><p>${esc(job.jobId)} • ${new Date(job.createdAt).toLocaleString()}</p></div><div>${badge(job.state)}</div></div><div class="progress"><div style="width:${downloadJobProgress(job)}%"></div></div><div class="hint">${esc(job.message||'')} • ${job.allowBrowserFallback?'Browser fallback allowed':'API-only'} • ${job.downloaded||0} downloaded • ${job.failed||0} failed • ${formatDuration(Number(job.elapsedSeconds||0))} elapsed</div><div class="actions">${job.zipReady?`<button class="btn primary" onclick="downloadZip('${esc(job.jobId)}')">Download ZIP</button>`:''}${Number(job.failed||0)>0&&['partial','failed','done'].includes(job.state)?`<button class="btn" onclick="retryDownloadJob('${esc(job.jobId)}')">Retry failed</button>`:''}<button class="btn" onclick="openDownloadJob('${esc(job.jobId)}')">Open folder</button></div><div class="table-wrap"><table><thead><tr><th>Asset ID</th><th>Status</th><th>Source</th><th>File</th><th>Error</th></tr></thead><tbody>${rows||'<tr><td colspan="5"><div class="empty">No assets.</div></td></tr>'}</tbody></table></div></div>`}
async function startDownloader(){const source=$('#downloadSource').value||'';const ids=extractDownloaderIds(source);if(!ids.length){toast('No asset IDs detected');return}try{const r=await api('/api/downloader/start',{method:'POST',body:JSON.stringify({name:$('#downloadName').value||'',priority:$('#downloadPriority').value||'Normal',source,allowBrowserFallback:$('#downloadAllowBrowserFallback').checked})});selectedDownloadJob=r.jobId;toast(`Started ${ids.length} asset download${ids.length===1?'':'s'}`);showView('downloader');await refreshJobs()}catch(e){toast(e.message)}}
async function retryDownloadJob(id){try{const r=await api(`/api/downloader/jobs/${id}/retry`,{method:'POST',body:JSON.stringify({allowBrowserFallback:$('#downloadAllowBrowserFallback').checked})});selectedDownloadJob=r.jobId;toast('Retry queued with current browser fallback setting; cache will be reused');showView('downloader');await refreshJobs()}catch(e){toast(e.message)}}
async function changeDownloadPriority(id,priority){try{await api(`/api/downloader/jobs/${id}/priority`,{method:'POST',body:JSON.stringify({priority})});toast(`Priority set to ${priority}`);await refreshJobs()}catch(e){toast(e.message)}}
async function controlDownloadJob(id,action){try{await api(`/api/downloader/jobs/${id}/${action}`,{method:'POST',body:'{}'});toast(action==='pause'?'Queued job paused':'Job queued to resume');await refreshJobs()}catch(e){toast(e.message)}}
function downloadZip(id){window.location.href=`/api/downloader/jobs/${encodeURIComponent(id)}/zip`}
async function openDownloadJob(id){try{await api(`/api/downloader/jobs/${id}/open-folder`,{method:'POST',body:'{}'})}catch(e){toast(e.message)}}
async function openDownloads(){try{await api('/api/downloader/open-root',{method:'POST',body:'{}'})}catch(e){toast(e.message)}}
function clearDownloaderInput(){$('#downloadSource').value='';$('#downloadName').value='';updateDownloaderDetected()}

async function saveSettings(){
  try{
    const data=await api('/api/config',{method:'POST',body:JSON.stringify({apiKey:$('#apiKey').value,rolhubKey:$('#rolhubKey').value,creatorType:$('#creatorType').value,creatorId:$('#creatorId').value,description:$('#description').value})});
    $('#apiKey').value='';$('#rolhubKey').value='';config=data;toast('Settings saved');await refreshConfig();
  }catch(e){toast(e.message)}
}
async function clearKey(){if(!confirm('Clear the saved API key?'))return;try{await api('/api/config',{method:'POST',body:JSON.stringify({clearApiKey:true})});$('#apiKey').value='';toast('API key cleared');await refreshConfig()}catch(e){toast(e.message)}}
async function retryJob(id){try{const r=await api(`/api/jobs/${id}/retry`,{method:'POST',body:'{}'});toast('Retry started');selectedJob=r.jobId;await refreshJobs()}catch(e){toast(e.message)}}
async function openJob(id){try{await api(`/api/jobs/${id}/open-folder`,{method:'POST',body:'{}'})}catch(e){toast(e.message)}}
async function openMigrations(){try{await api('/api/open-root',{method:'POST',body:'{}'})}catch(e){toast(e.message)}}
function downloadDiagnostics(id){window.open(`/api/jobs/${id}/diagnostics`,'_blank')}
function downloadPlugin(){window.open('/plugin.lua','_blank')}
async function authenticateRbxdl(){try{await api('/api/rbxdl/auth',{method:'POST',body:'{}'});toast('RBXDL session check started. Edge only opens if login/key renewal is needed.')}catch(e){toast(e.message)}}
async function testHelper(){try{const r=await api('/ping');toast(r.configured?'Helper ready':'Helper running; configure settings');await refreshConfig()}catch(e){toast(e.message)}}
async function refreshAll(){if(['file:','about:'].includes(location.protocol))return;try{await Promise.all([refreshConfig(),refreshJobs()]);setConnection('online')}catch(e){setConnection('offline');console.error(e)}}
let liveSource=null;
function connectLive(){
  if(liveSource)liveSource.close();
  if(['file:','about:'].includes(location.protocol))return; // Static design preview: no local API host.
  liveSource=new EventSource('/api/events');
  liveSource.addEventListener('snapshot',event=>{setConnection('online');try{applySnapshot(JSON.parse(event.data))}catch(e){console.error(e)}});
  liveSource.onopen=()=>setConnection('online');
  liveSource.onerror=()=>{if(liveSource&&liveSource.readyState===2)setConnection('offline');};
}
updateThemeToggle();
connectLive();
setInterval(()=>{if(!liveSource||liveSource.readyState===2)refreshAll()},3000);
refreshAll();
if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js').catch(()=>{})}
</script>
</body>
</html>
"""


MANIFEST = {
    "name": "RbxMigrator",
    "short_name": "RbxMigrator",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#080b12",
    "theme_color": "#111824",
}


SW_JS = r"""
const CACHE='tower-migrator-v329';
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(['/','/manifest.webmanifest']))));
self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
self.addEventListener('fetch',e=>{
  if(e.request.method!=='GET') return;
  e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));
});
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "TowerMigratorWeb/1.0"

    def log_message(self, fmt, *args):
        return

    def _send_bytes(
        self,
        status,
        content,
        content_type,
        extra_headers=None,
    ):
        self.send_response(status)
        self.send_header(
            "Content-Type",
            content_type,
        )
        self.send_header(
            "Content-Length",
            str(len(content)),
        )
        self.send_header(
            "Cache-Control",
            "no-store",
        )

        if extra_headers:
            for key, value in (
                extra_headers.items()
            ):
                self.send_header(
                    key,
                    value,
                )

        self.end_headers()
        self.wfile.write(content)

    def send_json(self, status, value):
        raw = json.dumps(
            value,
            ensure_ascii=False,
        ).encode("utf-8")

        self._send_bytes(
            status,
            raw,
            "application/json; charset=utf-8",
        )

    def read_json(self):
        length = int(
            self.headers.get(
                "Content-Length",
                "0",
            )
        )

        if length <= 0:
            return {}

        raw = self.rfile.read(
            length
        )

        return json.loads(
            raw.decode("utf-8")
        )

    # --------------------------------------------------------
    # GET
    # --------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(
            self.path
        )
        path = parsed.path

        if path == "/":
            self._send_bytes(
                200,
                INDEX_HTML.encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return

        if path == "/manifest.webmanifest":
            self._send_bytes(
                200,
                json.dumps(
                    MANIFEST
                ).encode("utf-8"),
                "application/manifest+json",
            )
            return

        if path == "/sw.js":
            self._send_bytes(
                200,
                SW_JS.encode("utf-8"),
                "application/javascript; charset=utf-8",
            )
            return

        if path == "/plugin.lua":
            plugin_path = (
                Path(__file__).parent
                / "TowerAnimationMigrator_Ultimate.plugin.lua"
            )

            self._send_bytes(
                200,
                plugin_path.read_bytes(),
                "text/plain; charset=utf-8",
                {
                    "Content-Disposition":
                        'attachment; filename="TowerAnimationMigrator_Ultimate.plugin.lua"'
                },
            )
            return

        if path == "/ping":
            config = (
                STATE.get_config_public()
            )

            self.send_json(
                200,
                {
                    "ok": True,
                    "configured":
                        config["configured"],
                    "creatorType":
                        config["creatorType"],
                    "creatorId":
                        config["creatorId"],
                    "web": True,
                },
            )
            return

        if path == "/api/snapshot":
            self.send_json(
                200,
                STATE.snapshot(),
            )
            return

        if path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            revision = -1

            try:
                while True:
                    new_revision = STATE.wait_for_change(
                        revision,
                        timeout=12.0,
                    )

                    if new_revision == revision:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue

                    revision = new_revision
                    payload = json.dumps(
                        STATE.snapshot(),
                        ensure_ascii=False,
                    )

                    event = (
                        "event: snapshot\n"
                        "data: " + payload + "\n\n"
                    ).encode("utf-8")

                    self.wfile.write(event)
                    self.wfile.flush()

            except (
                BrokenPipeError,
                ConnectionResetError,
                OSError,
            ):
                pass
            return

        if path == "/api/downloader/jobs":
            self.send_json(200, {"jobs": STATE.list_download_jobs()})
            return

        match = re.fullmatch(r"/api/downloader/jobs/([0-9a-fA-F-]+)/zip", path)
        if match:
            job = STATE.get_download_job(match.group(1))
            if not job:
                self.send_json(404, {"error": "Unknown downloader job."})
                return
            zip_path = Path(str(job.get("zipPath") or ""))
            if not zip_path.is_file():
                self.send_json(409, {"error": "ZIP is not ready yet."})
                return
            self._send_bytes(200, zip_path.read_bytes(), "application/zip", {"Content-Disposition": f'attachment; filename="{zip_path.name}"'})
            return

        if path == "/api/config":
            self.send_json(
                200,
                STATE.get_config_public(),
            )
            return

        if path == "/api/jobs":
            self.send_json(
                200,
                {
                    "jobs":
                        STATE.list_jobs(),
                    "history":
                        STATE.history,
                },
            )
            return

        match = re.fullmatch(
            r"/jobs/([0-9a-fA-F-]+)",
            path,
        )

        if match:
            job = STATE.get_job(
                match.group(1)
            )

            if job is None:
                self.send_json(
                    404,
                    {
                        "ok": False,
                        "error":
                            "Unknown job.",
                    },
                )
            else:
                self.send_json(
                    200,
                    job,
                )
            return

        match = re.fullmatch(
            r"/api/jobs/([0-9a-fA-F-]+)/diagnostics",
            path,
        )

        if match:
            job = STATE.get_job(
                match.group(1)
            )

            if job is None:
                self.send_json(
                    404,
                    {
                        "error":
                            "Unknown job."
                    },
                )
                return

            payload = {
                "tower":
                    job["tower"],
                "state":
                    job["state"],
                "mapping":
                    job.get(
                        "mapping",
                        {},
                    ),
                "errors":
                    job.get(
                        "errors",
                        {},
                    ),
                "fetchSources":
                    job.get(
                        "fetchSources",
                        {},
                    ),
                "assets":
                    job.get(
                        "assets",
                        {},
                    ),
            }

            raw = json.dumps(
                payload,
                indent=2,
                ensure_ascii=False,
            ).encode("utf-8")

            self._send_bytes(
                200,
                raw,
                "application/json; charset=utf-8",
                {
                    "Content-Disposition":
                        'attachment; filename="migration-diagnostics.json"'
                },
            )
            return

        self.send_json(
            404,
            {
                "error":
                    "Not found."
            },
        )

    # --------------------------------------------------------
    # POST
    # --------------------------------------------------------

    def do_POST(self):
        path = urlparse(
            self.path
        ).path

        try:
            payload = self.read_json()

            if path == "/sources/batch":
                job_id = str(
                    payload.get(
                        "jobId"
                    )
                    or ""
                ).strip()

                items = payload.get(
                    "items"
                )

                if not job_id:
                    raise ValueError(
                        "Batch source jobId is missing."
                    )

                if not isinstance(
                    items,
                    list,
                ):
                    raise ValueError(
                        "Batch source items must be a list."
                    )

                if len(items) > 32:
                    raise ValueError(
                        "Batch source request is too large."
                    )

                direct_count = 0
                fallback_count = 0

                for item in items:
                    if not isinstance(
                        item,
                        dict,
                    ):
                        continue

                    asset_id = str(
                        item.get(
                            "assetId"
                        )
                        or ""
                    ).strip()

                    if not re.fullmatch(
                        r"\d+",
                        asset_id,
                    ):
                        continue

                    success = bool(
                        item.get(
                            "success"
                        )
                    )

                    error = str(
                        item.get(
                            "error"
                        )
                        or ""
                    )

                    if success:
                        rbxmx = item.get(
                            "rbxmx"
                        )

                        if (
                            not isinstance(
                                rbxmx,
                                str,
                            )
                            or not rbxmx.lstrip().startswith(
                                "<roblox"
                            )
                        ):
                            success = False
                            error = (
                                "Studio source payload "
                                "was not valid RBXMX."
                            )
                        else:
                            with (
                                STATE.source_cache_lock
                            ):
                                STATE.source_cache[
                                    asset_id
                                ] = rbxmx

                    STATE.mark_prepare_item(
                        job_id,
                        asset_id,
                        success,
                        error,
                    )

                    if success:
                        direct_count += 1
                    else:
                        fallback_count += 1

                self.send_json(
                    200,
                    {
                        "ok": True,
                        "received": (
                            direct_count
                            + fallback_count
                        ),
                        "direct": direct_count,
                        "fallback": fallback_count,
                    },
                )
                return

            if path == "/source":
                asset_id = str(
                    payload.get(
                        "assetId"
                    )
                    or ""
                ).strip()

                rbxmx = payload.get(
                    "rbxmx"
                )

                if not re.fullmatch(
                    r"\d+",
                    asset_id,
                ):
                    raise ValueError(
                        "Source assetId "
                        "must be numeric."
                    )

                if (
                    not isinstance(
                        rbxmx,
                        str,
                    )
                    or not rbxmx.lstrip().startswith(
                        "<roblox"
                    )
                ):
                    raise ValueError(
                        "Source payload "
                        "is not valid RBXMX."
                    )

                with (
                    STATE.source_cache_lock
                ):
                    STATE.source_cache[
                        asset_id
                    ] = rbxmx

                job_id = str(
                    payload.get(
                        "jobId"
                    )
                    or ""
                ).strip()

                if job_id:
                    try:
                        STATE.mark_prepare_item(
                            job_id,
                            asset_id,
                            True,
                        )
                    except Exception:
                        pass

                self.send_json(
                    200,
                    {
                        "ok": True,
                        "assetId":
                            asset_id,
                    },
                )
                return

            if path == "/jobs/prepare":
                job_id = (
                    STATE.prepare_job(
                        payload
                    )
                )

                self.send_json(
                    202,
                    {
                        "ok": True,
                        "jobId": job_id,
                    },
                )
                return

            match = re.fullmatch(
                r"/jobs/([0-9a-fA-F-]+)/prepare-item",
                path,
            )

            if match:
                STATE.mark_prepare_item(
                    match.group(1),
                    str(
                        payload.get(
                            "assetId"
                        )
                        or ""
                    ),
                    bool(
                        payload.get(
                            "success"
                        )
                    ),
                    str(
                        payload.get(
                            "error"
                        )
                        or ""
                    ),
                )

                self.send_json(
                    200,
                    {
                        "ok": True,
                    },
                )
                return

            match = re.fullmatch(
                r"/jobs/([0-9a-fA-F-]+)/start",
                path,
            )

            if match:
                STATE.start_prepared_job(
                    match.group(1)
                )

                self.send_json(
                    202,
                    {
                        "ok": True,
                        "jobId":
                            match.group(1),
                    },
                )
                return

            if path == "/migrate":
                job_id = (
                    STATE.create_job(
                        payload
                    )
                )

                self.send_json(
                    202,
                    {
                        "ok": True,
                        "jobId": job_id,
                    },
                )
                return

            if path == "/api/downloader/start":
                job_id = STATE.create_download_job(payload)
                self.send_json(202, {"ok": True, "jobId": job_id})
                return

            match = re.fullmatch(r"/api/downloader/jobs/([0-9a-fA-F-]+)/retry", path)
            if match:
                job_id = STATE.retry_download_job(match.group(1), payload)
                self.send_json(202, {"ok": True, "jobId": job_id})
                return

            match = re.fullmatch(r"/api/downloader/jobs/([0-9a-fA-F-]+)/priority", path)
            if match:
                updated = STATE.set_download_priority(match.group(1), str(payload.get("priority") or ""))
                self.send_json(200, {"ok": True, "job": updated})
                return

            match = re.fullmatch(r"/api/downloader/jobs/([0-9a-fA-F-]+)/(pause|resume)", path)
            if match:
                updated = STATE.pause_download_job(match.group(1), match.group(2) == "pause")
                self.send_json(200, {"ok": True, "job": updated})
                return

            if path == "/api/downloader/open-root":
                folder = STATE.open_download_root()
                self.send_json(200, {"ok": True, "path": folder})
                return

            match = re.fullmatch(r"/api/downloader/jobs/([0-9a-fA-F-]+)/open-folder", path)
            if match:
                folder = STATE.open_download_job_folder(match.group(1))
                self.send_json(200, {"ok": True, "path": folder})
                return

            if path == "/api/rbxdl/auth":
                STATE.authenticate_rbxdl_async()
                self.send_json(
                    202,
                    {
                        "ok": True,
                        "message": "RBXDL session check started.",
                    },
                )
                return

            if path == "/api/config":
                value = (
                    STATE.save_config(
                        payload
                    )
                )
                self.send_json(
                    200,
                    value,
                )
                return

            if path == "/api/open-root":
                folder = (
                    STATE.open_root_folder()
                )
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "path": folder,
                    },
                )
                return

            match = re.fullmatch(
                r"/api/jobs/([0-9a-fA-F-]+)/open-folder",
                path,
            )

            if match:
                folder = (
                    STATE.open_job_folder(
                        match.group(1)
                    )
                )

                self.send_json(
                    200,
                    {
                        "ok": True,
                        "path": folder,
                    },
                )
                return

            match = re.fullmatch(
                r"/api/jobs/([0-9a-fA-F-]+)/retry",
                path,
            )

            if match:
                job_id = (
                    STATE.retry_failed(
                        match.group(1)
                    )
                )

                self.send_json(
                    202,
                    {
                        "ok": True,
                        "jobId": job_id,
                    },
                )
                return

            self.send_json(
                404,
                {
                    "error":
                        "Not found."
                },
            )

        except Exception as exc:
            self.send_json(
                400,
                {
                    "ok": False,
                    "error": str(exc),
                },
            )


def run():
    core.APP_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    server = ThreadingHTTPServer(
        (
            HOST,
            DEFAULT_PORT,
        ),
        Handler,
    )

    url = (
        f"http://{HOST}:"
        f"{DEFAULT_PORT}/"
    )

    print("")
    print(
        "Tower Animation Migrator Web"
    )
    print(
        "----------------------------"
    )
    print(
        f"Dashboard: {url}"
    )
    print(
        "Studio API: same port"
    )
    print(
        f"Migrations: {core.MIGRATIONS_DIR}"
    )
    print(
        f"Private data: {core.APP_DIR}"
    )
    print(
        "Close this window to stop the helper."
    )
    print("")

    threading.Timer(
        0.8,
        lambda:
            webbrowser.open(url),
    ).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
