"""HTTP plumbing. Yahoo and Telegram are both fetched with a browser-impersonating
curl handle, because a plain urllib user agent gets rate limited quickly.

A curl handle cannot be shared between threads and the universe is polled on a
pool, so every thread keeps its own session.
"""

import json
import threading
import time
from pathlib import Path

from curl_cffi import requests

from .config import CACHE_DIR, REQUEST_TIMEOUT

# Bot blocks and missing symbols do not recover by trying again.
_NO_RETRY_STATUS = {401, 403, 404}

_local = threading.local()
_cache_lock = threading.Lock()


def session():
    existing = getattr(_local, "session", None)
    if existing is None:
        existing = requests.Session(impersonate="chrome")
        existing.headers.update({"Accept-Language": "en-US,en;q=0.9"})
        _local.session = existing
    return existing


def reset_session() -> None:
    _local.session = None


NSE_HOME = "https://www.nseindia.com/"
NSE_DEALS_PAGE = "https://www.nseindia.com/report-detail/display-bulk-and-block-deals"


def nse_session():
    """NSE serves its APIs only to clients that already hold page cookies."""
    existing = getattr(_local, "nse", None)
    if existing is None:
        existing = requests.Session(impersonate="chrome")
        existing.headers.update({"Accept-Language": "en-US,en;q=0.9"})
        existing.get(NSE_HOME, timeout=REQUEST_TIMEOUT)
        existing.get(NSE_DEALS_PAGE, timeout=REQUEST_TIMEOUT)
        existing.headers.update(
            {
                "Referer": NSE_DEALS_PAGE,
                "Accept": "*/*",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        _local.nse = existing
    return existing


def nse_get_text(url: str, attempts: int = 2) -> str:
    """Raw body from an NSE API. Raises when NSE will not serve this client.

    NSE returns 403 to most cloud addresses, so callers must treat failure as
    normal and say so rather than reporting an empty result as "no deals".
    """
    last_error = None
    for attempt in range(attempts):
        try:
            response = nse_session().get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                return response.text
            last_error = f"HTTP {response.status_code}"
        except Exception as exc:
            last_error = repr(exc)
        _local.nse = None
        if attempt + 1 < attempts:
            time.sleep(1 + attempt)
    raise RuntimeError(f"NSE request failed ({last_error})")


def bse_session():
    """BSE's JSON APIs answer only with its own site as the origin.

    The ipo tool drives these endpoints through a real browser because BSE blocks
    plain HTTP clients. A curl handle impersonating Chrome clears the same bar,
    which is what lets this run without installing a browser.
    """
    existing = getattr(_local, "bse", None)
    if existing is None:
        existing = requests.Session(impersonate="chrome")
        existing.headers.update(
            {
                "Referer": "https://www.bseindia.com/",
                "Origin": "https://www.bseindia.com",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        _local.bse = existing
    return existing


def bse_get_json(url: str, attempts: int = 3):
    last_error = None
    for attempt in range(attempts):
        try:
            response = bse_session().get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                return response.json()
            last_error = f"HTTP {response.status_code}"
            if response.status_code in _NO_RETRY_STATUS:
                break
        except Exception as exc:
            last_error = repr(exc)
            _local.bse = None
        if attempt + 1 < attempts:
            time.sleep(1 + attempt)
    raise RuntimeError(f"BSE request failed ({last_error}): {url}")


def get_text(url: str, attempts: int = 3) -> str:
    """Raw body, for the pages that are scraped rather than read as JSON."""
    last_error = None
    for attempt in range(attempts):
        try:
            response = session().get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                return response.text
            last_error = f"HTTP {response.status_code}"
            if response.status_code in _NO_RETRY_STATUS:
                break
            reset_session()
        except Exception as exc:
            last_error = repr(exc)
            reset_session()
        if attempt + 1 < attempts:
            time.sleep(1 + attempt)
    raise RuntimeError(f"Request failed ({last_error}): {url}")


def get_json(url: str, attempts: int = 3):
    last_error = None
    for attempt in range(attempts):
        try:
            response = session().get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                return response.json()
            last_error = f"HTTP {response.status_code}"
            if response.status_code in _NO_RETRY_STATUS:
                break
            # 429 means the handle is already marked; a fresh one fares better.
            reset_session()
        except Exception as exc:
            last_error = repr(exc)
            reset_session()
        if attempt + 1 < attempts:
            time.sleep(1 + attempt)
    raise RuntimeError(f"Request failed ({last_error}): {url}")


def post_json(url: str, payload: dict, attempts: int = 3):
    last_error = None
    for attempt in range(attempts):
        try:
            response = session().post(url, json=payload, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                return response.json()
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            if response.status_code in _NO_RETRY_STATUS:
                break
        except Exception as exc:
            last_error = repr(exc)
            reset_session()
        if attempt + 1 < attempts:
            time.sleep(1 + attempt)
    raise RuntimeError(f"Post failed ({last_error})")


def cache_path(name: str) -> Path:
    return CACHE_DIR / name


def read_cache(name: str, ttl_seconds: int):
    path = cache_path(name)
    if not path.exists():
        return None
    if time.time() - path.stat().st_mtime > ttl_seconds:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_cache(name: str, payload) -> None:
    path = cache_path(name)
    # Written via a temporary file so a concurrent reader never sees half a file.
    temporary = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    try:
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        with _cache_lock:
            temporary.replace(path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
