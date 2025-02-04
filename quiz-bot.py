# -*- coding: utf-8 -*-
import os
import random
import logging
import time
import json
import signal
import sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import gspread
import telebot
from oauth2client.service_account import ServiceAccountCredentials
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Telegram Bot Token (замените "TOKEN" на настоящий токен)
TELEGRAM_BOT_TOKEN = "TOKEN"
SETTINGS_FILE = "user_settings.json"

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Словари для настроек пользователей (идентификатор пользователя – int)
user_sheets = {}
user_intervals = {}
user_preferences = {}
user_quiet_intervals = {}
user_timeouts = {}
user_states = {}         # Текущее состояние (какую команду вводит пользователь)
user_quiz = {}           # Текущие активные викторины
user_timeouts_active = {}  # Флаг активного отсчёта таймаута
user_quiz_active = {}      # Флаг включения автоматической отправки викторин

# Для предотвращения дублирования отправки квизов при коротких интервалах/таймаутах
last_quiz_sent = {}      # { user_id: timestamp_last_quiz }
SEND_QUIZ_COOLDOWN = 5   # в секундах, период, в течение которого повторная отправка не производится

# Setup Google Sheets API
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)

# Telegram Bot Setup
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Thread pool для управления потоками
executor = ThreadPoolExecutor(max_workers=10)


def save_user_settings():
    """Сохранить настройки пользователей в JSON-файл."""
    settings = {
        "preferences": {str(k): v for k, v in user_preferences.items()},
        "intervals": {str(k): v for k, v in user_intervals.items()},
        "timeouts": {str(k): v for k, v in user_timeouts.items()},
        "quiet_intervals": {
            str(k): (v[0].strftime("%H:%M"), v[1].strftime("%H:%M")) for k, v in user_quiet_intervals.items()
        },
        "sheets": {str(k): v.spreadsheet.url for k, v in user_sheets.items() if v}
    }

    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=4)

    logging.info("User settings saved.")


def load_user_settings():
    """Загрузить настройки пользователей из JSON-файла."""
    global user_preferences, user_intervals, user_timeouts, user_quiet_intervals, user_sheets

    if not os.path.exists(SETTINGS_FILE) or os.stat(SETTINGS_FILE).st_size == 0:
        logging.warning("No settings file found or file is empty. Creating a new one.")
        save_user_settings()
        return

    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            settings = json.load(f)

        # Приводим ключи к int
        user_preferences = {int(k): v for k, v in settings.get("preferences", {}).items()}
        user_intervals = {int(k): v for k, v in settings.get("intervals", {}).items()}
        user_timeouts = {int(k): v for k, v in settings.get("timeouts", {}).items()}
        user_quiet_intervals = {
            int(k): (datetime.strptime(v[0], "%H:%M").time(), datetime.strptime(v[1], "%H:%M").time())
            for k, v in settings.get("quiet_intervals", {}).items()
        }

        # Для sheets также приводим ключи к int
        sheets_from_file = settings.get("sheets", {})
        user_sheets_temp = {}
        for uid, sheet_url in sheets_from_file.items():
            try:
                user_sheets_temp[int(uid)] = client.open_by_url(sheet_url).sheet1
            except Exception as e:
                logging.error(f"Failed to reconnect Google Sheet for user {uid}: {e}")
                user_sheets_temp[int(uid)] = None
        user_sheets = user_sheets_temp

        logging.info("User settings loaded successfully.")

    except (json.JSONDecodeError, ValueError):
        logging.error("Settings file is corrupted. Resetting settings.")
        save_user_settings()


def get_commands_keyboard():
    """Генерирует inline-клавиатуру с командами бота."""
    keyboard = InlineKeyboardMarkup()
    commands = [
        ("Setup Google Sheet", "setup"),
        ("Start a Quiz", "quiz"),
        ("Set Quiz Mode", "setmode"),
        ("Set Question Interval", "setinterval"),
        ("Set Quiet Interval", "setquietinterval"),
        ("Set Answer Timeout", "settimeout"),
        ("Show Current Settings", "settings"),
        ("Stop Automatic Quiz", "stopquiz"),
        ("Stop Auto Quiz Send", "stopquizauto")
    ]
    for text, callback_data in commands:
        keyboard.add(InlineKeyboardButton(text, callback_data=callback_data))
    return keyboard


@bot.message_handler(commands=["start"])
def send_welcome(message):
    """Приветственное сообщение с опциями команд."""
    bot.send_message(
        message.chat.id,
        "こんにちは！I will quiz you on Japanese kanji!\n\nClick a command below to set up:",
        reply_markup=get_commands_keyboard()
    )


@bot.message_handler(commands=["help"])
def send_help(message):
    """Помощь с опциями команд."""
    help_text = "📌 **Click a command below to use it:**"
    bot.send_message(message.chat.id, help_text, reply_markup=get_commands_keyboard())


@bot.callback_query_handler(func=lambda call: True)
def handle_command_click(call):
    """Обработка нажатий кнопок inline-клавиатуры."""
    user_id = call.message.chat.id

    if call.data == "setup":
        bot.send_message(user_id, "🔗 Please send your Google Sheet URL now.")
        user_states[user_id] = "setup"
    elif call.data == "setmode":
        show_mode_selection(user_id)
        bot.answer_callback_query(call.id)
    elif call.data == "setinterval":
        bot.send_message(user_id, "⏳ Enter the quiz interval in minutes (1-60).")
        user_states[user_id] = "setinterval"
    elif call.data == "setquietinterval":
        bot.send_message(user_id, "🌙 Enter the quiet interval in `HH:MM-HH:MM` format (e.g., `22:00-07:00`).")
        user_states[user_id] = "setquietinterval"
    elif call.data == "settimeout":
        bot.send_message(user_id, "⌛ Enter the answer timeout in minutes (0 to 1440, 0 = no timeout).")
        user_states[user_id] = "settimeout"
    elif call.data == "quiz":
        # Включаем автоматическую отправку квизов и запускаем планировщик
        user_quiz_active[user_id] = True
        start_quiz_schedule(user_id)
    elif call.data == "stopquiz":
        if user_id in user_intervals:
            del user_intervals[user_id]
            bot.send_message(user_id, "✅ Automatic quizzes disabled.")
        else:
            bot.send_message(user_id, "⚠️ No active quiz schedule found.")
    elif call.data == "stopquizauto":
        if user_quiz_active.get(user_id, True):
            user_quiz_active[user_id] = False
            bot.send_message(user_id, "⛔ Auto quiz sending has been stopped.")
            logging.info(f"Quiz auto-send disabled for {user_id}.")
        else:
            bot.send_message(user_id, "⚠️ Auto quiz sending is already stopped.")
        bot.answer_callback_query(call.id)
    elif call.data == "settings":
        show_user_settings_inline(user_id)
    elif call.data.startswith("mode_"):
        mode = call.data.replace("mode_", "")
        user_preferences[user_id] = mode
        bot.send_message(user_id, f"✅ Quiz mode set to *{mode}*.", parse_mode="Markdown")
        save_user_settings()
        bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda message: message.chat.id in user_states)
def handle_user_input(message):
    """Обработка пользовательского ввода после выбора команды."""
    user_id = message.chat.id
    command = user_states[user_id]
    del user_states[user_id]

    if command == "setup":
        handle_setup_command(user_id, message)
    elif command == "setinterval":
        handle_set_interval_command(user_id, message)
    elif command == "settimeout":
        handle_set_timeout_command(user_id, message)
    elif command == "setquietinterval":
        handle_set_quiet_interval_command(user_id, message)


def handle_setup_command(user_id, message):
    """Обработка команды setup."""
    sheet_url = message.text.strip()
    try:
        sheet = client.open_by_url(sheet_url).sheet1
        user_sheets[user_id] = sheet
        save_user_settings()
        bot.send_message(user_id, "✅ Your Google Sheet has been set up and saved! Use /quiz to start.")
    except gspread.exceptions.SpreadsheetNotFound:
        bot.send_message(user_id, "❌ The spreadsheet URL is invalid or the sheet is not accessible.")
    except Exception as e:
        bot.send_message(user_id, f"❌ An error occurred: {str(e)}")


def handle_set_interval_command(user_id, message):
    """Обработка команды setinterval."""
    if message.text.isdigit():
        interval = int(message.text)
        if not (1 <= interval <= 60):
            bot.send_message(user_id, "⚠️ Question Interval must be between 1 and 60 minutes.")
            return
        user_intervals[user_id] = interval
        save_user_settings()
        bot.send_message(user_id, f"✅ Quiz interval set to *{interval} minutes*.", parse_mode="Markdown")
    else:
        bot.send_message(user_id, "⚠️ Please enter a valid number.")


def handle_set_timeout_command(user_id, message):
    """Обработка команды settimeout."""
    if message.text.isdigit():
        timeout = int(message.text)
        if not (0 <= timeout <= 1440):
            bot.send_message(user_id, "⚠️ Answer Timeout must be between 0 and 1440 minutes (0 = no timeout).")
            return
        user_timeouts[user_id] = timeout
        save_user_settings()
        bot.send_message(user_id, f"✅ Quiz timeout set to *{timeout} minutes*.", parse_mode="Markdown")
    else:
        bot.send_message(user_id, "⚠️ Please enter a valid number.")


def handle_set_quiet_interval_command(user_id, message):
    """Обработка команды setquietinterval."""
    try:
        quiet_times = message.text.strip().split("-")
        if len(quiet_times) != 2:
            raise ValueError("Invalid format")

        quiet_start = datetime.strptime(quiet_times[0], "%H:%M").time()
        quiet_end = datetime.strptime(quiet_times[1], "%H:%M").time()

        user_quiet_intervals[user_id] = (quiet_start, quiet_end)
        save_user_settings()

        bot.send_message(
            user_id,
            f"🌙 Quiet hours set from {quiet_start.strftime('%H:%M')} to {quiet_end.strftime('%H:%M')}."
        )
    except ValueError:
        bot.send_message(user_id, "⚠️ Invalid format. Use HH:MM-HH:MM (e.g., `22:00-07:00`).")


def start_quiz_schedule(user_id):
    """Запуск планировщика викторин для пользователя."""
    if user_id in user_intervals:
        bot.send_message(
            user_id,
            f"▶️ Automatic quizzes started. You will receive one every {user_intervals[user_id]} minutes."
        )
        # Запускаем планировщик, если квиз ещё не активен
        if user_id not in user_quiz:
            executor.submit(quiz_scheduler, user_id, user_intervals[user_id])
    else:
        bot.send_message(user_id, "⚠️ Set an interval first using /setinterval.")


def show_user_settings_inline(user_id):
    """Показывает текущие настройки пользователя, включая статус автоматического квиза и работы планировщика."""
    interval = user_intervals.get(user_id, None)
    mode = user_preferences.get(user_id, "Default (random)")
    timeout = user_timeouts.get(user_id, None)
    quiet = user_quiet_intervals.get(user_id, None)
    
    interval_text = f"{interval} minutes" if interval is not None else "Not set"
    timeout_text = f"{timeout} minutes" if timeout is not None else "Default (10 min)"
    quiet_text = f"{quiet[0].strftime('%H:%M')} - {quiet[1].strftime('%H:%M')}" if quiet else "Not set"
    
    auto_quiz_status = "Enabled" if user_quiz_active.get(user_id, True) else "Stopped"
    quiz_schedule_status = "Active" if interval is not None else "Inactive"
    
    settings_text = f"""
⚙️ **Your Current Settings**:
📚 Quiz Mode: *{mode}*
⏳ Question Interval: *{interval_text}*
⌛ Answer Timeout: *{timeout_text}*
🌙 Quiet Hours: *{quiet_text}*
🔄 Automatic Quiz Sending: *{auto_quiz_status}*
⏹️ Quiz Schedule: *{quiz_schedule_status}*
    """
    bot.send_message(user_id, settings_text, parse_mode="Markdown")


def quiz_scheduler(user_id, interval):
    """Планировщик викторин для пользователя с заданным интервалом."""
    logging.info(f"Quiz scheduler started for {user_id} with interval {interval} minutes.")

    while user_id in user_intervals:
        now = datetime.now().time()
        quiet_interval = user_quiet_intervals.get(user_id)

        if quiet_interval:
            quiet_start, quiet_end = quiet_interval
            if quiet_start <= quiet_end:
                if quiet_start <= now <= quiet_end:
                    logging.info(f"{user_id}: Quiet hours active. Sleeping for 60 seconds.")
                    time.sleep(60)
                    continue
            else:
                if now >= quiet_start or now <= quiet_end:
                    logging.info(f"{user_id}: Quiet hours active. Sleeping for 60 seconds.")
                    time.sleep(60)
                    continue

        # Отправляем квиз, если нет активного вопроса и кулдаун прошёл
        send_quiz_auto(user_id)
        time.sleep(interval * 60)

    logging.info(f"Quiz scheduler stopped for {user_id}.")


def send_quiz_auto(user_id):
    """Отправка викторины пользователю, если она включена."""
    if not user_quiz_active.get(user_id, True):
        logging.info(f"Quiz auto-send disabled for {user_id}.")
        return

    # Если уже есть активный квиз, не отправляем новый
    if user_id in user_quiz:
        logging.info(f"A quiz is already active for {user_id}. Skipping new quiz.")
        return

    # Если квиз отправлялся недавно, пропускаем отправку (чтобы не было наложения)
    if user_id in last_quiz_sent and (time.time() - last_quiz_sent[user_id]) < SEND_QUIZ_COOLDOWN:
        logging.info(f"Quiz was sent recently for {user_id}. Skipping new quiz.")
        return

    if user_id not in user_sheets:
        bot.send_message(user_id, "⚠️ Set up your Google Sheet first using /setup.")
        return

    sheet = user_sheets[user_id]
    data = sheet.get_all_records()

    if not data:
        bot.send_message(user_id, "⚠️ Your Google Sheet is empty!")
        return

    kanji_entry = random.choice(data)
    question_type = user_preferences.get(user_id, "random")
    if question_type == "random":
        question_type = random.choice(["reading", "meaning"])

    user_quiz[user_id] = {
        "kanji": kanji_entry["Kanji"],
        "reading": kanji_entry["Reading"],
        "meaning": kanji_entry["Meaning"],
        "type": question_type,
        "start_time": time.time()
    }

    if question_type == "reading":
        bot.send_message(user_id, f"🔹 What is the reading of this kanji: {kanji_entry['Kanji']}?")
    else:
        bot.send_message(user_id, f"🔹 What is the meaning of this kanji: {kanji_entry['Kanji']}?")

    last_quiz_sent[user_id] = time.time()
    logging.info(f"Quiz sent to {user_id}: {kanji_entry['Kanji']} ({question_type})")

    # Если Answer Timeout > 0, запускаем проверку таймаута.
    timeout_value = user_timeouts.get(user_id, 1)  # в минутах
    if timeout_value > 0:
        timeout_seconds = timeout_value * 60
        if not user_timeouts_active.get(user_id, False):
            user_timeouts_active[user_id] = True
            executor.submit(handle_timeout_check, user_id, timeout_seconds)
    else:
        logging.info(f"Answer Timeout is 0 for {user_id}: no timeout check scheduled.")


def handle_timeout_check(user_id, timeout):
    """Проверяет, ответил ли пользователь до истечения времени."""
    for _ in range(timeout):
        time.sleep(1)
        if user_id not in user_quiz:
            logging.info(f"User {user_id} answered before timeout expired. Timeout canceled.")
            user_timeouts_active[user_id] = False
            return

    if user_id in user_quiz and user_timeouts_active.get(user_id, False):
        handle_timeout(user_id)


def handle_timeout(user_id):
    """Обработка ситуации, когда время ответа истекло."""
    if user_id in user_quiz:
        correct_answer = user_quiz[user_id][user_quiz[user_id]["type"]]
        bot.send_message(
            user_id, 
            f"⌛ Time's up! The correct answer was: *{correct_answer}*.\n\nStarting a new quiz...",
            parse_mode="Markdown"
        )
        del user_quiz[user_id]
        user_timeouts_active[user_id] = False
        time.sleep(2)
        send_quiz_auto(user_id)


@bot.message_handler(func=lambda message: message.chat.id in user_quiz)
def check_answer(message):
    """Проверяет ответ пользователя на викторину.
    
    При включённом авто квизе следующий вопрос задаётся не сразу,
    а только после истечения полного времени таймаута.
    Если Answer Timeout = 0, следующий вопрос задаётся сразу после ответа.
    """
    user_id = message.chat.id
    user_response = message.text.strip().lower()

    if user_id not in user_quiz:
        bot.send_message(user_id, "⚠️ No active quiz! Use /quiz to start a new one.")
        return

    quiz_data = user_quiz[user_id]
    correct_answers = [ans.strip() for ans in quiz_data[quiz_data["type"]].lower().split(",")]

    if user_response in correct_answers:
        bot.send_message(
            user_id,
            f"✅ Correct! 🎉\n\nAll possible answers: *{', '.join(correct_answers)}*",
            parse_mode="Markdown"
        )
    else:
        bot.send_message(user_id, "❌ Incorrect! Try again.")

    # Удаляем активный квиз и отменяем таймаут
    del user_quiz[user_id]
    user_timeouts_active[user_id] = False

    # Получаем значение Answer Timeout (в минутах)
    timeout_value = user_timeouts.get(user_id, 1)
    if timeout_value == 0:
        # Если таймаут отключён – задаём следующий вопрос сразу после ответа.
        time.sleep(2)
        send_quiz_auto(user_id)
    else:
        # Если таймаут включён – вычисляем, сколько осталось до его истечения.
        elapsed = time.time() - quiz_data["start_time"]
        timeout_seconds = timeout_value * 60
        remaining = timeout_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        send_quiz_auto(user_id)


@bot.message_handler(commands=["stopquizauto"])
def stop_quiz_auto(message):
    """Останавливает автоматическую отправку викторин для пользователя."""
    user_id = message.chat.id

    if user_quiz_active.get(user_id, True):
        user_quiz_active[user_id] = False
        bot.send_message(user_id, "⛔ Automatic quiz sending has been disabled.")
        logging.info(f"Quiz auto-send disabled for {user_id}.")
    else:
        bot.send_message(user_id, "⚠️ Automatic quiz sending is already stopped.")


def show_mode_selection(user_id):
    """Показывает кнопки для выбора режима викторины."""
    keyboard = InlineKeyboardMarkup(row_width=3)
    modes = [
        ("Reading", "mode_reading"),
        ("Meaning", "mode_meaning"),
        ("Random", "mode_random")
    ]
    buttons = [InlineKeyboardButton(text, callback_data=callback_data) for text, callback_data in modes]
    keyboard.add(*buttons)
    bot.send_message(user_id, "🎯 Choose a quiz mode:", reply_markup=keyboard)


def signal_handler(sig, frame):
    """Грейсфул завершение работы при получении сигнала."""
    logging.info("Shutting down gracefully...")
    bot.stop_polling()
    sys.exit(0)


# Регистрируем обработчики сигналов
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Загружаем настройки и запускаем бота
load_user_settings()
bot.polling(none_stop=True)
