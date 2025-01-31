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

# Telegram Bot Token (from environment variable)
TELEGRAM_BOT_TOKEN = "TOKEN"
SETTINGS_FILE = "user_settings.json"

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Dictionaries for user-specific settings
user_sheets = {}
user_intervals = {}
user_preferences = {}
user_quiet_intervals = {}
user_timeouts = {}
user_states = {}  # Stores what command the user is entering
user_quiz = {}  # Tracks active quizzes
user_timeouts_active = {}
user_quiz_active = {}

# Setup Google Sheets API
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)

# Telegram Bot Setup
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Thread pool for managing threads
executor = ThreadPoolExecutor(max_workers=10)

def save_user_settings():
    """Save user settings to a JSON file."""
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
    """Load user settings from a JSON file."""
    global user_preferences, user_intervals, user_timeouts, user_quiet_intervals, user_sheets, client
    
    if not os.path.exists(SETTINGS_FILE) or os.stat(SETTINGS_FILE).st_size == 0:
        logging.warning("No settings file found or file is empty. Creating a new one.")
        save_user_settings()
        return

    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            settings = json.load(f)

        user_preferences = settings.get("preferences", {})
        user_intervals = settings.get("intervals", {})
        user_timeouts = settings.get("timeouts", {})
        user_quiet_intervals = {
            int(k): (datetime.strptime(v[0], "%H:%M").time(), datetime.strptime(v[1], "%H:%M").time())
            for k, v in settings.get("quiet_intervals", {}).items()
        }
        user_sheets = settings.get("sheets", {})

        for user_id, sheet_url in user_sheets.items():
            try:
                user_sheets[user_id] = client.open_by_url(sheet_url).sheet1
            except Exception as e:
                logging.error(f"Failed to reconnect Google Sheet for user {user_id}: {e}")
                user_sheets[user_id] = None

        logging.info("User settings loaded successfully.")

    except (json.JSONDecodeError, ValueError):
        logging.error("Settings file is corrupted. Resetting settings.")
        save_user_settings()

def get_commands_keyboard():
    """Generate an inline keyboard with bot commands."""
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
        ("Stop Auto Quiz Send", "stopquizauto")  # New button added
    ]
    for text, callback_data in commands:
        keyboard.add(InlineKeyboardButton(text, callback_data=callback_data))
    return keyboard

@bot.message_handler(commands=["start"])
def send_welcome(message):
    """Send a welcome message with command options."""
    bot.send_message(message.chat.id, "こんにちは！I will quiz you on Japanese kanji!\n\nClick a command below to set up:", reply_markup=get_commands_keyboard())

@bot.message_handler(commands=["help"])
def send_help(message):
    """Send help message with command options."""
    help_text = "📌 **Click a command below to use it:**"
    bot.send_message(message.chat.id, help_text, reply_markup=get_commands_keyboard())

@bot.callback_query_handler(func=lambda call: True)
def handle_command_click(call):
    """Handle inline keyboard button clicks."""
    user_id = call.message.chat.id

    if call.data == "setup":
        bot.send_message(user_id, "🔗 Please send your Google Sheet URL now.")
        user_states[user_id] = "setup"
    elif call.data == "setmode":
        show_mode_selection(user_id)
        bot.answer_callback_query(call.id)
    elif call.data == "setinterval":
        bot.send_message(user_id, "⏳ Enter the quiz interval in minutes (e.g., `15`).")
        user_states[user_id] = "setinterval"
    elif call.data == "setquietinterval":
        bot.send_message(user_id, "🌙 Enter the quiet interval in `HH:MM-HH:MM` format (e.g., `22:00-07:00`).")
        user_states[user_id] = "setquietinterval"
    elif call.data == "settimeout":
        bot.send_message(user_id, "⌛ Enter the timeout in minutes (e.g., `5`).")
        user_states[user_id] = "settimeout"
    elif call.data == "quiz":
        start_quiz_schedule(user_id)
    elif call.data == "stopquiz":
        if user_id in user_intervals:
            del user_intervals[user_id]
            bot.send_message(user_id, "✅ Automatic quizzes disabled.")
        else:
            bot.send_message(user_id, "⚠️ No active quiz schedule found.")

    elif call.data == "stopquizauto":
        user_key = str(user_id)

        if user_quiz_active.get(user_key, True):  # Check if quizzes are active
            user_quiz_active[user_key] = False  # Disable auto quiz sending
            bot.send_message(user_id, "⛔ Auto quiz sending has been stopped.")
            logging.info(f"Quiz auto-send disabled for {user_key}.")
        else:
            bot.send_message(user_id, "⚠️ Auto quiz sending is already stopped.")

        bot.answer_callback_query(call.id)  # Acknowledge button press

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
    """Handle user input after clicking a command."""
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
    """Handle the setup command."""
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
    """Handle the set interval command."""
    if message.text.isdigit():
        interval = int(message.text)
        user_intervals[user_id] = interval
        save_user_settings()
        bot.send_message(user_id, f"✅ Quiz interval set to *{interval} minutes*.")
    else:
        bot.send_message(user_id, "⚠️ Please enter a valid number.")

def handle_set_timeout_command(user_id, message):
    """Handle the set timeout command."""
    if message.text.isdigit():
        timeout = int(message.text)
        user_timeouts[user_id] = timeout
        save_user_settings()
        bot.send_message(user_id, f"✅ Quiz timeout set to *{timeout} minutes*. Use /startquiz to begin automatic quizzes.")
    else:
        bot.send_message(user_id, "⚠️ Please enter a valid number.")

def handle_set_quiet_interval_command(user_id, message):
    """Handle the set quiet interval command."""
    try:
        quiet_times = message.text.strip().split("-")
        if len(quiet_times) != 2:
            raise ValueError("Invalid format")

        quiet_start = datetime.strptime(quiet_times[0], "%H:%M").time()
        quiet_end = datetime.strptime(quiet_times[1], "%H:%M").time()
        
        user_quiet_intervals[user_id] = (quiet_start, quiet_end)
        save_user_settings()

        bot.send_message(user_id, f"🌙 Quiet hours set from {quiet_start.strftime('%H:%M')} to {quiet_end.strftime('%H:%M')}.")
    except ValueError:
        bot.send_message(user_id, "⚠️ Invalid format. Use HH:MM-HH:MM (e.g., `22:00-07:00`).")

def start_quiz_schedule(user_id):
    """Start the quiz scheduler for a user."""
    user_key = str(user_id)
    if user_key in user_intervals:
        bot.send_message(user_id, f"▶️ Automatic quizzes started. You will receive one every {user_intervals[user_key]} minutes.")
        if user_id not in user_quiz:
            executor.submit(quiz_scheduler, user_id, user_intervals[user_key])
    else:
        bot.send_message(user_id, "⚠️ Set an interval first using /setinterval.")

def show_user_settings_inline(user_id):
    """Show the current settings for a user."""
    user_key = str(user_id)
    interval = user_intervals.get(user_key, "Not set")
    mode = user_preferences.get(user_key, "Default (random)")
    timeout = user_timeouts.get(user_key, "Default (10 min)")
    quiet = user_quiet_intervals.get(user_id, "Not set")

    if isinstance(quiet, tuple):
        quiet = f"{quiet[0].strftime('%H:%M')} - {quiet[1].strftime('%H:%M')}"

    settings_text = f"""
⚙️ **Your Current Settings**:
📚 Quiz Mode: *{mode}*
⏳ Question Interval: *{interval} minutes*
⌛ Answer Timeout: *{timeout} minutes*
🌙 Quiet Hours: *{quiet}*
    """
    bot.send_message(user_id, settings_text, parse_mode="Markdown")

def quiz_scheduler(user_id, interval):
    """Schedule quizzes for a user at a given interval."""
    user_key = str(user_id)
    logging.info(f"Quiz scheduler started for {user_key} with interval {interval} minutes.")

    while user_key in user_intervals:
        now = datetime.now().time()
        quiet_interval = user_quiet_intervals.get(user_key, None)

        if quiet_interval:
            quiet_start, quiet_end = quiet_interval
            if quiet_start <= quiet_end:
                if quiet_start <= now <= quiet_end:
                    logging.info(f"{user_key}: Quiet hours active. Sleeping for 60 seconds.")
                    time.sleep(60)
                    continue
            else:
                if now >= quiet_start or now <= quiet_end:
                    logging.info(f"{user_key}: Quiet hours active. Sleeping for 60 seconds.")
                    time.sleep(60)
                    continue

        send_quiz_auto(user_id)

        user_timeouts_active[user_key] = False
        timeout = user_timeouts.get(user_key, 1) * 60
        executor.submit(handle_timeout_check, user_id, timeout)

        time.sleep(interval * 60)

    logging.info(f"Quiz scheduler stopped for {user_key}.")

# Add a new dictionary to track active quizzes


def send_quiz_auto(user_id):
    """Send a quiz question to the user if quizzes are enabled."""
    user_key = str(user_id)

    # Check if quiz sending is enabled
    if not user_quiz_active.get(user_key, True):
        logging.info(f"Quiz auto-send disabled for {user_key}.")
        return  # Stop sending quizzes

    logging.info(f"send_quiz_auto called for {user_key}.")

    if user_key not in user_sheets:
        bot.send_message(user_id, "⚠️ Set up your Google Sheet first using /setup.")
        return

    sheet = user_sheets[user_key]
    data = sheet.get_all_records()

    if not data:
        bot.send_message(user_id, "⚠️ Your Google Sheet is empty!")
        return

    kanji_entry = random.choice(data)
    question_type = user_preferences.get(user_key, "random")

    if question_type == "random":
        question_type = random.choice(["reading", "meaning"])

    user_quiz[user_key] = {
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

    logging.info(f"Quiz sent to {user_key}: {kanji_entry['Kanji']} ({question_type})")

    # Respect user-defined timeouts
    timeout = user_timeouts.get(user_key, 1) * 60
    if user_timeouts_active.get(user_key, False):
        logging.info(f"Timeout already active for {user_key}, skipping new timeout thread.")
        return

    user_timeouts_active[user_key] = True
    executor.submit(handle_timeout_check, user_id, timeout)


def handle_timeout_check(user_id, timeout):
    """Check if the user has answered within the timeout period."""
    user_key = str(user_id)
    for _ in range(timeout):
        time.sleep(1)
        if user_key not in user_quiz:
            logging.info(f"User {user_key} answered before timeout expired. Timeout canceled.")
            user_timeouts_active[user_key] = False
            return

    if user_key in user_quiz and user_timeouts_active.get(user_key, False):
        handle_timeout(user_id)

def handle_timeout(user_id):
    """Handle the timeout event."""
    user_key = str(user_id)
    if user_key in user_quiz:
        correct_answer = user_quiz[user_key][user_quiz[user_key]["type"]]
        bot.send_message(
            user_id, 
            f"⌛ Time's up! The correct answer was: *{correct_answer}*.\n\nStarting a new quiz...", 
            parse_mode="Markdown"
        )
        del user_quiz[user_key]
        user_timeouts_active[user_key] = False
        time.sleep(2)
        send_quiz_auto(user_id)

@bot.message_handler(func=lambda message: str(message.chat.id) in user_quiz)
def check_answer(message):
    """Check the user's answer to the quiz."""
    user_id = message.chat.id
    user_key = str(user_id)
    user_response = message.text.strip().lower()

    if user_key not in user_quiz:
        bot.send_message(user_id, "⚠️ No active quiz! Use /quiz to start a new one.")
        return

    quiz_data = user_quiz[user_key]
    correct_answers = quiz_data[quiz_data["type"]].lower().split(",")
    correct_answers = [ans.strip() for ans in correct_answers]

    if user_response in correct_answers:
        bot.send_message(user_id, f"✅ Correct! 🎉\n\nAll possible answers: *{', '.join(correct_answers)}*", parse_mode="Markdown")
        del user_quiz[user_key]
        if user_key in user_timeouts_active:
            user_timeouts_active[user_key] = False
        time.sleep(2)
        send_quiz_auto(user_id)
    else:
        bot.send_message(user_id, f"❌ Incorrect! Try again.")

@bot.message_handler(commands=["stopquizauto"])
def stop_quiz_auto(message):
    """Stops automatic quiz sending for a user."""
    user_id = message.chat.id
    user_key = str(user_id)

    if user_quiz_active.get(user_key, True):  # If quizzes are running
        user_quiz_active[user_key] = False  # Disable quiz sending
        bot.send_message(user_id, "⛔ Automatic quiz sending has been disabled.")
        logging.info(f"Quiz auto-send disabled for {user_key}.")
    else:
        bot.send_message(user_id, "⚠️ Automatic quiz sending is already stopped.")


def show_mode_selection(user_id):
    """Show the quiz mode selection buttons."""
    keyboard = InlineKeyboardMarkup(row_width=3)
    modes = [
        ("Reading", "mode_reading"),
        ("Meaning", "mode_meaning"),
        ("Random", "mode_random")
    ]
    buttons = [InlineKeyboardButton(text, callback_data=callback_data) for text, callback_data in modes]
    keyboard.add(*buttons)
    bot.send_message(user_id, "🎯 Choose a quiz mode:", reply_markup=keyboard)

@bot.callback_query_handler(func=lambda call: call.data.startswith("mode_"))
def handle_mode_selection(call):
    """Handle the quiz mode selection."""
    user_id = call.message.chat.id
    mode = call.data.replace("mode_", "")
    user_preferences[user_id] = mode
    bot.send_message(user_id, f"✅ Quiz mode set to *{mode}*.", parse_mode="Markdown")
    bot.answer_callback_query(call.id)

@bot.message_handler(commands=["quiz"])
def send_quiz(message):
    """Send a quiz question manually."""
    send_quiz_auto(message.chat.id)

def signal_handler(sig, frame):
    """Handle shutdown signals gracefully."""
    logging.info("Shutting down gracefully...")
    bot.stop_polling()
    sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Load user settings and start the bot
load_user_settings()
bot.polling(none_stop=True)