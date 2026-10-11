import asyncio

import html

import logging

import os

import re

import shutil

import tempfile

from pathlib import Path

from urllib.parse import urlparse



import aiosqlite
from aiohttp import web

import yt_dlp

from aiogram import Bot, Dispatcher, F, Router

from aiogram.client.default import DefaultBotProperties

from aiogram.enums import ChatMemberStatus, ParseMode

from aiogram.filters import Command, CommandObject, CommandStart

from aiogram.types import CallbackQuery, FSInputFile, Message

from aiogram.utils.keyboard import InlineKeyboardBuilder

from dotenv import load_dotenv



load_dotenv()

logging.basicConfig(level=logging.INFO)

log = logging.getLogger("videobot")



# ---------------------------------------------------------------- config

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN Environment Variable sozlanmagan.")
PORT = int(os.getenv("PORT", "10000"))

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()  # masalan: @mening_kanalim

COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()  # Instagram uchun ixtiyoriy

MAX_BYTES = int(os.getenv("MAX_FILE_MB", "50")) * 1024 * 1024

DB_PATH = os.getenv("DB_PATH", "bot.db")



URL_RE = re.compile(r"https?://\S+")

ALLOWED_HOSTS = (

    "instagram.com", "tiktok.com", "youtube.com", "youtu.be",

    "facebook.com", "fb.watch", "twitter.com", "x.com",

    "pinterest.com", "pin.it", "vimeo.com",

)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}



SLOTS = asyncio.Semaphore(3)  # bir vaqtda ko'pi bilan 3 ta yuklash

ACTIVE: set[int] = set()  # hozir yuklayotgan foydalanuvchilar



# ---------------------------------------------------------------- texts

T = {

    "uz": {

        "start": (

            "Salom, <b>{name}</b>! 👋\n\n"

            "Menga <b>Instagram, TikTok, YouTube Shorts, Facebook, X, Pinterest</b> "

            "havolasini yuboring — videoni yuklab beraman.\n\n"

            "🎵 Xohlasangiz, videodan MP3 ham ajratib beraman."

        ),

        "help": (

            "ℹ️ <b>Qanday ishlaydi?</b>\n"

            "1. Videoning havolasini nusxalang\n"

            "2. Shu yerga yuboring\n"

            "3. Video yoki audioni oling\n\n"

            "Buyruqlar: /start, /lang, /help"

        ),

        "choose_lang": "Tilni tanlang / Выберите язык:",

        "lang_set": "✅ Til o'zgartirildi.",

        "send_link": "Havolani yuboring 🔗",

        "bad_link": "❌ Bu havola qo'llab-quvvatlanmaydi. Instagram, TikTok, YouTube va boshqa havolalarni yuboring.",

        "busy": "⏳ Oldingi yuklash tugamaguncha kuting.",

        "loading": "⏳ Yuklanmoqda...",

        "audio_loading": "🎵 Audio tayyorlanmoqda...",

        "too_big": "⚠️ Fayl juda katta (Telegram limiti: {mb} MB).",

        "fail": "❌ Yuklab bo'lmadi. Havola noto'g'ri, video yopiq yoki o'chirilgan bo'lishi mumkin.",

        "sub_need": "Botdan foydalanish uchun kanalimizga obuna bo'ling 👇",

        "sub_btn": "📢 Kanalga o'tish",

        "sub_check": "✅ Tekshirish",

        "not_sub": "Siz hali obuna bo'lmagansiz ❗️",

        "audio_btn": "🎵 MP3 yuklash",

    },

    "ru": {

        "start": (

            "Привет, <b>{name}</b>! 👋\n\n"

            "Отправь мне ссылку на <b>Instagram, TikTok, YouTube Shorts, Facebook, X, Pinterest</b> "

            "— я скачаю видео.\n\n"

            "🎵 Если нужно, могу достать из видео MP3."

        ),

        "help": (

            "ℹ️ <b>Как это работает?</b>\n"

            "1. Скопируйте ссылку на видео\n"

            "2. Отправьте её сюда\n"

            "3. Получите видео или аудио\n\n"

            "Команды: /start, /lang, /help"

        ),

        "choose_lang": "Tilni tanlang / Выберите язык:",

        "lang_set": "✅ Язык изменён.",

        "send_link": "Отправьте ссылку 🔗",

        "bad_link": "❌ Эта ссылка не поддерживается. Отправьте ссылку на Instagram, TikTok, YouTube и т.д.",

        "busy": "⏳ Дождитесь окончания предыдущей загрузки.",

        "loading": "⏳ Загружаю...",

        "audio_loading": "🎵 Готовлю аудио...",

        "too_big": "⚠️ Файл слишком большой (лимит Telegram: {mb} МБ).",

        "fail": "❌ Не удалось скачать. Ссылка неверная, видео закрыто или удалено.",

        "sub_need": "Чтобы пользоваться ботом, подпишитесь на наш канал 👇",

        "sub_btn": "📢 Перейти в канал",

        "sub_check": "✅ Проверить",

        "not_sub": "Вы ещё не подписаны ❗️",

        "audio_btn": "🎵 Скачать MP3",

    },

}



router = Router()





# ---------------------------------------------------------------- database

async def db_init() -> None:

    async with aiosqlite.connect(DB_PATH) as db:

        await db.executescript(

            """

            CREATE TABLE IF NOT EXISTS users(

                id INTEGER PRIMARY KEY,

                lang TEXT DEFAULT 'uz',

                joined TEXT DEFAULT CURRENT_TIMESTAMP,

                downloads INTEGER DEFAULT 0

            );

            CREATE TABLE IF NOT EXISTS links(

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                url TEXT UNIQUE,

                video_id TEXT,

                audio_id TEXT

            );

            """

        )

        await db.commit()





async def get_lang(uid: int) -> str:

    async with aiosqlite.connect(DB_PATH) as db:

        cur = await db.execute("SELECT lang FROM users WHERE id=?", (uid,))

        row = await cur.fetchone()

    return row[0] if row else "uz"





async def upsert_user(uid: int) -> bool:

    """True qaytaradi, agar foydalanuvchi yangi bo'lsa."""

    async with aiosqlite.connect(DB_PATH) as db:

        cur = await db.execute("SELECT 1 FROM users WHERE id=?", (uid,))

        exists = await cur.fetchone()

        if not exists:

            await db.execute("INSERT INTO users(id) VALUES(?)", (uid,))

            await db.commit()

        return not exists





async def set_lang(uid: int, lang: str) -> None:

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("UPDATE users SET lang=? WHERE id=?", (lang, uid))

        await db.commit()





async def link_row(url: str) -> tuple:

    """(id, video_id, audio_id) qaytaradi, kerak bo'lsa yaratadi."""

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("INSERT OR IGNORE INTO links(url) VALUES(?)", (url,))

        await db.commit()

        cur = await db.execute("SELECT id, video_id, audio_id FROM links WHERE url=?", (url,))

        return await cur.fetchone()





async def link_by_id(link_id: int) -> tuple | None:

    async with aiosqlite.connect(DB_PATH) as db:

        cur = await db.execute("SELECT url, audio_id FROM links WHERE id=?", (link_id,))

        return await cur.fetchone()





async def save_file_id(link_id: int, column: str, file_id: str) -> None:

    assert column in ("video_id", "audio_id")

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(f"UPDATE links SET {column}=? WHERE id=?", (file_id, link_id))

        await db.commit()





async def count_download(uid: int) -> None:

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("UPDATE users SET downloads=downloads+1 WHERE id=?", (uid,))

        await db.commit()





# ---------------------------------------------------------------- helpers

def supported(url: str) -> bool:

    host = (urlparse(url).hostname or "").lower()

    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)





async def is_subscribed(bot: Bot, uid: int) -> bool:

    if not REQUIRED_CHANNEL:

        return True

    try:

        member = await bot.get_chat_member(REQUIRED_CHANNEL, uid)

        return member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)

    except Exception as e:  # bot kanalda admin emas va hokazo

        log.warning("Obunani tekshirib bo'lmadi: %s", e)

        return True





def lang_kb():

    kb = InlineKeyboardBuilder()

    kb.button(text="🇺🇿 O'zbekcha", callback_data="lang:uz")

    kb.button(text="🇷🇺 Русский", callback_data="lang:ru")

    return kb.as_markup()





def sub_kb(lang: str):

    kb = InlineKeyboardBuilder()

    kb.button(text=T[lang]["sub_btn"], url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}")

    kb.button(text=T[lang]["sub_check"], callback_data="chk")

    kb.adjust(1)

    return kb.as_markup()





def audio_kb(lang: str, link_id: int):

    kb = InlineKeyboardBuilder()

    kb.button(text=T[lang]["audio_btn"], callback_data=f"aud:{link_id}")

    return kb.as_markup()





def _download(url: str, audio: bool, workdir: str):

    opts = {

        "outtmpl": str(Path(workdir) / "%(id)s.%(ext)s"),

        "noplaylist": True,

        "quiet": True,

        "no_warnings": True,

        "socket_timeout": 20,

        "retries": 3,

    }

    if COOKIES_FILE:

        opts["cookiefile"] = COOKIES_FILE

    if audio:

        opts["format"] = "bestaudio/best"

        opts["postprocessors"] = [

            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}

        ]

    else:

        opts["format"] = (

            f"best[ext=mp4][filesize<{MAX_BYTES}]/"

            f"best[ext=mp4][filesize_approx<{MAX_BYTES}]/"

            "best[ext=mp4]/best"

        )

    with yt_dlp.YoutubeDL(opts) as ydl:

        info = ydl.extract_info(url, download=True)

        path = Path(ydl.prepare_filename(info))

    if audio:

        path = path.with_suffix(".mp3")

    if not path.exists():  # kengaytma o'zgargan bo'lsa

        files = sorted(Path(workdir).glob("*"))

        if not files:

            raise FileNotFoundError("Fayl topilmadi")

        path = files[0]

    return path, info





async def fetch(url: str, audio: bool):

    workdir = tempfile.mkdtemp(prefix="vb_")

    try:

        async with SLOTS:

            path, info = await asyncio.to_thread(_download, url, audio, workdir)

        return workdir, path, info

    except Exception:

        shutil.rmtree(workdir, ignore_errors=True)

        raise





# ---------------------------------------------------------------- handlers

@router.message(CommandStart())

async def cmd_start(message: Message):

    uid = message.from_user.id

    is_new = await upsert_user(uid)

    if is_new:

        await message.answer(T["uz"]["choose_lang"], reply_markup=lang_kb())

        return

    lang = await get_lang(uid)

    name = html.escape(message.from_user.first_name or "")

    await message.answer(T[lang]["start"].format(name=name))





@router.message(Command("lang"))

async def cmd_lang(message: Message):

    await upsert_user(message.from_user.id)

    await message.answer(T["uz"]["choose_lang"], reply_markup=lang_kb())





@router.message(Command("help"))

async def cmd_help(message: Message):

    lang = await get_lang(message.from_user.id)

    await message.answer(T[lang]["help"])





@router.callback_query(F.data.startswith("lang:"))

async def cb_lang(call: CallbackQuery):

    lang = call.data.split(":")[1]

    await upsert_user(call.from_user.id)

    await set_lang(call.from_user.id, lang)

    name = html.escape(call.from_user.first_name or "")

    await call.message.edit_text(T[lang]["start"].format(name=name))

    await call.answer(T[lang]["lang_set"])





@router.callback_query(F.data == "chk")

async def cb_check(call: CallbackQuery, bot: Bot):

    lang = await get_lang(call.from_user.id)

    if await is_subscribed(bot, call.from_user.id):

        await call.message.edit_text(T[lang]["send_link"])

        await call.answer()

    else:

        await call.answer(T[lang]["not_sub"], show_alert=True)





@router.message(Command("stats"), F.from_user.id.in_(ADMIN_IDS))

async def cmd_stats(message: Message):

    async with aiosqlite.connect(DB_PATH) as db:

        total = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]

        today = (

            await (

                await db.execute("SELECT COUNT(*) FROM users WHERE date(joined)=date('now')")

            ).fetchone()

        )[0]

        dl = (await (await db.execute("SELECT COALESCE(SUM(downloads),0) FROM users")).fetchone())[0]

    await message.answer(

        f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: {total}\n🆕 Bugun qo'shilgan: {today}\n📥 Yuklashlar: {dl}"

    )





@router.message(Command("broadcast"), F.from_user.id.in_(ADMIN_IDS))

async def cmd_broadcast(message: Message, command: CommandObject, bot: Bot):

    src = message.reply_to_message

    text = command.args

    if not src and not text:

        await message.answer("Foydalanish: /broadcast matn  yoki xabarga reply qilib /broadcast")

        return

    async with aiosqlite.connect(DB_PATH) as db:

        ids = [r[0] for r in await (await db.execute("SELECT id FROM users")).fetchall()]

    ok = fail = 0

    for uid in ids:

        try:

            if src:

                await src.copy_to(uid)

            else:

                await bot.send_message(uid, text)

            ok += 1

        except Exception:

            fail += 1

        await asyncio.sleep(0.05)

    await message.answer(f"✅ Yuborildi: {ok}\n❌ Xatolik: {fail}")





@router.message(F.text)

async def on_text(message: Message, bot: Bot):

    uid = message.from_user.id

    await upsert_user(uid)

    lang = await get_lang(uid)



    if not await is_subscribed(bot, uid):

        await message.answer(T[lang]["sub_need"], reply_markup=sub_kb(lang))

        return



    m = URL_RE.search(message.text)

    if not m or not supported(m.group(0)):

        await message.answer(T[lang]["bad_link"] if m else T[lang]["send_link"])

        return

    url = m.group(0)



    if uid in ACTIVE:

        await message.answer(T[lang]["busy"])

        return



    link_id, video_id, _ = await link_row(url)

    me = await bot.me()

    caption = f"📥 @{me.username}"



    # avval yuklangan bo'lsa, keshdan yuboramiz

    if video_id:

        try:

            await message.answer_video(video_id, caption=caption, reply_markup=audio_kb(lang, link_id))

            await count_download(uid)

            return

        except Exception:

            pass



    ACTIVE.add(uid)

    status = await message.answer(T[lang]["loading"])

    workdir = None

    try:

        workdir, path, info = await fetch(url, audio=False)

        if path.stat().st_size > MAX_BYTES:

            await status.edit_text(T[lang]["too_big"].format(mb=MAX_BYTES // 1024 // 1024))

            return

        if path.suffix.lower() in IMAGE_EXT:

            await message.answer_photo(FSInputFile(path), caption=caption)

        else:

            sent = await message.answer_video(

                FSInputFile(path),

                caption=caption,

                duration=int(info.get("duration") or 0) or None,

                width=info.get("width"),

                height=info.get("height"),

                supports_streaming=True,

                reply_markup=audio_kb(lang, link_id),

            )

            await save_file_id(link_id, "video_id", sent.video.file_id)

        await count_download(uid)

        await status.delete()

    except yt_dlp.utils.DownloadError as e:

        log.info("DownloadError %s: %s", url, e)

        await status.edit_text(T[lang]["fail"])

    except Exception:

        log.exception("Xatolik: %s", url)

        await status.edit_text(T[lang]["fail"])

    finally:

        ACTIVE.discard(uid)

        if workdir:

            shutil.rmtree(workdir, ignore_errors=True)





@router.callback_query(F.data.startswith("aud:"))

async def cb_audio(call: CallbackQuery, bot: Bot):

    uid = call.from_user.id

    lang = await get_lang(uid)

    row = await link_by_id(int(call.data.split(":")[1]))

    if not row:

        await call.answer(T[lang]["fail"], show_alert=True)

        return

    url, audio_id = row

    link_id = int(call.data.split(":")[1])

    me = await bot.me()

    caption = f"🎵 @{me.username}"



    if audio_id:

        await call.message.answer_audio(audio_id, caption=caption)

        await call.answer()

        return



    if uid in ACTIVE:

        await call.answer(T[lang]["busy"], show_alert=True)

        return



    ACTIVE.add(uid)

    await call.answer(T[lang]["audio_loading"])

    workdir = None

    try:

        workdir, path, info = await fetch(url, audio=True)

        if path.stat().st_size > MAX_BYTES:

            await call.message.answer(T[lang]["too_big"].format(mb=MAX_BYTES // 1024 // 1024))

            return

        sent = await call.message.answer_audio(

            FSInputFile(path),

            caption=caption,

            title=(info.get("title") or "Audio")[:64],

            performer=(info.get("uploader") or "")[:64] or None,

            duration=int(info.get("duration") or 0) or None,

        )

        await save_file_id(link_id, "audio_id", sent.audio.file_id)

    except Exception:

        log.exception("Audio xatolik: %s", url)

        await call.message.answer(T[lang]["fail"])

    finally:

        ACTIVE.discard(uid)

        if workdir:

            shutil.rmtree(workdir, ignore_errors=True)





# ---------------------------------------------------------------- run
async def health_check(request: web.Request) -> web.Response:
    return web.Response(text="Bot ishlayapti!", status=200)


async def start_web_server() -> None:
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    log.info("Health-check web-server %s-portda ishga tushdi", PORT)


async def main() -> None:
    await db_init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)

    try:
        # Telegram polling va UptimeRobot web-serveri bir vaqtda ishlaydi.
        await asyncio.gather(
            dp.start_polling(bot),
            start_web_server(),
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
