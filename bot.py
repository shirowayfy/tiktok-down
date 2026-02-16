import json
import logging
import os
import re
import tempfile

from aiogram import Bot, Dispatcher, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    FSInputFile,
)
from dotenv import load_dotenv
import yt_dlp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
PROXY = os.environ.get("PROXY")

USERS_FILE = os.path.join(os.path.dirname(__file__), "users.json")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

if PROXY:
    log.info("Using proxy for yt-dlp: %s", PROXY)

TIKTOK_REGEX = re.compile(r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/\S+")
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB Telegram limit


# --- User storage ---

def load_users() -> set[str]:
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE) as f:
                return set(json.load(f))
        except (json.JSONDecodeError, ValueError):
            log.warning("Corrupted users.json, starting fresh")
    return set()


def save_users(users: set[str]):
    with open(USERS_FILE, "w") as f:
        json.dump(sorted(users), f)


allowed_users = load_users()


def is_allowed(user_id: int, username: str | None) -> bool:
    if user_id == ADMIN_ID:
        return True
    if username and username.lower() in allowed_users:
        return True
    return False


# --- FSM for adding user ---

class AddUser(StatesGroup):
    waiting_for_username = State()


# --- Admin panel ---


@dp.message(F.text == "/admin", F.from_user.id == ADMIN_ID)
async def cmd_admin(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Добавить пользователя", callback_data="add_user")],
        [InlineKeyboardButton(text="Удалить пользователя", callback_data="remove_user")],
        [InlineKeyboardButton(text="Список пользователей", callback_data="list_users")],
    ])
    await message.answer("Управление пользователями:", reply_markup=kb)


@dp.callback_query(F.data == "add_user", F.from_user.id == ADMIN_ID)
async def cb_add_user(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddUser.waiting_for_username)
    await state.update_data(action="add", prompt_msg_id=callback.message.message_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
    ])
    await callback.message.edit_text("Введи username пользователя:\n\nПример: @Soln_z", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "remove_user", F.from_user.id == ADMIN_ID)
async def cb_remove_user(callback: CallbackQuery, state: FSMContext):
    if not allowed_users:
        await callback.message.edit_text("Список пользователей пуст.")
        await callback.answer()
        return
    await state.set_state(AddUser.waiting_for_username)
    await state.update_data(action="remove", prompt_msg_id=callback.message.message_id)
    lines = [f"• @{u}" for u in sorted(allowed_users)]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
    ])
    await callback.message.edit_text(
        "Введи username для удаления:\n\n" + "\n".join(lines),
        reply_markup=kb,
    )
    await callback.answer()


@dp.callback_query(F.data == "list_users", F.from_user.id == ADMIN_ID)
async def cb_list_users(callback: CallbackQuery):
    if not allowed_users:
        text = "Список пользователей пуст."
    else:
        lines = [f"• @{u}" for u in sorted(allowed_users)]
        text = "Пользователи:\n" + "\n".join(lines)
    await callback.message.edit_text(text)
    await callback.answer()


@dp.callback_query(F.data == "cancel", F.from_user.id == ADMIN_ID)
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Отменено.")
    await callback.answer()


@dp.message(AddUser.waiting_for_username, F.from_user.id == ADMIN_ID)
async def process_username(message: Message, state: FSMContext):
    data = await state.get_data()
    action = data.get("action", "add")
    prompt_msg_id = data.get("prompt_msg_id")

    username = message.text.strip().lstrip("@").lower()
    if not username:
        await message.answer("Введи username или нажми Отмена.")
        return

    if action == "remove":
        if username in allowed_users:
            allowed_users.discard(username)
            save_users(allowed_users)
            log.info("Admin removed user @%s", username)
            await message.answer(f"Пользователь @{username} удалён.")
        else:
            await message.answer(f"Пользователь @{username} не найден в списке.")
    else:
        if username in allowed_users:
            await message.answer(f"Пользователь @{username} уже добавлен.")
        else:
            allowed_users.add(username)
            save_users(allowed_users)
            log.info("Admin added user @%s", username)
            await message.answer(f"Пользователь @{username} добавлен.")

    if prompt_msg_id:
        await bot.delete_message(message.chat.id, prompt_msg_id)
    await state.clear()


# --- Bot commands ---

@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    if not is_allowed(message.from_user.id, message.from_user.username):
        log.info("Unauthorized user %s (@%s) tried /start", message.from_user.id, message.from_user.username)
        await message.answer("Нет доступа. Обратись к администратору.")
        return
    log.info("User %s sent /start", message.from_user.id)
    await message.answer(
        "Привет! Отправь мне ссылку на TikTok видео, и я скачаю его для тебя."
    )


@dp.message(F.text.regexp(TIKTOK_REGEX))
async def handle_tiktok_link(message: Message):
    if not is_allowed(message.from_user.id, message.from_user.username):
        log.info("Unauthorized user %s (@%s) tried to download", message.from_user.id, message.from_user.username)
        await message.answer("Нет доступа. Обратись к администратору.")
        return

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
