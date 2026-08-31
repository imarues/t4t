import asyncio
import html
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import socket
import stat
import time
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote, urlparse

import aiohttp
from PIL import Image, ImageOps
from yarl import URL

try:
    from playwright.async_api import async_playwright
except Exception:
    async_playwright = None

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
except Exception:
    CurlAsyncSession = None

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:
    pass

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
BOT_API_BASE = os.getenv("BOT_API_BASE", "http://127.0.0.1:8081").rstrip("/")
DOWNLOAD_ROOT = Path(os.getenv("DOWNLOAD_ROOT", "/tmp/direct-file-bot-downloads"))
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", "2000000000"))  # exact 2.00 GB decimal
# Source files above Telegram's send limit may be downloaded only after explicit
# confirmation so the bot can try rebuilding the IPA as a stronger ZIP.
MAX_SOURCE_BYTES = int(os.getenv("MAX_SOURCE_BYTES", "4000000000"))
MAX_EXTRACTED_BYTES = int(os.getenv("MAX_EXTRACTED_BYTES", "12000000000"))
PROGRESS_EDIT_INTERVAL = float(os.getenv("PROGRESS_EDIT_INTERVAL", "1.5"))
ARIA_CONNECTIONS = int(os.getenv("ARIA_CONNECTIONS", "16"))
ARIA_STALL_SECONDS = int(os.getenv("ARIA_STALL_SECONDS", "30"))
BROWSER_WARMUP_SECONDS = float(os.getenv("BROWSER_WARMUP_SECONDS", "4"))
ADMIN_USER_ID_LIST = [
    int(x.strip())
    for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
]
ALLOWED_USER_IDS = set(ADMIN_USER_ID_LIST)

if len(ALLOWED_USER_IDS) != 2:
    raise RuntimeError(
        "ALLOWED_USER_IDS must contain exactly two Telegram numeric user IDs, separated by a comma."
    )

OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", str(ADMIN_USER_ID_LIST[0] if ADMIN_USER_ID_LIST else 0)) or 0)
if OWNER_USER_ID not in ALLOWED_USER_IDS:
    raise RuntimeError("OWNER_USER_ID must be one of the two ALLOWED_USER_IDS.")

REQUIRED_CHANNEL_INVITE = os.getenv("REQUIRED_CHANNEL_INVITE", "").strip()
CHANNEL_CONFIG_PATH = Path(os.getenv("CHANNEL_CONFIG_PATH", "required_channel.json"))
LINK_LOG_PATH = Path(os.getenv("LINK_LOG_PATH", "links_log.txt"))
LINK_LOG_TIMEZONE = os.getenv("LINK_LOG_TIMEZONE", "Asia/Baghdad")
# Each ordinary user may have up to three active download/send jobs at once.
MAX_ACTIVE_DOWNLOADS_PER_USER = int(os.getenv("MAX_ACTIVE_DOWNLOADS_PER_USER", "3"))
# Private monitoring channel derived from https://t.me/c/3734893457/83
LINK_REPORT_CHANNEL_ID = int(os.getenv("LINK_REPORT_CHANNEL_ID", "-1003734893457"))

def _load_required_channel_id() -> int:
    raw = os.getenv("REQUIRED_CHANNEL_ID", "").strip()
    if raw and raw.lstrip("-").isdigit():
        return int(raw)
    try:
        data = json.loads(CHANNEL_CONFIG_PATH.read_text(encoding="utf-8"))
        value = str(data.get("channel_id", "")).strip()
        if value and value.lstrip("-").isdigit():
            return int(value)
    except Exception:
        pass
    return 0

REQUIRED_CHANNEL_ID = _load_required_channel_id()

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
FILENAME_STAR_RE = re.compile(r"filename\*=UTF-8''([^;]+)", re.IGNORECASE)
FILENAME_RE = re.compile(r'filename="?([^";]+)"?', re.IGNORECASE)
CONTENT_RANGE_RE = re.compile(r"bytes\s+\d+-\d+/(\d+|\*)", re.IGNORECASE)
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".avif", ".jfif", ".ico", ".svg", ".psd", ".jp2", ".j2k",
}

active_jobs = set()

class _NoopAsyncLock:
    def locked(self) -> bool:
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

# Legacy job bodies still use `async with download_lock`; making it a no-op
# allows true parallel transfers while the per-user counter below enforces 3 max.
download_lock = _NoopAsyncLock()
shutdown_event = asyncio.Event()
pending_jobs: Dict[str, Dict[str, Any]] = {}
user_flows: Dict[int, Dict[str, Any]] = {}
channel_bind_waiting = set()
link_log_lock = asyncio.Lock()
channel_config_lock = asyncio.Lock()
git_persist_lock = asyncio.Lock()
user_active_downloads: Dict[int, int] = {}
user_active_downloads_lock = asyncio.Lock()


def human_bytes(value: int) -> str:
    # Display file sizes using decimal units so the UI matches the
    # actual direct-send limit: 2,000,000,000 bytes = 2.00 GB.
    if value < 1000:
        return f"{value} B"
    units = ["KB", "MB", "GB", "TB"]
    number = float(value)
    for unit in units:
        number /= 1000.0
        if number < 1000 or unit == units[-1]:
            return f"{number:.2f} {unit}"
    return f"{value} B"


def human_eta(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0 or seconds > 7 * 24 * 3600:
        return "--"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def progress_bar(percent: float, width: int = 14) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(round(width * percent / 100.0))
    return "█" * filled + "░" * (width - filled)


def sanitize_filename(name: str) -> str:
    name = unquote(name or "").strip().replace("\\", "_").replace("/", "_")
    name = re.sub(r"[\x00-\x1f\x7f]", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "download.bin"
    if len(name) > 180:
        stem, dot, suffix = name.rpartition(".")
        if dot and len(suffix) <= 16:
            name = stem[: 160 - len(suffix)] + "." + suffix
        else:
            name = name[:180]
    return name


def filename_from_headers(headers: aiohttp.typedefs.LooseHeaders, final_url: str) -> str:
    cd = str(headers.get("Content-Disposition", ""))
    match = FILENAME_STAR_RE.search(cd)
    if match:
        return sanitize_filename(match.group(1))
    match = FILENAME_RE.search(cd)
    if match:
        return sanitize_filename(match.group(1))
    path_name = Path(urlparse(final_url).path).name
    return sanitize_filename(path_name or "download.bin")


def filename_from_url(url: str) -> str:
    path_name = Path(urlparse(url).path).name
    return sanitize_filename(path_name or "download.bin")


def normalize_url(url: str) -> str:
    return html.unescape(url.strip())


def referer_candidates(url: str) -> list[str]:
    """Browser-like referrers, including the parent site for file subdomains."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return []

    host = (parsed.hostname or "").strip(".")
    values = [f"{parsed.scheme}://{parsed.netloc}/"]
    parts = host.split(".")
    if len(parts) >= 3:
        root = ".".join(parts[-2:])
        values.extend([f"https://{root}/", f"https://www.{root}/"])

    out: list[str] = []
    seen = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def browser_headers(
    url: str,
    *,
    use_referer: bool = True,
    referer: Optional[str] = None,
    navigation: bool = False,
) -> Dict[str, str]:
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
            if navigation else "*/*"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
    }
    if navigation:
        headers.update({
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-User": "?1",
        })
    if use_referer:
        if referer:
            headers["Referer"] = referer
        else:
            refs = referer_candidates(url)
            if refs:
                headers["Referer"] = refs[0]
    return headers


def find_browser_executable() -> Optional[str]:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    for path in (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ):
        if Path(path).exists():
            return path
    return None


async def seed_http_cookies(session: aiohttp.ClientSession, url: str) -> None:
    """Visit likely parent pages once so ordinary anti-hotlink cookies can be set."""
    timeout = aiohttp.ClientTimeout(total=15, connect=8, sock_read=8)
    for ref in referer_candidates(url)[1:] + referer_candidates(url)[:1]:
        try:
            async with session.get(
                ref,
                headers=browser_headers(ref, use_referer=False, navigation=True),
                allow_redirects=True,
                timeout=timeout,
            ) as response:
                await response.content.read(64 * 1024)
                if response.status < 400:
                    return
        except Exception:
            continue


async def browser_session_warmup(
    session: aiohttp.ClientSession,
    url: str,
) -> Tuple[str, Optional[str]]:
    """
    Last-resort browser warmup. It runs only after ordinary download paths fail,
    collects browser cookies, probes the direct URL with a tiny Range request,
    then hands those cookies back to aiohttp/aria2 for the real streamed download.
    """
    if async_playwright is None:
        raise RuntimeError("Playwright غير متاح لمسار المتصفح الاحتياطي")
    executable = find_browser_executable()
    if not executable:
        raise RuntimeError("لم يتم العثور على Chrome/Chromium لمسار المتصفح الاحتياطي")

    final_url = url
    chosen_referer: Optional[str] = None
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=executable,
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            context = await browser.new_context(
                user_agent=BROWSER_UA,
                locale="en-US",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            page = await context.new_page()
            page.set_default_timeout(20_000)
            page.set_default_navigation_timeout(30_000)

            # Prefer the parent/main site first: file subdomains often require a
            # cookie or a same-site referrer created by a normal page visit.
            refs = referer_candidates(url)
            warm_refs = refs[1:] + refs[:1]
            for ref in warm_refs:
                try:
                    response = await page.goto(ref, wait_until="domcontentloaded")
                    await page.wait_for_timeout(int(BROWSER_WARMUP_SECONDS * 1000))
                    title = (await page.title()).lower()
                    if "just a moment" in title or "checking your browser" in title:
                        await page.wait_for_timeout(8_000)
                    if response is None or response.status < 400:
                        chosen_referer = ref
                        break
                except Exception:
                    continue

            # A one-byte probe through the browser context can refresh redirects
            # and cookies without buffering the actual multi-GB file in memory.
            probe_headers = browser_headers(
                url,
                referer=chosen_referer,
                navigation=True,
            )
            probe_headers["Range"] = "bytes=0-0"
            try:
                probe = await context.request.get(
                    url,
                    headers=probe_headers,
                    timeout=30_000,
                    fail_on_status_code=False,
                )
                if probe.status < 400:
                    final_url = probe.url
            except Exception:
                pass

            cookies = await context.cookies()
            for cookie in cookies:
                name = str(cookie.get("name") or "")
                value = str(cookie.get("value") or "")
                domain = str(cookie.get("domain") or "").lstrip(".")
                if not name or not domain:
                    continue
                scheme = "https" if cookie.get("secure") else "http"
                try:
                    session.cookie_jar.update_cookies(
                        {name: value},
                        response_url=URL(f"{scheme}://{domain}/"),
                    )
                except Exception:
                    continue
        finally:
            await browser.close()

    return final_url, chosen_referer


def custom_filename(typed: str, original: str) -> str:
    typed = sanitize_filename(typed)
    if Path(typed).suffix:
        return typed
    original_suffix = Path(original).suffix
    if original_suffix:
        return sanitize_filename(typed + original_suffix)
    return typed


def offer_keyboard(token: str) -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "📤 إرسال الآن", "callback_data": f"send:{token}"}],
            [{"text": "✏️ تغيير الاسم والصورة ثم إرسال", "callback_data": f"custom:{token}"}],
        ]
    }


def large_file_keyboard(token: str) -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "❌ إلغاء", "callback_data": f"cancel:{token}"},
                {"text": "🗜️ أكمل", "callback_data": f"compress:{token}"},
            ]
        ]
    }


def skip_thumbnail_keyboard(token: str) -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "⏭️ تخطي الصورة", "callback_data": f"skipthumb:{token}"}]
        ]
    }


def empty_keyboard() -> Dict[str, Any]:
    return {"inline_keyboard": []}


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class BotAPIError(RuntimeError):
    pass


class LargeFileDetected(RuntimeError):
    def __init__(self, size: int):
        self.size = int(size)
        super().__init__(f"حجم الملف {human_bytes(self.size)} أكبر من حد الإرسال المباشر {human_bytes(MAX_FILE_BYTES)}")


class CompressionNotEnough(RuntimeError):
    def __init__(self, original_size: int, compressed_size: int):
        self.original_size = int(original_size)
        self.compressed_size = int(compressed_size)
        super().__init__(
            f"بعد أقصى ضغط بقي الحجم {human_bytes(self.compressed_size)}، وهو أكبر من حد الإرسال {human_bytes(MAX_FILE_BYTES)}"
        )


async def bot_api(
    session: aiohttp.ClientSession,
    method: str,
    data: Optional[Dict[str, Any]] = None,
    *,
    timeout: Optional[float] = 60,
) -> Any:
    url = f"{BOT_API_BASE}/bot{BOT_TOKEN}/{method}"
    client_timeout = aiohttp.ClientTimeout(total=timeout) if timeout else aiohttp.ClientTimeout(total=None)
    async with session.post(url, json=data or {}, timeout=client_timeout) as response:
        text = await response.text()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BotAPIError(f"Invalid Bot API response ({response.status}): {text[:300]}") from exc
        if not payload.get("ok"):
            raise BotAPIError(payload.get("description", f"Bot API error {response.status}"))
        return payload.get("result")


async def send_text(
    session: aiohttp.ClientSession,
    chat_id: int,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        data["reply_markup"] = reply_markup
    return await bot_api(session, "sendMessage", data)


def _display_name(user: Dict[str, Any]) -> str:
    first = str(user.get("first_name") or "").strip()
    last = str(user.get("last_name") or "").strip()
    name = " ".join(x for x in (first, last) if x).strip()
    return name or "بدون اسم"


def _username_text(user: Dict[str, Any]) -> str:
    username = str(user.get("username") or "").strip().lstrip("@")
    return f"@{username}" if username else "لا يوجد"


def _owner_links_keyboard(user_id: int, username: str = "") -> Dict[str, Any]:
    # Always provide ID-based deep links, even when the account has no username.
    # The second scheme is client-dependent, so keeping both improves the chance
    # of opening the exact Telegram account from the owner notification.
    return {
        "inline_keyboard": [
            [{"text": "👤 فتح الحساب", "url": f"tg://user?id={user_id}"}],
            [{"text": "💬 فتح المحادثة", "url": f"tg://openmessage?user_id={user_id}"}],
        ]
    }


async def check_required_channel_membership(
    session: aiohttp.ClientSession,
    user_id: int,
) -> Tuple[bool, str]:
    if user_id in ALLOWED_USER_IDS:
        return True, "🛡️ مشرف — مستثنى"
    if not REQUIRED_CHANNEL_ID:
        return False, "⚠️ القناة لم تُربط بعد"
    try:
        member = await bot_api(
            session,
            "getChatMember",
            {"chat_id": REQUIRED_CHANNEL_ID, "user_id": user_id},
            timeout=30,
        )
    except Exception as exc:
        print(f"Membership check failed for {user_id}: {exc}", flush=True)
        return False, "⚠️ تعذر التحقق"

    status = str((member or {}).get("status") or "").lower()
    is_member = status in {"creator", "administrator", "member"}
    if status == "restricted":
        is_member = bool((member or {}).get("is_member"))
    return is_member, ("✅ مشترك" if is_member else "❌ غير مشترك")


async def notify_owner_about_user(
    session: aiohttp.ClientSession,
    user: Dict[str, Any],
    event_title: str,
    membership_text: Optional[str] = None,
) -> None:
    try:
        user_id = int(user.get("id") or 0)
    except Exception:
        return
    if not user_id or user_id in ALLOWED_USER_IDS:
        return
    if membership_text is None:
        _, membership_text = await check_required_channel_membership(session, user_id)
    username = str(user.get("username") or "").strip().lstrip("@")
    username_display = f"@{username}" if username else "لا يوجد"
    extra = ""
    text = (
        f"🔔 {event_title}\n\n"
        f"👤 الاسم: {_display_name(user)}\n"
        f"🏷 اليوزر: {username_display}\n"
        f"🆔 User ID: {user_id}\n"
        f"📡 حالة الاشتراك: {membership_text}"
        f"{extra}"
    )
    try:
        await send_text(
            session,
            OWNER_USER_ID,
            text,
            _owner_links_keyboard(user_id, username),
        )
    except Exception as exc:
        print(f"Owner notification failed: {exc}", flush=True)


def _git_run(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=check,
    )


def _git_branch_name() -> str:
    branch = os.getenv("GITHUB_REF_NAME", "").strip()
    if branch:
        return branch
    result = _git_run(["rev-parse", "--abbrev-ref", "HEAD"])
    value = (result.stdout or "").strip()
    return value if value and value != "HEAD" else "main"


def _configure_git_identity() -> None:
    _git_run(["config", "user.name", "Kira File Bot"])
    _git_run(["config", "user.email", "kira-bot@users.noreply.github.com"])


def _git_commit_and_push(paths: list[Path], message: str) -> bool:
    """Persist ordinary generated files with basic retry protection."""
    try:
        _configure_git_identity()
        branch = _git_branch_name()
        _git_run(["add", *[str(x) for x in paths]], check=True)

        commit = _git_run(["commit", "-m", message])
        output = (commit.stdout or "").lower()
        if commit.returncode != 0 and "nothing to commit" not in output:
            raise RuntimeError((commit.stdout or "git commit failed")[-2000:])

        for attempt in range(1, 4):
            push = _git_run(["push", "origin", f"HEAD:{branch}"])
            if push.returncode == 0:
                print(f"✅ Git persistence succeeded on attempt {attempt}.", flush=True)
                return True

            print(
                f"Git push attempt {attempt}/3 failed; refreshing branch and retrying...",
                flush=True,
            )
            fetch = _git_run(["fetch", "origin", branch])
            if fetch.returncode != 0:
                print(f"Git fetch warning: {(fetch.stdout or '')[-1200:]}", flush=True)
                time.sleep(attempt)
                continue

            # Merge any code/log updates made while this long-running bot job
            # was active. Prefer the remote side on textual conflicts, then
            # continue with our generated-file commit on top.
            merge = _git_run(
                ["merge", "--no-edit", "-X", "theirs", f"origin/{branch}"]
            )
            if merge.returncode != 0:
                _git_run(["merge", "--abort"])
                print(f"Git merge warning: {(merge.stdout or '')[-1200:]}", flush=True)
                time.sleep(attempt)
                continue

            _git_run(["add", *[str(x) for x in paths]])
            followup = _git_run(["commit", "-m", message])
            followup_output = (followup.stdout or "").lower()
            if (
                followup.returncode != 0
                and "nothing to commit" not in followup_output
            ):
                print(
                    f"Git follow-up commit warning: {(followup.stdout or '')[-1200:]}",
                    flush=True,
                )

        print("❌ Git persistence failed after 3 attempts.", flush=True)
        return False
    except Exception as exc:
        print(f"Git persistence warning: {exc}", flush=True)
        return False


def _remote_file_text(branch: str, path: Path) -> str:
    result = _git_run(["show", f"origin/{branch}:{path.as_posix()}"])
    if result.returncode != 0:
        return ""
    return result.stdout or ""


def _append_link_log_and_push(block: str, user_id: int) -> bool:
    """
    Append one Telegram link-log entry safely.

    The bot can stay alive for hours while the repository changes. Before every
    write we fetch the current branch and rebuild links_log.txt from the remote
    copy, so a normal code push does not make the log push non-fast-forward.
    If another update lands at the same moment, retry up to three times.
    """
    try:
        _configure_git_identity()
        branch = _git_branch_name()

        for attempt in range(1, 4):
            fetch = _git_run(["fetch", "origin", branch])
            if fetch.returncode != 0:
                print(
                    f"Link log fetch attempt {attempt}/3 failed: "
                    f"{(fetch.stdout or '')[-1200:]}",
                    flush=True,
                )
                time.sleep(attempt)
                continue

            # If a previous push actually succeeded but the connection dropped
            # before Git reported success, avoid writing the same entry twice.
            remote_text = _remote_file_text(branch, LINK_LOG_PATH)
            if block in remote_text:
                LINK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                LINK_LOG_PATH.write_text(remote_text, encoding="utf-8")
                print(
                    f"✅ Link log already persisted for Telegram user {user_id}.",
                    flush=True,
                )
                return True

            # Bring the working branch up to date. This can also pick up code
            # edits made while the running Python process continues in memory.
            merge = _git_run(
                ["merge", "--no-edit", "-X", "theirs", f"origin/{branch}"]
            )
            if merge.returncode != 0:
                _git_run(["merge", "--abort"])
                print(
                    f"Link log merge attempt {attempt}/3 failed: "
                    f"{(merge.stdout or '')[-1200:]}",
                    flush=True,
                )
                time.sleep(attempt)
                continue

            # Re-read after the merge, append exactly once, then commit.
            if LINK_LOG_PATH.exists():
                current = LINK_LOG_PATH.read_text(encoding="utf-8")
            else:
                current = ""
            if block not in current:
                LINK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                LINK_LOG_PATH.write_text(current + block, encoding="utf-8")

            _git_run(["add", str(LINK_LOG_PATH)], check=True)
            commit = _git_run(
                ["commit", "-m", f"Log link from Telegram user {user_id}"]
            )
            commit_output = (commit.stdout or "").lower()
            if commit.returncode != 0 and "nothing to commit" not in commit_output:
                print(
                    f"Link log commit attempt {attempt}/3 failed: "
                    f"{(commit.stdout or '')[-1200:]}",
                    flush=True,
                )
                time.sleep(attempt)
                continue

            push = _git_run(["push", "origin", f"HEAD:{branch}"])
            if push.returncode == 0:
                print(
                    f"✅ Link log persisted successfully "
                    f"(User ID: {user_id}, attempt {attempt}/3).",
                    flush=True,
                )
                return True

            print(
                f"Link log push attempt {attempt}/3 failed; retrying safely...",
                flush=True,
            )
            time.sleep(attempt)

        print(
            f"❌ Link log could not be persisted after 3 attempts "
            f"(User ID: {user_id}).",
            flush=True,
        )
        return False
    except Exception as exc:
        print(f"Link log persistence warning: {exc}", flush=True)
        return False


async def persist_link_log(user: Dict[str, Any], url: str) -> None:
    try:
        user_id = int(user.get("id") or 0)
    except Exception:
        return
    if not user_id or user_id in ALLOWED_USER_IDS:
        return

    first_name = str(user.get("first_name") or "").strip()
    last_name = str(user.get("last_name") or "").strip()
    full_name = " ".join(x for x in (first_name, last_name) if x).strip() or "لا يوجد"
    username = str(user.get("username") or "").strip().lstrip("@")
    username_display = f"@{username}" if username else "لا يوجد"
    try:
        now = datetime.now(ZoneInfo(LINK_LOG_TIMEZONE))
    except Exception:
        now = datetime.now()

    block = (
        f"[{now:%Y-%m-%d %H:%M}]\n"
        f"Name: {full_name}\n"
        f"User ID: {user_id}\n"
        f"Username: {username_display}\n"
        f"Link: {url}\n\n"
        "------------------------------\n\n"
    )

    # One process serializes writes so simultaneous users cannot race each other.
    # The Git helper then protects against external repository updates.
    async with link_log_lock:
        async with git_persist_lock:
            await asyncio.to_thread(
                _append_link_log_and_push,
                block,
                user_id,
            )


async def report_subscriber_link(
    session: aiohttp.ClientSession,
    user: Dict[str, Any],
    url: str,
) -> None:
    """Post each subscriber URL as its own message in the private report channel."""
    try:
        user_id = int(user.get("id") or 0)
    except Exception:
        return
    if not user_id or user_id in ALLOWED_USER_IDS or not LINK_REPORT_CHANNEL_ID:
        return

    first_name = str(user.get("first_name") or "").strip()
    last_name = str(user.get("last_name") or "").strip()
    full_name = " ".join(x for x in (first_name, last_name) if x).strip() or "لا يوجد"
    username = str(user.get("username") or "").strip().lstrip("@")
    username_display = f"@{username}" if username else "لا يوجد"
    try:
        now = datetime.now(ZoneInfo(LINK_LOG_TIMEZONE))
    except Exception:
        now = datetime.now()

    message = (
        "🔗 رابط جديد من مشترك\n\n"
        f"🕒 الوقت: {now:%Y-%m-%d %H:%M}\n"
        f"👤 الاسم: {full_name}\n"
        f"🆔 ID: {user_id}\n"
        f"📱 Username: {username_display}\n\n"
        f"🔗 الرابط:\n{url}"
    )
    try:
        await send_text(session, LINK_REPORT_CHANNEL_ID, message)
        print(f"✅ Subscriber link reported to channel (User ID: {user_id}).", flush=True)
    except Exception as exc:
        print(f"Link report channel warning for {user_id}: {exc}", flush=True)


async def _active_download_count(user_id: int) -> int:
    async with user_active_downloads_lock:
        return int(user_active_downloads.get(user_id, 0))


async def _claim_download_slot(user_id: int) -> bool:
    async with user_active_downloads_lock:
        current = int(user_active_downloads.get(user_id, 0))
        if current >= MAX_ACTIVE_DOWNLOADS_PER_USER:
            return False
        user_active_downloads[user_id] = current + 1
        return True


async def _release_download_slot(user_id: int) -> None:
    async with user_active_downloads_lock:
        current = int(user_active_downloads.get(user_id, 0))
        if current <= 1:
            user_active_downloads.pop(user_id, None)
        else:
            user_active_downloads[user_id] = current - 1


async def _run_with_download_slot(user_id: int, awaitable) -> None:
    try:
        await awaitable
    finally:
        await _release_download_slot(user_id)


def _forwarded_channel_id(message: Dict[str, Any]) -> Optional[int]:
    origin = message.get("forward_origin") or {}
    if origin.get("type") == "channel":
        chat = origin.get("chat") or {}
        value = chat.get("id")
        if value is not None:
            return int(value)
    legacy = message.get("forward_from_chat") or {}
    if legacy.get("type") == "channel" and legacy.get("id") is not None:
        return int(legacy["id"])
    return None


async def save_required_channel_id(channel_id: int) -> None:
    global REQUIRED_CHANNEL_ID
    REQUIRED_CHANNEL_ID = int(channel_id)
    payload = {
        "channel_id": REQUIRED_CHANNEL_ID,
        "invite_link_reference": REQUIRED_CHANNEL_INVITE,
        "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    async with channel_config_lock:
        CHANNEL_CONFIG_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        async with git_persist_lock:
            await asyncio.to_thread(
                _git_commit_and_push,
                [CHANNEL_CONFIG_PATH],
                f"Bind required Telegram channel {REQUIRED_CHANNEL_ID}",
            )


async def deny_non_subscriber(session: aiohttp.ClientSession, chat_id: int) -> None:
    await send_text(
        session,
        chat_id,
        "البوت متاح للاستخدام حصراً لدى مشتركين كيرا بلس.",
    )


async def edit_text(
    session: aiohttp.ClientSession,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> None:
    data: Dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        data["reply_markup"] = reply_markup
    try:
        await bot_api(session, "editMessageText", data)
    except BotAPIError as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def answer_callback(
    session: aiohttp.ClientSession,
    callback_query_id: str,
    text: Optional[str] = None,
    *,
    alert: bool = False,
) -> None:
    data: Dict[str, Any] = {"callback_query_id": callback_query_id, "show_alert": alert}
    if text:
        data["text"] = text[:200]
    try:
        await bot_api(session, "answerCallbackQuery", data)
    except Exception:
        pass


def response_size(response: aiohttp.ClientResponse) -> Optional[int]:
    content_range = response.headers.get("Content-Range", "")
    match = CONTENT_RANGE_RE.search(content_range)
    if match and match.group(1) != "*":
        return int(match.group(1))
    if response.status != 206 and response.content_length is not None:
        return int(response.content_length)
    return None


def response_size_from_headers(headers: Any, status: int) -> Optional[int]:
    """Read the real object size from Content-Range/Content-Length without downloading it."""
    content_range = str(headers.get("Content-Range", ""))
    match = CONTENT_RANGE_RE.search(content_range)
    if match and match.group(1) != "*":
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            pass

    # For a 206 response Content-Length is normally only the requested byte range,
    # not the complete file, so only trust it for a normal 200-style response.
    if int(status) != 206:
        raw_length = headers.get("Content-Length")
        if raw_length:
            try:
                return int(raw_length)
            except (TypeError, ValueError):
                pass
    return None


async def inspect_url_with_curl_cffi(url: str) -> Tuple[str, Optional[int], str]:
    """Browser-fingerprint preflight used by hosts that return 403 to aiohttp/HEAD.

    The request is streamed and immediately closed after the headers arrive, so a
    server that ignores Range won't make us download the whole file just to learn
    its size.
    """
    if CurlAsyncSession is None:
        return filename_from_url(url), None, url

    refs: list[Optional[str]] = [None]
    refs.extend(referer_candidates(url))
    profiles = ("chrome", "safari_ios", "safari")
    best_name = filename_from_url(url)
    best_url = url

    for profile in profiles:
        for ref in refs:
            # Range is preferred because Content-Range exposes the complete size.
            # Some hosts ignore/reject Range, so a streamed normal GET is the fallback.
            for use_range in (True, False):
                response = None
                try:
                    headers: Dict[str, str] = {
                        "Accept-Language": "en-US,en;q=0.9",
                        "Cache-Control": "no-cache",
                        "Pragma": "no-cache",
                    }
                    if use_range:
                        headers["Range"] = "bytes=0-0"

                    async with CurlAsyncSession(
                        impersonate=profile,
                        allow_redirects=True,
                        max_redirects=20,
                    ) as curl_session:
                        response = await curl_session.get(
                            url,
                            headers=headers,
                            referer=ref,
                            stream=True,
                            timeout=(12, 15),
                        )
                        status = int(response.status_code)
                        if status >= 400:
                            continue

                        final_url = str(response.url or url)
                        name = filename_from_headers(response.headers, final_url)
                        size = response_size_from_headers(response.headers, status)
                        best_name, best_url = name, final_url
                        if size is not None and size > 0:
                            return name, size, final_url
                except Exception:
                    continue
                finally:
                    try:
                        if response is not None:
                            await response.aclose()
                    except Exception:
                        pass

    return best_name, None, best_url


async def inspect_url(session: aiohttp.ClientSession, url: str) -> Tuple[str, Optional[int], str]:
    """Best-effort inspection. HTTP probe failures never block the real download."""
    url = normalize_url(url)
    timeout = aiohttp.ClientTimeout(total=25, connect=8, sock_read=10)
    base = browser_headers(url)
    best_name = filename_from_url(url)
    best_url = url

    # Cheap ordinary probes first. Never let them trigger a full download.
    attempts = [
        ("HEAD", {}),
        ("GET", {"Range": "bytes=0-0"}),
    ]

    for method, extra in attempts:
        headers = dict(base)
        headers.update(extra)
        try:
            async with session.request(
                method,
                url,
                headers=headers,
                allow_redirects=True,
                timeout=timeout,
            ) as response:
                if response.status >= 400:
                    continue
                name = filename_from_headers(response.headers, str(response.url))
                size = response_size(response)
                best_name, best_url = name, str(response.url)
                if size is not None and size > 0:
                    return name, size, str(response.url)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            continue

    # Use the same TLS/HTTP2 browser impersonation that already succeeds for the
    # real IPAOMTK download. This makes the size available before the user taps Send.
    try:
        curl_name, curl_size, curl_url = await asyncio.wait_for(
            inspect_url_with_curl_cffi(url), timeout=25
        )
    except asyncio.TimeoutError:
        curl_name, curl_size, curl_url = best_name, None, best_url
    if curl_size is not None:
        return curl_name, curl_size, curl_url
    if curl_name:
        best_name = curl_name
    if curl_url:
        best_url = curl_url

    # Some hosts don't advertise a size at all; real download is still allowed.
    return best_name, None, best_url


async def aria_rpc(
    session: aiohttp.ClientSession,
    port: int,
    secret: str,
    method: str,
    params: Optional[list] = None,
) -> Any:
    rpc_params = [f"token:{secret}"]
    if params:
        rpc_params.extend(params)
    payload = {
        "jsonrpc": "2.0",
        "id": secrets.token_hex(4),
        "method": method,
        "params": rpc_params,
    }
    async with session.post(
        f"http://127.0.0.1:{port}/jsonrpc",
        json=payload,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as response:
        body = await response.json(content_type=None)
        if "error" in body:
            raise RuntimeError(body["error"].get("message", "aria2 RPC error"))
        return body.get("result")


async def wait_aria_ready(session: aiohttp.ClientSession, port: int, secret: str) -> None:
    last_error: Optional[Exception] = None
    for _ in range(50):
        try:
            await aria_rpc(session, port, secret, "aria2.getVersion")
            return
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(0.1)
    raise RuntimeError(f"aria2 RPC لم يبدأ: {last_error}")


def cookie_header_for(session: aiohttp.ClientSession, url: str) -> str:
    try:
        cookies = session.cookie_jar.filter_cookies(url)
        return "; ".join(f"{name}={morsel.value}" for name, morsel in cookies.items())
    except Exception:
        return ""


async def download_with_aria2(
    session: aiohttp.ClientSession,
    url: str,
    output_dir: Path,
    filename: str,
    on_progress,
    *,
    max_bytes: int,
    detect_large_for_compression: bool = False,
    referer_override: Optional[str] = None,
) -> Path:
    port = pick_free_port()
    rpc_secret = secrets.token_urlsafe(24)
    aria = await asyncio.create_subprocess_exec(
        "aria2c",
        "--enable-rpc=true",
        "--rpc-listen-all=false",
        f"--rpc-listen-port={port}",
        f"--rpc-secret={rpc_secret}",
        "--console-log-level=warn",
        "--summary-interval=0",
        "--max-concurrent-downloads=1",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )

    try:
        await wait_aria_ready(session, port, rpc_secret)
        refs = referer_candidates(url)
        referer = referer_override or (refs[0] if refs else "")
        options: Dict[str, Any] = {
            "dir": str(output_dir.resolve()),
            "out": filename,
            "split": str(ARIA_CONNECTIONS),
            "max-connection-per-server": str(ARIA_CONNECTIONS),
            "min-split-size": "1M",
            "continue": "true",
            "file-allocation": "none",
            "allow-overwrite": "true",
            "auto-file-renaming": "false",
            "max-tries": "8",
            "retry-wait": "2",
            "connect-timeout": "20",
            "timeout": "45",
            "user-agent": BROWSER_UA,
            "follow-metalink": "false",
            "allow-piece-length-change": "true",
        }
        if referer:
            options["referer"] = referer
        cookie_header = cookie_header_for(session, url)
        if cookie_header:
            options["header"] = f"Cookie: {cookie_header}"

        gid = await aria_rpc(session, port, rpc_secret, "aria2.addUri", [[url], options])
        last_completed = -1
        last_progress_at = time.monotonic()

        while True:
            status = await aria_rpc(
                session,
                port,
                rpc_secret,
                "aria2.tellStatus",
                [
                    gid,
                    [
                        "status",
                        "totalLength",
                        "completedLength",
                        "downloadSpeed",
                        "files",
                        "errorMessage",
                    ],
                ],
            )
            state = status.get("status")
            total = int(status.get("totalLength") or 0)
            completed = int(status.get("completedLength") or 0)
            speed = int(status.get("downloadSpeed") or 0)

            if total > max_bytes:
                try:
                    await aria_rpc(session, port, rpc_secret, "aria2.forceRemove", [gid])
                finally:
                    if detect_large_for_compression and max_bytes == MAX_FILE_BYTES:
                        raise LargeFileDetected(total)
                    raise RuntimeError(
                        f"حجم الملف {human_bytes(total)} أكبر من الحد المسموح لهذه العملية {human_bytes(max_bytes)}"
                    )

            if completed != last_completed:
                last_completed = completed
                last_progress_at = time.monotonic()
            elif state == "active" and speed == 0 and time.monotonic() - last_progress_at >= ARIA_STALL_SECONDS:
                try:
                    await aria_rpc(session, port, rpc_secret, "aria2.forceRemove", [gid])
                except Exception:
                    pass
                raise RuntimeError(
                    f"توقف المصدر عند 0 B/s لمدة {ARIA_STALL_SECONDS} ثانية؛ سيتم تجربة مسار متصفح بديل"
                )

            await on_progress(completed, total, speed)

            if state == "complete":
                files = status.get("files") or []
                if not files:
                    raise RuntimeError("اكتمل التحميل لكن لم يتم العثور على مسار الملف")
                path = Path(files[0]["path"])
                if not path.exists():
                    raise RuntimeError("اكتمل التحميل لكن الملف غير موجود")
                return path
            if state in {"error", "removed"}:
                reason = status.get("errorMessage") or f"download status={state}"
                raise RuntimeError(reason)

            await asyncio.sleep(0.8)
    finally:
        if aria.returncode is None:
            aria.terminate()
            try:
                await asyncio.wait_for(aria.wait(), timeout=5)
            except asyncio.TimeoutError:
                aria.kill()
                await aria.wait()


async def download_with_http_fallback(
    session: aiohttp.ClientSession,
    url: str,
    output_dir: Path,
    filename: str,
    on_progress,
    *,
    max_bytes: int,
    detect_large_for_compression: bool = False,
    referer_override: Optional[str] = None,
) -> Path:
    path = output_dir / filename
    last_error: Optional[Exception] = None
    refs = [referer_override] if referer_override else referer_candidates(url)
    refs = [r for r in refs if r] or [None]

    for ref in refs:
        headers_base = browser_headers(url, referer=ref, navigation=True)
        for attempt in range(1, 4):
            existing = path.stat().st_size if path.exists() else 0
            headers = dict(headers_base)
            if existing > 0:
                headers["Range"] = f"bytes={existing}-"

            try:
                timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_read=90)
                async with session.get(
                    url,
                    headers=headers,
                    allow_redirects=True,
                    timeout=timeout,
                ) as response:
                    if response.status >= 400:
                        raise RuntimeError(f"HTTP {response.status}")

                    if existing and response.status != 206:
                        existing = 0
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass

                    total = response_size(response)
                    if response.status == 206 and total is None and response.content_length is not None:
                        total = existing + int(response.content_length)
                    if total is not None and total > max_bytes:
                        if detect_large_for_compression and max_bytes == MAX_FILE_BYTES:
                            raise LargeFileDetected(total)
                        raise RuntimeError(
                            f"حجم الملف {human_bytes(total)} أكبر من الحد المسموح لهذه العملية {human_bytes(max_bytes)}"
                        )

                    mode = "ab" if existing and response.status == 206 else "wb"
                    completed = existing
                    started = time.monotonic()
                    start_completed = completed
                    with open(path, mode) as handle:
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            if not chunk:
                                continue
                            handle.write(chunk)
                            completed += len(chunk)
                            if completed > max_bytes:
                                if detect_large_for_compression and max_bytes == MAX_FILE_BYTES:
                                    raise LargeFileDetected(total or completed)
                                raise RuntimeError(
                                    f"حجم الملف تجاوز الحد المسموح لهذه العملية {human_bytes(max_bytes)}"
                                )
                            elapsed = max(0.001, time.monotonic() - started)
                            speed = int((completed - start_completed) / elapsed)
                            await on_progress(completed, total or 0, speed)

                    return path
            except LargeFileDetected:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                last_error = exc
                if attempt < 3:
                    await asyncio.sleep(min(2 * attempt, 5))
                    continue
                break

        # A failed referrer may have left a partial response that cannot be resumed
        # with another referrer. Start the next profile cleanly.
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    raise RuntimeError(f"تعذر تنزيل الملف من المصدر بعد عدة محاولات: {last_error}")


async def download_with_curl_cffi(
    session: aiohttp.ClientSession,
    url: str,
    output_dir: Path,
    filename: str,
    on_progress,
    *,
    max_bytes: int,
    detect_large_for_compression: bool = False,
) -> Path:
    """TLS/HTTP2 browser-fingerprint fallback for direct links that reject normal clients."""
    if CurlAsyncSession is None:
        raise RuntimeError("curl_cffi غير متاح")

    path = output_dir / filename
    last_error: Optional[Exception] = None
    cookie_header = cookie_header_for(session, url)

    refs: list[Optional[str]] = [None]
    refs.extend(referer_candidates(url))
    deduped_refs: list[Optional[str]] = []
    seen_refs = set()
    for ref in refs:
        key = ref or ""
        if key not in seen_refs:
            seen_refs.add(key)
            deduped_refs.append(ref)

    # Try a few official aliases. curl_cffi maps these aliases to a supported
    # current fingerprint for the installed package version.
    profiles = ("chrome", "safari_ios", "safari")

    for profile in profiles:
        for ref in deduped_refs:
            for attempt in range(1, 3):
                existing = path.stat().st_size if path.exists() else 0
                headers: Dict[str, str] = {
                    "Accept-Language": "en-US,en;q=0.9",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                }
                if cookie_header:
                    headers["Cookie"] = cookie_header
                if existing > 0:
                    headers["Range"] = f"bytes={existing}-"

                response = None
                try:
                    async with CurlAsyncSession(
                        impersonate=profile,
                        allow_redirects=True,
                        max_redirects=20,
                    ) as curl_session:
                        response = await curl_session.get(
                            url,
                            headers=headers,
                            referer=ref,
                            stream=True,
                            # 20 s connect timeout, no overall transfer timeout.
                            timeout=(20, 0),
                        )

                        status = int(response.status_code)
                        if status >= 400:
                            raise RuntimeError(f"HTTP {status} عبر {profile}")

                        if existing and status != 206:
                            existing = 0
                            try:
                                path.unlink()
                            except FileNotFoundError:
                                pass

                        content_range = str(response.headers.get("Content-Range", ""))
                        match = CONTENT_RANGE_RE.search(content_range)
                        total: Optional[int] = None
                        if match and match.group(1) != "*":
                            total = int(match.group(1))
                        else:
                            raw_length = response.headers.get("Content-Length")
                            if raw_length:
                                try:
                                    length = int(raw_length)
                                    total = existing + length if status == 206 else length
                                except (TypeError, ValueError):
                                    total = None

                        if total is not None and total > max_bytes:
                            if detect_large_for_compression and max_bytes == MAX_FILE_BYTES:
                                raise LargeFileDetected(total)
                            raise RuntimeError(
                                f"حجم الملف {human_bytes(total)} أكبر من الحد المسموح لهذه العملية {human_bytes(max_bytes)}"
                            )

                        mode = "ab" if existing and status == 206 else "wb"
                        completed = existing
                        started = time.monotonic()
                        start_completed = completed
                        last_data_at = time.monotonic()

                        with open(path, mode) as handle:
                            async for chunk in response.aiter_content(chunk_size=1024 * 1024):
                                if not chunk:
                                    if time.monotonic() - last_data_at > 90:
                                        raise RuntimeError("توقف المصدر عن إرسال البيانات")
                                    continue
                                last_data_at = time.monotonic()
                                handle.write(chunk)
                                completed += len(chunk)
                                if completed > max_bytes:
                                    if detect_large_for_compression and max_bytes == MAX_FILE_BYTES:
                                        raise LargeFileDetected(total or completed)
                                    raise RuntimeError(
                                        f"حجم الملف تجاوز الحد المسموح لهذه العملية {human_bytes(max_bytes)}"
                                    )
                                elapsed = max(0.001, time.monotonic() - started)
                                speed = int((completed - start_completed) / elapsed)
                                await on_progress(completed, total or 0, speed)

                        if not path.exists() or path.stat().st_size == 0:
                            raise RuntimeError("انتهى الطلب بدون ملف قابل للإرسال")
                        return path
                except LargeFileDetected:
                    raise
                except Exception as exc:
                    last_error = exc
                    try:
                        if response is not None:
                            await response.aclose()
                    except Exception:
                        pass
                    if attempt < 2:
                        await asyncio.sleep(attempt)
                        continue
                    break
                finally:
                    try:
                        if response is not None:
                            await response.aclose()
                    except Exception:
                        pass

            # A new browser fingerprint/referrer should start from a clean file.
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    raise RuntimeError(f"فشل مسار بصمة المتصفح: {last_error}")


async def resilient_download(
    session: aiohttp.ClientSession,
    url: str,
    output_dir: Path,
    filename: str,
    on_progress,
    *,
    max_bytes: int = MAX_FILE_BYTES,
    detect_large_for_compression: bool = False,
) -> Path:
    errors = []

    # Lightweight cookie warmup first. It costs only a small page request and helps
    # many anti-hotlink/CDN configurations without invoking a real browser.
    await seed_http_cookies(session, url)

    try:
        return await download_with_aria2(
            session, url, output_dir, filename, on_progress,
            max_bytes=max_bytes,
            detect_large_for_compression=detect_large_for_compression,
        )
    except LargeFileDetected:
        raise
    except Exception as exc:
        errors.append(f"aria2: {str(exc)[:180]}")
        partial = output_dir / filename
        try:
            partial.unlink()
        except FileNotFoundError:
            pass

    try:
        return await download_with_http_fallback(
            session, url, output_dir, filename, on_progress,
            max_bytes=max_bytes,
            detect_large_for_compression=detect_large_for_compression,
        )
    except LargeFileDetected:
        raise
    except Exception as exc:
        errors.append(f"HTTP: {str(exc)[:180]}")
        try:
            (output_dir / filename).unlink()
        except FileNotFoundError:
            pass

    # TLS/HTTP2 fingerprint fallback. It keeps the exact direct URL the user sent;
    # no countdown page or link re-discovery is involved.
    try:
        return await download_with_curl_cffi(
            session, url, output_dir, filename, on_progress,
            max_bytes=max_bytes,
            detect_large_for_compression=detect_large_for_compression,
        )
    except LargeFileDetected:
        raise
    except Exception as exc:
        errors.append(f"curl_cffi: {str(exc)[:220]}")
        try:
            (output_dir / filename).unlink()
        except FileNotFoundError:
            pass

    # Real browser fallback only after the fast paths and fingerprint fallback fail.
    # It warms cookies for the same direct URL; it does not wait for a site countdown.
    try:
        browser_url, browser_ref = await browser_session_warmup(session, url)
        errors.append("Browser warmup: OK")

        try:
            return await download_with_aria2(
                session, browser_url, output_dir, filename, on_progress,
                max_bytes=max_bytes,
                detect_large_for_compression=detect_large_for_compression,
                referer_override=browser_ref,
            )
        except LargeFileDetected:
            raise
        except Exception as exc:
            errors.append(f"aria2+browser: {str(exc)[:180]}")
            try:
                (output_dir / filename).unlink()
            except FileNotFoundError:
                pass

        return await download_with_http_fallback(
            session, browser_url, output_dir, filename, on_progress,
            max_bytes=max_bytes,
            detect_large_for_compression=detect_large_for_compression,
            referer_override=browser_ref,
        )
    except LargeFileDetected:
        raise
    except Exception as exc:
        errors.append(f"Browser: {str(exc)[:220]}")

    raise RuntimeError(
        "تعذر تنزيل الملف بعد جميع مسارات التحميل المباشر. "
        + " | ".join(errors[-5:])
    )


async def send_local_document(
    session: aiohttp.ClientSession,
    chat_id: int,
    path: Path,
    caption: str,
) -> Any:
    # Local file URI is the fastest path with Local Bot API: Python doesn't copy
    # the multi-hundred-MB file through localhost before Telegram starts uploading it.
    return await bot_api(
        session,
        "sendDocument",
        {
            "chat_id": chat_id,
            "document": path.resolve().as_uri(),
            "caption": caption[:1024],
            "disable_content_type_detection": True,
        },
        timeout=None,
    )


def _curl_form_file(field: str, path: Path, mime: str) -> str:
    # curl's -F parser supports quoted paths; escaping keeps unusual filenames safe.
    escaped = str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    return f'{field}=@"{escaped}";type={mime}'


async def _send_thumbnail_via_curl(
    chat_id: int,
    path: Path,
    thumbnail: Path,
    caption: str,
) -> Any:
    """Fast native multipart path.

    A custom thumbnail forces multipart/form-data. Using native curl avoids Python's
    small-chunk multipart loop and feeds the Local Bot API over loopback efficiently.
    """
    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError("curl غير متاح")

    url = f"{BOT_API_BASE}/bot{BOT_TOKEN}/sendDocument"
    proc = await asyncio.create_subprocess_exec(
        curl,
        "--silent",
        "--show-error",
        "--http1.1",
        "--header",
        "Expect:",
        "--request",
        "POST",
        url,
        "--form-string",
        f"chat_id={chat_id}",
        "--form-string",
        f"caption={caption[:1024]}",
        "--form-string",
        "disable_content_type_detection=true",
        "--form",
        _curl_form_file("document", path, "application/octet-stream"),
        "--form",
        _curl_form_file("thumbnail", thumbnail, "image/jpeg"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    text = (stdout or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"فشل مسار الرفع السريع: {detail[-800:] or f'exit {proc.returncode}'}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BotAPIError(f"Invalid Bot API response: {text[:300]}") from exc
    if not payload.get("ok"):
        raise BotAPIError(payload.get("description", "Bot API upload error"))
    return payload.get("result")


async def send_document_with_thumbnail(
    session: aiohttp.ClientSession,
    chat_id: int,
    path: Path,
    thumbnail: Path,
    caption: str,
) -> Any:
    """Custom thumbnails require multipart. Prefer native curl, keep aiohttp fallback."""
    try:
        return await _send_thumbnail_via_curl(chat_id, path, thumbnail, caption)
    except Exception as fast_error:
        # Compatibility fallback; never lose a send just because the optimized path failed.
        url = f"{BOT_API_BASE}/bot{BOT_TOKEN}/sendDocument"
        timeout = aiohttp.ClientTimeout(total=None)
        with ExitStack() as stack:
            document_file = stack.enter_context(open(path, "rb"))
            thumb_file = stack.enter_context(open(thumbnail, "rb"))
            form = aiohttp.FormData(quote_fields=False)
            form.add_field("chat_id", str(chat_id))
            form.add_field("caption", caption[:1024])
            form.add_field("disable_content_type_detection", "true")
            form.add_field(
                "document",
                document_file,
                filename=path.name,
                content_type="application/octet-stream",
            )
            form.add_field(
                "thumbnail",
                thumb_file,
                filename="thumbnail.jpg",
                content_type="image/jpeg",
            )
            async with session.post(url, data=form, timeout=timeout) as response:
                text = await response.text()
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise BotAPIError(
                        f"Invalid Bot API response ({response.status}): {text[:300]} | fast={fast_error}"
                    ) from exc
                if not payload.get("ok"):
                    raise BotAPIError(payload.get("description", f"Bot API error {response.status}"))
                return payload.get("result")


async def save_telegram_image(
    session: aiohttp.ClientSession,
    file_id: str,
    token: str,
) -> Path:
    info = await bot_api(session, "getFile", {"file_id": file_id}, timeout=60)
    file_path = str(info.get("file_path") or "")
    if not file_path:
        raise RuntimeError("تعذر قراءة الصورة من تيليگرام")

    target_dir = DOWNLOAD_ROOT / "_thumbnails"
    target_dir.mkdir(parents=True, exist_ok=True)
    raw_path = target_dir / f"{token}_source"

    local = Path(file_path)
    if local.is_absolute() and local.exists():
        await asyncio.to_thread(shutil.copyfile, local, raw_path)
    else:
        file_url = f"{BOT_API_BASE}/file/bot{BOT_TOKEN}/{file_path.lstrip('/')}"
        async with session.get(file_url, timeout=aiohttp.ClientTimeout(total=None)) as response:
            if response.status >= 400:
                raise RuntimeError(f"تعذر تنزيل الصورة: HTTP {response.status}")
            with open(raw_path, "wb") as handle:
                async for chunk in response.content.iter_chunked(512 * 1024):
                    handle.write(chunk)

    output = target_dir / f"{token}.jpg"
    await asyncio.to_thread(convert_thumbnail, raw_path, output)
    try:
        raw_path.unlink()
    except FileNotFoundError:
        pass
    return output


def _save_thumbnail_jpeg(image: Image.Image, output: Path) -> None:
    image = ImageOps.exif_transpose(image)
    if image.mode not in ("RGB", "L"):
        background = Image.new("RGB", image.size, "white")
        if "A" in image.getbands():
            background.paste(image, mask=image.getchannel("A"))
        else:
            background.paste(image.convert("RGB"))
        image = background
    else:
        image = image.convert("RGB")
    image.thumbnail((320, 320), Image.Resampling.LANCZOS)

    for quality in (90, 85, 80, 75, 70, 65, 60, 55, 50):
        image.save(output, "JPEG", quality=quality, optimize=True, progressive=False)
        if output.stat().st_size < 195_000:
            return

    image.thumbnail((240, 240), Image.Resampling.LANCZOS)
    image.save(output, "JPEG", quality=45, optimize=True, progressive=False)
    if output.stat().st_size >= 200_000:
        raise RuntimeError("تعذر ضغط الصورة إلى حجم مصغّر مناسب")


def convert_thumbnail(source: Path, output: Path) -> None:
    # Pillow + pillow-heif handles the common raster formats, HEIC/HEIF and AVIF.
    # ImageMagick is a broad fallback for additional image containers/vector formats.
    try:
        with Image.open(source) as image:
            image.seek(0)
            _save_thumbnail_jpeg(image, output)
            return
    except Exception as first_error:
        converter = shutil.which("magick") or shutil.which("convert")
        if not converter:
            raise RuntimeError(f"صيغة الصورة غير مدعومة: {first_error}") from first_error

        converted = output.with_name(output.stem + "_converted.jpg")
        command = [
            converter,
            f"{source}[0]",
            "-auto-orient",
            "-thumbnail",
            "320x320>",
            "-background",
            "white",
            "-alpha",
            "remove",
            "-strip",
            "-quality",
            "88",
            str(converted),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            if result.returncode != 0 or not converted.exists():
                detail = (result.stderr or result.stdout or str(first_error)).strip()
                raise RuntimeError(f"صيغة الصورة غير مدعومة: {detail[:500]}")
            with Image.open(converted) as image:
                _save_thumbnail_jpeg(image, output)
        finally:
            try:
                converted.unlink()
            except FileNotFoundError:
                pass


def download_status_text(
    filename: str,
    completed: int,
    total: int,
    speed: int,
) -> str:
    if total > 0:
        percent = completed * 100.0 / total
        eta = (total - completed) / speed if speed > 0 else None
        return (
            "⬇️ جاري التحميل بأقصى سرعة متاحة\n"
            f"📦 {filename}\n\n"
            f"{progress_bar(percent)}  {percent:5.1f}%\n"
            f"💾 {human_bytes(completed)} / {human_bytes(total)}\n"
            f"⚡ {human_bytes(speed)}/s\n"
            f"⏱ المتبقي: {human_eta(eta)}"
        )
    return (
        "⬇️ جاري التحميل بأقصى سرعة متاحة\n"
        f"📦 {filename}\n\n"
        "📏 الحجم الكلي غير معروف بعد\n"
        f"💾 تم تحميل: {human_bytes(completed)}\n"
        f"⚡ {human_bytes(speed)}/s"
    )


def large_file_offer_text(filename: str, size: Optional[int]) -> str:
    size_text = human_bytes(size) if size is not None else "أكبر من الحد المباشر"
    return (
        "⚠️ حجم الملف كبير\n"
        f"📦 {filename}\n"
        f"📏 الحجم: {size_text}\n\n"
        f"يتجاوز حد الإرسال المباشر ({human_bytes(MAX_FILE_BYTES)} تقريبًا).\n\n"
        "هل تريد أن أجرب إعادة ضغطه بأقصى ضغط ZIP متوافق مع IPA؟\n"
        "إذا نجح وصار ضمن الحد سأرسله لك بصيغة ZIP.\n\n"
        "📌 للاستخدام: احفظ ملف ZIP ثم غيّر الامتداد من .zip إلى .ipa وسيصبح ملف تطبيق جاهزًا."
    )


def validate_ipa_archive(source: Path) -> Tuple[int, int]:
    """Validate safe ZIP paths and IPA structure. Returns (file_count, uncompressed_bytes)."""
    if not zipfile.is_zipfile(source):
        raise RuntimeError("الملف ليس حزمة IPA/ZIP صالحة لإعادة الضغط")

    total = 0
    file_count = 0
    has_payload = False
    symlink_paths = set()
    all_paths = []
    with zipfile.ZipFile(source, "r", allowZip64=True) as archive:
        for info in archive.infolist():
            raw_name = info.filename.replace("\\", "/")
            pure = PurePosixPath(raw_name)
            if pure.is_absolute() or ".." in pure.parts:
                raise RuntimeError("تم رفض الحزمة لأن بداخلها مسار غير آمن")
            all_paths.append(pure)
            if raw_name == "Payload" or raw_name.startswith("Payload/"):
                has_payload = True

            unix_mode = (int(info.external_attr) >> 16) & 0xFFFF
            if stat.S_ISLNK(unix_mode):
                symlink_paths.add(pure)
                try:
                    target = archive.read(info).decode("utf-8", "surrogateescape")
                except Exception as exc:
                    raise RuntimeError("تعذر التحقق من رابط رمزي داخل الحزمة") from exc
                target_path = PurePosixPath(target)
                if target_path.is_absolute() or ".." in target_path.parts:
                    raise RuntimeError("تم رفض الحزمة لأن بداخلها رابط رمزي غير آمن")

            if not info.is_dir():
                file_count += 1
                total += int(info.file_size)
                if total > MAX_EXTRACTED_BYTES:
                    raise RuntimeError(
                        f"الحجم بعد فك الحزمة يتجاوز حد المعالجة الآمن {human_bytes(MAX_EXTRACTED_BYTES)}"
                    )

    # Avoid archives that try to write a later file through a symlinked parent path.
    for pure in all_paths:
        parents = list(pure.parents)[:-1]  # exclude '.'
        if any(parent in symlink_paths for parent in parents):
            raise RuntimeError("تم رفض الحزمة لأن بنيتها تحتوي مسارًا يمر عبر رابط رمزي")

    if not has_payload:
        raise RuntimeError("الحزمة لا تحتوي مجلد Payload في الجذر، لذلك لا يمكن ضمان أنها IPA صالحة")
    return file_count, total


async def run_process_checked(*args: str, cwd: Optional[Path] = None) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"فشلت أداة الضغط: {detail[-1200:] or f'exit {proc.returncode}'}")


async def recompress_ipa_to_zip(
    session: aiohttp.ClientSession,
    chat_id: int,
    status_id: int,
    source: Path,
    job_dir: Path,
) -> Path:
    file_count, unpacked_size = await asyncio.to_thread(validate_ipa_archive, source)
    free = shutil.disk_usage(job_dir).free
    # Source is already on disk; reserve space for extracted data, rebuilt ZIP and temp overhead.
    required = unpacked_size + min(source.stat().st_size, MAX_FILE_BYTES + 300_000_000) + 512_000_000
    if free < required:
        raise RuntimeError(
            "المساحة المؤقتة المتاحة غير كافية لإعادة ضغط هذا الملف. "
            f"المتاح {human_bytes(free)} والمطلوب تقريبًا {human_bytes(required)}"
        )

    extract_dir = job_dir / "ipa_unpacked"
    extract_dir.mkdir(parents=True, exist_ok=True)
    await edit_text(
        session, chat_id, status_id,
        "🗜️ جاري تجهيز الملف للضغط القوي...\n"
        f"📦 الحجم الأصلي: {human_bytes(source.stat().st_size)}\n"
        f"📂 المحتوى بعد الفك: {human_bytes(unpacked_size)}\n"
        f"🧩 عدد الملفات: {file_count}",
        empty_keyboard(),
    )

    # Info-ZIP unzip preserves Unix permissions and stored symbolic links on Linux.
    # Paths/symlinks are validated above before extraction.
    await run_process_checked("unzip", "-q", str(source), "-d", str(extract_dir))

    top_entries = sorted(p.name for p in extract_dir.iterdir())
    if "Payload" not in top_entries:
        raise RuntimeError("تعذر العثور على Payload بعد فك الحزمة")

    output = job_dir / f"{source.stem}.zip"
    try:
        output.unlink()
    except FileNotFoundError:
        pass

    # Info-ZIP -9 is the strongest standard Deflate level while keeping a conventional
    # ZIP/IPA container. -y stores symbolic links as links instead of dereferencing them.
    proc = await asyncio.create_subprocess_exec(
        "zip", "-9", "-y", "-q", "-r", str(output), *top_entries,
        cwd=str(extract_dir),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    last_edit = 0.0
    while proc.returncode is None:
        await asyncio.sleep(2.0)
        if proc.returncode is not None:
            break
        now = time.monotonic()
        if now - last_edit >= 4.0:
            last_edit = now
            current = output.stat().st_size if output.exists() else 0
            await edit_text(
                session, chat_id, status_id,
                "🗜️ جاري إعادة الضغط بأقصى مستوى ZIP...\n"
                f"📦 الحجم الأصلي: {human_bytes(source.stat().st_size)}\n"
                f"💾 حجم ZIP الحالي: {human_bytes(current)}\n"
                "⏳ قد تستغرق هذه المرحلة وقتًا حسب حجم اللعبة.",
                empty_keyboard(),
            )

    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not output.exists():
        detail = (stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"فشل إنشاء ZIP المضغوط: {detail[-1200:] or f'exit {proc.returncode}'}")

    # Verify the rebuilt archive before sending it.
    await asyncio.to_thread(validate_ipa_archive, output)
    return output


async def inspect_and_offer(
    session: aiohttp.ClientSession,
    chat_id: int,
    user_id: int,
    url: str,
) -> None:
    status_msg = await send_text(session, chat_id, "🔎 جاري فحص الرابط...")
    status_id = int(status_msg["message_id"])

    try:
        filename, announced_size, final_url = await inspect_url(session, url)

        token = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
        pending_jobs[token] = {
            "token": token,
            "chat_id": chat_id,
            "user_id": user_id,
            "url": normalize_url(url),
            "final_url": final_url,
            "filename": filename,
            "size": announced_size,
            "message_id": status_id,
            "created_at": time.time(),
            "started": False,
            "custom_filename": None,
            "thumbnail": None,
            "large_confirmed": False,
        }

        if announced_size is not None and announced_size > MAX_FILE_BYTES:
            await edit_text(
                session, chat_id, status_id,
                large_file_offer_text(filename, announced_size),
                large_file_keyboard(token),
            )
            return

        size_text = human_bytes(announced_size) if announced_size is not None else "غير معروف مسبقًا"
        await edit_text(
            session,
            chat_id,
            status_id,
            f"📦 الاسم: {filename}\n📏 الحجم: {size_text}",
            offer_keyboard(token),
        )
    except Exception as exc:
        await edit_text(session, chat_id, status_id, f"❌ تعذر تجهيز الرابط\n{str(exc)[:3000]}", empty_keyboard())


async def run_download_job(
    session: aiohttp.ClientSession,
    job: Dict[str, Any],
    filename: str,
    thumbnail: Optional[Path] = None,
) -> None:
    chat_id = int(job["chat_id"])
    status_id = int(job["message_id"])
    url = str(job["url"])
    queued = download_lock.locked()

    if queued:
        await edit_text(
            session,
            chat_id,
            status_id,
            f"⏳ يوجد تحميل آخر قيد التنفيذ، تمت إضافة الملف للطابور...\n📦 {filename}",
            empty_keyboard(),
        )
    else:
        await edit_text(
            session,
            chat_id,
            status_id,
            f"🚀 بدء التحميل...\n📦 {filename}",
            empty_keyboard(),
        )

    async with download_lock:
        job_dir = DOWNLOAD_ROOT / f"{chat_id}_{status_id}_{int(time.time())}"
        job_dir.mkdir(parents=True, exist_ok=True)
        last_edit = 0.0
        last_percent = -1
        keep_pending = False

        try:
            if queued:
                await edit_text(
                    session,
                    chat_id,
                    status_id,
                    f"🚀 جاء دور الملف — بدء التحميل الآن...\n📦 {filename}",
                    empty_keyboard(),
                )

            async def on_progress(completed: int, total: int, speed: int) -> None:
                nonlocal last_edit, last_percent
                now = time.monotonic()
                pct = int(completed * 100 / total) if total else -1
                if now - last_edit < PROGRESS_EDIT_INTERVAL and pct == last_percent:
                    return
                last_edit = now
                last_percent = pct
                await edit_text(
                    session,
                    chat_id,
                    status_id,
                    download_status_text(filename, completed, total, speed),
                    empty_keyboard(),
                )

            path = await resilient_download(
                session, url, job_dir, filename, on_progress,
                max_bytes=MAX_FILE_BYTES,
                detect_large_for_compression=True,
            )
            actual_size = path.stat().st_size

            await edit_text(
                session,
                chat_id,
                status_id,
                "✅ اكتمل التحميل 100%\n"
                f"📦 {path.name}\n"
                f"💾 {human_bytes(actual_size)}\n\n"
                "⬆️ جاري الإرسال إلى تيليگرام...",
                empty_keyboard(),
            )

            caption = f"✅ {path.name}\n📦 {human_bytes(actual_size)}"
            if thumbnail is not None and thumbnail.exists():
                await send_document_with_thumbnail(session, chat_id, path, thumbnail, caption)
            else:
                await send_local_document(session, chat_id, path, caption)

            await edit_text(
                session,
                chat_id,
                status_id,
                "✅ تم التحميل والإرسال بنجاح\n"
                f"📦 {path.name}\n"
                f"💾 {human_bytes(actual_size)}",
                empty_keyboard(),
            )
        except LargeFileDetected as exc:
            keep_pending = True
            job["started"] = False
            job["size"] = exc.size
            await edit_text(
                session, chat_id, status_id,
                large_file_offer_text(filename, exc.size),
                large_file_keyboard(str(job["token"])),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await edit_text(
                session,
                chat_id,
                status_id,
                f"❌ فشل التنفيذ\n{str(exc)[:3000]}",
                empty_keyboard(),
            )
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)
            if thumbnail is not None:
                try:
                    thumbnail.unlink()
                except FileNotFoundError:
                    pass
            if not keep_pending:
                pending_jobs.pop(str(job["token"]), None)


async def run_large_compression_job(
    session: aiohttp.ClientSession,
    job: Dict[str, Any],
) -> None:
    chat_id = int(job["chat_id"])
    status_id = int(job["message_id"])
    url = str(job["url"])
    filename = str(job["filename"])
    queued = download_lock.locked()

    if queued:
        await edit_text(
            session, chat_id, status_id,
            f"⏳ يوجد تحميل آخر قيد التنفيذ، تمت إضافة الملف الكبير للطابور...\n📦 {filename}",
            empty_keyboard(),
        )
    else:
        await edit_text(
            session, chat_id, status_id,
            f"🚀 بدء تحميل الملف الكبير لتجربة الضغط...\n📦 {filename}",
            empty_keyboard(),
        )

    async with download_lock:
        job_dir = DOWNLOAD_ROOT / f"large_{chat_id}_{status_id}_{int(time.time())}"
        job_dir.mkdir(parents=True, exist_ok=True)
        last_edit = 0.0
        last_percent = -1
        original_size = 0
        compressed_size = 0

        try:
            async def on_progress(completed: int, total: int, speed: int) -> None:
                nonlocal last_edit, last_percent
                now = time.monotonic()
                pct = int(completed * 100 / total) if total else -1
                if now - last_edit < PROGRESS_EDIT_INTERVAL and pct == last_percent:
                    return
                last_edit = now
                last_percent = pct
                await edit_text(
                    session, chat_id, status_id,
                    download_status_text(filename, completed, total, speed),
                    empty_keyboard(),
                )

            source = await resilient_download(
                session, url, job_dir, filename, on_progress,
                max_bytes=MAX_SOURCE_BYTES,
                detect_large_for_compression=False,
            )
            original_size = source.stat().st_size

            if original_size <= MAX_FILE_BYTES:
                await edit_text(
                    session, chat_id, status_id,
                    "✅ الحجم الفعلي صار ضمن حد الإرسال، لا حاجة للضغط.\n"
                    f"📦 {source.name}\n💾 {human_bytes(original_size)}\n\n⬆️ جاري الإرسال...",
                    empty_keyboard(),
                )
                await send_local_document(
                    session, chat_id, source,
                    f"✅ {source.name}\n📦 {human_bytes(original_size)}",
                )
                await edit_text(
                    session, chat_id, status_id,
                    "✅ تم التحميل والإرسال بنجاح\n"
                    f"📦 {source.name}\n💾 {human_bytes(original_size)}",
                    empty_keyboard(),
                )
                return

            compressed = await recompress_ipa_to_zip(session, chat_id, status_id, source, job_dir)
            compressed_size = compressed.stat().st_size
            if compressed_size > MAX_FILE_BYTES:
                raise CompressionNotEnough(original_size, compressed_size)

            saved = original_size - compressed_size
            percent_saved = (saved * 100.0 / original_size) if original_size else 0.0
            await edit_text(
                session, chat_id, status_id,
                "✅ نجح الضغط وصار الملف ضمن حد الإرسال\n"
                f"📦 الأصلي: {human_bytes(original_size)}\n"
                f"🗜️ بعد الضغط: {human_bytes(compressed_size)}\n"
                f"📉 التوفير: {human_bytes(max(0, saved))} ({max(0.0, percent_saved):.1f}%)\n\n"
                "⬆️ جاري إرسال ملف ZIP...",
                empty_keyboard(),
            )

            caption = (
                f"✅ {compressed.name}\n"
                f"📦 {human_bytes(compressed_size)}\n\n"
                "📌 احفظ الملف ثم غيّر الامتداد من .zip إلى .ipa لاستخدامه كتطبيق."
            )
            await send_local_document(session, chat_id, compressed, caption)
            await edit_text(
                session, chat_id, status_id,
                "✅ تم الضغط والإرسال بنجاح\n"
                f"📦 {compressed.name}\n"
                f"💾 {human_bytes(compressed_size)}\n\n"
                "📌 للاستخدام: احفظ ملف ZIP ثم غيّر الامتداد من .zip إلى .ipa.",
                empty_keyboard(),
            )
        except CompressionNotEnough as exc:
            await edit_text(
                session, chat_id, status_id,
                "❌ فشل الضغط الكافي\n\n"
                f"📦 الحجم الأصلي: {human_bytes(exc.original_size)}\n"
                f"🗜️ بعد أقصى ضغط: {human_bytes(exc.compressed_size)}\n"
                f"🚫 حد الإرسال: {human_bytes(MAX_FILE_BYTES)} تقريبًا\n\n"
                "الملف حجمه أكبر بكثير أو أن محتوياته مضغوطة أصلًا، لذلك حتى بعد أقصى ضغط ZIP بقي أكبر من الحد.",
                empty_keyboard(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await edit_text(
                session, chat_id, status_id,
                f"❌ فشل ضغط الملف أو تجهيزه\n{str(exc)[:3000]}",
                empty_keyboard(),
            )
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)
            pending_jobs.pop(str(job["token"]), None)


async def start_large_job_task(
    session: aiohttp.ClientSession,
    job: Dict[str, Any],
) -> bool:
    if job.get("started"):
        return False
    user_id = int(job.get("user_id") or 0)
    if not await _claim_download_slot(user_id):
        return False
    job["started"] = True
    job["large_confirmed"] = True
    try:
        task = asyncio.create_task(
            _run_with_download_slot(user_id, run_large_compression_job(session, job))
        )
    except Exception:
        job["started"] = False
        await _release_download_slot(user_id)
        raise
    active_jobs.add(task)
    task.add_done_callback(active_jobs.discard)
    return True


async def start_job_task(
    session: aiohttp.ClientSession,
    job: Dict[str, Any],
    filename: str,
    thumbnail: Optional[Path] = None,
) -> bool:
    if job.get("started"):
        return False
    user_id = int(job.get("user_id") or 0)
    if not await _claim_download_slot(user_id):
        return False
    job["started"] = True
    try:
        task = asyncio.create_task(
            _run_with_download_slot(
                user_id, run_download_job(session, job, filename, thumbnail)
            )
        )
    except Exception:
        job["started"] = False
        await _release_download_slot(user_id)
        raise
    active_jobs.add(task)
    task.add_done_callback(active_jobs.discard)
    return True


async def handle_callback(session: aiohttp.ClientSession, callback: Dict[str, Any]) -> None:
    callback_id = str(callback.get("id") or "")
    sender = callback.get("from") or {}
    user_id = sender.get("id")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    data = str(callback.get("data") or "")

    if user_id is None or chat_id is None:
        await answer_callback(session, callback_id)
        return

    user_id = int(user_id)
    chat_id = int(chat_id)
    if user_id not in ALLOWED_USER_IDS:
        subscribed, _ = await check_required_channel_membership(session, user_id)
        if not subscribed:
            await answer_callback(
                session, callback_id,
                "البوت متاح للاستخدام حصراً لدى مشتركين كيرا بلس.",
                alert=True,
            )
            try:
                await deny_non_subscriber(session, chat_id)
            except Exception:
                pass
            return

    if ":" not in data:
        await answer_callback(session, callback_id)
        return

    action, token = data.split(":", 1)
    job = pending_jobs.get(token)
    if not job or int(job.get("user_id", -1)) != user_id or int(job.get("chat_id", 0)) != chat_id:
        await answer_callback(session, callback_id, "انتهت صلاحية هذا الطلب. أرسل الرابط من جديد.", alert=True)
        return

    if job.get("started"):
        await answer_callback(session, callback_id, "التحميل بدأ بالفعل.")
        return

    if action == "cancel":
        user_flows.pop(user_id, None)
        pending_jobs.pop(token, None)
        await answer_callback(session, callback_id, "تم الإلغاء")
        await edit_text(
            session, chat_id, int(job["message_id"]),
            "❌ تم إلغاء الطلب.",
            empty_keyboard(),
        )
        return

    if action == "compress":
        user_flows.pop(user_id, None)
        started = await start_large_job_task(session, job)
        if started:
            await answer_callback(session, callback_id, "بدأت محاولة الضغط")
        else:
            await answer_callback(
                session, callback_id,
                f"لديك {MAX_ACTIVE_DOWNLOADS_PER_USER} عمليات نشطة بالفعل. انتظر اكتمال إحداها.",
                alert=True,
            )
        return

    if action == "send":
        user_flows.pop(user_id, None)
        started = await start_job_task(session, job, str(job["filename"]))
        if started:
            await answer_callback(session, callback_id, "بدأ التحميل")
        else:
            await answer_callback(
                session, callback_id,
                f"لديك {MAX_ACTIVE_DOWNLOADS_PER_USER} عمليات نشطة بالفعل. انتظر اكتمال إحداها.",
                alert=True,
            )
        return

    if action == "custom":
        user_flows[user_id] = {"stage": "name", "token": token}
        await answer_callback(session, callback_id)
        await edit_text(
            session,
            chat_id,
            int(job["message_id"]),
            f"📦 الاسم الحالي: {job['filename']}\n"
            f"📏 الحجم: {human_bytes(job['size']) if job['size'] is not None else 'غير معروف مسبقًا'}\n\n"
            "✏️ أرسل الاسم الجديد الآن.\n"
            "إذا كتبت الامتداد سأستخدمه كما كتبته، وإذا لم تكتب امتدادًا سأحتفظ بامتداد الملف الأصلي.",
            empty_keyboard(),
        )
        return

    if action == "skipthumb":
        flow = user_flows.get(user_id) or {}
        if flow.get("token") != token or flow.get("stage") != "photo":
            await answer_callback(session, callback_id, "انتهت هذه الخطوة. أرسل الرابط من جديد.", alert=True)
            return
        user_flows.pop(user_id, None)
        filename = str(job.get("custom_filename") or job["filename"])
        started = await start_job_task(session, job, filename)
        if started:
            await answer_callback(session, callback_id, "تم تخطي الصورة — بدأ التحميل")
        else:
            await answer_callback(
                session, callback_id,
                f"لديك {MAX_ACTIVE_DOWNLOADS_PER_USER} عمليات نشطة بالفعل. انتظر اكتمال إحداها.",
                alert=True,
            )
        return

    await answer_callback(session, callback_id)


async def handle_custom_flow_message(
    session: aiohttp.ClientSession,
    message: Dict[str, Any],
    user_id: int,
    chat_id: int,
) -> bool:
    flow = user_flows.get(user_id)
    if not flow:
        return False

    token = str(flow.get("token") or "")
    job = pending_jobs.get(token)
    if not job or job.get("started") or int(job.get("chat_id", 0)) != chat_id:
        user_flows.pop(user_id, None)
        return False

    stage = flow.get("stage")
    text = (message.get("text") or "").strip()

    if stage == "name":
        if not text or text.startswith("/"):
            await send_text(session, chat_id, "✏️ أرسل الاسم الجديد كنص أولاً.")
            return True
        new_name = custom_filename(text, str(job["filename"]))
        job["custom_filename"] = new_name
        user_flows[user_id] = {"stage": "photo", "token": token}
        await send_text(
            session,
            chat_id,
            f"✅ الاسم الجديد: {new_name}\n\n"
            "🖼️ أرسل الآن الصورة التي تريدها كصورة مصغّرة للملف.\n"
            "يمكنك إرسالها كصورة أو كملف صورة، وسأحوّلها تلقائيًا للصيغة المناسبة.",
            skip_thumbnail_keyboard(token),
        )
        return True

    if stage == "photo":
        photo = message.get("photo") or []
        document = message.get("document") or {}
        file_id: Optional[str] = None

        if photo:
            file_id = str(photo[-1].get("file_id") or "")
        elif document:
            mime = str(document.get("mime_type") or "").lower()
            file_name = str(document.get("file_name") or "")
            ext = Path(file_name).suffix.lower()
            if mime.startswith("image/") or ext in IMAGE_EXTENSIONS:
                file_id = str(document.get("file_id") or "")

        if not file_id:
            await send_text(
                session,
                chat_id,
                "🖼️ أرسل صورة أو ملف صورة، أو اضغط «تخطي الصورة».",
                skip_thumbnail_keyboard(token),
            )
            return True

        try:
            await send_text(session, chat_id, "🖼️ تم استلام الصورة — جاري تجهيزها...")
            thumbnail = await save_telegram_image(session, file_id, token)
        except Exception as exc:
            await send_text(
                session,
                chat_id,
                f"❌ تعذر تجهيز هذه الصورة: {str(exc)[:1000]}\n"
                "أرسل صورة أخرى أو اضغط «تخطي الصورة».",
                skip_thumbnail_keyboard(token),
            )
            return True

        user_flows.pop(user_id, None)
        filename = str(job.get("custom_filename") or job["filename"])
        started = await start_job_task(session, job, filename, thumbnail)
        if not started:
            try:
                thumbnail.unlink()
            except FileNotFoundError:
                pass
            await send_text(
                session, chat_id,
                f"⚠️ لديك {MAX_ACTIVE_DOWNLOADS_PER_USER} عمليات تحميل نشطة بالفعل. "
                "انتظر اكتمال إحدى العمليات ثم أعد المحاولة.",
            )
        return True

    return False


async def process_update(session: aiohttp.ClientSession, update: Dict[str, Any]) -> None:
    my_member = update.get("my_chat_member")
    if my_member:
        chat = my_member.get("chat") or {}
        if str(chat.get("type") or "") == "private":
            user_id = int(chat.get("id") or 0)
            if user_id and user_id not in ALLOWED_USER_IDS:
                old_status = str((my_member.get("old_chat_member") or {}).get("status") or "").lower()
                new_status = str((my_member.get("new_chat_member") or {}).get("status") or "").lower()
                user_info: Dict[str, Any] = {
                    "id": user_id,
                    "first_name": chat.get("first_name"),
                    "last_name": chat.get("last_name"),
                    "username": chat.get("username"),
                }
                if new_status in {"kicked", "left"} and old_status not in {"kicked", "left"}:
                    await notify_owner_about_user(session, user_info, "🚫 المستخدم حظر البوت")
                elif old_status in {"kicked", "left"} and new_status not in {"kicked", "left"}:
                    await notify_owner_about_user(session, user_info, "✅ المستخدم ألغى حظر البوت")
        return

    channel_post = update.get("channel_post")
    if channel_post:
        chat = channel_post.get("chat") or {}
        text = (channel_post.get("text") or "").strip()
        if str(chat.get("type") or "") == "channel" and text == "#KIRA_BIND_CHANNEL":
            await save_required_channel_id(int(chat["id"]))
            try:
                await send_text(
                    session,
                    OWNER_USER_ID,
                    f"✅ تم ربط قناة الاشتراك بنجاح.\n🆔 Channel ID: {int(chat['id'])}",
                )
            except Exception:
                pass
        return

    callback = update.get("callback_query")
    if callback:
        await handle_callback(session, callback)
        return

    message = update.get("message")
    if not message:
        return

    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    user_id = sender.get("id")
    text = (message.get("text") or "").strip()
    if chat_id is None or user_id is None:
        return

    chat_id = int(chat_id)
    user_id = int(user_id)
    is_admin = user_id in ALLOWED_USER_IDS

    # The two configured admin IDs bypass subscription checks, logging and entry alerts.
    if is_admin and text.startswith("/setchannel"):
        channel_bind_waiting.add(user_id)
        await send_text(
            session,
            chat_id,
            "حوّل الآن أي منشور من قناة كيرا بلس الخاصة إلى هذا البوت، وسأحفظ رقم القناة تلقائياً.\n\n"
            "إذا كانت القناة تمنع Forward، انشر داخل القناة هذا النص مؤقتاً:\n#KIRA_BIND_CHANNEL",
        )
        return

    if is_admin and user_id in channel_bind_waiting:
        forwarded_id = _forwarded_channel_id(message)
        if forwarded_id:
            try:
                info = await bot_api(session, "getChat", {"chat_id": forwarded_id}, timeout=30)
                if str((info or {}).get("type") or "") != "channel":
                    raise RuntimeError("المصدر ليس قناة")
                await save_required_channel_id(forwarded_id)
                channel_bind_waiting.discard(user_id)
                await send_text(
                    session,
                    chat_id,
                    f"✅ تم ربط قناة الاشتراك بنجاح.\n🆔 Channel ID: {forwarded_id}",
                )
            except Exception as exc:
                await send_text(session, chat_id, f"❌ تعذر ربط القناة: {str(exc)[:500]}")
            return
        await send_text(session, chat_id, "حوّل منشوراً من القناة نفسها، أو أرسل /setchannel لإلغاء وإعادة المحاولة.")
        return

    # Log every URL sent by non-admin users, even if they are not subscribed.
    match = URL_RE.search(text)
    if match and not is_admin:
        url_for_log = normalize_url(match.group(0).rstrip(".,);]}\"'"))
        task = asyncio.create_task(persist_link_log(sender, url_for_log))
        active_jobs.add(task)
        task.add_done_callback(active_jobs.discard)

    if text.startswith("/start") or text.startswith("/help"):
        user_flows.pop(user_id, None)
        if not is_admin:
            subscribed, membership_text = await check_required_channel_membership(session, user_id)
            await notify_owner_about_user(
                session,
                sender,
                "👋 مستخدم ضغط Start",
                membership_text,
            )
            if not subscribed:
                await deny_non_subscriber(session, chat_id)
                return
        await send_text(
            session,
            chat_id,
            "أرسل رابط HTTP/HTTPS مباشر للملف.\n\n"
            f"يمكنك تشغيل حتى {MAX_ACTIVE_DOWNLOADS_PER_USER} تحميلات بنفس الوقت لكل مستخدم.\n\n"
            "سأفحص الاسم والحجم ثم أعطيك خيار الإرسال مباشرة أو تغيير الاسم والصورة المصغّرة قبل الإرسال.\n\n"
            f"حد الإرسال المباشر: {human_bytes(MAX_FILE_BYTES)} تقريباً.\n"
            "إذا كان ملف IPA أكبر من الحد، سيظهر خيار لتجربة إعادة ضغطه وإرساله كـ ZIP قابل للتحويل إلى IPA بمجرد تغيير الامتداد.",
        )
        return

    if not is_admin:
        subscribed, _ = await check_required_channel_membership(session, user_id)
        if not subscribed:
            await deny_non_subscriber(session, chat_id)
            return
        if match:
            url_for_report = normalize_url(match.group(0).rstrip(".,);]}\"'"))
            report_task = asyncio.create_task(
                report_subscriber_link(session, sender, url_for_report)
            )
            active_jobs.add(report_task)
            report_task.add_done_callback(active_jobs.discard)

    if await handle_custom_flow_message(session, message, user_id, chat_id):
        return

    if not match:
        await send_text(session, chat_id, "أرسل رابط مباشر يبدأ بـ http:// أو https://")
        return

    if await _active_download_count(user_id) >= MAX_ACTIVE_DOWNLOADS_PER_USER:
        await send_text(
            session, chat_id,
            f"⚠️ لديك {MAX_ACTIVE_DOWNLOADS_PER_USER} عمليات تحميل نشطة بالفعل. "
            "انتظر اكتمال إحدى العمليات ثم أرسل رابطًا جديدًا.",
        )
        return

    url = normalize_url(match.group(0).rstrip(".,);]}\"'"))
    task = asyncio.create_task(inspect_and_offer(session, chat_id, user_id, url))
    active_jobs.add(task)
    task.add_done_callback(active_jobs.discard)


async def polling_loop() -> None:
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(connector=connector, cookie_jar=cookie_jar) as session:
        me = await bot_api(session, "getMe")
        print(f"Bot started as @{me.get('username', 'unknown')}", flush=True)
        try:
            await bot_api(session, "deleteWebhook", {"drop_pending_updates": False})
        except Exception as exc:
            print(f"deleteWebhook warning: {exc}", flush=True)

        offset: Optional[int] = None
        while not shutdown_event.is_set():
            try:
                result = await bot_api(
                    session,
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": 30,
                        "allowed_updates": ["message", "callback_query", "my_chat_member", "channel_post"],
                    },
                    timeout=45,
                )
                for update in result or []:
                    offset = int(update["update_id"]) + 1
                    await process_update(session, update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"Polling error: {exc}", flush=True)
                await asyncio.sleep(2)

        if active_jobs:
            print(f"Cancelling {len(active_jobs)} active task(s)...", flush=True)
            for task in list(active_jobs):
                task.cancel()
            await asyncio.gather(*list(active_jobs), return_exceptions=True)


def request_shutdown() -> None:
    shutdown_event.set()


async def main() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except NotImplementedError:
            pass
    await polling_loop()


if __name__ == "__main__":
    asyncio.run(main())
