# =============================================================================
# FluxFetch | Universal Downloader — production Telegram bot (v2)
# =============================================================================
import asyncio
import contextlib
import logging
import os
import re
import secrets
import shutil
import time
from collections import OrderedDict
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

import aiofiles
import aiofiles.os
import yt_dlp
from yt_dlp.utils import DownloadError

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaPhoto,
    InputMediaVideo,
    KeyboardButton,
    LinkPreviewOptions,
    MenuButtonCommands,
    MessageEntity,
    ReplyKeyboardMarkup,
    ReplyParameters,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =============================================================================
# CONFIG
# =============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN environment variable is required.")

BOT_OWNER_HAS_PREMIUM = os.getenv("BOT_OWNER_HAS_PREMIUM", "false").lower() == "true"

# --- NEW: Cookies & Proxy support ---
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES", "")
PROXY_URL = os.getenv("PROXY_URL", "")

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Write cookies file if provided via env
COOKIES_FILE: Optional[Path] = None
if YOUTUBE_COOKIES.strip():
    COOKIES_FILE = DOWNLOAD_DIR / "cookies.txt"
    COOKIES_FILE.write_text(YOUTUBE_COOKIES)

BOT_NAME = "FluxFetch | Universal Downloader"
BOT_SHORT_DESCRIPTION = "Instant media grabber. Paste a link, get your file. Auto-cleaned."
BOT_DESCRIPTION = (
    "FluxFetch grabs media from links and sends it right here.\n\n"
    "Supported: YouTube, TikTok, Instagram, X/Twitter, Facebook, Reddit, Vimeo, "
    "SoundCloud, Twitch clips, direct file links and 1000+ more sites.\n\n"
    "Privacy: no media is kept. Every file is deleted from the server right after delivery."
)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_CAPTION = 1024

DOWNLOAD_SEM = asyncio.Semaphore(3)
JANITOR_INTERVAL_S = 10 * 60
ORPHAN_AGE_S = 30 * 60

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
START_TIME = time.time()
ACTIVE_JOBS = 0

# =============================================================================
# LOGGING WITH TOKEN REDACTION
# =============================================================================
class TokenRedactionFilter(logging.Filter):
    _GENERIC = re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}")

    def __init__(self, token: str):
        super().__init__()
        self._token = token

    def _scrub(self, text: str) -> str:
        if self._token:
            text = text.replace(self._token, "[REDACTED]")
        return self._GENERIC.sub("bot[REDACTED]", text)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = self._scrub(message)
        record.args = ()
        if record.exc_info:
            record.exc_text = self._scrub(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = self._scrub(record.exc_text)
        return True


_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
_handler.addFilter(TokenRedactionFilter(BOT_TOKEN))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("fluxfetch")

# =============================================================================
# PREMIUM EMOJI
# =============================================================================
PREMIUM_EMOJI_IDS: dict[str, str] = {
    "fetch": "", "settings": "", "help": "", "delete": "", "spoiler": "",
    "retry": "", "ok": "", "warn": "", "rocket": "", "broom": "",
}

EMOJI_FALLBACK: dict[str, str] = {
    "fetch": "📥", "settings": "⚙️", "help": "ℹ️", "delete": "🗑️",
    "spoiler": "🫣", "retry": "🔄", "ok": "✅", "warn": "⚠️",
    "rocket": "🚀", "broom": "🧹",
}


def _usable(key: str) -> bool:
    return BOT_OWNER_HAS_PREMIUM and PREMIUM_EMOJI_IDS.get(key, "").isdigit()


def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def with_emoji(text: str, key: Optional[str] = None) -> tuple[str, Optional[list[MessageEntity]]]:
    if not BOT_OWNER_HAS_PREMIUM:
        return text, None
    entities: list[MessageEntity] = []
    for k in ([key] if key else list(EMOJI_FALLBACK)):
        if not _usable(k):
            continue
        fb = EMOJI_FALLBACK[k]
        start = 0
        while (i := text.find(fb, start)) != -1:
            entities.append(MessageEntity(
                type=MessageEntity.CUSTOM_EMOJI,
                offset=_u16(text[:i]),
                length=_u16(fb),
                custom_emoji_id=PREMIUM_EMOJI_IDS[k],
            ))
            start = i + len(fb)
    entities.sort(key=lambda e: e.offset)
    return text, (entities or None)


def _icon(key: str) -> dict[str, str]:
    return {"icon_custom_emoji_id": PREMIUM_EMOJI_IDS[key]} if _usable(key) else {}


def _label(key: str, words: str) -> str:
    return words if _usable(key) else f"{EMOJI_FALLBACK[key]} {words}"


# =============================================================================
# BRANDING
# =============================================================================
async def apply_branding(bot) -> None:
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("help", "How to use FluxFetch"),
        BotCommand("settings", "Delivery preferences"),
        BotCommand("status", "Bot & queue status"),
    ]

    async def step(name: str, check, apply):
        try:
            if await check():
                log.info("Branding: %s already up to date", name)
            else:
                await apply()
                log.info("Branding: %s updated", name)
        except RetryAfter as e:
            log.warning("Branding: %s rate limited (%s)", name, e)
        except Exception:
            log.exception("Branding step failed: %s", name)

    async def _name_ok():
        return (await bot.get_my_name()).name == BOT_NAME

    async def _short_ok():
        return (await bot.get_my_short_description()).short_description == BOT_SHORT_DESCRIPTION

    async def _desc_ok():
        return (await bot.get_my_description()).description == BOT_DESCRIPTION

    async def _cmds_ok():
        cur = [(c.command, c.description) for c in await bot.get_my_commands()]
        return cur == [(c.command, c.description) for c in commands]

    async def _menu_ok():
        return (await bot.get_chat_menu_button()).type == "commands"

    await step("name", _name_ok, lambda: bot.set_my_name(BOT_NAME))
    await step("short description", _short_ok, lambda: bot.set_my_short_description(BOT_SHORT_DESCRIPTION))
    await step("description", _desc_ok, lambda: bot.set_my_description(BOT_DESCRIPTION))
    await step("commands", _cmds_ok, lambda: bot.set_my_commands(commands))
    await step("menu button", _menu_ok, lambda: bot.set_chat_menu_button(menu_button=MenuButtonCommands()))


# =============================================================================
# KEYBOARDS
# =============================================================================
CB_RE = re.compile(r"^(del|spoil|retry|set):([a-z0-9]{1,32})$")


def make_cb(prefix: str, token: str) -> str:
    token = re.sub(r"[^a-z0-9]", "", token.lower())[:32]
    data = f"{prefix}:{token}"
    if not CB_RE.match(data) or len(data.encode("utf-8")) > 64:
        raise ValueError("invalid callback_data")
    return data


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    rows = [[
        KeyboardButton(_label("fetch", "Fetch"), **_icon("fetch")),
        KeyboardButton(_label("settings", "Settings"), **_icon("settings")),
        KeyboardButton(_label("help", "Help"), **_icon("help")),
    ]]
    return ReplyKeyboardMarkup(
        rows, resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Paste your link here...",
    )


def media_keyboard(token: str, kind: str) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(_label("delete", "Delete"), callback_data=make_cb("del", token), **_icon("delete"))]
    if kind in ("photo", "video"):
        row.append(InlineKeyboardButton(_label("spoiler", "Spoiler"), callback_data=make_cb("spoil", token), **_icon("spoiler")))
    row.append(InlineKeyboardButton(_label("retry", "Retry"), callback_data=make_cb("retry", token), **_icon("retry")))
    return InlineKeyboardMarkup([row])


def retry_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(_label("retry", "Retry"), callback_data=make_cb("retry", token), **_icon("retry"))
    ]])


def settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    doc = "ON" if settings["doc"] else "OFF"
    spo = "ON" if settings["spo"] else "OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📄 Send as file: {doc}", callback_data=make_cb("set", "doc"))],
        [InlineKeyboardButton(_label("spoiler", f"Spoiler by default: {spo}"), callback_data=make_cb("set", "spo"), **_icon("spoiler"))],
    ])


# =============================================================================
# HELPERS
# =============================================================================
VIDEO_EXT = {"mp4", "m4v", "mov"}
AUDIO_EXT = {"mp3", "m4a"}
PHOTO_EXT = {"jpg", "jpeg", "png", "webp"}


async def safe_remove(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        await aiofiles.os.remove(path)
    except FileNotFoundError:
        pass
    except Exception:
        log.warning("safe_remove failed for %s", path, exc_info=True)


async def cleanup_job(job_id: str) -> None:
    for p in list(DOWNLOAD_DIR.glob(f"{job_id}*")):
        await safe_remove(p)


def human_size(n: Optional[float]) -> str:
    if not n:
        return "unknown"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _fmt_duration(sec: Optional[float]) -> str:
    if not sec:
        return "unknown"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def pick_kind(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in PHOTO_EXT:
        return "photo"
    return "document"


_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def extract_url(text: str) -> Optional[str]:
    m = _URL_RE.search(text or "")
    if not m:
        return None
    return m.group(0).rstrip(".,;:!?)]}'\"")


def _safe_name(title: Optional[str]) -> str:
    return (re.sub(r"[^\w\-. ]+", "_", title or "").strip()[:80]) or "media"


def _rp(message_id: Optional[int]) -> Optional[ReplyParameters]:
    return ReplyParameters(message_id=message_id, allow_sending_without_reply=True) if message_id else None


def _retry_secs(e: RetryAfter) -> float:
    ra = e.retry_after
    return ra.total_seconds() if isinstance(ra, timedelta) else float(ra)


async def tg_call(factory):
    timed_out_once = False
    for _ in range(6):
        try:
            return await factory()
        except RetryAfter as e:
            wait = _retry_secs(e) + 1
            log.warning("RetryAfter: sleeping %.0fs", wait)
            await asyncio.sleep(wait)
        except TimedOut:
            if timed_out_once:
                raise
            timed_out_once = True
            log.warning("TimedOut: retrying once")
    raise RuntimeError("Telegram call kept being rate limited")


async def send_text(bot, chat_id: int, text: str, *, reply_markup=None, reply_to: Optional[int] = None):
    t, ents = with_emoji(text)
    return await tg_call(lambda: bot.send_message(
        chat_id, t, entities=ents, reply_markup=reply_markup,
        link_preview_options=NO_PREVIEW, reply_parameters=_rp(reply_to),
    ))


async def edit_text(msg, text: str, reply_markup=None) -> None:
    t, ents = with_emoji(text)
    with contextlib.suppress(TelegramError):
        await msg.edit_text(t, entities=ents, reply_markup=reply_markup, link_preview_options=NO_PREVIEW)


class Registry:
    def __init__(self, cap: int = 1000):
        self._d: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._cap = cap

    def add(self, **data) -> str:
        token = secrets.token_hex(6)
        self._d[token] = data
        while len(self._d) > self._cap:
            self._d.popitem(last=False)
        return token

    def get(self, token: str) -> Optional[dict[str, Any]]:
        return self._d.get(token)

    def pop(self, token: str) -> None:
        self._d.pop(token, None)


REGISTRY = Registry()


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("settings", {"doc": False, "spo": False})


# =============================================================================
# YT-DLP PIPELINE (extended)
# =============================================================================
def _ydl_opts(job_id: str) -> dict[str, Any]:
    """Extended yt-dlp options with cookies, proxy, and YouTube workarounds."""
    opts: dict[str, Any] = {
        "format_sort": ["res:1080", "vcodec:h264", "acodec:m4a"],
        "format": "bv*+ba/b",
        "concurrent_fragment_downloads": 4,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "outtmpl": str(DOWNLOAD_DIR / f"{job_id}.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["tv", "mweb", "web"],
            },
        },
    }

    if COOKIES_FILE and COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)

    if PROXY_URL.strip():
        opts["proxy"] = PROXY_URL.strip()

    return opts


def _extract_metadata(url: str, job_id: str) -> dict[str, Any]:
    with yt_dlp.YoutubeDL({**_ydl_opts(job_id), "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
        info = ydl.sanitize_info(info)
    if info.get("entries"):
        info = next((e for e in info["entries"] if e), info)
    return info


def _estimate_size(info: dict[str, Any]) -> Optional[int]:
    parts = info.get("requested_formats")
    if parts:
        sizes = [(f.get("filesize") or f.get("filesize_approx") or 0) for f in parts]
        return sum(sizes) or None
    return info.get("filesize") or info.get("filesize_approx")


def _download(url: str, job_id: str) -> None:
    with yt_dlp.YoutubeDL(_ydl_opts(job_id)) as ydl:
        ydl.download([url])


def _find_output(job_id: str) -> Optional[Path]:
    junk = {".part", ".ytdl", ".temp", ".json"}
    files = [p for p in DOWNLOAD_DIR.glob(f"{job_id}.*") if p.is_file() and p.suffix not in junk]
    return max(files, key=lambda p: p.stat().st_size) if files else None


def _caption(info: dict[str, Any]) -> str:
    title = info.get("title") or ""
    who = info.get("uploader") or info.get("channel") or ""
    cap = f"{title}\n— {who}" if who and title else (title or who)
    return cap[:MAX_CAPTION]


def _summary(info: dict[str, Any], size: Optional[int]) -> str:
    res = f"{info.get('width')}x{info.get('height')}" if info.get("width") and info.get("height") else "unknown"
    return (
        f"Title: {(info.get('title') or 'unknown')[:200]}\n"
        f"Uploader: {info.get('uploader') or 'unknown'}\n"
        f"Duration: {_fmt_duration(info.get('duration'))}\n"
        f"Resolution: {res}\n"
        f"Format: {info.get('ext') or 'unknown'}\n"
        f"Size: {human_size(size)}"
    )


def _too_big_message(info: dict[str, Any], size: Optional[int]) -> str:
    return (
        "⚠️ This file is larger than Telegram's 50 MB bot upload limit, so I can't send it.\n\n"
        + _summary(info, size)
    )


async def deliver(bot, chat_id: int, path: Path, info: dict[str, Any], settings: dict,
                  token: str, reply_to: Optional[int]) -> None:
    size = (await aiofiles.os.stat(path)).st_size
    kind = pick_kind(path)
    if settings["doc"]:
        kind = "document"
    if kind == "photo" and size > MAX_PHOTO_BYTES:
        kind = "document"

    async with aiofiles.open(path, "rb") as f:
        data = await f.read()
    filename = f"{_safe_name(info.get('title'))}{path.suffix}"
    caption = _caption(info)
    rp = _rp(reply_to)

    async def send(k: str):
        kb = media_keyboard(token, k)
        common = dict(reply_markup=kb, reply_parameters=rp, write_timeout=300, read_timeout=120)
        if k == "video":
            return await tg_call(lambda: bot.send_video(
                chat_id, InputFile(data, filename=filename), caption=caption,
                duration=int(info["duration"]) if info.get("duration") else None,
                width=info.get("width"), height=info.get("height"),
                supports_streaming=True, has_spoiler=settings["spo"], **common))
        if k == "audio":
            return await tg_call(lambda: bot.send_audio(
                chat_id, InputFile(data, filename=filename), caption=caption,
                duration=int(info["duration"]) if info.get("duration") else None,
                title=(info.get("title") or "")[:100] or None,
                performer=(info.get("uploader") or "")[:100] or None, **common))
        if k == "photo":
            return await tg_call(lambda: bot.send_photo(
                chat_id, InputFile(data, filename=filename), caption=caption,
                has_spoiler=settings["spo"], **common))
        return await tg_call(lambda: bot.send_document(
            chat_id, InputFile(data, filename=filename), caption=caption,
            disable_content_type_detection=True, **common))

    sent_kind = kind
    try:
        msg = await send(kind)
    except BadRequest:
        if kind in ("photo", "video"):
            log.warning("send_%s rejected, falling back to document", kind, exc_info=True)
            sent_kind = "document"
            msg = await send("document")
        else:
            raise

    file_id = None
    if msg.video:
        file_id = msg.video.file_id
    elif msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.audio:
        file_id = msg.audio.file_id
    elif msg.document:
        file_id = msg.document.file_id
    entry = REGISTRY.get(token)
    if entry is not None:
        entry.update(file_id=file_id, kind=sent_kind, caption=caption, spoiler=settings["spo"])


async def process_url(url: str, chat_id: int, user_id: int, bot, settings: dict,
                      reply_to: Optional[int] = None) -> None:
    global ACTIVE_JOBS
    job_id = secrets.token_hex(8)
    token = REGISTRY.add(url=url, user_id=user_id, file_id=None, kind=None,
                         caption=None, spoiler=settings["spo"])
    status = None
    try:
        status = await send_text(bot, chat_id, "📥 Fetching info…", reply_to=reply_to)
        info = await asyncio.to_thread(_extract_metadata, url, job_id)

        est = _estimate_size(info)
        if est and est > MAX_UPLOAD_BYTES:
            await send_text(bot, chat_id, _too_big_message(info, est), reply_to=reply_to)
            return

        await edit_text(status, "📥 Downloading…")
        async with DOWNLOAD_SEM:
            ACTIVE_JOBS += 1
            try:
                await asyncio.to_thread(_download, url, job_id)
            finally:
                ACTIVE_JOBS -= 1

        path = _find_output(job_id)
        if path is None:
            raise RuntimeError("download finished but no output file was found")

        size = (await aiofiles.os.stat(path)).st_size
        if size > MAX_UPLOAD_BYTES:
            await send_text(bot, chat_id, _too_big_message(info, size), reply_to=reply_to)
            return

        await edit_text(status, "📥 Uploading…")
        with contextlib.suppress(TelegramError):
            await bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
        await deliver(bot, chat_id, path, info, settings, token, reply_to)

    except DownloadError:
        log.warning("DownloadError for %s", url, exc_info=True)
        await send_text(
            bot, chat_id,
            "⚠️ I couldn't fetch that link. It may be private, geo-blocked, removed, or unsupported.",
            reply_markup=retry_keyboard(token), reply_to=reply_to)
    except Exception:
        log.exception("Unexpected error while processing %s", url)
        with contextlib.suppress(Exception):
            await send_text(
                bot, chat_id,
                "⚠️ Something went wrong on my side. Please try again.",
                reply_markup=retry_keyboard(token), reply_to=reply_to)
    finally:
        await cleanup_job(job_id)
        if status is not None:
            with contextlib.suppress(TelegramError):
                await status.delete()


# =============================================================================
# JANITOR
# =============================================================================
def _sweep_orphans() -> int:
    now = time.time()
    removed = 0
    for p in DOWNLOAD_DIR.iterdir():
        try:
            if p.is_file() and now - p.stat().st_mtime > ORPHAN_AGE_S:
                p.unlink()
                removed += 1
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("Janitor could not remove %s", p, exc_info=True)
    return removed


async def janitor() -> None:
    while True:
        try:
            removed = await asyncio.to_thread(_sweep_orphans)
            if removed:
                log.info("Janitor removed %d orphaned file(s)", removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Janitor sweep failed")
        await asyncio.sleep(JANITOR_INTERVAL_S)


def _dir_usage() -> tuple[int, int]:
    files = [p for p in DOWNLOAD_DIR.iterdir() if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


# =============================================================================
# HANDLERS
# =============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🚀 Welcome to FluxFetch | Universal Downloader!\n\n"
        "📥 Paste any link (video, photo, audio, file) and I'll send it right here.\n"
        "🧹 Everything is deleted from my server right after delivery.\n\n"
        "ℹ️ Tap Help for details, or ⚙️ Settings to change how I deliver."
    )
    await send_text(context.bot, update.effective_chat.id, text, reply_markup=main_reply_keyboard())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "ℹ️ How to use FluxFetch\n\n"
        "1. Paste a link (YouTube, TikTok, Instagram, X, Reddit, Vimeo, SoundCloud, direct file URLs and 1000+ more).\n"
        "2. I fetch it at up to 1080p and upload it.\n"
        "3. Use the buttons under the result: 🗑️ Delete, 🫣 Spoiler, 🔄 Retry.\n\n"
        "⚠️ Telegram bots can upload up to 50 MB. Bigger files get a summary instead.\n"
        "🧹 Files are auto-cleaned after every upload."
    )
    await send_text(context.bot, update.effective_chat.id, text)


def _settings_text(settings: dict) -> str:
    return (
        "⚙️ Settings\n\n"
        f"Send as file (no compression/preview): {'ON' if settings['doc'] else 'OFF'}\n"
        f"Spoiler by default (photo/video): {'ON' if settings['spo'] else 'OFF'}"
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    await send_text(context.bot, update.effective_chat.id, _settings_text(settings),
                    reply_markup=settings_keyboard(settings))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    count, used = await asyncio.to_thread(_dir_usage)
    up = int(time.time() - START_TIME)
    text = (
        "✅ FluxFetch is online\n\n"
        f"Active downloads: {ACTIVE_JOBS}/3\n"
        f"Files on disk: {count} ({human_size(used)})\n"
        f"Uptime: {up // 3600}h {(up % 3600) // 60}m\n"
        f"Premium emoji: {'on' if BOT_OWNER_HAS_PREMIUM else 'off'}\n"
        f"Cookies: {'loaded' if COOKIES_FILE and COOKIES_FILE.exists() else 'none'}\n"
        f"Proxy: {'set' if PROXY_URL.strip() else 'none'}"
    )
    await send_text(context.bot, update.effective_chat.id, text)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    chat_id = update.effective_chat.id

    core = re.sub(r"^[^\w]+", "", text).lower()
    if core == "settings":
        return await cmd_settings(update, context)
    if core == "help":
        return await cmd_help(update, context)
    if core == "fetch":
        return await send_text(context.bot, chat_id, "📥 Paste the link you want to download.")

    url = next((e.url for e in (msg.entities or []) if e.type == MessageEntity.TEXT_LINK and e.url), None)
    url = url or extract_url(text)
    if not url:
        return await send_text(context.bot, chat_id, "⚠️ I couldn't find a link in your message. Paste a full URL starting with http(s)://")

    await process_url(url, chat_id, update.effective_user.id, context.bot,
                      get_settings(context), reply_to=msg.message_id)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    m = CB_RE.match(query.data or "")
    if not m:
        return await query.answer()
    action, token = m.groups()
    settings = get_settings(context)

    if action == "set":
        if token == "doc":
            settings["doc"] = not settings["doc"]
        elif token == "spo":
            settings["spo"] = not settings["spo"]
        t, ents = with_emoji(_settings_text(settings))
        with contextlib.suppress(TelegramError):
            await query.edit_message_text(t, entities=ents, reply_markup=settings_keyboard(settings),
                                          link_preview_options=NO_PREVIEW)
        return await query.answer("Saved")

    entry = REGISTRY.get(token)
    if entry is None:
        return await query.answer("This button has expired. Send the link again.", show_alert=True)
    if entry["user_id"] != query.from_user.id:
        return await query.answer("This isn't your download.", show_alert=True)

    if action == "del":
        with contextlib.suppress(TelegramError):
            await query.message.delete()
        REGISTRY.pop(token)
        return await query.answer("Deleted")

    if action == "spoil":
        if not entry.get("file_id") or entry.get("kind") not in ("photo", "video"):
            return await query.answer("Spoiler isn't available for this file.", show_alert=True)
        new = not entry["spoiler"]
        cls = InputMediaVideo if entry["kind"] == "video" else InputMediaPhoto
        media = cls(media=entry["file_id"], caption=entry["caption"], has_spoiler=new)
        try:
            await query.edit_message_media(media=media, reply_markup=query.message.reply_markup)
            entry["spoiler"] = new
            return await query.answer("Spoiler on" if new else "Spoiler off")
        except RetryAfter as e:
            return await query.answer(f"Too fast, wait {int(_retry_secs(e)) + 1}s", show_alert=True)
        except BadRequest:
            log.warning("spoiler toggle failed", exc_info=True)
            return await query.answer("Couldn't toggle spoiler.", show_alert=True)

    if action == "retry":
        await query.answer("Retrying…")
        if entry.get("file_id") is None:
            with contextlib.suppress(TelegramError):
                await query.message.delete()
        await process_url(entry["url"], query.message.chat_id, query.from_user.id,
                          context.bot, settings)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled exception in handler", exc_info=context.error)


# =============================================================================
# MAIN
# =============================================================================
async def _post_init(app: Application) -> None:
    await apply_branding(app.bot)
    app.bot_data["janitor"] = asyncio.create_task(janitor(), name="janitor")


async def _post_shutdown(app: Application) -> None:
    task = app.bot_data.get("janitor")
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def main() -> None:
    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg not found in PATH: merged video+audio formats will fail.")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(300)
        .pool_timeout(30)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=CB_RE))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("Starting %s (long polling)", BOT_NAME)
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
