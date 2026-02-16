import logging
import os
import re
import tempfile

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile
from dotenv import load_dotenv
import yt_dlp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
PROXY = os.environ.get("PROXY")  # optional, e.g. socks5://127.0.0.1:1080

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

if PROXY:
    log.info("Using proxy for yt-dlp: %s", PROXY)

TIKTOK_REGEX = re.compile(r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/\S+")
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB Telegram limit


@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    log.info("User %s sent /start", message.from_user.id)
    await message.answer(
        "Привет! Отправь мне ссылку на TikTok видео, и я скачаю его для тебя."
    )


@dp.message(F.text.regexp(TIKTOK_REGEX))
async def handle_tiktok_link(message: Message):
    url_match = TIKTOK_REGEX.search(message.text)
    if not url_match:
        return
    url = url_match.group(0)
    log.info("User %s sent TikTok link: %s", message.from_user.id, url)

    status_msg = await message.answer("Скачиваю видео...")

    tmp_dir = tempfile.mkdtemp()
    outtmpl = os.path.join(tmp_dir, "%(id)s.%(ext)s")

    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": outtmpl,
        **({"proxy": PROXY} if PROXY else {}),
    }

    try:
        log.info("Downloading video from %s", url)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

        file_size = os.path.getsize(filename)
        log.info("Downloaded: %s (%.2f MB)", filename, file_size / 1024 / 1024)

        if file_size > MAX_FILE_SIZE:
            log.warning("File too large: %.2f MB", file_size / 1024 / 1024)
            await status_msg.edit_text(
                "Видео слишком большое (>50 МБ) для отправки в Telegram."
            )
            return

        log.info("Sending video to user %s", message.from_user.id)
        video = FSInputFile(filename)
        await message.answer_video(video)
        await status_msg.delete()
        log.info("Video sent successfully to user %s", message.from_user.id)

    except Exception as e:
        log.exception("Failed to process video: %s", url)
        await status_msg.edit_text(f"Не удалось скачать видео: {e}")
    finally:
        for f in os.listdir(tmp_dir):
            os.remove(os.path.join(tmp_dir, f))
        os.rmdir(tmp_dir)
        log.info("Cleaned up temp dir %s", tmp_dir)


if __name__ == "__main__":
    dp.run_polling(bot)
