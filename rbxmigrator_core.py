from __future__ import annotations
import base64
import ctypes
from concurrent.futures import ThreadPoolExecutor, as_completed
import http.cookiejar
import json
import os
import queue
import requests
import secrets
import re
import threading
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from ctypes import wintypes
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk


APP_TITLE = "RbxMigrator v3.37 — migration preflight lock"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765

ASSETS_BASE_URL = "https://apis.roblox.com/assets/v1"
CREATE_URL = f"{ASSETS_BASE_URL}/assets"
ASSET_DELIVERY_V2 = "https://assetdelivery.roblox.com/v2/assetId/{asset_id}"
ASSET_DELIVERY_V1 = "https://assetdelivery.roblox.com/v1/asset/?id={asset_id}"
RBXDL_BASE_URL = "https://rbxdl.johnmarctumulak.com"
RBXDL_DOWNLOAD_URL = RBXDL_BASE_URL + "/download/asset"

REQUEST_TIMEOUT = 90
OPERATION_TIMEOUT = 180
POLL_DELAY = 0.75
RBXDL_MIN_DOWNLOAD_INTERVAL = 8.5
RBXDL_RATE_LIMIT_RETRY_PADDING = 0.75
MAX_FILE_BYTES = 20 * 1024 * 1024
RBXDL_PORTAL_DASHBOARD = "https://rbxdl.johnmarctumulak.com/portal/dashboard"
RBXDL_PORTAL_LOGIN_TIMEOUT_SECONDS = 5 * 60
RBXDL_PORTAL_BATCH_TIMEOUT_SECONDS = 20 * 60

# Public single-downloader fallback concurrency.
#
# This is NOT a private/bulk endpoint. It simply runs several normal RBXDL
# single-download browser flows concurrently. Eight is a fast default without
# going completely wild with browser processes. Override with:
#
#   set TM_SINGLE_WORKERS=4
#
# before launching the helper if you want a lighter mode.
try:
    RBXDL_SINGLE_WORKERS = max(
        1,
        min(
            12,
            int(os.environ.get("TM_SINGLE_WORKERS", "4")),
        ),
    )
except Exception:
    RBXDL_SINGLE_WORKERS = 8

# Persistent/private app data stays in LocalAppData.
#
# With Microsoft Store Python, LocalAppData may be virtualized under
# AppData\Local\Packages\PythonSoftwareFoundation...\LocalCache\Local.
# That is fine for private settings/session state, because the app itself
# reads/writes it consistently.
APP_DIR = Path(
    os.environ.get("LOCALAPPDATA")
    or os.environ.get("APPDATA")
    or Path.home()
) / "RobloxTowerAnimationMigrator"

# User-facing migration output should NOT live in virtualized AppData.
# Put it beside this Python file instead:
#
#   F:\Roblox Bulk Uploader\
#       RbxMigrator.py
#       rbxmigrator_core.py
#       migrations\
#
WORKSPACE_DIR = Path(__file__).resolve().parent

SETTINGS_FILE = APP_DIR / "settings.json"
MIGRATIONS_DIR = WORKSPACE_DIR / "migrations"
RBXDL_PORTAL_PROFILE_DIR = APP_DIR / "rbxdl_portal_profile"
RBXDL_SESSION_FILE = APP_DIR / "rbxdl_portal_session.json"

# Create the two roots independently:
# - APP_DIR: encrypted/private settings and RBXDL session/profile
# - MIGRATIONS_DIR: visible migration artifacts beside the app
APP_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

MIGRATIONS_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


# ============================================================
# Windows DPAPI
# ============================================================

class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _make_blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    return blob, buffer


def protect_secret(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Secure key saving is only supported on Windows.")

    input_blob, keepalive = _make_blob(value.encode("utf-8"))
    output_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    ok = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "Roblox Tower Animation Migrator",
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    )
    _ = keepalive

    if not ok:
        raise ctypes.WinError()

    try:
        raw = ctypes.string_at(
            output_blob.pbData,
            output_blob.cbData,
        )
        return base64.b64encode(raw).decode("ascii")
    finally:
        if output_blob.pbData:
            kernel32.LocalFree(output_blob.pbData)


def unprotect_secret(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Secure key loading is only supported on Windows.")

    encrypted = base64.b64decode(value)
    input_blob, keepalive = _make_blob(encrypted)
    output_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    )
    _ = keepalive

    if not ok:
        raise ctypes.WinError()

    try:
        raw = ctypes.string_at(
            output_blob.pbData,
            output_blob.cbData,
        )
        return raw.decode("utf-8")
    finally:
        if output_blob.pbData:
            kernel32.LocalFree(output_blob.pbData)


def load_settings() -> dict:
    try:
        if SETTINGS_FILE.is_file():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_settings_file(data: dict):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(SETTINGS_FILE)


# ============================================================
# General helpers
# ============================================================

def safe_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:100] or "Animation"


def suggested_animation_name(entry: dict) -> str:
    relative = str(entry.get("relativePath") or "")
    parts = [p for p in relative.split(".") if p]

    if len(parts) >= 2:
        parent = parts[-2]
        leaf = parts[-1]

        if leaf.isdigit():
            return safe_filename(f"{parent}_{leaf}")

    name = str(entry.get("name") or "")
    if name and not name.isdigit():
        return safe_filename(name)

    if parts:
        return safe_filename("_".join(parts[-2:]))

    return "Animation"


def suggested_asset_name(entry: dict, asset_type: str = "Animation") -> str:
    """Choose a readable upload name for any supported migrator asset type."""
    if asset_type == "Animation":
        return suggested_animation_name(entry)

    relative = str(entry.get("relativePath") or "")
    parts = [part for part in relative.split(".") if part]
    name = str(entry.get("name") or "").strip()

    if name and not name.isdigit():
        return safe_filename(name)

    if parts:
        return safe_filename("_".join(parts[-2:]))

    return safe_filename(asset_type or "Asset")


# ============================================================
# HTTP helpers
# ============================================================

def parse_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""

    prefix = f"{exc.code} {exc.reason}"

    if not body.strip():
        return prefix

    try:
        data = json.loads(body)

        if isinstance(data, dict):
            if data.get("message"):
                return f"{prefix} - {data['message']}"

            error = data.get("error")
            if isinstance(error, dict) and error.get("message"):
                return f"{prefix} - {error['message']}"

            errors = data.get("errors")
            if errors:
                return f"{prefix} - {json.dumps(errors, ensure_ascii=False)}"
    except Exception:
        pass

    return f"{prefix} - {body[:700]}"


def open_bytes(request_or_url, timeout=REQUEST_TIMEOUT):
    try:
        with urllib.request.urlopen(
            request_or_url,
            timeout=timeout,
        ) as response:
            return (
                response.read(),
                response.headers.get_content_type(),
                response.geturl(),
            )
    except urllib.error.HTTPError as exc:
        raise RuntimeError(parse_http_error(exc)) from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from None


def open_json(
    url: str,
    api_key: str,
    *,
    method="GET",
    body=None,
    content_type=None,
):
    headers = {
        "x-api-key": api_key,
        "Accept": "application/json",
        "User-Agent": "RobloxTowerAnimationMigrator/OneClick",
    }

    if content_type:
        headers["Content-Type"] = content_type

    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method,
    )

    raw, _, _ = open_bytes(request)

    if not raw.strip():
        return {}

    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as exc:
        raise RuntimeError(f"Roblox returned invalid JSON: {exc}") from None


# ============================================================
# Roblox asset download/upload
# ============================================================

def _guess_asset_extension(
    data: bytes,
    content_type: str = "",
    url: str = "",
    asset_type: str | None = None,
) -> str:
    stripped = data.lstrip()
    lower_type = (content_type or "").lower()
    lower_url = (url or "").lower()

    # Audio signatures / MIME types.
    if data.startswith(b"ID3") or (
        len(data) >= 2
        and data[0] == 0xFF
        and (data[1] & 0xE0) == 0xE0
    ) or "audio/mpeg" in lower_type or lower_url.endswith(".mp3"):
        return ".mp3"

    if data.startswith(b"OggS") or "audio/ogg" in lower_type or lower_url.endswith(".ogg"):
        return ".ogg"

    if (
        len(data) >= 12
        and data[:4] == b"RIFF"
        and data[8:12] == b"WAVE"
    ) or "audio/wav" in lower_type or lower_url.endswith(".wav"):
        return ".wav"

    if data.startswith(b"fLaC") or "audio/flac" in lower_type or lower_url.endswith(".flac"):
        return ".flac"

    # Roblox native mesh content begins with a "version X.XX" header.
    # Unlike FBX/OBJ, this can be uploaded as raw Mesh through Open Cloud.
    if (
        (content_type or "").lower().startswith("model/x-file-mesh-data")
        or re.match(rb"^version [1-9]\.[0-9]{2}\r?\n", data[:32])
        or lower_url.endswith(".mesh")
    ):
        return ".mesh"

    # Roblox model / animation formats.
    if stripped.startswith(b"<roblox"):
        return ".rbxmx"

    if "xml" in lower_type or lower_url.endswith(".rbxmx"):
        return ".rbxmx"

    if lower_url.endswith(".rbxm"):
        return ".rbxm"

    if asset_type == "Mesh":
        # Do NOT pretend arbitrary bytes are .mesh. Invalid formats must fail
        # before upload instead of producing a misleading new asset ID.
        return ".bin"

    if asset_type == "Audio":
        # Unknown audio should not be mislabeled as an RBXM. Keep it generic so
        # the caller can report a useful unsupported-format error.
        return ".bin"

    return ".rbxm"


def _guess_animation_extension(
    data: bytes,
    content_type: str = "",
    url: str = "",
) -> str:
    return _guess_asset_extension(
        data,
        content_type,
        url,
        "Animation",
    )


def _extract_candidate_urls(value):
    """Yield likely download/status URLs found anywhere in a JSON response."""
    if isinstance(value, dict):
        preferred = (
            "downloadUrl",
            "downloadURL",
            "download_url",
            "fileUrl",
            "fileURL",
            "file_url",
            "location",
            "url",
            "href",
            "statusUrl",
            "statusURL",
            "status_url",
            "pollUrl",
            "pollURL",
            "poll_url",
        )

        seen = set()

        for key in preferred:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                seen.add(candidate.strip())
                yield candidate.strip()

        for key, candidate in value.items():
            if key in preferred:
                continue

            if isinstance(candidate, (dict, list)):
                yield from _extract_candidate_urls(candidate)
            elif isinstance(candidate, str):
                lowered_key = str(key).lower()
                candidate = candidate.strip()

                if candidate and (
                    "url" in lowered_key
                    or "download" in lowered_key
                    or "location" in lowered_key
                    or "href" in lowered_key
                ):
                    if candidate not in seen:
                        yield candidate

    elif isinstance(value, list):
        for item in value:
            yield from _extract_candidate_urls(item)


def _normalise_rbxdl_url(value: str) -> str:
    return urllib.parse.urljoin(
        RBXDL_BASE_URL + "/",
        value,
    )


def _looks_like_json(data: bytes, content_type: str) -> bool:
    lower_type = (content_type or "").lower()
    stripped = data.lstrip()

    return (
        "json" in lower_type
        or stripped.startswith(b"{")
        or stripped.startswith(b"[")
    )


def _rbxdl_open(opener, request):
    try:
        with opener.open(
            request,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            return (
                response.read(),
                response.headers.get_content_type(),
                response.geturl(),
            )
    except urllib.error.HTTPError as exc:
        raise RuntimeError(parse_http_error(exc)) from None
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Network error: {exc.reason}"
        ) from None


def _rbxdl_follow_result(
    opener,
    payload,
    *,
    max_hops: int = 8,
):
    """
    Handles the common RBXDL pattern:
    POST -> JSON containing a signed/one-time URL -> GET -> file.
    It also tolerates a status/poll URL that eventually exposes the download URL.
    """
    current = payload

    for _ in range(max_hops):
        if isinstance(current, dict):
            error = (
                current.get("error")
                or current.get("message")
            )

            success_value = current.get("success")
            if success_value is False and error:
                raise RuntimeError(str(error))

            # Some services return base64 file content directly.
            for key in (
                "base64",
                "fileBase64",
                "file_base64",
                "dataBase64",
                "data_base64",
            ):
                encoded = current.get(key)
                if isinstance(encoded, str) and encoded:
                    try:
                        data = base64.b64decode(encoded)
                    except Exception:
                        continue

                    if data:
                        return (
                            data,
                            _guess_animation_extension(data),
                            "RBXDL",
                        )

        urls = list(_extract_candidate_urls(current))

        if not urls:
            raise RuntimeError(
                "RBXDL responded, but no downloadable URL was found. "
                f"Response: {json.dumps(current, ensure_ascii=False)[:700]}"
            )

        last_error = None

        for candidate in urls:
            url = _normalise_rbxdl_url(candidate)

            # Never recursively call the original POST endpoint as a GET.
            if url.rstrip("/") == RBXDL_DOWNLOAD_URL.rstrip("/"):
                continue

            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "Accept": "*/*",
                        "Referer": RBXDL_BASE_URL + "/",
                        "Origin": RBXDL_BASE_URL,
                        "User-Agent":
                            "Mozilla/5.0 RobloxTowerAnimationMigrator/OneClick",
                    },
                    method="GET",
                )

                data, content_type, final_url = _rbxdl_open(
                    opener,
                    request,
                )

                if not data:
                    raise RuntimeError(
                        "RBXDL returned an empty response."
                    )

                if _looks_like_json(data, content_type):
                    current = json.loads(
                        data.decode(
                            "utf-8",
                            errors="replace",
                        )
                    )

                    # A status endpoint may still be processing.
                    status_text = ""
                    if isinstance(current, dict):
                        status_text = str(
                            current.get("status")
                            or current.get("state")
                            or ""
                        ).lower()

                    if status_text in {
                        "pending",
                        "processing",
                        "queued",
                        "working",
                    }:
                        time.sleep(0.75)

                    break

                return (
                    data,
                    _guess_animation_extension(
                        data,
                        content_type,
                        final_url,
                    ),
                    "RBXDL",
                )

            except Exception as exc:
                last_error = exc
        else:
            if last_error:
                raise RuntimeError(str(last_error))
            raise RuntimeError(
                "RBXDL did not provide a usable download URL."
            )

    raise RuntimeError(
        "RBXDL download did not finish in time."
    )



class _RBXDLSingleRateGate:
    """Shared adaptive RBXDL Single cadence, priority/FIFO tickets and restart state.

    An atomic reservation is made *before* a browser clicks Download. This
    prevents another browser from slipping through the interval that existed
    between wait_for_turn() and note_download_started() in older versions.
    Server-provided cooldowns extend the common deadline, never shorten it.
    """
    STATE_FILE = APP_DIR / "rbxdl-single-scheduler.json"

    def __init__(self):
        self.lock = threading.Condition(threading.RLock())
        self.learned_spacing = max(0.5, float(os.environ.get("TM_SINGLE_INITIAL_GAP", "2.0")))
        self.next_allowed_at = 0.0
        self.rate_limit_events = 0
        self._sequence = 0
        self._tickets = {}
        self._last_saved = 0.0
        self._load()

    def _load(self):
        try:
            data = json.loads(self.STATE_FILE.read_text(encoding="utf-8"))
            self.learned_spacing = max(self.learned_spacing, min(120.0, float(data.get("spacing", 0))))
            remaining = max(0.0, min(120.0, float(data.get("nextAllowedEpoch", 0)) - time.time()))
            self.next_allowed_at = time.monotonic() + remaining
            self.rate_limit_events = max(0, int(data.get("rateLimitEvents", 0)))
        except (OSError, ValueError, TypeError):
            pass

    def _persist(self):
        remaining = max(0.0, self.next_allowed_at - time.monotonic())
        data = {"spacing": self.learned_spacing,
                "nextAllowedEpoch": time.time() + remaining,
                "rateLimitEvents": self.rate_limit_events}
        try:
            self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            temp = self.STATE_FILE.with_name(self.STATE_FILE.name + ".tmp")
            temp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(temp, self.STATE_FILE)
        except OSError:
            pass

    def wait_for_turn(self, priority=1):
        """Reserve the earliest legal click time; higher priority wins waiting ties.

        Priority 2 = high, 1 = normal, 0 = low. The deadline is reserved
        atomically, so several ready workers cannot click at once.
        """
        with self.lock:
            self._sequence += 1
            ticket = self._sequence
            self._tickets[ticket] = max(0, min(2, int(priority)))
            try:
                while True:
                    chosen = min(self._tickets, key=lambda t: (-self._tickets[t], t))
                    wait = max(0.0, self.next_allowed_at - time.monotonic())
                    if ticket == chosen and wait <= 0:
                        self.next_allowed_at = time.monotonic() + self.learned_spacing
                        del self._tickets[ticket]
                        self._persist()
                        self.lock.notify_all()
                        return
                    self.lock.wait(timeout=min(max(wait, 0.1), 1.0))
            finally:
                if ticket in self._tickets:
                    del self._tickets[ticket]
                    self.lock.notify_all()

    def note_download_started(self):
        # Reservation already recorded atomically by wait_for_turn().
        pass

    def penalize(self, wait_seconds):
        seconds = max(0.0, min(120.0, float(wait_seconds)))
        with self.lock:
            self.rate_limit_events += 1
            self.learned_spacing = max(self.learned_spacing, min(120.0, seconds + RBXDL_RATE_LIMIT_RETRY_PADDING))
            self.next_allowed_at = max(self.next_allowed_at, time.monotonic() + seconds + RBXDL_RATE_LIMIT_RETRY_PADDING)
            self._persist()
            self.lock.notify_all()

    def snapshot(self):
        with self.lock:
            return {"spacing": round(self.learned_spacing, 2),
                    "rateLimitEvents": self.rate_limit_events,
                    "wait": round(max(0.0, self.next_allowed_at - time.monotonic()), 1),
                    "waitingWorkers": len(self._tickets)}


_RBXDL_SINGLE_RATE_GATE = _RBXDLSingleRateGate()

class RBXDLBrowserFetcher:
    """
    Drives RBXDL through a real installed browser (Microsoft Edge) so RBXDL's
    own frontend can obtain and consume its fresh single-use security token.

    This deliberately does NOT forge/reuse RBXDL tokens and does NOT use a
    Roblox .ROBLOSECURITY cookie.
    """

    def __init__(
        self,
        headless: bool = True,
        min_download_interval: float = 0.0,
        priority: int = 1,
    ):
        self.priority = max(0, min(2, int(priority)))
        self.playwright = None
        self.browser = None
        self.context = None
        self.last_download_started_at = 0.0
        self.headless = bool(headless)
        self.min_download_interval = max(
            0.0,
            float(min_download_interval),
        )

    def start(self):
        if self.browser is not None:
            return

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "RBXDL browser fallback requires Playwright. "
                "Run the updated generator, or install it with: "
                "py -m pip install --user playwright"
            ) from exc

        self.playwright = sync_playwright().start()

        launch_errors = []

        for channel in ("msedge", "chrome"):
            try:
                self.browser = self.playwright.chromium.launch(
                    channel=channel,
                    headless=self.headless,
                    args=[
                        "--disable-background-timer-throttling",
                    ],
                )
                break
            except Exception as exc:
                launch_errors.append(
                    f"{channel}: {exc}"
                )

        if self.browser is None:
            try:
                # Works if the user already has Playwright's Chromium
                # installed. The generator intentionally does not force a
                # browser download because Edge is normally already present
                # on Windows.
                self.browser = self.playwright.chromium.launch(
                    headless=self.headless,
                    args=[],
                )
            except Exception as exc:
                launch_errors.append(
                    f"playwright chromium: {exc}"
                )

        if self.browser is None:
            self.close()
            raise RuntimeError(
                "Could not launch a browser for RBXDL. "
                + " | ".join(launch_errors)
            )

        self.context = self.browser.new_context(
            accept_downloads=True,
            viewport={
                "width": 1280,
                "height": 800,
            },
        )

    def close(self):
        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                pass
            self.context = None

        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = None

        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
            self.playwright = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    @staticmethod
    def _first_visible(locator):
        try:
            count = locator.count()
        except Exception:
            return None

        for index in range(count):
            candidate = locator.nth(index)

            try:
                if candidate.is_visible():
                    return candidate
            except Exception:
                continue

        return None

    @staticmethod
    def _page_diagnostics(page):
        """Small, actionable UI snapshot; never print cookies or page HTML."""
        try:
            location = str(page.url).split("?", 1)[0][:180]
        except Exception:
            location = "unknown"
        try:
            title = str(page.title())[:90]
        except Exception:
            title = "unknown"
        try:
            labels = page.locator("button").all_text_contents(timeout=2_000)
            buttons = ", ".join(
                " ".join(str(label).split())[:35]
                for label in labels[:8]
                if str(label).strip()
            )[:180]
        except Exception:
            buttons = "unavailable"
        try:
            message = " ".join(
                page.locator("body").inner_text(timeout=2_000).split()
            )[:200]
        except Exception:
            message = "unavailable"
        return (
            f"URL={location}; title={title!r}; "
            f"buttons={buttons!r}; page_text={message!r}"
        )

    def _find_asset_input(self, page):
        """Use an explicit asset-ID input; never fill a random login/search box."""
        selectors = [
            'input[placeholder*="6894586021"]',
            'input[placeholder*="Asset ID"]',
            'input[placeholder*="asset id"]',
            'input[aria-label*="Asset ID"]',
            'input[id="assetId"]',
            'input[name="assetId"]',
            'input[inputmode="numeric"]',
            'input[type="number"]',
        ]
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            for selector in selectors:
                candidate = self._first_visible(page.locator(selector))
                if candidate is not None:
                    return candidate
            # Generic fallback is safe only when there is exactly one visible
            # text input. Sites sometimes add an unrelated login/search box.
            try:
                generic = page.locator('input[type="text"]')
                visible = [generic.nth(i) for i in range(generic.count())
                           if generic.nth(i).is_visible()]
                if len(visible) == 1:
                    return visible[0]
            except Exception:
                pass
            if page.is_closed():
                raise RuntimeError("RBXDL browser page closed before the asset input appeared.")
            page.wait_for_timeout(300)

        raise RuntimeError(
            "RBXDL_UI_NOT_READY: Asset ID input unavailable after 15 seconds. "
            + self._page_diagnostics(page)
        )

    def _find_download_button(self, page):
        """Wait for a visible, enabled Single Download control."""
        candidates = [
            page.get_by_role(
                "button", name=re.compile(r"^\s*Download\s*$", re.IGNORECASE),
            ),
            page.get_by_role(
                "button", name=re.compile(r"^\s*Download(?:\s+Asset)?\s*$", re.IGNORECASE),
            ),
            page.locator('button:has-text("Download")'),
            page.locator('[data-testid="download-button"]'),
            page.locator('input[type="submit"][value*="Download"]'),
            page.locator('input[type="button"][value*="Download"]'),
            page.locator('[role="button"][aria-label*="Download"]'),
        ]
        deadline = time.monotonic() + 15.0
        seen_disabled = False
        while time.monotonic() < deadline:
            for locator in candidates:
                button = self._first_visible(locator)
                if button is None:
                    continue
                try:
                    if not button.is_enabled():
                        seen_disabled = True
                        continue
                except Exception:
                    continue
                return button
            if page.is_closed():
                raise RuntimeError("RBXDL browser page closed before the Download button appeared.")
            page.wait_for_timeout(300)

        note = " (button visible but disabled)" if seen_disabled else " (button absent)"
        raise RuntimeError(
            "RBXDL_UI_NOT_READY: Single Download button unavailable after 15 seconds"
            + note + ". This is a website/browser-session failure, NOT proof "
            "that the asset does not exist. " + self._page_diagnostics(page)
        )

    def _find_save_control(self, page):
        candidates = [
            page.get_by_role(
                "link",
                name=re.compile(
                    r"Save\s*File",
                    re.IGNORECASE,
                ),
            ),
            page.get_by_role(
                "button",
                name=re.compile(
                    r"Save\s*File",
                    re.IGNORECASE,
                ),
            ),
            page.locator(
                'a:has-text("Save File")'
            ),
            page.locator(
                'button:has-text("Save File")'
            ),
            page.locator(
                'a[download]'
            ),
        ]

        for locator in candidates:
            control = self._first_visible(locator)

            if control is not None:
                return control

        return None

    def _wait_for_rbxdl_cooldown(self):
        if self.last_download_started_at <= 0:
            return

        elapsed = time.monotonic() - self.last_download_started_at
        remaining = self.min_download_interval - elapsed

        if remaining > 0:
            time.sleep(remaining)

    @staticmethod
    def _extract_wait_seconds(body_text: str):
        match = re.search(
            r"please\s+wait\s+(\d+(?:\.\d+)?)s?\s+before\s+downloading\s+another\s+asset",
            body_text,
            flags=re.IGNORECASE,
        )

        if not match:
            return None

        try:
            return float(match.group(1))
        except Exception:
            return None

    def _reset_browser(self):
        self.close()
        time.sleep(0.5)
        self.start()

    def _fetch_once(self, asset_id: str):
        self.start()

        page = self.context.new_page()
        observed_downloads = []

        page.on(
            "download",
            lambda download:
                observed_downloads.append(download),
        )

        try:
            page.goto(
                RBXDL_BASE_URL,
                wait_until="domcontentloaded",
                timeout=90_000,
            )

            # The RBXDL page uses client-side UI. Wait for controls below
            # instead of assuming a fixed 1.2 s delay is sufficient.
            asset_input = self._find_asset_input(page)
            asset_input.fill(str(asset_id))

            download_button = self._find_download_button(page)

            # Local spacing prevents one worker from spamming itself.
            self._wait_for_rbxdl_cooldown()

            # RBXDL's public Single Downloader cooldown is effectively shared
            # across the client/IP. Coordinate ALL worker browsers here.
            _RBXDL_SINGLE_RATE_GATE.wait_for_turn(priority=self.priority)

            download_button.click()
            self.last_download_started_at = time.monotonic()
            _RBXDL_SINGLE_RATE_GATE.note_download_started()

            deadline = time.monotonic() + 120

            while time.monotonic() < deadline:
                if page.is_closed():
                    raise RuntimeError(
                        "RBXDL page closed while preparing the download."
                    )

                if observed_downloads:
                    download = observed_downloads.pop(0)
                    break

                save_control = self._find_save_control(page)

                if save_control is not None:
                    try:
                        with page.expect_download(
                            timeout=90_000
                        ) as download_info:
                            save_control.click()

                        download = download_info.value
                        break
                    except Exception:
                        if observed_downloads:
                            download = observed_downloads.pop(0)
                            break

                        if page.is_closed():
                            raise RuntimeError(
                                "RBXDL page closed during the download step."
                            )

                try:
                    body_text = page.locator(
                        "body"
                    ).inner_text(
                        timeout=2_000
                    )
                except Exception as exc:
                    if page.is_closed():
                        raise RuntimeError(
                            "RBXDL page closed while checking status."
                        ) from exc
                    body_text = ""

                wait_seconds = self._extract_wait_seconds(
                    body_text
                )

                if wait_seconds is not None:
                    # Learn this server-side cooldown globally so every other
                    # worker stops clicking Download until the same deadline.
                    _RBXDL_SINGLE_RATE_GATE.penalize(
                        wait_seconds
                    )

                    # Do not recurse using a page that we intentionally close.
                    # Tell the outer retry loop to create a fresh page/token.
                    raise RuntimeError(
                        f"RBXDL_RATE_LIMIT:{wait_seconds}"
                    )

                lowered = body_text.lower()

                error_markers = [
                    "single-use security token is invalid",
                    "asset unavailable",
                    "failed to download",
                    "not authorized",
                ]

                matched_error = next(
                    (
                        marker
                        for marker in error_markers
                        if marker in lowered
                    ),
                    None,
                )

                if matched_error:
                    raise RuntimeError(
                        "RBXDL browser flow reported an error: "
                        + matched_error
                    )

                page.wait_for_timeout(400)
            else:
                raise RuntimeError(
                    "Timed out waiting for RBXDL's Save File/download step."
                )

            suggested = (
                download.suggested_filename
                or f"asset_{asset_id}.bin"
            )

            suffix = Path(suggested).suffix.lower()

            with tempfile.TemporaryDirectory(
                prefix="rbxdl_"
            ) as temp_dir:
                destination = (
                    Path(temp_dir)
                    / safe_filename(suggested)
                )

                download.save_as(
                    str(destination)
                )

                data = destination.read_bytes()

            if not data:
                raise RuntimeError(
                    "RBXDL browser download returned an empty file."
                )

            if suffix not in {
                ".rbxm",
                ".rbxmx",
                ".mesh",
                ".mp3",
                ".ogg",
                ".wav",
                ".flac",
            }:
                suffix = _guess_asset_extension(
                    data,
                    "",
                    suggested,
                )

            return (
                data,
                suffix,
                "RBXDL browser",
            )

        finally:
            try:
                if not page.is_closed():
                    page.close()
            except Exception:
                pass

    def fetch(
        self,
        asset_id: str,
        *,
        retry_rate_limits_forever: bool = False,
        on_rate_limit=None,
    ):
        """
        Fetch one asset through RBXDL's public Single Downloader.

        Normal browser/navigation failures remain bounded to four attempts.

        When retry_rate_limits_forever=True, ONLY RBXDL_RATE_LIMIT responses
        are retried indefinitely. This is intentionally used by the standalone
        website Downloader, where a temporary cooldown should never become a
        final failed asset.

        Other callers retain bounded rate-limit retries by default. Web TM
        v3.28 opts migration jobs into unbounded *rate-limit-only* retries;
        ordinary browser and asset errors remain bounded.
        """
        last_error = None
        normal_attempt = 0
        rate_limit_count = 0

        while True:
            try:
                return self._fetch_once(
                    asset_id
                )

            except Exception as exc:
                message = str(exc)
                last_error = exc

                rate_match = re.search(
                    r"RBXDL_RATE_LIMIT:(\d+(?:\.\d+)?)",
                    message,
                )

                if rate_match:
                    rate_limit_count += 1
                    server_wait = float(
                        rate_match.group(1)
                    )

                    # _fetch_once already teaches the process-wide rate gate
                    # about the server-reported cooldown. Calling penalize()
                    # again is harmless but unnecessarily extends the wait,
                    # so just wait on the shared deadline here.
                    if on_rate_limit is not None:
                        try:
                            on_rate_limit(
                                server_wait,
                                rate_limit_count,
                            )
                        except Exception:
                            pass

                    # _fetch_once() reserves its next click slot itself.
                    # Do NOT reserve a second slot here just to wait, or
                    # one throttled retry consumes two cooldown windows.
                    if retry_rate_limits_forever:
                        continue

                    # Preserve the old bounded behavior for migration mode:
                    # a rate-limit retry consumes one of the four attempts.
                    normal_attempt += 1

                    if normal_attempt < 4:
                        continue

                    break

                normal_attempt += 1
                lowered = message.lower()

                # Client-side loading/layout changes can temporarily hide
                # controls. Retry only those pre-download UI errors, with a
                # fresh page on each attempt. Do NOT retry access-denied or
                # other permanent asset failures as though they were UI bugs.
                if "rbxdl_ui_not_ready:" in lowered:
                    if normal_attempt < 3:
                        time.sleep(min(normal_attempt, 2))
                        if normal_attempt == 2:
                            self._reset_browser()
                        continue
                    raise RuntimeError(
                        "RBXDL's Single Downloader UI remained unavailable "
                        "after 3 page loads. Check the site manually for "
                        "outages, login/consent screens or layout changes. "
                        f"Last browser diagnostic: {message}"
                    ) from exc

                # The server says its one-use token is invalid. A single
                # ordinary fresh-page retry is safe; do not forge or reuse
                # security tokens, and do not loop forever on auth failures.
                if "single-use security token is invalid" in lowered:
                    if normal_attempt < 2:
                        time.sleep(1)
                        continue
                    raise

                if (
                    "target page, context or browser has been closed"
                    in lowered
                    or "browser has been closed"
                    in lowered
                    or "context has been closed"
                    in lowered
                    or "page closed"
                    in lowered
                ):
                    if normal_attempt < 4:
                        self._reset_browser()
                        continue

                raise

        raise RuntimeError(
            f"RBXDL browser failed after retries: {last_error}"
        )


def fetch_animation_asset_rbxdl(
    asset_id: str,
    browser_fetcher: RBXDLBrowserFetcher | None = None,
):
    owns_fetcher = browser_fetcher is None

    if owns_fetcher:
        browser_fetcher = RBXDLBrowserFetcher()

    try:
        return browser_fetcher.fetch(
            asset_id
        )
    finally:
        if owns_fetcher:
            browser_fetcher.close()



def fetch_asset_roblox(
    asset_id: str,
    asset_type: str = "Animation",
    timeout: float = REQUEST_TIMEOUT,
):
    errors = []

    for url in (
        ASSET_DELIVERY_V2.format(asset_id=asset_id),
        ASSET_DELIVERY_V1.format(asset_id=asset_id),
    ):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "*/*",
                    "User-Agent":
                        "RbxMigrator/OneClick",
                },
            )

            data, content_type, final_url = open_bytes(
                request, timeout=timeout
            )

            if _looks_like_json(data, content_type):
                metadata = json.loads(
                    data.decode(
                        "utf-8",
                        errors="replace",
                    )
                )

                location = None

                if isinstance(metadata, dict):
                    direct = metadata.get("location")
                    if isinstance(direct, str):
                        location = direct

                    locations = metadata.get("locations")
                    if (
                        not location
                        and isinstance(locations, list)
                    ):
                        for candidate in locations:
                            if (
                                isinstance(candidate, dict)
                                and isinstance(
                                    candidate.get("location"),
                                    str,
                                )
                            ):
                                location = candidate["location"]
                                break

                if not location:
                    raise RuntimeError(
                        "Asset Delivery returned no downloadable location."
                    )

                data, content_type, final_url = open_bytes(
                    urllib.request.Request(
                        location,
                        headers={
                            "User-Agent":
                                "RbxMigrator/OneClick"
                        },
                    ), timeout=timeout
                )

            if not data:
                raise RuntimeError(
                    "Asset Delivery returned an empty file."
                )

            extension = _guess_asset_extension(
                data,
                content_type,
                final_url,
                asset_type,
            )

            return (
                data,
                extension,
                "Roblox",
            )

        except Exception as exc:
            errors.append(str(exc))

    raise RuntimeError(
        "Roblox Asset Delivery failed: "
        + " | ".join(errors)
    )


def fetch_animation_asset_roblox(asset_id: str):
    return fetch_asset_roblox(
        asset_id,
        "Animation",
    )


def fetch_animation_asset_from_studio(
    asset_id: str,
    source_cache: dict | None,
):
    if source_cache is None:
        raise RuntimeError(
            "No Studio source cache was provided."
        )

    rbxmx = source_cache.get(asset_id)

    if not isinstance(rbxmx, str) or not rbxmx:
        raise RuntimeError(
            "Studio did not provide source animation data."
        )

    data = rbxmx.encode("utf-8")

    return (
        data,
        ".rbxmx",
        "Roblox Studio AnimationClipProvider",
    )


def fetch_animation_asset(
    asset_id: str,
    browser_fetcher: RBXDLBrowserFetcher | None = None,
    source_cache: dict | None = None,
):
    """
    Fetch order:
      1. Roblox Studio AnimationClipProvider source sent by the plugin.
      2. Official public Roblox Asset Delivery.
      3. RBXDL through its normal browser flow.

    The first path is the "regular Roblox" authenticated Studio path. It works
    only when the current Studio session is actually allowed to load the
    animation. No .ROBLOSECURITY cookie is requested or stored.
    """
    studio_error = None
    official_error = None

    try:
        return fetch_animation_asset_from_studio(
            asset_id,
            source_cache,
        )
    except Exception as exc:
        studio_error = exc

    try:
        return fetch_animation_asset_roblox(
            asset_id
        )
    except Exception as exc:
        official_error = exc

    try:
        return fetch_animation_asset_rbxdl(
            asset_id,
            browser_fetcher,
        )
    except Exception as fallback_error:
        raise RuntimeError(
            "Studio fetch failed: "
            f"{studio_error} | "
            "Official fetch failed: "
            f"{official_error} | "
            "RBXDL browser fallback failed: "
            f"{fallback_error}"
        ) from None



def _upload_content_type(
    file_path: Path,
    asset_type: str,
) -> str:
    suffix = file_path.suffix.lower()

    by_suffix = {
        ".rbxm": "model/x-rbxm",
        ".rbxmx": "model/x-rbxm",
        ".mesh": "model/x-file-mesh-data",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
    }

    if asset_type == "Mesh":
        if suffix not in {".mesh", ".bin"}:
            raise RuntimeError(
                f"Mesh asset requires native Roblox .mesh data, got {suffix!r}. "
                "Do not upload OBJ/FBX/RBXM as a raw Mesh asset."
            )
        header = file_path.read_bytes()[:32]
        if not re.match(rb"^version [1-9]\.[0-9]{2}\r?\n", header):
            raise RuntimeError(
                "Not a native Roblox .mesh file (missing mesh version header). "
                "Only unmodified Roblox Asset Delivery mesh bytes can be uploaded."
            )
        return "model/x-file-mesh-data"

    content_type = by_suffix.get(suffix)

    if content_type:
        return content_type

    if asset_type == "Animation":
        return "model/x-rbxm"

    if asset_type == "Audio":
        guessed = _guess_asset_extension(
            file_path.read_bytes()[:64],
            "",
            file_path.name,
            "Audio",
        )
        guessed_type = by_suffix.get(guessed)
        if guessed_type:
            return guessed_type

    raise RuntimeError(
        f"Unsupported {asset_type} upload format: {suffix or 'no extension'}"
    )


def build_multipart(
    request_json: str,
    file_path: Path,
    file_content_type: str = "model/x-rbxm",
):
    boundary = "----RobloxTowerAsset" + uuid.uuid4().hex
    b = boundary.encode("ascii")
    content = file_path.read_bytes()

    body = b"".join([
        b"--" + b + b"\r\n",
        b'Content-Disposition: form-data; name="request"\r\n',
        b"Content-Type: application/json; charset=utf-8\r\n\r\n",
        request_json.encode("utf-8"),
        b"\r\n",
        b"--" + b + b"\r\n",
        (
            f'Content-Disposition: form-data; name="fileContent"; '
            f'filename="{file_path.name}"\r\n'
        ).encode("utf-8"),
        (f"Content-Type: {file_content_type}\r\n\r\n").encode("ascii"),
        content,
        b"\r\n",
        b"--" + b + b"--\r\n",
    ])

    return body, f"multipart/form-data; boundary={boundary}"


def create_asset(
    api_key: str,
    file_path: Path,
    display_name: str,
    creator_type: str,
    creator_id: str,
    description: str,
    asset_type: str = "Animation",
):
    if asset_type not in {"Animation", "Audio", "Mesh"}:
        raise RuntimeError(
            f"Unsupported migrator asset type: {asset_type}"
        )

    creator = (
        {"groupId": creator_id}
        if creator_type == "Group"
        else {"userId": creator_id}
    )

    request_data = {
        "assetType": asset_type,
        "displayName": display_name,
        "description": description,
        "creationContext": {
            "creator": creator,
        },
    }

    body, content_type = build_multipart(
        json.dumps(request_data, separators=(",", ":")),
        file_path,
        _upload_content_type(
            file_path,
            asset_type,
        ),
    )

    result = open_json(
        CREATE_URL,
        api_key,
        method="POST",
        body=body,
        content_type=content_type,
    )

    operation_path = (
        result.get("path")
        if isinstance(result, dict)
        else None
    )

    if not operation_path:
        raise RuntimeError(
            "Roblox did not return an operation path."
        )

    return str(operation_path)


def create_animation(
    api_key: str,
    file_path: Path,
    display_name: str,
    creator_type: str,
    creator_id: str,
    description: str,
):
    return create_asset(
        api_key,
        file_path,
        display_name,
        creator_type,
        creator_id,
        description,
        "Animation",
    )


def wait_for_operation(
    api_key: str,
    operation_path: str,
):
    if operation_path.startswith(("http://", "https://")):
        url = operation_path
    else:
        url = f"{ASSETS_BASE_URL}/{operation_path.lstrip('/')}"

    deadline = time.monotonic() + OPERATION_TIMEOUT

    while time.monotonic() < deadline:
        operation = open_json(url, api_key)

        if not isinstance(operation, dict):
            raise RuntimeError("Unexpected Roblox operation response.")

        if operation.get("done") is True:
            if operation.get("error"):
                raise RuntimeError(
                    "Roblox operation failed: "
                    + json.dumps(
                        operation["error"],
                        ensure_ascii=False,
                    )
                )

            status = operation.get("status")
            if isinstance(status, dict) and (
                status.get("code") or status.get("message")
            ):
                raise RuntimeError(
                    "Roblox operation failed: "
                    f"{status.get('code', '')} "
                    f"{status.get('message', '')}".strip()
                )

            response = operation.get("response")

            if isinstance(response, dict):
                asset_id = response.get("assetId")
                if asset_id:
                    return str(asset_id)

                path = response.get("path")
                if path:
                    match = re.search(
                        r"assets/(\d+)",
                        str(path),
                    )
                    if match:
                        return match.group(1)

            raise RuntimeError(
                "Upload finished but no new asset ID was returned."
            )

        time.sleep(POLL_DELAY)

    raise RuntimeError(
        "Timed out waiting for Roblox to process the upload."
    )


# ============================================================
# Desktop app
# ============================================================


class RBXDLBulkUnavailable(RuntimeError):
    """RBXDL bulk portal cannot be used; caller should fall back to single."""


ROLHUB_API_BASE = "https://rbxdl.rolhubstudios.com/api/v1"


class RolhubApiClient:
    def __init__(self, api_key, status_callback=None, item_callback=None, file_ready_callback=None):
        self.api_key = str(api_key or "").strip()
        self.status_callback = status_callback
        self.item_callback = item_callback
        self.file_ready_callback = file_ready_callback
        self.session = requests.Session()
        self.session.headers.update({"Authorization": "Bearer " + self.api_key})

    def status(self, message):
        if self.status_callback:
            try:
                self.status_callback(message)
            except Exception:
                pass

    def item(self, asset_id, status, **meta):
        if self.item_callback:
            try:
                self.item_callback(str(asset_id), status, meta)
            except Exception:
                pass

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass

    def _request(self, method, path, **kwargs):
        for _ in range(6):
            response = self.session.request(method, ROLHUB_API_BASE + path, timeout=REQUEST_TIMEOUT, **kwargs)
            if response.status_code == 429:
                try:
                    wait = float(response.headers.get("Retry-After", "5"))
                except ValueError:
                    wait = 5.0
                self.status(f"Rolhub rate limited; waiting {wait:.0f}s")
                time.sleep(min(max(wait, 1.0), 120.0))
                continue
            return response
        raise RuntimeError("Rolhub rate limit did not clear after retries.")

    def download_batch(self, asset_ids, output_dir):
        asset_ids = list(dict.fromkeys(str(a) for a in asset_ids))
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        mapping, errors = {}, {}

        if not self.api_key:
            return mapping, {a: "No Rolhub API key configured." for a in asset_ids}

        for start in range(0, len(asset_ids), 500):
            chunk = asset_ids[start:start + 500]
            try:
                self._run_chunk(chunk, output_dir / f"chunk-{start // 500 + 1:03d}", mapping, errors)
            except Exception as exc:
                for a in chunk:
                    if a not in mapping:
                        errors[a] = f"Rolhub API: {exc}"
                        self.item(a, "failed", source="Rolhub API", error=errors[a])
        return mapping, errors

    def _run_chunk(self, chunk, work_dir, mapping, errors):
        work_dir.mkdir(parents=True, exist_ok=True)
        self.status(f"Rolhub API: submitting {len(chunk)} asset(s)...")
        response = self._request("POST", "/jobs", json={"ids": [int(a) for a in chunk]})
        if response.status_code == 401:
            raise RuntimeError("Rolhub API key was rejected (401).")
        if not response.ok:
            raise RuntimeError(f"job create failed: HTTP {response.status_code} {response.text[:300]}")
        job_id = response.json()["Job"]["Job_Id"]

        deadline = time.monotonic() + 30 * 60
        job = {}
        while time.monotonic() < deadline:
            poll = self._request("GET", f"/jobs/{job_id}")
            if not poll.ok:
                raise RuntimeError(f"job poll failed: HTTP {poll.status_code}")
            job = poll.json().get("Job", {})
            counts = job.get("Counts", {})
            self.status(
                f"Rolhub API: {counts.get('Done', 0)}/{counts.get('Total', len(chunk))} done, "
                f"{counts.get('Failed', 0)} failed"
            )
            if job.get("State") in ("finished", "cancelled"):
                break
            time.sleep(3)
        else:
            raise RuntimeError("Timed out waiting for Rolhub job.")

        zip_response = self._request("GET", f"/jobs/{job_id}/zip", stream=True)
        if not zip_response.ok:
            raise RuntimeError(f"zip download failed: HTTP {zip_response.status_code}")
        zip_path = work_dir / "rolhub.zip"
        with zip_path.open("wb") as handle:
            for part in zip_response.iter_content(chunk_size=256 * 1024):
                if part:
                    handle.write(part)

        extracted = work_dir / "extracted"
        RBXDLPortalDownloader._safe_extract_zip(zip_path, extracted)

        for path in extracted.rglob("rolhubstudios-rbxdl-*"):
            if not path.is_file() or path.suffix.lower() == ".txt":
                continue
            match = re.search(r"rolhubstudios-rbxdl-(\d+)", path.name)
            if not match or match.group(1) not in chunk:
                continue
            asset_id = match.group(1)
            if asset_id in mapping or path.stat().st_size == 0:
                continue
            mapping[asset_id] = path
            if self.file_ready_callback:
                self.file_ready_callback(asset_id, path, "Rolhub API")
            self.item(asset_id, "ready", source="Rolhub API", filename=path.name, size=path.stat().st_size)

        failures = job.get("Failures") or []
        if isinstance(failures, dict):
            failures = [{"Id": k, **(v if isinstance(v, dict) else {"Message": str(v)})} for k, v in failures.items()]
        for failure in failures:
            asset_id = str(failure.get("Id") or failure.get("Asset_Id") or "")
            if asset_id and asset_id not in mapping:
                reason = failure.get("Reason", "failed")
                message = failure.get("Message", "")
                errors[asset_id] = f"Rolhub API {reason}: {message}".strip()
                self.item(asset_id, "failed", source="Rolhub API", error=errors[asset_id])

        for asset_id in chunk:
            if asset_id not in mapping and asset_id not in errors:
                errors[asset_id] = "Rolhub API did not return this asset."


class RBXDLPortalDownloader:
    # If RBXDL explicitly says this account's Beta access is expired/not
    # active, there is no point hammering the Portal again for every asset.
    # Trip this once and every later job in the same helper process goes
    # straight to the parallel single-downloader fallback.
    _portal_circuit_lock = threading.RLock()
    _portal_disabled_reason = None

    @classmethod
    def _trip_portal_circuit(cls, reason: str):
        reason = str(reason or "RBXDL Portal unavailable.")

        with cls._portal_circuit_lock:
            if not cls._portal_disabled_reason:
                cls._portal_disabled_reason = reason

    @classmethod
    def _portal_circuit_reason(cls):
        with cls._portal_circuit_lock:
            return cls._portal_disabled_reason

    @staticmethod
    def _looks_beta_expired_text(text: str) -> bool:
        lowered = str(text or "").lower()

        return (
            "beta access has expired" in lowered
            or "beta access expired" in lowered
            or (
                "beta access" in lowered
                and "not active" in lowered
            )
            or (
                "redeem" in lowered
                and "beta key" in lowered
            )
        )

    """
    Browserless-by-default RBXDL Creator Portal client.

    Normal migrations do NOT open a browser. The helper restores the saved
    RBXDL/Discord session, GETs /portal/dashboard to obtain a fresh CSRF token,
    and calls the same bulk endpoints used by RBXDL's frontend directly.

    Edge/Chrome only opens when the saved session or Key Access is no longer
    valid. The user signs in / renews access, the helper captures the new
    session cookies, closes the browser, and continues the migration.

    Bulk endpoints:
      POST /bulk/fetch-asset
      POST /bulk/complete
      POST /bulk/download-zip
      POST /bulk/release-lock
    """

    MAX_CONCURRENCY = 4
    RATE_LIMIT_COOLDOWN = 4.5
    RETRY_DELAY = 3.5
    MAX_RATE_RETRIES = 4
    MAX_OTHER_RETRIES = 2

    def __init__(
        self,
        status_callback=None,
        item_callback=None,
        file_ready_callback=None,
    ):
        self.status_callback = status_callback
        self.item_callback = item_callback
        # Called only AFTER the complete asset file exists on disk.
        self.file_ready_callback = file_ready_callback
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/154 Safari/537.36"
                ),
                "Accept": "application/json, text/html, */*",
                "Referer": RBXDL_PORTAL_DASHBOARD,
            }
        )
        self.csrf_token = ""
        self.playwright = None
        self.context = None
        self.page = None
        self._auth_lock = threading.RLock()
        self._load_saved_session()

    # --------------------------------------------------------
    # Callbacks / status
    # --------------------------------------------------------

    def status(self, message: str):
        if self.status_callback is not None:
            try:
                self.status_callback(message)
            except Exception:
                pass

    def item_status(
        self,
        asset_id: str,
        status: str,
        **metadata,
    ):
        if self.item_callback is not None:
            try:
                self.item_callback(
                    str(asset_id),
                    status,
                    metadata,
                )
            except Exception:
                pass

    def notify_file_ready(self, asset_id, file_path, source):
        """Publish a completed file for streaming upload, if enabled."""
        if self.file_ready_callback is not None:
            self.file_ready_callback(str(asset_id), Path(file_path), str(source))

    # --------------------------------------------------------
    # Persistent HTTP session
    # --------------------------------------------------------

    def _load_saved_session(self):
        if not RBXDL_SESSION_FILE.is_file():
            return

        try:
            data = json.loads(
                RBXDL_SESSION_FILE.read_text(
                    encoding="utf-8"
                )
            )
        except Exception:
            return

        cookies = data.get("cookies")
        if not isinstance(cookies, list):
            return

        for item in cookies:
            if not isinstance(item, dict):
                continue

            name = item.get("name")
            value = item.get("value")

            if not name or value is None:
                continue

            try:
                cookie = requests.cookies.create_cookie(
                    name=str(name),
                    value=str(value),
                    domain=str(
                        item.get("domain")
                        or "rbxdl.johnmarctumulak.com"
                    ).lstrip("."),
                    path=str(item.get("path") or "/"),
                    secure=bool(item.get("secure", True)),
                    expires=(
                        int(item["expires"])
                        if item.get("expires")
                        not in (None, -1, 0)
                        else None
                    ),
                    rest={
                        "HttpOnly": bool(
                            item.get("httpOnly", False)
                        )
                    },
                )
                self.session.cookies.set_cookie(cookie)
            except Exception:
                continue

    def _save_session(self):
        APP_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        cookies = []

        for cookie in self.session.cookies:
            cookies.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "secure": bool(cookie.secure),
                    "expires": cookie.expires,
                }
            )

        RBXDL_SESSION_FILE.write_text(
            json.dumps(
                {
                    "savedAt": int(time.time()),
                    "cookies": cookies,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _import_browser_cookies(self, cookies):
        self.session.cookies.clear()

        for item in cookies:
            name = item.get("name")
            value = item.get("value")

            if not name or value is None:
                continue

            domain = str(
                item.get("domain")
                or "rbxdl.johnmarctumulak.com"
            ).lstrip(".")

            try:
                cookie = requests.cookies.create_cookie(
                    name=str(name),
                    value=str(value),
                    domain=domain,
                    path=str(item.get("path") or "/"),
                    secure=bool(item.get("secure", True)),
                    expires=(
                        int(item["expires"])
                        if item.get("expires")
                        not in (None, -1, 0)
                        else None
                    ),
                    rest={
                        "HttpOnly": bool(
                            item.get("httpOnly", False)
                        )
                    },
                )
                self.session.cookies.set_cookie(cookie)
            except Exception:
                continue

        self._save_session()

    @staticmethod
    def _extract_csrf(html: str) -> str:
        patterns = (
            r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']csrf-token["\']',
        )

        for pattern in patterns:
            match = re.search(
                pattern,
                html,
                flags=re.IGNORECASE,
            )

            if match:
                return match.group(1)

        return ""

    @staticmethod
    def _looks_logged_out(response) -> bool:
        url = str(response.url or "").lower()
        text = (response.text or "").lower()

        return (
            "/portal/login" in url
            or "/auth/discord" in url
            or "sign in with discord" in text
            or "login with discord" in text
        )

    @classmethod
    def _looks_access_expired(cls, response) -> bool:
        text = (response.text or "").lower()

        return (
            cls._looks_beta_expired_text(text)
            or "key access expired" in text
            or "access expired" in text
            or "key access (0d)" in text
        )

    def _refresh_http_session(self) -> tuple[bool, str]:
        try:
            response = self.session.get(
                RBXDL_PORTAL_DASHBOARD,
                timeout=30,
                allow_redirects=True,
            )
        except Exception as exc:
            return False, f"RBXDL dashboard request failed: {exc}"

        self._save_session()

        if self._looks_logged_out(response):
            return False, "RBXDL login is required."

        if self._looks_access_expired(response):
            response_text = response.text or ""

            if self._looks_beta_expired_text(response_text):
                reason = (
                    "RBXDL Beta access has expired or is not active. "
                    "Portal is disabled for this helper session; "
                    "using parallel single downloads instead."
                )
                self._trip_portal_circuit(reason)
                return False, reason

            return False, "RBXDL Key Access has expired."

        if response.status_code >= 400:
            return (
                False,
                f"RBXDL dashboard returned HTTP {response.status_code}.",
            )

        csrf = self._extract_csrf(
            response.text or ""
        )

        if not csrf:
            return False, "RBXDL dashboard did not expose a CSRF token."

        self.csrf_token = csrf
        return True, "RBXDL session ready."

    # --------------------------------------------------------
    # Interactive auth fallback
    # --------------------------------------------------------

    def _browser_context_alive(self) -> bool:
        if self.context is None:
            return False

        try:
            _ = self.context.pages
            return True
        except Exception:
            return False

    @staticmethod
    def _page_alive(page) -> bool:
        if page is None:
            return False

        try:
            return not page.is_closed()
        except Exception:
            return False

    def _open_auth_browser(
        self,
        headless: bool = False,
    ):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "RBXDL login requires Playwright. Install it with: "
                "py -m pip install --user playwright"
            ) from exc

        RBXDL_PORTAL_PROFILE_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.playwright = sync_playwright().start()
        errors = []

        for channel in ("msedge", "chrome"):
            try:
                self.context = (
                    self.playwright.chromium.launch_persistent_context(
                        user_data_dir=str(
                            RBXDL_PORTAL_PROFILE_DIR
                        ),
                        channel=channel,
                        headless=headless,
                        accept_downloads=False,
                        viewport={
                            "width": 1450,
                            "height": 900,
                        },
                    )
                )
                break
            except Exception as exc:
                errors.append(
                    f"{channel}: {exc}"
                )

        if self.context is None:
            raise RuntimeError(
                "Could not launch Edge/Chrome for RBXDL login. "
                + " | ".join(errors)
            )

        pages = [
            page
            for page in self.context.pages
            if self._page_alive(page)
        ]

        self.page = (
            pages[-1]
            if pages
            else self.context.new_page()
        )

        self.page.goto(
            RBXDL_PORTAL_DASHBOARD,
            wait_until="domcontentloaded",
            timeout=90_000,
        )

    def _sync_session_from_auth_browser(
        self,
    ) -> tuple[bool, str]:
        """
        Copy the current persistent browser cookies into requests.Session and
        immediately verify bulk access over HTTP.

        This deliberately does NOT depend on a particular dashboard textbox or
        button being present. If RBXDL changes its UI, login detection still
        works as long as the authenticated dashboard/session itself works.
        """
        if not self._browser_context_alive():
            return (
                False,
                "RBXDL authentication browser is not available.",
            )

        try:
            cookies = self.context.cookies(
                [RBXDL_BASE_URL]
            )
        except Exception as exc:
            return (
                False,
                "Could not read RBXDL browser cookies: "
                + str(exc),
            )

        if cookies:
            self._import_browser_cookies(
                cookies
            )

        return self._refresh_http_session()

    def _recover_session_from_saved_profile(
        self,
    ) -> tuple[bool, str]:
        """
        If the visible auth window gets closed after Discord/RBXDL has already
        written its cookies to the persistent profile, reopen that same profile
        invisibly, import the cookies, verify access, then close it again.

        This lets the workflow recover even when the OAuth/login page closes
        itself or the user closes Edge immediately after seeing a success page.
        """
        try:
            self._close_auth_browser()
        except Exception:
            pass

        try:
            self._open_auth_browser(
                headless=True
            )

            ok, reason = (
                self._sync_session_from_auth_browser()
            )

            return ok, reason

        except Exception as exc:
            return (
                False,
                "Could not recover RBXDL session from the saved browser profile: "
                + str(exc),
            )

        finally:
            try:
                self._close_auth_browser()
            except Exception:
                pass

    def _close_auth_browser(self):
        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                pass

        self.context = None
        self.page = None

        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass

        self.playwright = None

    def _interactive_auth(self):
        with self._auth_lock:
            self.status(
                "RBXDL session/Key Access needs refreshing — "
                "opening Edge for login/access renewal..."
            )

            self._open_auth_browser(
                headless=False
            )

            deadline = (
                time.monotonic()
                + RBXDL_PORTAL_LOGIN_TIMEOUT_SECONDS
            )

            last_reason = (
                "Waiting for RBXDL login/Key Access."
            )
            last_status_at = 0.0
            reopen_count = 0

            try:
                while time.monotonic() < deadline:
                    # The important test is the authenticated HTTP session,
                    # not whether some specific dashboard control is visible.
                    if self._browser_context_alive():
                        ok, reason = (
                            self._sync_session_from_auth_browser()
                        )

                        last_reason = reason

                        if ok:
                            self.status(
                                "RBXDL login/Key Access verified — "
                                "browser is no longer needed."
                            )
                            return

                        now = time.monotonic()

                        if (
                            now - last_status_at
                            >= 3.0
                        ):
                            self.status(
                                "Waiting for RBXDL login/Key Access to become usable: "
                                + str(reason)
                            )
                            last_status_at = now

                        time.sleep(0.75)
                        continue

                    # OAuth pages and users can close the visible window. The
                    # persistent profile may already contain the successful
                    # cookies, so check it silently before reopening anything.
                    self.status(
                        "RBXDL auth window closed — checking the saved login session..."
                    )

                    ok, reason = (
                        self._recover_session_from_saved_profile()
                    )

                    last_reason = reason

                    if ok:
                        self.status(
                            "RBXDL login/Key Access recovered from the saved session — "
                            "continuing in bulk mode."
                        )
                        return

                    reopen_count += 1

                    if reopen_count > 2:
                        raise RuntimeError(
                            "RBXDL authentication window closed before bulk access "
                            "could be verified. Last check: "
                            + str(reason)
                        )

                    self.status(
                        "RBXDL session is not ready yet — reopening Edge. "
                        "Finish login/Key Access and leave the window open until "
                        "the migrator confirms it."
                    )

                    self._open_auth_browser(
                        headless=False
                    )

                    time.sleep(0.75)

                raise RuntimeError(
                    "Timed out waiting for RBXDL login/Key Access renewal. "
                    "Last check: "
                    + str(last_reason)
                )

            finally:
                self._close_auth_browser()

    def ensure_authenticated_session(self):
        ok, reason = self._refresh_http_session()

        if ok:
            self.status(
                "RBXDL session valid — continuing without opening a browser."
            )
            return

        self.status(reason)
        self._interactive_auth()

    def close(self):
        """
        Public cleanup hook used by the web migration worker.

        Most migrations are browserless, so this is usually just a cheap
        requests.Session cleanup. If an interactive RBXDL login/Key Access
        browser happens to still be open, close it safely too.
        """
        try:
            self._close_auth_browser()
        except Exception:
            pass

        try:
            self.session.close()
        except Exception:
            pass

    # --------------------------------------------------------
    # Authenticated portal requests
    # --------------------------------------------------------

    def _request_headers(self):
        return {
            "Accept": "application/json",
            "X-CSRF-TOKEN": self.csrf_token,
            "Referer": RBXDL_PORTAL_DASHBOARD,
            "Origin": RBXDL_BASE_URL,
        }

    def _auth_related_response(self, response) -> bool:
        text = ""

        try:
            payload = response.json()

            if isinstance(payload, dict):
                text = str(
                    payload.get("message")
                    or payload.get("error")
                    or payload
                )
            else:
                text = str(payload or "")
        except Exception:
            text = response.text or ""

        lowered = text.lower()

        # Important: RBXDL can report expired Beta access with a response code
        # other than the old 401/403/419 paths. Detect the actual message first.
        if self._looks_beta_expired_text(lowered):
            self._trip_portal_circuit(
                "RBXDL Beta access has expired or is not active. "
                "Portal is disabled for this helper session."
            )
            return True

        if response.status_code in (401, 419):
            return True

        if response.status_code != 403:
            return False

        return any(
            phrase in lowered
            for phrase in (
                "key access",
                "access expired",
                "unauthenticated",
                "csrf",
                "token mismatch",
                "login required",
            )
        )

    def _refresh_or_reauth(self):
        ok, _ = self._refresh_http_session()

        if ok:
            return

        self._interactive_auth()

    def _clone_worker_session(self):
        session = requests.Session()
        session.headers.update(
            self.session.headers
        )

        for cookie in self.session.cookies:
            session.cookies.set_cookie(
                requests.cookies.create_cookie(
                    name=cookie.name,
                    value=cookie.value,
                    domain=cookie.domain,
                    path=cookie.path,
                    secure=bool(cookie.secure),
                    expires=cookie.expires,
                )
            )

        return session

    def _fetch_asset_once(
        self,
        session,
        asset_id: str,
        batch_id: str,
    ):
        response = session.post(
            RBXDL_BASE_URL + "/bulk/fetch-asset",
            json={
                "assetId": asset_id,
                "batchId": batch_id,
            },
            headers=self._request_headers(),
            timeout=REQUEST_TIMEOUT,
        )

        if self._auth_related_response(response):
            return {
                "authRefresh": True,
                "status": response.status_code,
            }

        payload = None

        try:
            payload = response.json()
        except Exception:
            payload = None

        if response.ok:
            return {
                "success": True,
                "status": response.status_code,
                "filename": (
                    payload.get("filename")
                    if isinstance(payload, dict)
                    else None
                )
                or f"asset_{asset_id}.bin",
                "size": (
                    payload.get("size", 0)
                    if isinstance(payload, dict)
                    else 0
                ),
                "privacy": (
                    payload.get("privacy", "Public")
                    if isinstance(payload, dict)
                    else "Public"
                ),
            }

        message = ""

        if isinstance(payload, dict):
            message = str(
                payload.get("message")
                or payload.get("error")
                or ""
            )

        if not message:
            message = (
                response.text.strip()
                or f"HTTP {response.status_code}"
            )

        return {
            "success": False,
            "status": response.status_code,
            "message": message,
        }

    def _fetch_assets_bulk(
        self,
        asset_ids: list[str],
        batch_id: str,
    ):
        items = {
            asset_id: {
                "id": asset_id,
                "status": "pending",
                "retries": 0,
                "retryAfter": 0.0,
                "filename": None,
                "size": 0,
                "privacy": "Public",
                "error": None,
            }
            for asset_id in asset_ids
        }

        pending = deque(asset_ids)
        lock = threading.RLock()
        condition = threading.Condition(lock)
        active = 0
        cooldown_until = 0.0
        auth_refresh_needed = False
        stop = False

        def emit(asset_id: str, status: str, **extra):
            self.item_status(
                asset_id,
                status,
                **extra,
            )

        for asset_id in asset_ids:
            emit(asset_id, "queued")

        def worker():
            nonlocal active
            nonlocal cooldown_until
            nonlocal auth_refresh_needed
            nonlocal stop

            session = self._clone_worker_session()

            while True:
                with condition:
                    if stop:
                        return

                    now = time.monotonic()

                    if cooldown_until > now:
                        condition.wait(
                            timeout=min(
                                0.5,
                                cooldown_until - now,
                            )
                        )
                        continue

                    chosen = None
                    queue_len = len(pending)

                    for _ in range(queue_len):
                        asset_id = pending.popleft()
                        item = items[asset_id]

                        if item["retryAfter"] > now:
                            pending.append(asset_id)
                            continue

                        chosen = asset_id
                        break

                    if chosen is None:
                        if active == 0 and not pending:
                            return

                        # Pending items exist but are waiting for retryAfter.
                        if pending:
                            soonest = min(
                                items[asset_id]["retryAfter"]
                                for asset_id in pending
                            )
                            wait_for = max(
                                0.05,
                                min(
                                    0.5,
                                    soonest - now,
                                ),
                            )
                            condition.wait(
                                timeout=wait_for
                            )
                            continue

                        condition.wait(
                            timeout=0.2
                        )
                        continue

                    item = items[chosen]
                    item["status"] = "active"
                    active += 1

                emit(
                    chosen,
                    "fetching",
                    retries=item["retries"],
                )

                time.sleep(0.06)

                try:
                    result = self._fetch_asset_once(
                        session,
                        chosen,
                        batch_id,
                    )
                except Exception as exc:
                    result = {
                        "success": False,
                        "status": 0,
                        "message": str(exc),
                    }

                with condition:
                    active -= 1

                    if result.get("authRefresh"):
                        item["status"] = "pending"
                        pending.appendleft(chosen)
                        auth_refresh_needed = True
                        stop = True
                        condition.notify_all()
                        continue

                    if result.get("success"):
                        item.update(
                            {
                                "status": "success",
                                "filename": result.get("filename"),
                                "size": int(result.get("size") or 0),
                                "privacy": result.get("privacy") or "Public",
                                "error": None,
                            }
                        )

                        emit(
                            chosen,
                            "ready",
                            filename=item["filename"],
                            size=item["size"],
                            privacy=item["privacy"],
                        )

                        condition.notify_all()
                        continue

                    status_code = int(
                        result.get("status") or 0
                    )
                    message = str(
                        result.get("message")
                        or "Asset unavailable."
                    )

                    lowered = message.lower()
                    is_rate_limit = (
                        status_code == 429
                        or "rate limit" in lowered
                        or "too many" in lowered
                    )
                    is_skipped = (
                        status_code == 422
                        or "place files" in lowered
                    )
                    is_non_retryable = (
                        is_skipped
                        or status_code == 403
                        or "restricted asset" in lowered
                        or "asset unavailable" in lowered
                    )

                    if is_skipped:
                        item["status"] = "skipped"
                        item["error"] = message
                        emit(
                            chosen,
                            "skipped",
                            error=message,
                        )
                        condition.notify_all()
                        continue

                    if is_rate_limit:
                        cooldown_until = max(
                            cooldown_until,
                            time.monotonic()
                            + self.RATE_LIMIT_COOLDOWN,
                        )

                    max_retries = (
                        self.MAX_RATE_RETRIES
                        if is_rate_limit
                        else self.MAX_OTHER_RETRIES
                    )

                    if (
                        not is_non_retryable
                        and item["retries"] < max_retries
                    ):
                        item["retries"] += 1
                        item["status"] = "pending"
                        item["retryAfter"] = (
                            time.monotonic()
                            + self.RETRY_DELAY
                        )
                        item["error"] = message
                        pending.append(chosen)

                        emit(
                            chosen,
                            "retrying",
                            retries=item["retries"],
                            error=message,
                        )
                    else:
                        item["status"] = "failed"
                        item["error"] = message
                        emit(
                            chosen,
                            "failed",
                            error=message,
                        )

                    condition.notify_all()

        # The first attempt may discover that the saved session expired after
        # dashboard validation. Re-authenticate once and resume the exact queue.
        auth_attempts = 0

        while True:
            with condition:
                stop = False
                auth_refresh_needed = False

            threads = [
                threading.Thread(
                    target=worker,
                    daemon=True,
                )
                for _ in range(
                    min(
                        self.MAX_CONCURRENCY,
                        max(1, len(asset_ids)),
                    )
                )
            ]

            for thread in threads:
                thread.start()

            for thread in threads:
                thread.join()

            if not auth_refresh_needed:
                break

            auth_attempts += 1

            if auth_attempts > 1:
                raise RuntimeError(
                    "RBXDL authentication expired again while fetching the batch."
                )

            raise RBXDLBulkUnavailable(
                "RBXDL bulk session/Key Access expired while fetching the batch."
            )

        return list(items.values())

    def _post_complete(
        self,
        batch_id: str,
        total_count: int,
        success_count: int,
        skipped_count: int,
        failed_count: int,
        total_bytes: int,
        duration_seconds: int,
    ):
        try:
            self.session.post(
                RBXDL_BASE_URL + "/bulk/complete",
                json={
                    "batch_id": batch_id,
                    "total_count": total_count,
                    "success_count": success_count,
                    "skipped_count": skipped_count,
                    "failed_count": failed_count,
                    "total_bytes": total_bytes,
                    "duration_seconds": duration_seconds,
                },
                headers=self._request_headers(),
                timeout=30,
            )
        except Exception:
            pass

    def _download_zip_to_file(
        self,
        batch_id: str,
        manifest: str,
        output_path: Path,
    ) -> Path:
        """
        Stream RBXDL's server-side ZIP directly to disk while emitting live
        status updates.

        The old implementation waited on response.content. That made the web
        dashboard look frozen at "requesting server-side ZIP" until the whole
        archive had finished packaging/downloading.

        This implementation:
          * shows a packaging timer before response headers arrive
          * shows live ZIP byte/percent progress
          * writes to .part first, then atomically renames
          * refreshes RBXDL auth once on CSRF/login/Key-Access failure
          * uses explicit connect/read timeouts so it cannot appear stuck forever
        """

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temp_path = output_path.with_suffix(
            output_path.suffix + ".part"
        )

        for attempt in range(2):
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass

            shared = {
                "error": None,
                "auth": False,
                "downloaded": 0,
                "total": 0,
                "headers_received": False,
                "first_bytes": False,
            }

            def worker():
                try:
                    response = self.session.post(
                        RBXDL_BASE_URL + "/bulk/download-zip",
                        data={
                            "_token": self.csrf_token,
                            "batchId": batch_id,
                            "manifest": manifest,
                        },
                        headers={
                            "Accept": "*/*",
                            "Referer": RBXDL_PORTAL_DASHBOARD,
                            "X-CSRF-TOKEN": self.csrf_token,
                        },
                        stream=True,
                        allow_redirects=True,
                        timeout=(20, 120),
                    )

                    shared["headers_received"] = True

                    if self._auth_related_response(response):
                        shared["auth"] = True
                        return

                    if not response.ok:
                        try:
                            body = response.text or ""
                        except Exception:
                            body = ""

                        raise RuntimeError(
                            "RBXDL ZIP endpoint failed: "
                            f"HTTP {response.status_code} - "
                            + body[:500]
                        )

                    try:
                        shared["total"] = int(
                            response.headers.get(
                                "content-length"
                            )
                            or 0
                        )
                    except Exception:
                        shared["total"] = 0

                    with temp_path.open("wb") as handle:
                        first_chunk = True

                        for chunk in response.iter_content(
                            chunk_size=256 * 1024
                        ):
                            if not chunk:
                                continue

                            if first_chunk:
                                first_chunk = False
                                shared["first_bytes"] = True

                                if not chunk.startswith(b"PK"):
                                    preview = chunk[:300].decode(
                                        "utf-8",
                                        errors="replace",
                                    )

                                    raise RuntimeError(
                                        "RBXDL ZIP endpoint did not return a ZIP. "
                                        + preview
                                    )

                            handle.write(chunk)
                            shared["downloaded"] += len(
                                chunk
                            )

                except Exception as exc:
                    shared["error"] = exc

            thread = threading.Thread(
                target=worker,
                daemon=True,
            )

            thread.start()

            started = time.monotonic()
            last_message = None

            while thread.is_alive():
                elapsed = int(
                    time.monotonic() - started
                )

                downloaded = int(
                    shared["downloaded"]
                )

                total = int(
                    shared["total"]
                )

                if not shared["headers_received"]:
                    message = (
                        "RBXDL is packaging the server-side ZIP "
                        f"({elapsed}s)..."
                    )

                elif not shared["first_bytes"]:
                    message = (
                        "RBXDL ZIP response opened; waiting for archive bytes "
                        f"({elapsed}s)..."
                    )

                elif total > 0:
                    percent = min(
                        100,
                        round(
                            downloaded
                            / total
                            * 100
                        ),
                    )

                    message = (
                        "Downloading RBXDL ZIP "
                        f"{percent}% "
                        f"({downloaded / 1048576:.1f} / "
                        f"{total / 1048576:.1f} MB)"
                    )

                else:
                    message = (
                        "Downloading RBXDL ZIP "
                        f"({downloaded / 1048576:.1f} MB)"
                    )

                if message != last_message:
                    self.status(message)
                    last_message = message

                thread.join(
                    timeout=0.75
                )

            if shared["auth"]:
                if attempt == 0:
                    raise RBXDLBulkUnavailable(
                        "RBXDL bulk ZIP endpoint requires login/Key Access."
                    )

                raise RuntimeError(
                    "RBXDL ZIP endpoint still requires authentication "
                    "after refreshing the session."
                )

            if shared["error"] is not None:
                raise RuntimeError(
                    str(shared["error"])
                ) from shared["error"]

            if not temp_path.is_file():
                raise RuntimeError(
                    "RBXDL ZIP request finished but no archive was written."
                )

            if temp_path.stat().st_size <= 0:
                raise RuntimeError(
                    "RBXDL ZIP endpoint returned an empty archive."
                )

            with temp_path.open("rb") as handle:
                if handle.read(2) != b"PK":
                    raise RuntimeError(
                        "RBXDL ZIP file has an invalid signature."
                    )

            if output_path.exists():
                output_path.unlink()

            temp_path.replace(
                output_path
            )

            self.status(
                "RBXDL ZIP downloaded "
                f"({output_path.stat().st_size / 1048576:.1f} MB)."
            )

            return output_path

        raise RuntimeError(
            "RBXDL ZIP download failed after authentication refresh."
        )

    def _release_lock(self, batch_id: str):
        try:
            self.session.post(
                RBXDL_BASE_URL + "/bulk/release-lock",
                json={
                    "batchId": batch_id,
                },
                headers=self._request_headers(),
                timeout=15,
            )
        except Exception:
            pass

    # --------------------------------------------------------
    # ZIP helpers
    # --------------------------------------------------------

    @staticmethod
    def _safe_extract_zip(
        zip_path: Path,
        destination: Path,
    ):
        destination.mkdir(
            parents=True,
            exist_ok=True,
        )
        destination_root = destination.resolve()

        with zipfile.ZipFile(
            zip_path,
            "r",
        ) as archive:
            for info in archive.infolist():
                target = (
                    destination
                    / info.filename
                ).resolve()

                if (
                    target != destination_root
                    and destination_root
                    not in target.parents
                ):
                    raise RuntimeError(
                        "RBXDL ZIP contains an unsafe path."
                    )

            archive.extractall(destination)

    @staticmethod
    def _map_files_to_asset_ids(
        extracted_root: Path,
        asset_ids: list[str],
    ):
        asset_files = [
            path
            for path in extracted_root.rglob("*")
            if (
                path.is_file()
                and path.suffix.lower()
                in {
                    ".rbxm",
                    ".rbxmx",
                    ".bin",
                    ".mesh",
                    ".mp3",
                    ".ogg",
                    ".wav",
                    ".flac",
                }
            )
        ]

        mapping = {}
        errors = {}

        for asset_id in asset_ids:
            matches = [
                path
                for path in asset_files
                if asset_id
                in str(
                    path.relative_to(
                        extracted_root
                    )
                )
            ]

            if matches:
                mapping[asset_id] = matches[0]

        missing = [
            asset_id
            for asset_id in asset_ids
            if asset_id not in mapping
        ]

        manifests = [
            path
            for path in extracted_root.rglob("*")
            if (
                path.is_file()
                and path.name.lower()
                in {
                    "_manifest.txt",
                    "manifest.txt",
                }
            )
        ]

        if missing and manifests:
            try:
                text = manifests[0].read_text(
                    encoding="utf-8",
                    errors="ignore",
                )
            except Exception:
                text = ""

            by_name = {
                path.name: path
                for path in asset_files
            }

            for asset_id in list(missing):
                match = re.search(
                    rf"\[{re.escape(asset_id)}\]\s+([^\r\n]+?)\s+\(",
                    text,
                )

                if not match:
                    continue

                candidate = by_name.get(
                    Path(
                        match.group(1).strip()
                    ).name
                )

                if candidate is not None:
                    mapping[asset_id] = candidate
                    missing.remove(asset_id)

        for asset_id in missing:
            errors[asset_id] = (
                "RBXDL ZIP did not contain a file that could be mapped "
                "to this asset ID."
            )

        return mapping, errors

    # --------------------------------------------------------
    # Public single-downloader fallback
    # --------------------------------------------------------

    def _download_single_fallback(
        self,
        asset_ids: list[str],
        output_dir: Path,
        reason: str,
        *,
        retry_rate_limits_forever: bool = False,
        priority: int = 1,
    ):
        """
        Parallel fallback built from RBXDL's normal public single downloader.

        Each worker owns its own Playwright/browser instance. That matters:
        Playwright's synchronous API is not thread-safe across one shared
        browser object.

        We keep the worker count bounded (default 8, max 12), reuse each
        worker's browser for multiple assets, and preserve per-asset live
        status updates for the web dashboard.
        """
        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        asset_ids = list(
            dict.fromkeys(
                str(value)
                for value in asset_ids
            )
        )

        if not asset_ids:
            return {}, {}

        worker_count = min(
            RBXDL_SINGLE_WORKERS,
            len(asset_ids),
        )
        # A missing web control affects the whole Single flow, not one asset.
        # Stop wasting minutes trying the same broken page for 100 IDs.
        ui_circuit_open = threading.Event()
        consecutive_ui_failures = 0
        ui_reason = [""]
        ui_threshold = min(4, len(asset_ids))
        visible_browser = os.environ.get("TM_RBXDL_SINGLE_VISIBLE", "").lower() in {"1", "true", "yes"}

        retry_note = (
            " Rate-limit retries will continue until success."
            if retry_rate_limits_forever
            else ""
        )

        self.status(
            "RBXDL coordinated single fallback active — "
            f"{worker_count} worker(s), {len(asset_ids)} asset(s). "
            + str(reason)
            + retry_note
        )

        mapping = {}
        errors = {}
        result_lock = threading.RLock()
        work_queue = queue.Queue()
        started_at = time.monotonic()
        completed = 0
        workers_started = 0
        startup_errors = []

        for index, asset_id in enumerate(
            asset_ids,
            start=1,
        ):
            work_queue.put(
                (
                    index,
                    asset_id,
                )
            )

            self.item_status(
                asset_id,
                "fallback_queued",
                source="RBXDL Single Downloader",
                fallback=True,
                index=index,
                total=len(asset_ids),
                workers=worker_count,
            )

        def report_progress():
            elapsed = max(
                time.monotonic() - started_at,
                0.001,
            )

            with result_lock:
                finished = completed
                ready_count = len(mapping)
                failed_count = len(errors)

            rate_per_minute = (
                finished / elapsed
            ) * 60.0

            remaining = max(
                len(asset_ids) - finished,
                0,
            )

            eta = (
                remaining / (finished / elapsed)
                if finished > 0
                else None
            )

            eta_text = (
                f"{eta:.0f}s"
                if eta is not None
                else "calculating"
            )

            gate = _RBXDL_SINGLE_RATE_GATE.snapshot()

            learned_gap = float(
                gate.get("spacing") or 0.0
            )

            cadence_text = (
                f" • RBXDL gap {learned_gap:.1f}s"
                if learned_gap >= 2.0
                else ""
            )

            self.status(
                "RBXDL coordinated single "
                f"{finished}/{len(asset_ids)} "
                f"• {ready_count} ready "
                f"• {failed_count} failed "
                f"• {rate_per_minute:.1f}/min "
                f"• ETA {eta_text}"
                + cadence_text
            )

        def worker(worker_number: int):
            nonlocal completed
            nonlocal workers_started
            nonlocal consecutive_ui_failures

            fetcher = None

            try:
                try:
                    fetcher = RBXDLBrowserFetcher(
                        headless=not visible_browser,
                        min_download_interval=0.0,
                        priority=priority,
                    )
                    fetcher.start()

                    with result_lock:
                        workers_started += 1

                except Exception as exc:
                    with result_lock:
                        startup_errors.append(
                            f"worker {worker_number}: {exc}"
                        )
                    return

                while True:
                    try:
                        index, asset_id = work_queue.get_nowait()
                    except queue.Empty:
                        return

                    try:
                        if ui_circuit_open.is_set():
                            raise RuntimeError(
                                "RBXDL_SINGLE_UI_UNAVAILABLE: Batch stopped after "
                                "repeated missing Download controls. Check RBXDL manually "
                                "and retry the failed assets later. " + ui_reason[0]
                            )
                        self.item_status(
                            asset_id,
                            "fetching",
                            source="RBXDL Single Downloader",
                            fallback=True,
                            index=index,
                            total=len(asset_ids),
                            worker=worker_number,
                            workers=worker_count,
                        )

                        def on_rate_limit(
                            wait_seconds,
                            rate_limit_count,
                        ):
                            self.item_status(
                                asset_id,
                                "rate_limited",
                                source="RBXDL Single Downloader",
                                fallback=True,
                                error="",
                                waitSeconds=wait_seconds,
                                rateLimitCount=rate_limit_count,
                                index=index,
                                total=len(asset_ids),
                                worker=worker_number,
                                workers=worker_count,
                            )

                            self.status(
                                "RBXDL cooldown — "
                                f"{asset_id} waiting about "
                                f"{wait_seconds:.0f}s "
                                f"(rate-limit retry #{rate_limit_count})."
                            )

                        data, extension, _ = fetcher.fetch(
                            asset_id,
                            retry_rate_limits_forever=
                                retry_rate_limits_forever,
                            on_rate_limit=on_rate_limit,
                        )

                        file_path = (
                            output_dir
                            / f"{asset_id}{extension}"
                        )

                        file_path.write_bytes(
                            data
                        )

                        with result_lock:
                            # Only consecutive UI failures indicate a systemic outage.
                            # Scattered failures across a healthy batch must NOT trip
                            # the circuit and falsely fail every remaining asset.
                            consecutive_ui_failures = 0
                            mapping[
                                asset_id
                            ] = file_path

                        self.notify_file_ready(
                            asset_id, file_path, "RBXDL Single Downloader"
                        )

                        self.item_status(
                            asset_id,
                            "ready",
                            source="RBXDL Single Downloader",
                            fallback=True,
                            filename=file_path.name,
                            size=len(data),
                            index=index,
                            total=len(asset_ids),
                            worker=worker_number,
                            workers=worker_count,
                        )

                    except Exception as exc:
                        message = str(exc)

                        is_ui_missing = (
                            "RBXDL_UI_NOT_READY:" in message
                            or "Single Downloader UI remained unavailable" in message
                        )
                        with result_lock:
                            if is_ui_missing:
                                consecutive_ui_failures += 1
                                if (consecutive_ui_failures >= ui_threshold
                                        and not ui_circuit_open.is_set()):
                                    ui_reason[0] = message[:700]
                                    ui_circuit_open.set()
                                    self.status(
                                        "RBXDL Single unavailable for the batch: "
                                        f"{consecutive_ui_failures} consecutive attempts "
                                        "could not find the site control. Remaining items "
                                        "will be marked failed for a later retry."
                                    )
                            else:
                                # Other errors (e.g., an invalid per-request token)
                                # break a streak of missing controls.
                                consecutive_ui_failures = 0

                        with result_lock:
                            errors[
                                asset_id
                            ] = message

                        self.item_status(
                            asset_id,
                            "failed",
                            source="RBXDL Single Downloader",
                            fallback=True,
                            error=message,
                            index=index,
                            total=len(asset_ids),
                            worker=worker_number,
                            workers=worker_count,
                        )

                    finally:
                        with result_lock:
                            completed += 1

                        work_queue.task_done()
                        report_progress()

            finally:
                if fetcher is not None:
                    try:
                        fetcher.close()
                    except Exception:
                        pass

        threads = [
            threading.Thread(
                target=worker,
                args=(worker_number,),
                name=f"RBXDL-Single-{worker_number}",
                daemon=True,
            )
            for worker_number in range(
                1,
                worker_count + 1,
            )
        ]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        # If headless browser startup completely failed, preserve the old
        # recovery behavior: use ONE visible browser, not eight windows.
        if workers_started == 0 and not work_queue.empty():
            self.status(
                "Headless RBXDL single workers could not start — "
                "retrying remaining assets with one visible browser."
            )

            visible_fetcher = None

            try:
                visible_fetcher = RBXDLBrowserFetcher(
                    headless=False,
                    min_download_interval=0.0,
                    priority=priority,
                )
                visible_fetcher.start()

                while True:
                    try:
                        index, asset_id = work_queue.get_nowait()
                    except queue.Empty:
                        break

                    try:
                        self.item_status(
                            asset_id,
                            "fetching",
                            source="RBXDL Single Downloader",
                            fallback=True,
                            index=index,
                            total=len(asset_ids),
                            worker=1,
                            workers=1,
                        )

                        def on_visible_rate_limit(
                            wait_seconds,
                            rate_limit_count,
                        ):
                            self.item_status(
                                asset_id,
                                "rate_limited",
                                source="RBXDL Single Downloader",
                                fallback=True,
                                error="",
                                waitSeconds=wait_seconds,
                                rateLimitCount=rate_limit_count,
                                index=index,
                                total=len(asset_ids),
                                worker=1,
                                workers=1,
                            )

                            self.status(
                                "RBXDL cooldown — "
                                f"{asset_id} waiting about "
                                f"{wait_seconds:.0f}s "
                                f"(rate-limit retry #{rate_limit_count})."
                            )

                        data, extension, _ = visible_fetcher.fetch(
                            asset_id,
                            retry_rate_limits_forever=
                                retry_rate_limits_forever,
                            on_rate_limit=on_visible_rate_limit,
                        )

                        file_path = (
                            output_dir
                            / f"{asset_id}{extension}"
                        )
                        file_path.write_bytes(data)

                        mapping[asset_id] = file_path
                        self.notify_file_ready(
                            asset_id, file_path, "RBXDL Single Downloader"
                        )

                        self.item_status(
                            asset_id,
                            "ready",
                            source="RBXDL Single Downloader",
                            fallback=True,
                            filename=file_path.name,
                            size=len(data),
                            index=index,
                            total=len(asset_ids),
                            worker=1,
                            workers=1,
                        )

                    except Exception as exc:
                        message = str(exc)
                        errors[asset_id] = message

                        self.item_status(
                            asset_id,
                            "failed",
                            source="RBXDL Single Downloader",
                            fallback=True,
                            error=message,
                            index=index,
                            total=len(asset_ids),
                            worker=1,
                            workers=1,
                        )

                    finally:
                        completed += 1
                        work_queue.task_done()
                        report_progress()

            finally:
                if visible_fetcher is not None:
                    try:
                        visible_fetcher.close()
                    except Exception:
                        pass

        # If only some workers failed to start, the surviving workers drain
        # the same queue, so no asset is lost. This is only a sanity net.
        leftovers = []

        while True:
            try:
                index, asset_id = work_queue.get_nowait()
            except queue.Empty:
                break

            leftovers.append(
                asset_id
            )
            work_queue.task_done()

        for asset_id in leftovers:
            message = (
                "RBXDL single fallback had no available browser worker. "
                + (
                    " | ".join(startup_errors)
                    if startup_errors
                    else "Browser startup failed."
                )
            )
            errors[asset_id] = message

            self.item_status(
                asset_id,
                "failed",
                source="RBXDL Single Downloader",
                fallback=True,
                error=message,
                total=len(asset_ids),
                workers=worker_count,
            )

        self.status(
            "RBXDL coordinated single fallback finished: "
            f"{len(mapping)} ready, {len(errors)} failed "
            f"using up to {worker_count} worker(s)."
        )

        return mapping, errors

    def _bulk_session_available(self) -> tuple[bool, str]:
        """
        Check whether the saved RBXDL Creator Portal session currently has
        usable bulk access. This check is silent and never opens a browser.
        """
        circuit_reason = self._portal_circuit_reason()

        if circuit_reason:
            return False, circuit_reason

        ok, reason = self._refresh_http_session()

        if ok:
            return True, reason

        return False, reason

    def _restore_bulk_access_interactively(
        self,
        reason: str,
    ) -> tuple[bool, str]:
        """
        Bulk is much faster than the public single downloader, especially for
        hundreds of animations. If the saved session/Key Access is missing or
        expired, give the user one chance to renew it in Edge before falling
        back to single downloads.

        Flow:
            silent session check failed
            -> open Edge to RBXDL portal
            -> user logs in / renews Key Access
            -> browser closes automatically
            -> verify the HTTP session again
            -> return whether bulk is usable
        """
        circuit_reason = self._portal_circuit_reason()

        if circuit_reason:
            self.status(
                "RBXDL Portal circuit breaker is active — "
                "skipping login/renewal and using parallel single fallback. "
                + str(circuit_reason)
            )
            return False, circuit_reason

        self.status(
            "RBXDL bulk access is unavailable: "
            + str(reason)
            + " Opening Edge once; migration will continue automatically "
            "as soon as the authenticated bulk session is verified."
        )

        try:
            self._interactive_auth()
        except Exception as exc:
            return (
                False,
                "RBXDL login/Key Access renewal did not complete: "
                + str(exc),
            )

        ok, refreshed_reason = (
            self._refresh_http_session()
        )

        if ok:
            self.status(
                "RBXDL bulk access restored — continuing with bulk download."
            )
            return True, refreshed_reason

        return (
            False,
            "RBXDL bulk access is still unavailable after login/renewal: "
            + str(refreshed_reason),
        )

    # --------------------------------------------------------
    # Public API
    # --------------------------------------------------------

    def _download_official_assets(self, asset_ids, output_dir):
        """Try public/authorized Roblox Asset Delivery, never evade a 403.

        Keep workers and per-endpoint timeouts bounded; use format signatures
        so an HTML/error document is never saved as an animation/audio file.
        """
        output_dir = Path(output_dir)
        results = {}
        if not asset_ids:
            return results
        output_dir.mkdir(parents=True, exist_ok=True)
        recognizable = {".mp3", ".ogg", ".wav", ".flac", ".mesh", ".rbxm", ".rbxmx"}

        def fetch_one(asset_id):
            try:
                data, extension, _ = fetch_asset_roblox(
                    asset_id, asset_type="Audio", timeout=4.0
                )
                if data.startswith(b"<roblox!"):
                    extension = ".rbxm"
                elif data.lstrip().startswith(b"<roblox"):
                    extension = ".rbxmx"
                elif data.lstrip().lower().startswith((b"<html", b"<!doctype html")):
                    return None
                if extension not in recognizable:
                    return None
                # The extension is based on recognizable content signatures,
                # not merely a remote filename/content-type suggestion.
                if extension in {".mp3", ".ogg", ".wav", ".flac", ".mesh"}:
                    actual = _guess_asset_extension(data, "", "", "Audio")
                    if actual != extension:
                        return None
                path = output_dir / f"{asset_id}{extension}"
                path.write_bytes(data)
                return path
            except Exception:
                # 403, 404, network and format errors remain unresolved.
                # Never retry an access-denied file with invented credentials.
                return None

        with ThreadPoolExecutor(max_workers=min(8, len(asset_ids))) as pool:
            pending = {pool.submit(fetch_one, aid): aid for aid in asset_ids}
            for future in as_completed(pending):
                asset_id = pending[future]
                try:
                    path = future.result()
                except Exception:
                    path = None
                if path is not None:
                    results[asset_id] = path
                    self.item_status(
                        asset_id, "ready", source="Roblox Asset Delivery",
                        filename=path.name, size=path.stat().st_size,
                    )
                    self.notify_file_ready(asset_id, path, "Roblox Asset Delivery")
        return results

    def download_single_batch(
        self,
        asset_ids: list[str],
        output_dir: Path,
        reason: str = "Standalone downloader requested assets.",
        priority: int = 1,
        *,
        allow_browser_fallback: bool = False,
    ):
        """API-first downloader, with an explicitly opted-in browser fallback.

        1. Roblox Asset Delivery (public/authorized only).
        2. RBXDL Creator Portal bulk API using an existing authorized session.
        3. Optional RBXDL public Single browser UI, with first-party one-use tokens.

        RBXDL does not document a general browserless public Single API. Never
        synthesize, replay, scrape for, or bypass the one-use security tokens.
        """
        asset_ids = list(dict.fromkeys(
            str(value) for value in asset_ids
            if re.fullmatch(r"\d+", str(value))
        ))
        if not asset_ids:
            return {}, {}

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        results: dict[str, Path] = {}
        errors: dict[str, str] = {}

        self.status(
            f"API-first (stage 1/3): checking Roblox Asset Delivery for "
            f"{len(asset_ids)} uncached asset(s)."
        )
        official = self._download_official_assets(
            asset_ids, output_dir / "roblox-direct"
        )
        results.update(official)
        remaining = [aid for aid in asset_ids if aid not in results]
        if not remaining:
            self.status("API-first: all assets resolved through Roblox Asset Delivery.")
            return results, {}

        self.status(
            f"API-first (stage 2/3): {len(remaining)} unresolved; checking "
            "authenticated RBXDL Creator Portal bulk HTTP backend."
        )
        try:
            bulk_ok, bulk_reason = self._bulk_session_available()
        except Exception as exc:
            bulk_ok, bulk_reason = False, f"Portal session check: {exc}"

        if bulk_ok:
            try:
                portal_map, portal_errors = self.download_batch(
                    remaining,
                    output_dir / "portal-direct",
                    retry_rate_limits_forever=False,
                    allow_interactive_auth=False,
                    allow_single_fallback=True,
                    bulk_already_validated=True,
                )
                results.update(portal_map)
                for aid, detail in portal_errors.items():
                    if aid not in results:
                        errors[aid] = f"RBXDL Portal: {detail}"
            except Exception as exc:
                bulk_reason = f"Portal bulk request failed: {exc}"
                self.status(bulk_reason)
        else:
            self.status("RBXDL Portal HTTP unavailable: " + str(bulk_reason))

        remaining = [aid for aid in asset_ids if aid not in results]
        for aid in remaining:
            errors.setdefault(aid, f"Roblox Asset Delivery did not return an authorized usable file. "
                                       f"RBXDL Creator Portal: {bulk_reason if 'failed:' in str(bulk_reason) or not bulk_ok else 'no usable file returned'}." )

        # Legacy strict-mode variable stays respected even with UI checkbox on.
        strict_mode = os.environ.get("TM_RBXDL_BACKEND_ONLY", "").lower() in {"1", "true", "yes"}
        if not (allow_browser_fallback and not strict_mode):
            if remaining:
                detail = (
                    "API-only mode: no authorized direct download was available. "
                    "To use RBXDL public Single, explicitly enable browser fallback "
                    "in the Downloader form. That path requires normal website "
                    "tokens, a browser and any applicable rate limits."
                )
                for aid in remaining:
                    errors[aid] = f"{errors.get(aid, '')} {detail}".strip()
                    self.item_status(aid, "failed", source="API-only", error=errors[aid])
            self.status(
                "API-first complete: "
                f"{len(results)} ready, {len(remaining)} unresolved. "
                "Browser fallback was not enabled."
            )
            return results, {aid: errors[aid] for aid in remaining}

        if not remaining:
            return results, {}

        self.status(
            f"API-first (stage 3/3): explicit browser fallback enabled for "
            f"{len(remaining)} asset(s), with normal RBXDL token/cooldown handling."
        )
        fallback_map, fallback_errors = self._download_single_fallback(
            remaining,
            output_dir / "single-fallback",
            str(reason),
            retry_rate_limits_forever=False,
            priority=priority,
        )
        results.update(fallback_map)
        for aid in remaining:
            if aid in fallback_map:
                errors.pop(aid, None)
            else:
                errors[aid] = fallback_errors.get(aid, errors[aid])
        return results, {aid: errors[aid] for aid in asset_ids if aid not in results}

    def download_batch(
        self,
        asset_ids: list[str],
        output_dir: Path,
        *,
        retry_rate_limits_forever: bool = False,
        allow_interactive_auth: bool = False,
        allow_single_fallback: bool = False,
        bulk_already_validated: bool = False,
    ):
        asset_ids = list(
            dict.fromkeys(
                str(value)
                for value in asset_ids
            )
        )

        if not asset_ids:
            return {}, {}

        if len(asset_ids) > 500:
            # RBXDL Creator Portal accepts at most 500 assets per bulk batch.
            # Transparently split huge Studio selections into as many batches
            # as necessary, then merge every mapping/error back together.
            batch_size = 500
            total_batches = (
                len(asset_ids)
                + batch_size
                - 1
            ) // batch_size

            combined_mapping = {}
            combined_errors = {}

            self.status(
                f"Large migration detected: {len(asset_ids)} assets → "
                f"{total_batches} RBXDL batch(es) of up to {batch_size}."
            )

            for batch_index in range(
                total_batches
            ):
                start_index = (
                    batch_index
                    * batch_size
                )

                chunk = asset_ids[
                    start_index:
                    start_index + batch_size
                ]

                batch_number = (
                    batch_index + 1
                )

                self.status(
                    f"RBXDL batch {batch_number}/{total_batches}: "
                    f"{len(chunk)} asset(s) "
                    f"({start_index + 1}-"
                    f"{start_index + len(chunk)} "
                    f"of {len(asset_ids)})..."
                )

                chunk_dir = (
                    output_dir
                    / (
                        f"batch-"
                        f"{batch_number:03d}"
                        f"-of-"
                        f"{total_batches:03d}"
                    )
                )

                chunk_mapping, chunk_errors = (
                    self.download_batch(
                        chunk,
                        chunk_dir,
                        retry_rate_limits_forever=retry_rate_limits_forever,
                        allow_interactive_auth=allow_interactive_auth,
                        allow_single_fallback=allow_single_fallback,
                        bulk_already_validated=bulk_already_validated,
                    )
                )

                combined_mapping.update(
                    chunk_mapping
                )

                combined_errors.update(
                    chunk_errors
                )

                self.status(
                    f"RBXDL batch {batch_number}/{total_batches} complete: "
                    f"{len(chunk_mapping)} ready, "
                    f"{len(chunk_errors)} failed. "
                    f"Overall {len(combined_mapping)}/{len(asset_ids)} ready."
                )

            self.status(
                f"All {total_batches} RBXDL batches finished: "
                f"{len(combined_mapping)} ready, "
                f"{len(combined_errors)} failed."
            )

            return (
                combined_mapping,
                combined_errors,
            )

        bulk_ok, bulk_reason = (
            (True, "Portal session already validated")
            if bulk_already_validated
            else self._bulk_session_available()
        )

        if not bulk_ok and allow_interactive_auth:
            bulk_ok, bulk_reason = (
                self._restore_bulk_access_interactively(
                    bulk_reason
                )
            )

        if not bulk_ok:
            if not allow_single_fallback:
                raise RBXDLBulkUnavailable(str(bulk_reason))
            self.status(
                "RBXDL bulk could not be restored — "
                "falling back to the public single downloader. "
                + str(bulk_reason)
            )

            return self._download_single_fallback(
                asset_ids,
                output_dir / "single-fallback",
                bulk_reason,
                retry_rate_limits_forever=retry_rate_limits_forever,
            )

        batch_id = (
            "batch_"
            + secrets.token_hex(32)
        )

        started_at = time.monotonic()

        try:
            try:
                self.status(
                    f"RBXDL native bulk API: processing "
                    f"{len(asset_ids)} asset(s)..."
                )

                results = self._fetch_assets_bulk(
                    asset_ids,
                    batch_id,
                )

                successful = [
                    item
                    for item in results
                    if item["status"] == "success"
                ]

                skipped = [
                    item
                    for item in results
                    if item["status"] == "skipped"
                ]

                failed = [
                    item
                    for item in results
                    if item["status"] == "failed"
                ]

                if not successful:
                    details = "; ".join(
                        f"{item['id']}: "
                        f"{item.get('error') or 'failed'}"
                        for item in failed
                    )

                    raise RuntimeError(
                        "RBXDL bulk returned no successful assets. "
                        + details
                    )

                total_bytes = sum(
                    int(item.get("size") or 0)
                    for item in successful
                )

                duration = max(
                    1,
                    round(
                        time.monotonic()
                        - started_at
                    ),
                )

                manifest_lines = [
                    "RBXDL Bulk Asset Download",
                    (
                        "Downloaded: "
                        + time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(),
                        )
                    ),
                    f"Total Assets: {len(results)}",
                    f"Bundled: {len(successful)}",
                    f"Skipped: {len(skipped)}",
                    f"Failed: {len(failed)}",
                    "",
                ]

                for index, item in enumerate(
                    successful,
                    start=1,
                ):
                    manifest_lines.append(
                        f"{index}. [{item['id']}] "
                        f"{item.get('filename')} "
                        f"({item.get('size') or 0} bytes) "
                        f"[{item.get('privacy') or 'Public'}]"
                    )

                if skipped:
                    manifest_lines.extend(
                        [
                            "",
                            f"Skipped Assets ({len(skipped)}):",
                        ]
                    )

                    for item in skipped:
                        manifest_lines.append(
                            f"- ID: {item['id']} "
                            f"({item.get('error') or 'Skipped'})"
                        )

                if failed:
                    manifest_lines.extend(
                        [
                            "",
                            f"Failed Assets ({len(failed)}):",
                        ]
                    )

                    for item in failed:
                        manifest_lines.append(
                            f"- ID: {item['id']} "
                            f"(Error: {item.get('error') or 'Failed'})"
                        )

                manifest = (
                    "\n".join(manifest_lines)
                    + "\n"
                )

                self._post_complete(
                    batch_id,
                    len(results),
                    len(successful),
                    len(skipped),
                    len(failed),
                    total_bytes,
                    duration,
                )

                self.status(
                    "RBXDL assets ready — starting server-side ZIP packaging..."
                )

                output_dir.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                zip_path = (
                    output_dir
                    / "rbxdl-bulk.zip"
                )

                self._download_zip_to_file(
                    batch_id,
                    manifest,
                    zip_path,
                )

                extracted = (
                    output_dir
                    / "extracted"
                )

                self._safe_extract_zip(
                    zip_path,
                    extracted,
                )

                mapping, mapping_errors = (
                    self._map_files_to_asset_ids(
                        extracted,
                        asset_ids,
                    )
                )

                for resolved_id, resolved_path in mapping.items():
                    self.notify_file_ready(
                        resolved_id, resolved_path, "RBXDL Portal native API"
                    )
                    self.item_status(
                        resolved_id,
                        "ready",
                        source="RBXDL Portal native API",
                        filename=Path(resolved_path).name,
                        size=Path(resolved_path).stat().st_size,
                    )

                for item in skipped + failed:
                    mapping_errors[
                        item["id"]
                    ] = str(
                        item.get("error")
                        or item["status"]
                    )

                # Any individual failures from bulk get one more shot through
                # the public single downloader.
                remaining = [
                    asset_id
                    for asset_id in asset_ids
                    if asset_id not in mapping
                ]

                if remaining and not allow_single_fallback:
                    for asset_id in remaining:
                        mapping_errors.setdefault(
                            asset_id,
                            "Creator Portal did not return this asset.",
                        )

                if remaining and allow_single_fallback:
                    self.status(
                        f"Bulk left {len(remaining)} asset(s) unresolved — "
                        "retrying those through the public single downloader..."
                    )

                    single_map, single_errors = (
                        self._download_single_fallback(
                            remaining,
                            output_dir
                            / "single-retry",
                            "Bulk did not return these assets.",
                            retry_rate_limits_forever=retry_rate_limits_forever,
                        )
                    )

                    mapping.update(
                        single_map
                    )

                    for asset_id in remaining:
                        if asset_id in single_map:
                            mapping_errors.pop(
                                asset_id,
                                None,
                            )
                        elif asset_id in single_errors:
                            mapping_errors[
                                asset_id
                            ] = single_errors[
                                asset_id
                            ]

                return mapping, mapping_errors

            except RBXDLBulkUnavailable as exc:
                if not allow_interactive_auth:
                    if allow_single_fallback:
                        return self._download_single_fallback(
                            asset_ids,
                            output_dir / "single-fallback",
                            str(exc),
                            retry_rate_limits_forever=retry_rate_limits_forever,
                        )
                    raise
                
                # The portal session/Key Access died while a bulk batch was
                # already running. Release this batch before re-authenticating
                # so the retry does not collide with a stale portal lock.
                self.status(
                    "RBXDL bulk access expired during the batch — "
                    "releasing the old batch and opening Edge to renew access..."
                )

                try:
                    self._release_lock(
                        batch_id
                    )
                except Exception:
                    pass

                # Prevent the outer finally block from releasing the same lock
                # again after the recursive retry completes.
                batch_id = ""

                restored, restore_reason = (
                    self._restore_bulk_access_interactively(
                        str(exc)
                    )
                )

                if restored:
                    self.status(
                        "RBXDL access restored — retrying this batch in bulk mode."
                    )

                    return self.download_batch(
                        asset_ids,
                        output_dir
                        / "bulk-retry-after-auth",
                        retry_rate_limits_forever=retry_rate_limits_forever,
                        allow_interactive_auth=allow_interactive_auth,
                        allow_single_fallback=allow_single_fallback,
                    )

                if not allow_single_fallback:
                    raise RBXDLBulkUnavailable(str(restore_reason))
                self.status(
                    "RBXDL bulk still unavailable after renewal attempt — "
                    "using the opted-in single downloader. "
                    + str(restore_reason)
                )

                return self._download_single_fallback(
                    asset_ids,
                    output_dir
                    / "single-fallback",
                    restore_reason,
                retry_rate_limits_forever=retry_rate_limits_forever,
                )

            except Exception as exc:
                if not allow_single_fallback:
                    raise
                # Bulk is an optimization. If it breaks for any portal-specific
                # reason, still give the public single flow a chance.
                self.status(
                    "RBXDL bulk failed — falling back to single downloader: "
                    + str(exc)
                )

                return self._download_single_fallback(
                    asset_ids,
                    output_dir
                    / "single-fallback",
                    str(exc),
                retry_rate_limits_forever=retry_rate_limits_forever,
                )

        finally:
            if batch_id:
                self._release_lock(
                    batch_id
                )



class MigratorApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title(APP_TITLE)
        self.geometry("1000x620")
        self.minsize(840, 520)

        self.jobs = {}
        self.jobs_lock = threading.Lock()
        self.config_lock = threading.Lock()
        self.runtime_config = {}

        # Official Studio-side source cache. The plugin uses
        # AnimationClipProvider:GetAnimationClipAsync() and sends a temporary
        # RBXMX representation here when Studio itself can access an animation.
        self.source_cache = {}
        self.source_cache_lock = threading.Lock()

        self.server = None
        self.server_thread = None
        self.ui_queue = queue.Queue()

        self.api_key_var = tk.StringVar()
        self.creator_type_var = tk.StringVar(value="Group")
        self.creator_id_var = tk.StringVar()
        self.description_var = tk.StringVar(
            value="Migrated tower animation"
        )
        self.remember_key_var = tk.BooleanVar(value=True)
        self.show_key_var = tk.BooleanVar(value=False)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        self.server_status_var = tk.StringVar(
            value="Starting local migration server..."
        )

        self._load_saved_settings()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.after(100, self._tick)
        self.after(250, self.start_server)

    # --------------------------------------------------------
    # Settings/config
    # --------------------------------------------------------

    def _load_saved_settings(self):
        settings = load_settings()

        creator_type = settings.get("creator_type", "Group")
        if creator_type not in {"User", "Group"}:
            creator_type = "Group"

        self.creator_type_var.set(creator_type)
        self.creator_id_var.set(
            str(settings.get("creator_id", ""))
        )
        self.description_var.set(
            str(
                settings.get(
                    "description",
                    "Migrated tower animation",
                )
            )
        )
        self.port_var.set(
            str(settings.get("port", DEFAULT_PORT))
        )

        encrypted = settings.get("api_key_dpapi")

        if isinstance(encrypted, str) and encrypted:
            try:
                self.api_key_var.set(
                    unprotect_secret(encrypted)
                )
                self.remember_key_var.set(True)
            except Exception:
                self.api_key_var.set("")
                self.remember_key_var.set(False)

    def save_settings(self):
        data = {
            "creator_type": self.creator_type_var.get(),
            "creator_id": self.creator_id_var.get().strip(),
            "description": self.description_var.get(),
            "port": self.port_var.get().strip(),
        }

        key = self.api_key_var.get().strip()

        if self.remember_key_var.get() and key:
            try:
                data["api_key_dpapi"] = protect_secret(key)
            except Exception:
                pass

        save_settings_file(data)

    def sync_runtime_config(self):
        config = {
            "api_key": self.api_key_var.get().strip(),
            "creator_type": self.creator_type_var.get(),
            "creator_id": self.creator_id_var.get().strip(),
            "description": self.description_var.get(),
        }

        with self.config_lock:
            self.runtime_config = config

    def get_runtime_config(self):
        with self.config_lock:
            return dict(self.runtime_config)

    # --------------------------------------------------------
    # GUI
    # --------------------------------------------------------

    def _build_ui(self):
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        settings = ttk.LabelFrame(
            outer,
            text="One-time setup",
            padding=10,
        )
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)

        ttk.Label(
            settings,
            text="Open Cloud API key",
        ).grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 8),
            pady=4,
        )

        self.key_entry = ttk.Entry(
            settings,
            textvariable=self.api_key_var,
            show="*",
        )
        self.key_entry.grid(
            row=0,
            column=1,
            sticky="ew",
            pady=4,
        )

        ttk.Checkbutton(
            settings,
            text="Show",
            variable=self.show_key_var,
            command=self.toggle_key,
        ).grid(
            row=0,
            column=2,
            padx=6,
        )

        ttk.Checkbutton(
            settings,
            text="Remember key (Windows-encrypted)",
            variable=self.remember_key_var,
        ).grid(
            row=0,
            column=3,
            sticky="w",
        )

        ttk.Label(
            settings,
            text="Creator",
        ).grid(
            row=1,
            column=0,
            sticky="w",
            padx=(0, 8),
            pady=4,
        )

        creator_frame = ttk.Frame(settings)
        creator_frame.grid(
            row=1,
            column=1,
            columnspan=3,
            sticky="w",
            pady=4,
        )

        ttk.Combobox(
            creator_frame,
            textvariable=self.creator_type_var,
            values=("User", "Group"),
            state="readonly",
            width=10,
        ).pack(side="left")

        ttk.Label(
            creator_frame,
            text="ID",
        ).pack(
            side="left",
            padx=(14, 5),
        )

        ttk.Entry(
            creator_frame,
            textvariable=self.creator_id_var,
            width=24,
        ).pack(side="left")

        ttk.Label(
            settings,
            text="Description",
        ).grid(
            row=2,
            column=0,
            sticky="w",
            padx=(0, 8),
            pady=4,
        )

        ttk.Entry(
            settings,
            textvariable=self.description_var,
        ).grid(
            row=2,
            column=1,
            columnspan=3,
            sticky="ew",
            pady=4,
        )

        server_frame = ttk.Frame(settings)
        server_frame.grid(
            row=3,
            column=0,
            columnspan=4,
            sticky="ew",
            pady=(8, 0),
        )

        ttk.Label(
            server_frame,
            text="Local port",
        ).pack(side="left")

        ttk.Entry(
            server_frame,
            textvariable=self.port_var,
            width=8,
        ).pack(
            side="left",
            padx=(6, 10),
        )

        ttk.Button(
            server_frame,
            text="Restart Server",
            command=self.restart_server,
        ).pack(side="left")

        ttk.Button(
            server_frame,
            text="Save Settings",
            command=self.save_settings,
        ).pack(
            side="left",
            padx=(8, 0),
        )

        ttk.Label(
            server_frame,
            textvariable=self.server_status_var,
        ).pack(
            side="left",
            padx=(14, 0),
        )

        help_text = (
            "Keep this app open. In Studio, select a tower and click the "
            "'Migrate Tower' plugin button. The app fetches every unique "
            "Animation asset, uploads fresh copies to the creator above, "
            "then the plugin replaces the selected tower's AnimationIds. "
            "If Roblox Asset Delivery fails, the helper automatically tries RBXDL."
        )

        ttk.Label(
            outer,
            text=help_text,
            wraplength=940,
        ).pack(
            fill="x",
            pady=(10, 8),
        )

        jobs_frame = ttk.LabelFrame(
            outer,
            text="Migration jobs",
            padding=8,
        )
        jobs_frame.pack(
            fill="both",
            expand=True,
        )

        columns = (
            "tower",
            "state",
            "progress",
            "migrated",
            "failed",
            "message",
        )

        self.tree = ttk.Treeview(
            jobs_frame,
            columns=columns,
            show="headings",
        )

        headings = {
            "tower": "Tower",
            "state": "State",
            "progress": "Progress",
            "migrated": "Migrated",
            "failed": "Failed",
            "message": "Message",
        }

        for name, label in headings.items():
            self.tree.heading(name, text=label)

        self.tree.column("tower", width=180)
        self.tree.column("state", width=105)
        self.tree.column("progress", width=110)
        self.tree.column("migrated", width=90, anchor="center")
        self.tree.column("failed", width=75, anchor="center")
        self.tree.column("message", width=390)

        scrollbar = ttk.Scrollbar(
            jobs_frame,
            orient="vertical",
            command=self.tree.yview,
        )
        self.tree.configure(
            yscrollcommand=scrollbar.set,
        )

        self.tree.pack(
            side="left",
            fill="both",
            expand=True,
        )
        scrollbar.pack(
            side="right",
            fill="y",
        )

        job_actions = ttk.Frame(outer)
        job_actions.pack(fill="x", pady=(8, 0))

        ttk.Button(
            job_actions,
            text="Copy Selected Job Errors",
            command=self.copy_selected_job_errors,
        ).pack(side="right")

    def toggle_key(self):
        self.key_entry.configure(
            show="" if self.show_key_var.get() else "*"
        )

    # --------------------------------------------------------
    # Local API server
    # --------------------------------------------------------

    def get_port(self):
        raw = self.port_var.get().strip()

        if not re.fullmatch(r"\d+", raw):
            raise ValueError("Port must be numeric.")

        port = int(raw)

        if not 1 <= port <= 65535:
            raise ValueError(
                "Port must be between 1 and 65535."
            )

        return port

    def make_handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def send_json(self, status, value):
                raw = json.dumps(
                    value,
                    ensure_ascii=False,
                ).encode("utf-8")

                self.send_response(status)
                self.send_header(
                    "Content-Type",
                    "application/json",
                )
                self.send_header(
                    "Content-Length",
                    str(len(raw)),
                )
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/ping":
                    config = app.get_runtime_config()
                    configured = bool(
                        config.get("api_key")
                        and re.fullmatch(
                            r"\d+",
                            config.get("creator_id", ""),
                        )
                    )

                    self.send_json(
                        200,
                        {
                            "ok": True,
                            "configured": configured,
                            "creatorType":
                                config.get("creator_type"),
                            "creatorId":
                                config.get("creator_id"),
                        },
                    )
                    return

                match = re.fullmatch(
                    r"/jobs/([0-9a-fA-F-]+)",
                    self.path,
                )

                if match:
                    job = app.get_job(match.group(1))

                    if job is None:
                        self.send_json(
                            404,
                            {
                                "ok": False,
                                "error": "Unknown job.",
                            },
                        )
                    else:
                        self.send_json(200, job)
                    return

                self.send_json(
                    404,
                    {
                        "ok": False,
                        "error": "Not found.",
                    },
                )

            def do_POST(self):
                try:
                    length = int(
                        self.headers.get(
                            "Content-Length",
                            "0",
                        )
                    )
                    raw = self.rfile.read(length)
                    payload = json.loads(
                        raw.decode("utf-8")
                    )

                    if self.path == "/source":
                        asset_id = str(
                            payload.get("assetId")
                            or ""
                        ).strip()

                        rbxmx = payload.get("rbxmx")

                        if not re.fullmatch(
                            r"\d+",
                            asset_id,
                        ):
                            raise ValueError(
                                "Source assetId must be numeric."
                            )

                        if (
                            not isinstance(rbxmx, str)
                            or not rbxmx.lstrip().startswith(
                                "<roblox"
                            )
                        ):
                            raise ValueError(
                                "Source payload is not valid RBXMX text."
                            )

                        with app.source_cache_lock:
                            app.source_cache[asset_id] = rbxmx

                        self.send_json(
                            200,
                            {
                                "ok": True,
                                "assetId": asset_id,
                            },
                        )
                        return

                    if self.path == "/migrate":
                        job_id = app.create_job(payload)

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
                            "ok": False,
                            "error": "Not found.",
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

        return Handler

    def start_server(self):
        if self.server is not None:
            return

        try:
            port = self.get_port()

            self.server = ThreadingHTTPServer(
                (HOST, port),
                self.make_handler(),
            )

            self.server_thread = threading.Thread(
                target=self.server.serve_forever,
                daemon=True,
            )
            self.server_thread.start()

            self.server_status_var.set(
                f"Listening on {HOST}:{port}"
            )
            self.save_settings()

        except Exception as exc:
            self.server = None
            self.server_status_var.set(
                f"Server error: {exc}"
            )

    def stop_server(self):
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass

        self.server = None
        self.server_thread = None

    def restart_server(self):
        self.stop_server()
        self.start_server()

    # --------------------------------------------------------
    # Jobs
    # --------------------------------------------------------

    def create_job(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Invalid migration request.")

        tower = str(payload.get("tower") or "").strip()
        animations = payload.get("animations")

        if not tower:
            raise ValueError("Tower name is missing.")

        if not isinstance(animations, list):
            raise ValueError(
                "Animation manifest is missing."
            )

        config = self.get_runtime_config()

        if not config.get("api_key"):
            raise ValueError(
                "Desktop app has no API key configured."
            )

        if not re.fullmatch(
            r"\d+",
            config.get("creator_id", ""),
        ):
            raise ValueError(
                "Desktop app has no valid creator ID configured."
            )

        unique = {}

        for entry in animations:
            if not isinstance(entry, dict):
                continue

            raw_id = str(
                entry.get("assetId") or ""
            )
            match = re.search(r"\d+", raw_id)

            if not match:
                continue

            old_id = match.group(0)

            if old_id not in unique:
                unique[old_id] = dict(entry)

        if not unique:
            raise ValueError(
                "No valid Animation asset IDs were found."
            )

        job_id = str(uuid.uuid4())

        job = {
            "ok": True,
            "jobId": job_id,
            "tower": tower,
            "state": "queued",
            "total": len(unique),
            "completed": 0,
            "migrated": 0,
            "failed": 0,
            "message": "Queued",
            "mapping": {},
            "errors": {},
        }

        with self.jobs_lock:
            self.jobs[job_id] = job

        self.ui_queue.put(
            ("job_created", job_id)
        )

        threading.Thread(
            target=self.run_job,
            args=(
                job_id,
                tower,
                unique,
                config,
            ),
            daemon=True,
        ).start()

        return job_id

    def get_job(self, job_id):
        with self.jobs_lock:
            value = self.jobs.get(job_id)
            return (
                json.loads(json.dumps(value))
                if value is not None
                else None
            )

    def update_job(self, job_id, **changes):
        with self.jobs_lock:
            job = self.jobs.get(job_id)

            if not job:
                return

            job.update(changes)

        self.ui_queue.put(
            ("job_updated", job_id)
        )

    def run_job(
        self,
        job_id,
        tower,
        unique,
        config,
    ):
        job_folder = (
            MIGRATIONS_DIR
            / safe_filename(tower)
            / job_id
        )
        job_folder.mkdir(
            parents=True,
            exist_ok=True,
        )

        mapping = {}
        errors = {}
        prepared_files = {}
        fetch_sources = {}

        with self.source_cache_lock:
            studio_sources = {
                old_id: self.source_cache.get(old_id)
                for old_id in unique.keys()
                if self.source_cache.get(old_id)
            }

        unresolved = []
        official_errors = {}

        # ----------------------------------------------------
        # Pass 1: official Roblox sources
        # ----------------------------------------------------

        for index, (old_id, entry) in enumerate(
            unique.items(),
            start=1,
        ):
            name = suggested_animation_name(
                entry
            )

            self.update_job(
                job_id,
                state="fetching",
                completed=0,
                message=(
                    f"Preparing Roblox source "
                    f"{index}/{len(unique)}: {name}"
                ),
            )

            # Best path: Studio itself successfully loaded the animation using
            # AnimationClipProvider and sent us a temporary RBXMX.
            studio_rbxmx = studio_sources.get(
                old_id
            )

            if studio_rbxmx:
                file_path = (
                    job_folder
                    / f"{safe_filename(name)}_{old_id}.rbxmx"
                )

                file_path.write_text(
                    studio_rbxmx,
                    encoding="utf-8",
                )

                prepared_files[old_id] = file_path
                fetch_sources[old_id] = (
                    "Roblox Studio AnimationClipProvider"
                )
                continue

            # Second official path: public Asset Delivery.
            try:
                data, extension, source = (
                    fetch_animation_asset_roblox(
                        old_id
                    )
                )

                file_path = (
                    job_folder
                    / f"{safe_filename(name)}_{old_id}{extension}"
                )
                file_path.write_bytes(data)

                prepared_files[old_id] = file_path
                fetch_sources[old_id] = source

            except Exception as exc:
                official_errors[old_id] = str(exc)
                unresolved.append(old_id)

        # ----------------------------------------------------
        # Pass 2: one RBXDL Portal bulk job for everything
        # Roblox couldn't provide directly.
        # ----------------------------------------------------

        if unresolved:
            portal = None

            try:
                portal = RBXDLPortalDownloader(
                    status_callback=lambda message:
                        self.update_job(
                            job_id,
                            state="portal",
                            message=message,
                        )
                )

                self.update_job(
                    job_id,
                    state="portal",
                    message=(
                        f"Sending {len(unresolved)} unresolved "
                        f"asset(s) to RBXDL bulk portal..."
                    ),
                )

                portal_files, portal_errors = (
    portal.download_batch(
        unresolved,
        job_folder / "rbxdl-portal",
        allow_single_fallback=True,
    )
)

                for old_id, portal_file in (
                    portal_files.items()
                ):
                    prepared_files[old_id] = (
                        portal_file
                    )
                    fetch_sources[old_id] = (
                        "RBXDL Bulk Portal"
                    )

                for old_id in unresolved:
                    if old_id not in prepared_files:
                        portal_error = (
                            portal_errors.get(old_id)
                            or "RBXDL Portal did not return this asset."
                        )

                        errors[old_id] = (
                            "Official Roblox fetch failed: "
                            + official_errors.get(
                                old_id,
                                "unknown error",
                            )
                            + " | RBXDL: "
                            + portal_error
                        )

            except Exception as exc:
                portal_failure = str(exc)

                for old_id in unresolved:
                    if old_id not in prepared_files:
                        errors[old_id] = (
                            "Official Roblox fetch failed: "
                            + official_errors.get(
                                old_id,
                                "unknown error",
                            )
                            + " | RBXDL Portal bulk job failed: "
                            + portal_failure
                        )

            finally:
                if portal is not None:
                    portal.close()

        # ----------------------------------------------------
        # Pass 3: upload all successfully prepared files.
        # ----------------------------------------------------

        completed = 0

        for old_id, entry in unique.items():
            name = suggested_animation_name(
                entry
            )

            if old_id not in prepared_files:
                completed += 1

                self.update_job(
                    job_id,
                    completed=completed,
                    migrated=len(mapping),
                    failed=len(errors),
                    mapping=dict(mapping),
                    errors=dict(errors),
                    message=(
                        f"Processed {completed}/{len(unique)}"
                    ),
                )
                continue

            file_path = prepared_files[old_id]

            try:
                if file_path.stat().st_size > MAX_FILE_BYTES:
                    raise RuntimeError(
                        "Prepared animation exceeds Roblox's 20 MB upload limit."
                    )

                self.update_job(
                    job_id,
                    state="uploading",
                    message=(
                        f"Uploading {name} "
                        f"(source: {fetch_sources.get(old_id, 'unknown')})"
                    ),
                )

                operation = create_animation(
                    config["api_key"],
                    file_path,
                    name,
                    config["creator_type"],
                    config["creator_id"],
                    config["description"],
                )

                new_id = wait_for_operation(
                    config["api_key"],
                    operation,
                )

                mapping[old_id] = new_id

                # If a previous fetch-stage error was set but we ultimately
                # migrated this asset, do not count it as failed.
                errors.pop(
                    old_id,
                    None,
                )

            except Exception as exc:
                errors[old_id] = (
                    f"Upload failed after fetching via "
                    f"{fetch_sources.get(old_id, 'unknown')}: {exc}"
                )

            completed += 1

            self.update_job(
                job_id,
                completed=completed,
                migrated=len(mapping),
                failed=len(errors),
                mapping=dict(mapping),
                errors=dict(errors),
                message=(
                    f"Processed {completed}/{len(unique)}"
                ),
            )

        with self.source_cache_lock:
            for old_id in unique.keys():
                self.source_cache.pop(
                    old_id,
                    None,
                )

        if mapping and errors:
            state = "partial"
            message = (
                f"Finished with {len(mapping)} migrated "
                f"and {len(errors)} failed."
            )
        elif mapping:
            state = "done"
            message = (
                f"Successfully migrated "
                f"{len(mapping)} unique animation(s)."
            )
        else:
            state = "failed"

            first_error = next(
                iter(errors.values()),
                "Unknown fetch/upload error.",
            )

            message = (
                "No animations could be migrated. "
                + first_error
            )

        try:
            (
                job_folder
                / "migration-diagnostics.json"
            ).write_text(
                json.dumps(
                    {
                        "tower": tower,
                        "mapping": mapping,
                        "errors": errors,
                        "fetchSources":
                            fetch_sources,
                    },
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
            mapping=dict(mapping),
            errors=dict(errors),
            migrated=len(mapping),
            failed=len(errors),
            completed=len(unique),
            message=message,
        )

    def copy_selected_job_errors(self):
        selected = self.tree.selection()

        if not selected:
            messagebox.showinfo(
                "No job selected",
                "Select a migration job first.",
                parent=self,
            )
            return

        iid = selected[0]

        if not iid.startswith("job_"):
            return

        job_id = iid[4:]
        job = self.get_job(job_id)

        if not job:
            return

        errors = job.get("errors") or {}

        if not errors:
            messagebox.showinfo(
                "No errors",
                "That job has no per-animation errors.",
                parent=self,
            )
            return

        lines = [
            f"Tower: {job.get('tower', '')}",
            f"State: {job.get('state', '')}",
            "",
        ]

        for asset_id, error in errors.items():
            lines.append(f"{asset_id}: {error}")

        text = "\n".join(lines)

        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()

        messagebox.showinfo(
            "Errors copied",
            f"Copied {len(errors)} per-animation error(s) to the clipboard.",
            parent=self,
        )

    # --------------------------------------------------------
    # Main loop / job table
    # --------------------------------------------------------

    def refresh_job_row(self, job_id):
        job = self.get_job(job_id)
        if not job:
            return

        iid = "job_" + job_id

        values = (
            job["tower"],
            job["state"],
            f'{job["completed"]}/{job["total"]}',
            job["migrated"],
            job["failed"],
            job["message"],
        )

        if self.tree.exists(iid):
            self.tree.item(
                iid,
                values=values,
            )
        else:
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=values,
            )

        self.tree.see(iid)

    def _tick(self):
        self.sync_runtime_config()

        try:
            while True:
                event = self.ui_queue.get_nowait()

                if event[0] in {
                    "job_created",
                    "job_updated",
                }:
                    self.refresh_job_row(event[1])

        except queue.Empty:
            pass

        self.after(100, self._tick)

    def on_close(self):
        try:
            self.save_settings()
        except Exception:
            pass

        self.stop_server()
        self.destroy()


if __name__ == "__main__":
    APP_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    MigratorApp().mainloop()
