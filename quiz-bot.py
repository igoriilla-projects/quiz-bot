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

# Функция загрузки файла локализации
def load_localization(filename="localization.json"):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Ошибка загрузки файла локализации: {e}")
        return {}

# Загружаем локализацию в глобальную переменную
loc = load_localization()

# Токен Telegram-бота (замените "TOKEN" на настоящий токен)
TELEGRAM_BOT_TOKEN = "TOKEN"
SETTINGS_FILE = "user_settings.json"

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Словари для настроек пользователей (идентификатор пользователя – int)
user_sheets = {}
user_intervals = {}      # интервал викторины (в минутах)
user_preferences = {}
user_quiet_intervals = {}  # кортеж (quiet_start, quiet_end)
user_timeouts = {}       # таймаут ответа (в минутах)
user_states = {}         # текущее состояние (какую команду ввёл пользователь)
user_quiz = {}           # текущие активные викторины
user_timeouts_active = {}  # флаг активного отсчёта таймаута
user_quiz_active = {}    # флаг включения автоматической отправки викторин
user_next_quiz_sent = {} # флаг, показывающий, что следующий квиз уже отправлен (после правильного ответа)

# Для предотвращения дублирования отправки викторин при коротких интервалах/таймаутах
last_quiz_sent = {}      # { user_id: время последней отправки викторины }
SEND_QUIZ_COOLDOWN = 5   # в секундах

# Настройка API Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)

# Настройка Telegram-бота
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
    logging.info("Настройки пользователя сохранены.")

def load_user_settings():
    """Загрузить настройки пользователей из JSON-файла."""
    global user_preferences, user_intervals, user_timeouts, user_quiet_intervals, user_sheets
    if not os.path.exists(SETTINGS_FILE) or os.stat(SETTINGS_FILE).st_size == 0:
        logging.warning("Файл настроек не найден или пуст. Создаю новый файл.")
        save_user_settings()
        return
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            settings = json.load(f)
        user_preferences = {int(k): v for k, v in settings.get("preferences", {}).items()}
        user_intervals = {int(k): v for k, v in settings.get("intervals", {}).items()}
        user_timeouts = {int(k): v for k, v in settings.get("timeouts", {}).items()}
        user_quiet_intervals = {
            int(k): (datetime.strptime(v[0], "%H:%M").time(), datetime.strptime(v[1], "%H:%M").time())
            for k, v in settings.get("quiet_intervals", {}).items()
        }
        sheets_from_file = settings.get("sheets", {})
        user_sheets_temp = {}
        for uid, sheet_url in sheets_from_file.items():
            try:
                user_sheets_temp[int(uid)] = client.open_by_url(sheet_url).sheet1
            except Exception as e:
                logging.error(f"Не удалось переподключиться к Google таблице для пользователя {uid}: {e}")
                user_sheets_temp[int(uid)] = None
        user_sheets = user_sheets_temp
        logging.info("Настройки пользователя успешно загружены.")
    except (json.JSONDecodeError, ValueError):
        logging.error("Файл настроек повреждён. Сбрасываю настройки.")
        save_user_settings()

def get_commands_keyboard():
    """Генерирует inline-клавиатуру с командами бота."""
    keyboard = InlineKeyboardMarkup()
    commands = [
        (loc["btn_setup"], "setup"),
        (loc["btn_quiz"], "quiz"),
        (loc["btn_setmode"], "setmode"),
        (loc["btn_setinterval"], "setinterval"),
        (loc["btn_setquietinterval"], "setquietinterval"),
        (loc["btn_settimeout"], "settimeout"),
        (loc["btn_settings"], "settings"),
        (loc["btn_stopquiz"], "stopquiz"),
        (loc["btn_stopquizauto"], "stopquizauto")
    ]
    for text, callback_data in commands:
        keyboard.add(InlineKeyboardButton(text, callback_data=callback_data))
    return keyboard

@bot.message_handler(commands=["start"])
def send_welcome(message):
    """Приветственное сообщение с опциями команд."""
    bot.send_message(
        message.chat.id,
        loc["welcome_message"],
        reply_markup=get_commands_keyboard()
    )

@bot.message_handler(commands=["help"])
def send_help(message):
    """Отправка справочного сообщения."""
    bot.send_message(message.chat.id, loc["help_message"], reply_markup=get_commands_keyboard())

@bot.callback_query_handler(func=lambda call: True)
def handle_command_click(call):
    """Обработка нажатий кнопок inline-клавиатуры."""
    user_id = call.message.chat.id
    if call.data == "setup":
        bot.send_message(user_id, loc["setup_prompt"])
        user_states[user_id] = "setup"
    elif call.data == "setmode":
        show_mode_selection(user_id)
        bot.answer_callback_query(call.id)
    elif call.data == "setinterval":
        bot.send_message(user_id, loc["setinterval_prompt"])
        user_states[user_id] = "setinterval"
    elif call.data == "setquietinterval":
        bot.send_message(user_id, loc["setquietinterval_prompt"])
        user_states[user_id] = "setquietinterval"
    elif call.data == "settimeout":
        bot.send_message(user_id, loc["settimeout_prompt"])
        user_states[user_id] = "settimeout"
    elif call.data == "quiz":
        user_quiz_active[user_id] = True
        start_quiz_schedule(user_id)
    elif call.data == "stopquiz":
        if user_id in user_intervals:
            del user_intervals[user_id]
            bot.send_message(user_id, loc["stopquiz_success"])
        else:
            bot.send_message(user_id, loc["stopquiz_not_found"])
    elif call.data == "stopquizauto":
        if user_quiz_active.get(user_id, True):
            user_quiz_active[user_id] = False
            bot.send_message(user_id, loc["stopquizauto_success"])
            logging.info(f"Автоматическая отправка викторин отключена для {user_id}.")
        else:
            bot.send_message(user_id, loc["stopquizauto_already"])
        bot.answer_callback_query(call.id)
    elif call.data == "settings":
        show_user_settings_inline(user_id)
    elif call.data == "next_question":
        if not user_next_quiz_sent.get(user_id, False):
            user_next_quiz_sent[user_id] = True
            send_quiz_auto(user_id)
        bot.answer_callback_query(call.id)
    elif call.data.startswith("mode_"):
        mode = call.data.replace("mode_", "")
        user_preferences[user_id] = mode
        bot.send_message(user_id, loc["mode_set"].format(mode=mode), parse_mode="Markdown")
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
    """Обработка команды настройки Google таблицы."""
    sheet_url = message.text.strip()
    try:
        sheet = client.open_by_url(sheet_url).sheet1
        user_sheets[user_id] = sheet
        save_user_settings()
        bot.send_message(user_id, loc["google_sheet_setup_success"])
    except gspread.exceptions.SpreadsheetNotFound:
        bot.send_message(user_id, loc["google_sheet_setup_error"])
    except Exception as e:
        bot.send_message(user_id, loc["google_sheet_setup_exception"].format(error=str(e)))

def handle_set_interval_command(user_id, message):
    """Обработка команды установки интервала викторины."""
    if message.text.isdigit():
        interval = int(message.text)
        if not (1 <= interval <= 60):
            bot.send_message(user_id, loc["setinterval_invalid"])
            return
        user_intervals[user_id] = interval
        save_user_settings()
        logging.info(f"Пользователь {user_id} установил интервал викторины: {interval} минут.")
        bot.send_message(user_id, loc["setinterval_success"].format(interval=interval), parse_mode="Markdown")
    else:
        bot.send_message(user_id, loc["setinterval_invalid_input"])

def handle_set_timeout_command(user_id, message):
    """Обработка команды установки таймаута ответа."""
    if message.text.isdigit():
        timeout = int(message.text)
        if not (0 <= timeout <= 1440):
            bot.send_message(user_id, loc["settimeout_invalid"])
            return
        user_timeouts[user_id] = timeout
        save_user_settings()
        logging.info(f"Пользователь {user_id} установил таймаут ответа: {timeout} минут.")
        bot.send_message(user_id, loc["settimeout_success"].format(timeout=timeout), parse_mode="Markdown")
    else:
        bot.send_message(user_id, loc["settimeout_invalid_input"])

def handle_set_quiet_interval_command(user_id, message):
    """Обработка команды установки тихого режима."""
    try:
        quiet_times = message.text.strip().split("-")
        if len(quiet_times) != 2:
            raise ValueError("Неверный формат")
        quiet_start = datetime.strptime(quiet_times[0], "%H:%M").time()
        quiet_end = datetime.strptime(quiet_times[1], "%H:%M").time()
        user_quiet_intervals[user_id] = (quiet_start, quiet_end)
        save_user_settings()
        logging.info(f"Пользователь {user_id} установил тихий режим: {quiet_start.strftime('%H:%M')} - {quiet_end.strftime('%H:%M')}.")
        bot.send_message(user_id, loc["setquietinterval_success"].format(
            start=quiet_start.strftime("%H:%M"),
            end=quiet_end.strftime("%H:%M")
        ))
    except ValueError:
        bot.send_message(user_id, loc["setquietinterval_invalid"])

def start_quiz_schedule(user_id):
    """Запуск планировщика викторин для пользователя."""
    if user_id in user_intervals:
        bot.send_message(user_id, loc["quiz_start"].format(interval=user_intervals[user_id]))
        if user_id not in user_quiz:
            executor.submit(quiz_scheduler, user_id, user_intervals[user_id])
    else:
        bot.send_message(user_id, loc["set_interval_first"])

def show_user_settings_inline(user_id):
    """Показывает текущие настройки пользователя, включая оставшееся время до следующего вопроса."""
    interval = user_intervals.get(user_id, None)
    mode = user_preferences.get(user_id, "По умолчанию (случайный)")
    timeout = user_timeouts.get(user_id, None)
    quiet = user_quiet_intervals.get(user_id, None)
    interval_text = f"{interval} минут" if interval is not None else "Не установлено"
    timeout_text = f"{timeout} минут" if timeout is not None else "По умолчанию (10 мин)"
    quiet_text = f"{quiet[0].strftime('%H:%M')} - {quiet[1].strftime('%H:%M')}" if quiet else "Не установлено"
    auto_quiz_status = "Включена" if user_quiz_active.get(user_id, True) else "Отключена"
    quiz_schedule_status = "Активно" if interval is not None else "Не активно"

    # Вычисляем оставшееся время до следующего вопроса (если возможно)
    if user_id in user_intervals and user_id in last_quiz_sent:
        next_quiz_time = last_quiz_sent[user_id] + user_intervals[user_id] * 60
        remaining_seconds = int(next_quiz_time - time.time())
        if remaining_seconds < 0:
            remaining_time_str = "0 сек"
        else:
            minutes, seconds = divmod(remaining_seconds, 60)
            remaining_time_str = f"{minutes} мин {seconds} сек"
    else:
        remaining_time_str = "Не активно"

    settings_text = loc["settings_message"].format(
        mode=mode,
        interval=interval_text,
        timeout=timeout_text,
        quiet=quiet_text,
        auto_quiz=auto_quiz_status,
        schedule=quiz_schedule_status
    )
    # Добавляем информацию о времени до следующего вопроса
    settings_text += f"\n⏱️ Оставшееся время до следующего вопроса: *{remaining_time_str}*"
    bot.send_message(user_id, settings_text, parse_mode="Markdown")

def quiz_scheduler(user_id, interval):
    """Планировщик викторин для пользователя с заданным интервалом."""
    logging.info(f"Планировщик викторин запущен для {user_id} с интервалом {interval} минут.")
    while user_id in user_intervals:
        now = datetime.now().time()
        quiet_interval = user_quiet_intervals.get(user_id)
        if quiet_interval:
            quiet_start, quiet_end = quiet_interval
            if quiet_start <= quiet_end:
                if quiet_start <= now <= quiet_end:
                    logging.info(f"{user_id}: Тихий режим активен. Жду 60 секунд.")
                    time.sleep(60)
                    continue
            else:
                if now >= quiet_start or now <= quiet_end:
                    logging.info(f"{user_id}: Тихий режим активен. Жду 60 секунд.")
                    time.sleep(60)
                    continue
        send_quiz_auto(user_id)
        time.sleep(interval * 60)
    logging.info(f"Планировщик викторин остановлен для {user_id}.")

def send_quiz_auto(user_id):
    """Отправка викторины пользователю, если она включена."""
    if not user_quiz_active.get(user_id, True):
        logging.info(f"Автоматическая отправка викторин отключена для {user_id}.")
        return
    if user_id in user_quiz:
        logging.info(loc["quiz_already_active"].format(user=user_id))
        return
    if user_id in last_quiz_sent and (time.time() - last_quiz_sent[user_id]) < SEND_QUIZ_COOLDOWN:
        logging.info(loc["quiz_recently_sent"].format(user=user_id))
        return
    if user_id not in user_sheets:
        bot.send_message(user_id, loc["sheet_not_set"])
        return
    sheet = user_sheets[user_id]
    data = sheet.get_all_records()
    if not data:
        bot.send_message(user_id, loc["sheet_empty"])
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
        bot.send_message(user_id, loc["reading_question"].format(kanji=kanji_entry["Kanji"]))
    else:
        bot.send_message(user_id, loc["meaning_question"].format(kanji=kanji_entry["Kanji"]))
    last_quiz_sent[user_id] = time.time()
    logging.info(loc["quiz_sent"].format(user=user_id, kanji=kanji_entry["Kanji"], type=question_type))
    timeout_value = user_timeouts.get(user_id, 1)
    if timeout_value > 0:
        timeout_seconds = timeout_value * 60
        if not user_timeouts_active.get(user_id, False):
            user_timeouts_active[user_id] = True
            executor.submit(handle_timeout_check, user_id, timeout_seconds)
    else:
        logging.info(f"Таймаут ответа равен 0 для {user_id}: проверка таймаута не запущена.")

def handle_timeout_check(user_id, timeout):
    """Проверяет, ответил ли пользователь до истечения времени."""
    for _ in range(timeout):
        time.sleep(1)
        if user_id not in user_quiz:
            logging.info(f"Пользователь {user_id} ответил до истечения таймаута. Таймаут отменён.")
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
            loc["timeout_message"].format(answer=correct_answer),
            parse_mode="Markdown"
        )
        del user_quiz[user_id]
        user_timeouts_active[user_id] = False
        time.sleep(2)
        send_quiz_auto(user_id)

def wait_and_send_next(user_id, delay):
    """
    Ожидает заданное время (delay) после правильного ответа и, если пользователь не нажал кнопку «Следующий»,
    отправляет следующий квиз.
    """
    time.sleep(delay)
    if not user_next_quiz_sent.get(user_id, False):
        user_next_quiz_sent[user_id] = True
        send_quiz_auto(user_id)

@bot.message_handler(func=lambda message: message.chat.id in user_quiz)
def check_answer(message):
    """
    Проверяет ответ пользователя на викторину.
    Если ответ неверный, пользователь может повторять попытки до истечения таймаута.
    При правильном ответе отправляется сообщение с кнопкой «Следующий» — для перехода к следующему вопросу.
    Если кнопку не нажали, следующий вопрос будет задан автоматически через рассчитанную задержку.
    """
    user_id = message.chat.id
    user_response = message.text.strip().lower()
    if user_id not in user_quiz:
        bot.send_message(user_id, loc["no_active_quiz"])
        return
    quiz_data = user_quiz[user_id]
    correct_answers = [ans.strip() for ans in quiz_data[quiz_data["type"]].lower().split(",")]
    if user_response in correct_answers:
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton(loc["btn_next"], callback_data="next_question"))
        bot.send_message(
            user_id,
            loc["correct_answer_message"].format(answers=', '.join(correct_answers), btn_next=loc["btn_next"]),
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        del user_quiz[user_id]
        user_timeouts_active[user_id] = False
        user_next_quiz_sent[user_id] = False
        timeout_value = user_timeouts.get(user_id, 1)
        if timeout_value == 0:
            delay = 2
        else:
            elapsed = time.time() - quiz_data["start_time"]
            timeout_seconds = timeout_value * 60
            delay = timeout_seconds - elapsed
            if delay < 0:
                delay = 0
        executor.submit(wait_and_send_next, user_id, delay)
    else:
        bot.send_message(user_id, loc["incorrect_answer_message"])

@bot.message_handler(commands=["stopquizauto"])
def stop_quiz_auto(message):
    """Останавливает автоматическую отправку викторин для пользователя."""
    user_id = message.chat.id
    if user_quiz_active.get(user_id, True):
        user_quiz_active[user_id] = False
        bot.send_message(user_id, loc["stopquizauto_success"])
        logging.info(f"Автоматическая отправка викторин отключена для {user_id}.")
    else:
        bot.send_message(user_id, loc["stopquizauto_already"])

def show_mode_selection(user_id):
    """Показывает кнопки для выбора режима викторины."""
    keyboard = InlineKeyboardMarkup(row_width=3)
    modes = [
        (loc["mode_reading"], "mode_reading"),
        (loc["mode_meaning"], "mode_meaning"),
        (loc["mode_random"], "mode_random")
    ]
    buttons = [InlineKeyboardButton(text, callback_data=callback_data) for text, callback_data in modes]
    keyboard.add(*buttons)
    bot.send_message(user_id, loc["mode_selection"], reply_markup=keyboard)

def signal_handler(sig, frame):
    """Грейсфул завершение работы при получении сигнала."""
    logging.info("Завершаю работу...")
    bot.stop_polling()
    sys.exit(0)

# Регистрируем обработчики сигналов
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Загружаем настройки и запускаем бота
load_user_settings()
bot.polling(none_stop=True)
