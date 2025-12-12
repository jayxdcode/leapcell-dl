# app.py
import os
import asyncio
import time
import pathlib
import urllib.parse
import shutil
import re
import base64
import logging
from typing import Optional, Tuple

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
import redis.asyncio as aioredis
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import aiofiles
import aiohttp

app = FastAPI(title="LeapCell downloader + rclone->mega cache")

# -- logging setup --
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("leapcell-dl")

# Config via env vars
SERVICE_URL_TEMPLATE = os.getenv("SERVICE_URL_TEMPLATE", "https://leapcell.example/item/{id}")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_TTL = int(os.getenv("REDIS_TTL", str(60 * 60 * 24)))
BROWSER_EXECUTABLE_PATH = os.getenv("BROWSER_EXECUTABLE_PATH")
RCLONE_REMOTE = os.getenv("RCLONE_REMOTE", "mega")
RCLONE_REMOTE_FOLDER = os.getenv("RCLONE_REMOTE_FOLDER", "leapcell_cache")
# WAIT_MS was previously in ms; keep naming but convert to seconds when used
WAIT_MS = float(os.getenv("WAIT_MS", "2500"))  # milliseconds
PLAYWRIGHT_DOWNLOAD_TIMEOUT = int(os.getenv("PLAYWRIGHT_DOWNLOAD_TIMEOUT_MS", "15000"))  # ms
# A global soft timeout for whole operation (seconds) to avoid indefinite runs
OPERATION_TIMEOUT_S = float(os.getenv("OPERATION_TIMEOUT_S", "55"))

# Redis client (async)
redis = aioredis.from_url(REDIS_URL, decode_responses=True)


def make_target_url(id: str) -> str:
    return SERVICE_URL_TEMPLATE.format(id=id)


async def rclone_rstream_upload_bytes(data_bytes: bytes, remote_folder: str, filename: str) -> str:
    """
    Streams data_bytes to 'rclone rcat <remote>:<folder>/<filename>' and then returns rclone link.
    Added logging and timeouts so callers see progress in stdout.
    """
    remote_target = f"{RCLONE_REMOTE}:{remote_folder.rstrip('/')}/{filename}"
    logger.info("rclone: starting rcat to %s (size=%d bytes)", remote_target, len(data_bytes))
    try:
        proc = await asyncio.create_subprocess_exec(
            "rclone", "rcat", remote_target,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        out, err = await proc.communicate(input=data_bytes)
        if proc.returncode != 0:
            err_text = err.decode().strip() or "no stderr"
            logger.error("rclone rcat failed: %s", err_text)
            raise RuntimeError(f"rclone rcat failed: {err_text}")
        logger.info("rclone: rcat finished, now requesting shareable link")

        proc2 = await asyncio.create_subprocess_exec(
            "rclone", "link", remote_target,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        out2, err2 = await proc2.communicate()
        if proc2.returncode != 0:
            err_text = err2.decode().strip() or "no stderr"
            logger.error("rclone link failed: %s", err_text)
            raise RuntimeError(f"rclone link failed: {err_text}")

        link = out2.decode().strip()
        logger.info("rclone: link obtained: %s", link)
        return link
    except Exception:
        logger.exception("Unexpected error during rclone upload")
        raise


async def fetch_download_url_from_page(target_url: str, selector_hint: Optional[str] = None) -> str:
    """
    Navigates to target_url with Playwright, logs progress, verifies browser/context/page states,
    then tries to find a likely download URL and returns it (absolute URL).
    This helper does not itself download the file — it returns the URL to download.
    """
    logger.info("fetch: starting browser fetch for %s", target_url)
    try:
        # Use an overall timeout so we don't run forever
        async with async_playwright() as p:
            # Shortened connection class name logic for logging clarity
            logger.info("fetch: playwright started")
            
            browser_launch_args = {"headless": True}
            if BROWSER_EXECUTABLE_PATH:
                browser_launch_args["executable_path"] = BROWSER_EXECUTABLE_PATH
                logger.info("fetch: using provided browser executable: %s", BROWSER_EXECUTABLE_PATH)

            logger.info("fetch: launching chromium...")
            browser = await p.chromium.launch(**browser_launch_args)
            
            # Basic verification
            try:
                # Some older Playwright versions or specific contexts might behave differently
                if hasattr(browser, "is_connected"):
                    connected = browser.is_connected()
                    logger.info("fetch: browser.is_connected() => %s", connected)
                else:
                    logger.info("fetch: browser object created")
            except Exception:
                logger.exception("fetch: error checking browser connection state")

            context = await browser.new_context()
            page = await context.new_page()
            logger.info("fetch: new page created")

            # Navigate
            logger.info("fetch: navigating to %s (timeout=%sms)", target_url, PLAYWRIGHT_DOWNLOAD_TIMEOUT)
            try:
                response = await page.goto(target_url, timeout=PLAYWRIGHT_DOWNLOAD_TIMEOUT)
                if response:
                    logger.info("fetch: navigation finished: status=%s url=%s", response.status, response.url)
                else:
                    logger.warning("fetch: navigation returned no response object")
            except PlaywrightTimeoutError:
                logger.exception("fetch: page.goto timed out after %s ms", PLAYWRIGHT_DOWNLOAD_TIMEOUT)
                raise

            # Wait for load state
            try:
                logger.info("fetch: waiting for load state 'networkidle' for up to %sms", PLAYWRIGHT_DOWNLOAD_TIMEOUT)
                await page.wait_for_load_state("networkidle", timeout=PLAYWRIGHT_DOWNLOAD_TIMEOUT)
                logger.info("fetch: load state reached (networkidle)")
            except PlaywrightTimeoutError:
                logger.warning("fetch: waiting for networkidle timed out, proceeding anyway")

            # post-load small wait (converted ms->s)
            wait_s = WAIT_MS / 1000.0
            logger.info("fetch: sleeping for %.3fs to allow JS to stabilize", wait_s)
            await asyncio.sleep(wait_s)

            # Try to find download link: use hint selector if provided, otherwise look for likely links
            if selector_hint:
                logger.info("fetch: looking for selector hint: %s", selector_hint)
                try:
                    el = await page.query_selector(selector_hint)
                    if el:
                        href = await el.get_attribute("href")
                        if href:
                            download_url = urllib.parse.urljoin(page.url, href)
                            logger.info("fetch: found download URL via hint: %s", download_url)
                            await browser.close()
                            return download_url
                        else:
                            logger.warning("fetch: element found for hint but no href attribute")
                    else:
                        logger.warning("fetch: selector hint not found on the page")
                except Exception:
                    logger.exception("fetch: error while trying selector hint")

            logger.info("fetch: scanning page anchors for likely file links")
            anchors = await page.query_selector_all("a")
            candidate = None
            for a in anchors:
                href = await a.get_attribute("href")
                if not href:
                    continue
                # common file extensions
                if re.search(r"\.(zip|tar\.gz|tgz|mp4|mkv|pdf|exe|bin|7z|rar)$", href, re.IGNORECASE):
                    candidate = urllib.parse.urljoin(page.url, href)
                    logger.info("fetch: candidate found (by extension): %s", candidate)
                    break
                # or 'download' rel/class
                cls = await a.get_attribute("class") or ""
                rel = await a.get_attribute("rel") or ""
                if "download" in (href.lower() + cls.lower() + rel.lower()):
                    candidate = urllib.parse.urljoin(page.url, href)
                    logger.info("fetch: candidate found (by download marker): %s", candidate)
                    break

            if not candidate:
                # If nothing obvious, log and return the page URL itself for later manual handling
                logger.warning("fetch: no obvious download link found; will return page URL for further inspection")
                candidate = page.url

            await browser.close()
            logger.info("fetch: browser closed, returning candidate url")
            return candidate

    except Exception:
        logger.exception("fetch: unexpected error during fetch flow")
        raise


@app.get("/", response_class=PlainTextResponse)
async def root_handler():
    """
    Shows simple usage instructions.
    """
    return (
        "LeapCell Downloader + Rclone/Mega Cache API\n"
        "===========================================\n\n"
        "Usage:\n"
        "  GET /api/v1/fetch?id=<ITEM_ID>\n"
        "      - Fetches the download URL for the given item ID.\n"
        "      - Optional query param: &selector_hint=<CSS_SELECTOR>\n\n"
        "Example:\n"
        "  curl \"http://localhost:8000/api/v1/fetch?id=12345\"\n\n"
        "Environment:\n"
        f"  SERVICE_URL_TEMPLATE: {SERVICE_URL_TEMPLATE}\n"
        f"  RCLONE_REMOTE: {RCLONE_REMOTE}\n"
    )


@app.get("/api/v1/fetch")
async def fetch_item_handler(
    id: str = Query(..., description="The item ID to fetch"),
    selector_hint: Optional[str] = Query(None, description="Optional CSS selector hint for the download link")
):
    logger.info("API: /api/v1/fetch called (id=%s, selector_hint=%s)", id, selector_hint)
    
    # Check cache first
    try:
        cached_url = await redis.get(f"leapcell:link:{id}")
        if cached_url:
            logger.info("API: cache hit for %s => %s", id, cached_url)
            return JSONResponse({"id": id, "url": cached_url, "cached": True})
    except Exception:
        logger.warning("API: redis unavailable, skipping cache check")

    target = make_target_url(id)
    
    try:
        # enforce a soft per-request timeout to avoid gateway/timeouts
        result_url = await asyncio.wait_for(
            fetch_download_url_from_page(target, selector_hint),
            timeout=OPERATION_TIMEOUT_S
        )
        
        logger.info("API: fetch completed for %s => %s", id, result_url)
        
        # store in redis cache for fast responses later
        try:
            await redis.set(f"leapcell:link:{id}", result_url, ex=REDIS_TTL)
            logger.info("API: cached result in redis for %s (ttl=%s)", id, REDIS_TTL)
        except Exception:
            logger.exception("API: failed to write to redis (non-fatal)")

        return JSONResponse({"id": id, "url": result_url, "cached": False})
    
    except asyncio.TimeoutError:
        logger.error("API: overall operation timed out after %.1fs", OPERATION_TIMEOUT_S)
        raise HTTPException(status_code=504, detail="Operation timed out")
    except Exception as e:
        logger.exception("API: unexpected error while handling request")
        raise HTTPException(status_code=500, detail=str(e))
