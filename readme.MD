# Quiz Bot for Telegram

A Telegram bot that quizzes users on Japanese Kanji. The bot uses Google Sheets to manage quiz data and offers various customization options for quiz intervals, timeouts, and quiet hours.

## Features

- Quizzes users on Kanji readings and meanings.
- Integrates with Google Sheets for quiz content.
- Customizable quiz intervals and answer timeouts.
- Quiet hours setting to avoid disturbances.
- Inline keyboard for easy command access.
- Automatic and manual quiz modes.

## Setup Instructions

### Prerequisites

1. **Python 3.x** installed on your system.
2. Required Python packages:
   - `telebot`
   - `gspread`
   - `oauth2client`

   Install them using:
   ```bash
   pip install pyTelegramBotAPI gspread oauth2client
   ```

3. **Google Sheets API Credentials**:
   - Create a project in the [Google Cloud Console](https://console.cloud.google.com/).
   - Enable the Google Sheets API.
   - Create credentials and download the `credentials.json` file.

4. **Telegram Bot Token**:
   - Create a bot via [BotFather](https://t.me/botfather) on Telegram.
   - Replace `"TOKEN"` in the script with your actual bot token.

### Configuration

1. Place the `credentials.json` file in the same directory as the script.
2. Ensure your Google Sheet is shared with the service account email from your credentials file.
3. Run the bot using:
   ```bash
   python quiz-bot.py
   ```

## Usage

### Commands

- `/start`: Displays the welcome message and inline keyboard.
- `/help`: Shows help information.
- `/setup`: Link your Google Sheet for quiz data.
- `/quiz`: Start the quiz.
- `/setmode`: Choose between reading, meaning, or random quiz modes.
- `/setinterval`: Set how often you receive quiz questions.
- `/setquietinterval`: Define quiet hours during which no quizzes will be sent.
- `/settimeout`: Set a timeout for answering questions.
- `/settings`: Display your current quiz settings.
- `/stopquiz`: Stop receiving automatic quizzes.
- `/stopquizauto`: Stop the automatic quiz feature.

### Google Sheet Format

Ensure your Google Sheet has the following columns:

- **Kanji**: The Kanji character.
- **Reading**: The correct reading(s) of the Kanji.
- **Meaning**: The correct meaning(s) of the Kanji.

### Example Sheet:

| Kanji | Reading | Meaning |
|-------|---------|---------|
| 日    | nichi, jitsu | sun, day |
| 月    | getsu, gatsu | moon, month |

## Customization Options

- **Quiz Interval**: Set how frequently quizzes are sent (1-60 minutes).
- **Answer Timeout**: Define how long users have to answer a quiz (0-1440 minutes).
- **Quiet Hours**: Specify time ranges when no quizzes will be sent (e.g., `22:00-07:00`).
- **Quiz Modes**:
  - *Reading*: Quiz focuses on the reading of Kanji.
  - *Meaning*: Quiz focuses on the meaning of Kanji.
  - *Random*: Randomly selects between reading and meaning.

## Logging

Logs are saved in the console output, providing information on bot activity and errors.

## Graceful Shutdown

To safely stop the bot, use `Ctrl+C` or send a termination signal. The bot will save user settings and shut down gracefully.

## Troubleshooting

- **Invalid Spreadsheet URL**: Ensure the link is correct and the sheet is shared with the service account.
- **No Quizzes Sent**: Check if the quiz interval and quiz mode are set correctly.
- **API Errors**: Verify your Google API credentials and permissions.

## License

This project is licensed under the MIT License.

---

For any issues or contributions, feel free to submit a pull request or raise an issue in the repository.

