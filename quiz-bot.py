import os
import random
import gspread
import telebot
import threading
import time
from datetime import datetime
from oauth2client.service_account import ServiceAccountCredentials
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Telegram Bot Token (Replace with your actual token)
TELEGRAM_BOT_TOKEN = "TOKEN"

# Dictionaries for user-specific settings
user_sheets = {}
user_intervals = {}
user_preferences = {}
user_quiet_intervals = {}
user_timeouts = {}
user_states = {}  # Stores what command the user is entering
user_quiz = {}  # Tracks active quizzes

# Setup Google Sheets API
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)

# Telegram Bot Setup
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Interactive Keyboard for Commands
def get_commands_keyboard():
    keyboard = InlineKeyboardMarkup()
    commands = [
        ("Setup Google Sheet", "setup"),
        ("Start a Quiz", "quiz"),
        ("Set Quiz Mode", "setmode"),
        ("Set Quiz Interval", "setinterval"),
        ("Set Quiet Interval", "setquietinterval"),
        ("Set Quiz Timeout", "settimeout"),
        ("Show current Settings", "settings"),
        ("Stop Automatic Quiz", "stopquiz")
    ]
    for text, callback_data in commands:
        keyboard.add(InlineKeyboardButton(text, callback_data=callback_data))
    return keyboard

@bot.message_handler(commands=["start"])
def send_welcome(message):
    bot.send_message(message.chat.id, "こんにちは！I will quiz you on Japanese kanji!\n\nClick a command below to set up:", reply_markup=get_commands_keyboard())

@bot.message_handler(commands=["help"])
def send_help(message):
    help_text = "📌 **Click a command below to use it:**"
    bot.send_message(message.chat.id, help_text, reply_markup=get_commands_keyboard())

# Handle button clicks
@bot.callback_query_handler(func=lambda call: True)
def handle_command_click(call):
    user_id = call.message.chat.id
    
    if call.data == "setup":
        bot.send_message(user_id, "🔗 Please send your Google Sheet URL now.")
        user_states[user_id] = "setup"
    elif call.data == "setmode":
        show_mode_selection(user_id)
        bot.answer_callback_query(call.id)  # Avoids UI freeze
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
        send_quiz_auto(user_id)
    elif call.data == "stopquiz":
        if user_id in user_intervals:
            del user_intervals[user_id]
            bot.send_message(user_id, "✅ Automatic quizzes disabled.")
        else:
            bot.send_message(user_id, "⚠️ No active quiz schedule found.")
    elif call.data == "settings":
        show_user_settings_inline(user_id)
    elif call.data.startswith("mode_"):  # Handle mode selection directly
        mode = call.data.replace("mode_", "")
        user_preferences[user_id] = mode
        bot.send_message(user_id, f"✅ Quiz mode set to *{mode}*.", parse_mode="Markdown")
        bot.answer_callback_query(call.id)  # Prevents "loading" issue    


# Handle user input after clicking a command
@bot.message_handler(func=lambda message: message.chat.id in user_states)
def handle_user_input(message):
    user_id = message.chat.id
    command = user_states[user_id]
    del user_states[user_id]  # Remove state after receiving input

    if command == "setup":
        sheet_url = message.text.strip()
        try:
            sheet = client.open_by_url(sheet_url).sheet1
            user_sheets[user_id] = sheet
            bot.send_message(user_id, "✅ Your Google Sheet has been set up! Use /quiz to start.")
        except Exception as e:
            bot.send_message(user_id, f"❌ Failed to access sheet: {str(e)}")

    elif command == "setinterval":
        if message.text.isdigit():
            interval = int(message.text)
            user_intervals[user_id] = interval
            bot.send_message(user_id, f"✅ Quiz interval set to *{interval} minutes*. Use /startquiz to begin automatic quizzes.")
        else:
            bot.send_message(user_id, "⚠️ Please enter a valid number.")

    elif command == "settimeout":
        if message.text.isdigit():
            timeout = int(message.text)
            user_timeouts[user_id] = timeout
            bot.send_message(user_id, f"✅ Quiz timeout set to *{timeout} minutes*. Use /startquiz to begin automatic quizzes.")
        else:
            bot.send_message(user_id, "⚠️ Please enter a valid number.")            

    elif command == "setquietinterval":
        try:
            quiet_times = message.text.strip().split("-")
            if len(quiet_times) != 2:
                raise ValueError("Invalid format")

            # Convert input times to datetime.time
            quiet_start = datetime.strptime(quiet_times[0], "%H:%M").time()
            quiet_end = datetime.strptime(quiet_times[1], "%H:%M").time()
            
            # Store in dictionary
            user_quiet_intervals[user_id] = (quiet_start, quiet_end)

            bot.send_message(user_id, f"🌙 Quiet hours set from {quiet_start.strftime('%H:%M')} to {quiet_end.strftime('%H:%M')}.")
        except ValueError:
            bot.send_message(user_id, "⚠️ Invalid format. Use HH:MM-HH:MM (e.g., `22:00-07:00`).")
        



@bot.message_handler(commands=["startquiz"])
def start_quiz_schedule(message):
    user_id = message.chat.id
    if user_id in user_intervals:
        bot.send_message(user_id, f"▶️ Automatic quizzes started. You will receive one every {user_intervals[user_id]} minutes.")
        thread = threading.Thread(target=quiz_scheduler, args=(user_id, user_intervals[user_id]), daemon=True)
        thread.start()
    else:
        bot.send_message(user_id, "⚠️ Set an interval first using /setinterval.")


def show_user_settings_inline(user_id):
    interval = user_intervals.get(user_id, "Not set")
    mode = user_preferences.get(user_id, "Default (random)")
    timeout = user_timeouts.get(user_id, "Default (10 min)")
    quiet = user_quiet_intervals.get(user_id, "Not set")

    if isinstance(quiet, tuple):
        quiet = f"{quiet[0].strftime('%H:%M')} - {quiet[1].strftime('%H:%M')}"

    settings_text = f"""
⚙️ **Your Current Settings**:
📚 Quiz Mode: *{mode}*
⏳ Quiz Interval: *{interval} minutes*
⌛ Timeout: *{timeout} minutes*
🌙 Quiet Hours: *{quiet}*
    """
    bot.send_message(user_id, settings_text, parse_mode="Markdown")



def quiz_scheduler(user_id, interval):
    while user_id in user_intervals:
        now = datetime.now().time()
        quiet_interval = user_quiet_intervals.get(user_id, None)

        if quiet_interval:
            quiet_start, quiet_end = quiet_interval

            # Handle intervals crossing midnight (e.g., 22:00-07:00)
            if quiet_start <= quiet_end:
                if quiet_start <= now <= quiet_end:
                    time.sleep(60)
                    continue
            else:
                if now >= quiet_start or now <= quiet_end:
                    time.sleep(60)
                    continue

        send_quiz_auto(user_id)
        time.sleep(interval * 60)


def send_quiz_auto(user_id):
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
    
    user_quiz[user_id] = {"kanji": kanji_entry["Kanji"], "reading": kanji_entry["Reading"], "meaning": kanji_entry["Meaning"], "type": question_type, "start_time": time.time()}
    
    if question_type == "reading":
        bot.send_message(user_id, f"🔹 What is the reading of this kanji: {kanji_entry['Kanji']}?")
    else:
        bot.send_message(user_id, f"🔹 What is the meaning of this kanji: {kanji_entry['Kanji']}?")

    timeout = user_timeouts.get(user_id, 10 * 60)  # Default timeout is 10 minutes
    threading.Timer(timeout, handle_timeout, args=[user_id]).start()


def handle_timeout(user_id):
    if user_id in user_quiz:
        correct_answer = user_quiz[user_id][user_quiz[user_id]["type"]]
        bot.send_message(user_id, f"⌛ Time's up! The correct answer was: *{correct_answer}*. Try another with /quiz.", parse_mode="Markdown")
        del user_quiz[user_id]

@bot.message_handler(func=lambda message: message.chat.id in user_quiz)
def check_answer(message):
    user_id = message.chat.id
    user_response = message.text.strip()
    correct_answer = user_quiz[user_id][user_quiz[user_id]["type"]]
    
    if user_response.lower() == correct_answer.lower():
        bot.send_message(user_id, "✅ Correct! 🎉 Type /quiz for another one!")
    else:
        bot.send_message(user_id, f"❌ Wrong! The correct answer was: *{correct_answer}*.", parse_mode="Markdown")
    
    del user_quiz[user_id]


# Function to show mode selection buttons
def show_mode_selection(user_id):
    keyboard = InlineKeyboardMarkup(row_width=3)  # Ensures proper layout
    modes = [
        ("Reading", "mode_reading"),
        ("Meaning", "mode_meaning"),
        ("Random", "mode_random")
    ]
    buttons = [InlineKeyboardButton(text, callback_data=callback_data) for text, callback_data in modes]
    keyboard.add(*buttons)  # Correct way to add multiple buttons
    bot.send_message(user_id, "🎯 Choose a quiz mode:", reply_markup=keyboard)


@bot.callback_query_handler(func=lambda call: call.data.startswith("mode_"))
def handle_mode_selection(call):
    print(f"DEBUG: Mode selection triggered with {call.data}")  # Debugging line
    user_id = call.message.chat.id
    mode = call.data.replace("mode_", "")
    user_preferences[user_id] = mode
    bot.send_message(user_id, f"✅ Quiz mode set to *{mode}*.", parse_mode="Markdown")


@bot.message_handler(commands=["quiz"])
def send_quiz(message):
    send_quiz_auto(message.chat.id)

bot.polling(none_stop=True)
