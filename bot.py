
# ==========================================
# 2. БАЗА ДАННЫХ (SQLite)
# ==========================================
import sqlite3

import asyncio
import csv
import logging
import os
import sys
from datetime import datetime, timedelta
import aiohttp
import pytz
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ==========================================
# 1. КОНФИГУРАЦИЯ И НАСТРОЙКИ
# ==========================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

if not BOT_TOKEN:
    raise ValueError("ОШИБКА: Задайте BOT_TOKEN в файле .env!")

KYIV_TZ = pytz.timezone("Europe/Kyiv")
DB_NAME = "school_bot.db"

# Координаты городов для погоды
CITIES = {
    "Чернигов": {"lat": 51.4939, "lon": 31.2943},
    "Киев": {"lat": 50.4501, "lon": 30.5234},
    "Львов": {"lat": 49.8397, "lon": 24.0297},
    "Одесса": {"lat": 46.4825, "lon": 30.7233},
}

def get_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            class_name TEXT,
            city TEXT DEFAULT 'Чернигов',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_name TEXT,
            day_of_week INTEGER,
            lesson_number INTEGER,
            subject TEXT,
            teacher TEXT,
            room TEXT,
            start_time TEXT,
            end_time TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS temp_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_name TEXT,
            date TEXT,
            lesson_number INTEGER,
            new_subject TEXT,
            new_teacher TEXT,
            new_room TEXT,
            is_cancelled BOOLEAN DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            reminder_minutes INTEGER DEFAULT 5,
            weather_enabled BOOLEAN DEFAULT 1
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            lesson_date TEXT,
            lesson_number INTEGER,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


def get_user(user_id: int):
    conn = get_connection()
    user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return user


def save_user_class(user_id: int, class_name: str):
    conn = get_connection()
    conn.execute("""
        INSERT INTO users (user_id, class_name) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET class_name = ?
    """, (user_id, class_name, class_name))
    conn.execute("""
        INSERT INTO user_settings (user_id) VALUES (?)
        ON CONFLICT(user_id) DO NOTHING
    """, (user_id,))
    conn.commit()
    conn.close()


def get_user_settings(user_id: int):
    conn = get_connection()
    settings = conn.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    if not settings:
        conn = get_connection()
        conn.execute("INSERT INTO user_settings (user_id) VALUES (?)", (user_id,))
        conn.commit()
        settings = conn.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        conn.close()
    return settings


def update_user_settings(user_id: int, reminder_minutes: int = None, weather_enabled: int = None):
    conn = get_connection()
    if reminder_minutes is not None:
        conn.execute("UPDATE user_settings SET reminder_minutes = ? WHERE user_id = ?", (reminder_minutes, user_id))
    if weather_enabled is not None:
        conn.execute("UPDATE user_settings SET weather_enabled = ? WHERE user_id = ?", (weather_enabled, user_id))
    conn.commit()
    conn.close()


def get_schedule_for_day(class_name: str, day_of_week: int, date_str: str):
    conn = get_connection()
    lessons = conn.execute("""
        SELECT * FROM schedule 
        WHERE class_name = ? AND day_of_week = ? 
        ORDER BY lesson_number ASC
    """, (class_name, day_of_week)).fetchall()

    result = []
    for lesson in lessons:
        lesson_dict = dict(lesson)
        temp = conn.execute("""
            SELECT * FROM temp_changes 
            WHERE class_name = ? AND date = ? AND lesson_number = ?
        """, (class_name, date_str, lesson_dict["lesson_number"])).fetchone()

        if temp:
            if temp["is_cancelled"]:
                continue
            lesson_dict["subject"] = temp["new_subject"]
            lesson_dict["teacher"] = temp["new_teacher"]
            lesson_dict["room"] = temp["new_room"]

        result.append(lesson_dict)

    result.sort(key=lambda x: x["lesson_number"])
    conn.close()
    return result


def get_full_week_schedule(class_name: str):
    conn = get_connection()
    schedule = conn.execute("""
        SELECT * FROM schedule WHERE class_name = ? ORDER BY day_of_week, lesson_number
    """, (class_name,)).fetchall()
    conn.close()
    return schedule


def import_schedule_from_csv(file_path: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM schedule")
    count = 0
    with open(file_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cursor.execute("""
                INSERT INTO schedule (class_name, day_of_week, lesson_number, subject, teacher, room, start_time, end_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["class_name"],
                int(row["day_of_week"]),
                int(row["lesson_number"]),
                row["subject"],
                row["teacher"],
                row["room"],
                row["start_time"],
                row["end_time"]
            ))
            count += 1
    conn.commit()
    conn.close()
    return count


# ==========================================
# 3. ПОГОДА (Open-Meteo API)
# ==========================================
def get_weather_description(code: int) -> str:
    codes = {
        0: "☀️ Ясно", 1: "🌤 Преимущественно ясно", 2: "⛅️ Переменная облачность",
        3: "☁️ Пасмурно", 45: "🌫 Туман", 51: "🌧 Легкая морось", 61: "🌧 Небольшой дождь",
        63: "🌧 Дождь", 71: "🌨 Снег", 95: "⛈ Гроза"
    }
    return codes.get(code, "🌦 Облачно")


async def fetch_weather(city: str = "Чернигов") -> str:
    coords = CITIES.get(city, CITIES["Чернигов"])
    url = f"https://api.open-meteo.com/v1/forecast?latitude={coords['lat']}&longitude={coords['lon']}&current=temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m&timezone=Europe%2FKyiv"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as response:
                if response.status != 200:
                    return "🌤 Погода временно недоступна."
                data = await response.json()
                current = data.get("current", {})

                temp = current.get("temperature_2m")
                app_temp = current.get("apparent_temperature")
                precip = current.get("precipitation", 0)
                code = current.get("weather_code", 0)
                wind = current.get("wind_speed_10m")

                desc = get_weather_description(code)

                text = (
                    f"🌤 **Погода в г. {city}**\n"
                    f"🌡 Температура: {temp:+.1f} °C\n"
                    f"🤔 Ощущается как: {app_temp:+.1f} °C\n"
                    f"Состояние: {desc}\n"
                )
                if precip is not None and precip > 0:
                    text += f"🌧 Осадки: {precip} мм\n"
                if wind is not None:
                    text += f"💨 Ветер: {wind} км/ч\n"

                if temp is not None and temp < 10:
                    text += "🧥 *Совет: захвати куртку, если на улице прохладно!*"
                elif temp is not None and temp >= 20:
                    text += "☀️ *Совет: На улице тепло!*"

                return text
    except Exception:
        return "🌤 Погода временно недоступна."


# ==========================================
# 4. КЛАВИАТУРЫ
# ==========================================
def get_classes_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="9А", callback_data="class_9А"), InlineKeyboardButton(text="9Б", callback_data="class_9Б")],
        [InlineKeyboardButton(text="9В", callback_data="class_9В"), InlineKeyboardButton(text="9Г", callback_data="class_9Г")]
    ])


def get_main_menu_keyboard(is_admin: bool = False):
    keyboard = [
        [InlineKeyboardButton(text="📅 Расписание сегодня", callback_data="menu_today"), InlineKeyboardButton(text="⏭ На завтра", callback_data="menu_tomorrow")],
        [InlineKeyboardButton(text="🗓 На неделю", callback_data="menu_week"), InlineKeyboardButton(text="🌤 Погода", callback_data="menu_weather")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu_settings"), InlineKeyboardButton(text="🏫 Сменить класс", callback_data="menu_class")]
    ]
    if is_admin:
        keyboard.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_settings_keyboard(reminder_min: int, weather_on: int):
    w_status = "✅ Вкл" if weather_on else "❌ Выкл"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⏰ Напоминание: {reminder_min} мин.", callback_data="set_remind_menu")],
        [InlineKeyboardButton(text=f"🌤 Погода в уведомлениях: {w_status}", callback_data="toggle_weather")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")]
    ])


def get_reminder_time_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="5 минут", callback_data="remind_5"),
            InlineKeyboardButton(text="10 минут", callback_data="remind_10"),
            InlineKeyboardButton(text="15 минут", callback_data="remind_15")
        ],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_settings")]
    ])


def get_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика классов", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📥 Загрузить CSV расписания", callback_data="admin_csv")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_menu")]
    ])


# ==========================================
# 5. РОУТЕРЫ И ЛОГИКА БОТА
# ==========================================
class AdminStates(StatesGroup):
    waiting_for_csv = State()


router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message):
    user = get_user(message.from_user.id)
    if not user or not user["class_name"]:
        await message.answer(
            "Привет! 👋 Я твой школьный помощник. Буду напоминать о следующих уроках, учителях, кабинетах и погоде перед занятием.\n\n"
            "Выбери свой класс:",
            reply_markup=get_classes_keyboard()
        )
    else:
        is_admin = (message.from_user.id == ADMIN_ID)
        await message.answer(
            f"С возвращением! Твой текущий класс: **{user['class_name']}**.",
            reply_markup=get_main_menu_keyboard(is_admin),
            parse_mode="Markdown"
        )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "🛠 **Доступные команды:**\n"
        "/start — Перезапуск и выбор класса\n"
        "/class — Изменить класс\n"
        "/schedule — Расписание на сегодня\n"
        "/tomorrow — Расписание на завтра\n"
        "/week — Расписание на всю неделю\n"
        "/weather — Текущая погода\n"
        "/settings — Настройки уведомлений\n"
        "/help — Справка",
        parse_mode="Markdown"
    )


@router.message(Command("class"))
@router.callback_query(F.data == "menu_class")
async def process_class_command(event: Message | CallbackQuery):
    msg = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        await event.answer()
    await msg.answer("Выбери свой класс:", reply_markup=get_classes_keyboard())


@router.callback_query(F.data.startswith("class_"))
async def process_class_choice(callback: CallbackQuery):
    class_name = callback.data.split("_")[1]
    save_user_class(callback.from_user.id, class_name)
    is_admin = (callback.from_user.id == ADMIN_ID)

    await callback.message.edit_text(
        f"Ты выбрал **{class_name}**. Теперь я буду показывать расписание этого класса.\n"
        "Если ты выбрал неправильный класс, его можно изменить в настройках или командой /class.",
        reply_markup=get_main_menu_keyboard(is_admin),
        parse_mode="Markdown"
    )
    await callback.answer()


def format_lessons_list(lessons, date_title):
    if not lessons:
        return f"📅 **Расписание на {date_title}:**\n\nУроков нет! 🎉 (или расписание еще не заполнено)"

    text = f"📅 **Расписание на {date_title}:**\n\n"
    for l in lessons:
        text += (
            f"🔹 **Урок №{l['lesson_number']}** ({l['start_time']} - {l['end_time']})\n"
            f"📚 Предмет: {l['subject']}\n"
            f"👨‍🏫 Учитель: {l['teacher']}\n"
            f"🚪 Кабинет: {l['room']}\n\n"
        )
    return text


@router.message(Command("schedule"))
@router.callback_query(F.data == "menu_today")
async def cmd_schedule_today(event: Message | CallbackQuery):
    user = get_user(event.from_user.id)
    if not user or not user["class_name"]:
        msg = event.message if isinstance(event, CallbackQuery) else event
        await msg.answer("Сначала выбери класс командой /start или /class.")
        return

    now = datetime.now(KYIV_TZ)
    lessons = get_schedule_for_day(user["class_name"], now.weekday(), now.strftime("%Y-%m-%d"))
    text = format_lessons_list(lessons, "сегодня")

    msg = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        await event.answer()
    await msg.answer(text, parse_mode="Markdown")


@router.message(Command("tomorrow"))
@router.callback_query(F.data == "menu_tomorrow")
async def cmd_schedule_tomorrow(event: Message | CallbackQuery):
    user = get_user(event.from_user.id)
    if not user or not user["class_name"]:
        msg = event.message if isinstance(event, CallbackQuery) else event
        await msg.answer("Сначала выбери класс командой /start или /class.")
        return

    tomorrow = datetime.now(KYIV_TZ) + timedelta(days=1)
    lessons = get_schedule_for_day(user["class_name"], tomorrow.weekday(), tomorrow.strftime("%Y-%m-%d"))
    text = format_lessons_list(lessons, "завтра")

    msg = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        await event.answer()
    await msg.answer(text, parse_mode="Markdown")


@router.message(Command("week"))
@router.callback_query(F.data == "menu_week")
async def cmd_schedule_week(event: Message | CallbackQuery):
    user = get_user(event.from_user.id)
    if not user or not user["class_name"]:
        msg = event.message if isinstance(event, CallbackQuery) else event
        await msg.answer("Сначала выбери класс.")
        return

    full_schedule = get_full_week_schedule(user["class_name"])
    if not full_schedule:
        text = "📅 Расписание на неделю еще не заполнено."
    else:
        days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
        text = f"📅 **Расписание класса {user['class_name']} на неделю:**\n\n"
        
        week_dict = {i: [] for i in range(7)}
        for item in full_schedule:
            week_dict[item["day_of_week"]].append(item)

        for day_num, lessons in week_dict.items():
            if not lessons:
                continue
            text += f"📌 *{days[day_num]}*\n"
            for l in lessons:
                text += f"  {l['lesson_number']}. {l['subject']} ({l['start_time']}) каб. {l['room']}\n"
            text += "\n"

    msg = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        await event.answer()
    await msg.answer(text, parse_mode="Markdown")


@router.message(Command("weather"))
@router.callback_query(F.data == "menu_weather")
async def cmd_weather(event: Message | CallbackQuery):
    user = get_user(event.from_user.id)
    city = user["city"] if user and user["city"] else "Чернигов"
    weather_text = await fetch_weather(city)

    msg = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        await event.answer()
    await msg.answer(weather_text, parse_mode="Markdown")


@router.message(Command("settings"))
@router.callback_query(F.data == "menu_settings")
async def cmd_settings(event: Message | CallbackQuery):
    settings = get_user_settings(event.from_user.id)
    keyboard = get_settings_keyboard(settings["reminder_minutes"], settings["weather_enabled"])

    msg = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        await event.answer()
    await msg.answer("⚙️ **Настройки уведомлений:**", reply_markup=keyboard, parse_mode="Markdown")


@router.callback_query(F.data == "back_menu")
async def back_to_menu(callback: CallbackQuery):
    is_admin = (callback.from_user.id == ADMIN_ID)
    await callback.message.edit_text("📌 Главное меню:", reply_markup=get_main_menu_keyboard(is_admin))
    await callback.answer()


@router.callback_query(F.data == "set_remind_menu")
async def set_remind_menu(callback: CallbackQuery):
    await callback.message.edit_text("⏰ Выбери за сколько минут присылать напоминание:", reply_markup=get_reminder_time_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("remind_"))
async def set_remind_time(callback: CallbackQuery):
    mins = int(callback.data.split("_")[1])
    update_user_settings(callback.from_user.id, reminder_minutes=mins)
    settings = get_user_settings(callback.from_user.id)
    await callback.message.edit_text(
        f"✅ Время напоминания изменено на {mins} минут.",
        reply_markup=get_settings_keyboard(settings["reminder_minutes"], settings["weather_enabled"])
    )
    await callback.answer()


@router.callback_query(F.data == "toggle_weather")
async def toggle_weather(callback: CallbackQuery):
    settings = get_user_settings(callback.from_user.id)
    new_val = 0 if settings["weather_enabled"] else 1
    update_user_settings(callback.from_user.id, weather_enabled=new_val)
    settings = get_user_settings(callback.from_user.id)
    await callback.message.edit_text(
        "⚙️ **Настройки уведомлений:**",
        reply_markup=get_settings_keyboard(settings["reminder_minutes"], settings["weather_enabled"]),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data == "admin_panel")
async def admin_panel_handler(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("У вас нет доступа к админ-панели.", show_alert=True)
        return

    await callback.message.edit_text("🛠 **Административная панель:**", reply_markup=get_admin_keyboard(), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    conn = get_connection()
    stats = conn.execute("SELECT class_name, COUNT(*) as count FROM users WHERE class_name IS NOT NULL GROUP BY class_name").fetchall()
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()

    text = f"📊 **Статистика пользователей:**\n\nВсего пользователей: {total_users}\n\n"
    for row in stats:
        text += f"🔹 Класс {row['class_name']}: {row['count']} уч.\n"

    await callback.message.edit_text(text, reply_markup=get_admin_keyboard(), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "admin_csv")
async def admin_csv_prompt(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return

    await state.set_state(AdminStates.waiting_for_csv)
    await callback.message.edit_text(
        "📥 Пожалуйста, отправьте CSV-файл с расписанием.\n"
        "Формат колонок: `class_name,day_of_week,lesson_number,subject,teacher,room,start_time,end_time`",
        parse_mode="Markdown"
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_csv, F.document)
async def process_csv_upload(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        return

    document = message.document
    if not document.file_name.endswith(".csv"):
        await message.answer("❌ Пожалуйста, отправьте файл с расширением .csv")
        return

    file_info = await bot.get_file(document.file_id)
    file_path = f"temp_{document.file_name}"
    await bot.download_file(file_info.file_path, file_path)

    try:
        count = import_schedule_from_csv(file_path)
        os.remove(file_path)
        await state.clear()
        await message.answer(
            f"✅ Расписание успешно импортировано! Записано уроков: {count}",
            reply_markup=get_admin_keyboard()
        )
    except Exception as e:
        if os.path.exists(file_path):
            os.remove(file_path)
        await message.answer(f"❌ Ошибка при обработке CSV: {e}")


# ==========================================
# 6. ПЛАНИРОВЩИК НАПОМИНАНИЙ
# ==========================================
async def check_lessons_reminders(bot: Bot):
    now = datetime.now(KYIV_TZ)
    current_date = now.strftime("%Y-%m-%d")
    current_time_str = now.strftime("%H:%M")
    day_of_week = now.weekday()

    conn = get_connection()
    users = conn.execute("SELECT user_id, class_name, city FROM users WHERE class_name IS NOT NULL").fetchall()

    for user in users:
        user_id = user["user_id"]
        class_name = user["class_name"]
        city = user["city"]

        settings = get_user_settings(user_id)
        if not settings:
            continue

        reminder_minutes = settings["reminder_minutes"]
        weather_enabled = settings["weather_enabled"]

        lessons = get_schedule_for_day(class_name, day_of_week, current_date)

        for lesson in lessons:
            start_time = lesson["start_time"]
            if not start_time or start_time == "00:00":
                continue

            try:
                lesson_dt = datetime.strptime(f"{current_date} {start_time}", "%Y-%m-%d %H:%M")
                lesson_dt = KYIV_TZ.localize(lesson_dt)
                reminder_dt = lesson_dt - timedelta(minutes=reminder_minutes)
                reminder_time_str = reminder_dt.strftime("%H:%M")

                if current_time_str == reminder_time_str:
                    sent = conn.execute("""
                        SELECT * FROM sent_notifications 
                        WHERE user_id = ? AND lesson_date = ? AND lesson_number = ?
                    """, (user_id, current_date, lesson["lesson_number"])).fetchone()

                    if not sent:
                        msg = (
                            f"🔔 Через {reminder_minutes} минут начинается урок!\n"
                            f"🏫 Класс: {class_name}\n"
                            f"📚 Предмет: {lesson['subject']}\n"
                            f"👨‍🏫 Учитель: {lesson['teacher']}\n"
                            f"🚪 Кабинет: {lesson['room']}\n"
                            f"🕒 Начало: {lesson['start_time']}"
                        )

                        if weather_enabled:
                            weather_info = await fetch_weather(city)
                            msg += f"\n\n{weather_info}"

                        await bot.send_message(user_id, msg, parse_mode="Markdown")

                        conn.execute("""
                            INSERT INTO sent_notifications (user_id, lesson_date, lesson_number)
                            VALUES (?, ?, ?)
                        """, (user_id, current_date, lesson["lesson_number"]))
                        conn.commit()
            except Exception as e:
                logging.error(f"Ошибка при отправке уведомления для {user_id}: {e}")

    conn.close()


# ==========================================
# 7. ЗАПУСК БОТА
# ==========================================
async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)]
    )

    init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=KYIV_TZ)
    scheduler.add_job(check_lessons_reminders, "interval", minutes=1, args=[bot])
    scheduler.start()

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("🤖 Школьный помощник успешно запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
        
