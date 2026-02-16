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

def load_users() -> set[int]:
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            return set(json.load(f))
    return set()


def save_users(users: set[int]):
    with open(USERS_FILE, "w") as f:
        json.dump(list(users), f)


allowed_users = load_users()


def is_allowed(user_id: int) -> bool:
    return user_id == ADMIN_ID or user_id in allowed_users


# --- FSM for adding user ---

class AddUser(StatesGroup):
    waiting_for_id = State()


# --- Access check ---

@dp.message(~F.from_user.id.in_({ADMIN_ID}) & ~F.from_user.id.in_(allowed_users), F.text == "/admin")
async def no_access_admin(message: Message):
    await message.answer("Нет доступа.")


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
    await state.set_state(AddUser.waiting_for_id)
    await state.update_data(action="add")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
    ])
    await callback.message.edit_text("Введи ID пользователя:\n\nПример: 714284843", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "remove_user", F.from_user.id == ADMIN_ID)
async def cb_remove_user(callback: CallbackQuery, state: FSMContext):
    if not allowed_users:
        await callback.message.edit_text("Список пользователей пуст.")
        await callback.answer()
        return
    await state.set_state(AddUser.waiting_for_id)
    await state.update_data(action="remove")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
    ])
    await callback.message.edit_text("Введи ID пользователя для удаления:\n\nПример: 714284843", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "list_users", F.from_user.id == ADMIN_ID)
async def cb_list_users(callback: CallbackQuery):
    if not allowed_users:
        text = "Список пользователей пуст."
    else:
        lines = [f"• {uid}" for uid in sorted(allowed_users)]
        text = "Пользователи:\n" + "\n".join(lines)
    await callback.message.edit_text(text)
    await callback.answer()


@dp.callback_query(F.data == "cancel", F.from_user.id == ADMIN_ID)
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Отменено.")
    await callback.answer()


@dp.message(AddUser.waiting_for_id, F.from_user.id == ADMIN_ID)
async def process_user_id(message: Message, state: FSMContext):
    data = await state.get_data()
    action = data.get("action", "add")

    text = message.text.strip()
    if not text.isdigit():
        await message.answer("Это не похоже на ID. Введи числовой ID или нажми Отмена.")
        return

    user_id = int(text)

    if action == "remove":
        if user_id in allowed_users:
            allowed_users.discard(user_id)
            save_users(allowed_users)
            log.info("Admin removed user %s", user_id)
            await message.answer(f"Пользователь {user_id} удалён.")
        else:
            await message.answer(f"Пользователь {user_id} не найден в списке.")
    else:
        if user_id in allowed_users:
            await message.answer(f"Пользователь {user_id} уже добавлен.")
        else:
            allowed_users.add(user_id)
            save_users(allowed_users)
            log.info("Admin added user %s", user_id)
            await message.answer(f"Пользователь {user_id} добавлен.")

    await state.clear()


# --- Bot commands ---

@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    if not is_allowed(message.from_user.id):
        log.info("Unauthorized user %s tried /start", message.from_user.id)
        await message.answer(f"Нет доступа. Обратись к администратору.\n\nТвой ID: {message.from_user.id}")
        return
    log.info("User %s sent /start", message.from_user.id)
    await message.answer(
        "Привет! Отправь мне ссылку на TikTok видео, и я скачаю его для тебя."
    )


@dp.message(F.text.regexp(TIKTOK_REGEX))
async def handle_tiktok_link(message: Message):
    if not is_allowed(message.from_user.id):
        log.info("Unauthorized user %s tried to download", message.from_user.id)
        await message.answer(f"Нет доступа. Обратись к администратору.\n\nТвой ID: {message.from_user.id}")
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
