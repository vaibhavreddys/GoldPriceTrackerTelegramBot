from flask import Flask, request, jsonify
import threading
import os
import requests
from bs4 import BeautifulSoup
import cloudscraper
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext

# Fetch the token from the environment variable
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("No TOKEN environment variable found. Please set the TOKEN variable.")

# Create a Flask app for the dummy HTTP server
app = Flask(__name__)

@app.route("/")
def home():
    return jsonify({"status": "ok", "message": "Server is running"})

# Function to scrape the gold price table
def get_gold_prices():
    url = "https://www.goodreturns.in/gold-rates/bangalore.html"
    
    # Create a cloudscraper instance
    scraper = cloudscraper.create_scraper()
    response = scraper.get(url)

    if response.status_code != 200:
        return "Sorry, I couldn't fetch the gold prices. The website blocked the request."

    soup = BeautifulSoup(response.text, 'html.parser')

    # Locate the section containing the table
    section = soup.find('section', {'data-gr-title': 'Today 22 Carat Gold Price Per Gram in Bangalore (INR)'})
    if not section:
        return "Sorry, I couldn't find the gold price table."

    # Locate the table within the section
    table = section.find('table', {'class': 'table-conatiner'})
    if not table:
        return "Sorry, I couldn't find the gold price table."

    # Extract table headers
    headers = [th.text.strip() for th in table.find('thead').find_all('th')]

    # Extract table rows
    rows = []
    for row in table.find('tbody').find_all('tr'):
        cells = [cell.text.strip() for cell in row.find_all('td')]
        rows.append(cells)

    # Calculate the maximum width for each column
    column_widths = [max(len(headers[i]), max(len(row[i]) for row in rows)) for i in range(len(headers))]

    # Format the table data with proper alignment
    # Center-align headers
    table_data = "<b>" + " | ".join(headers[i].center(column_widths[i]) for i in range(len(headers))) + "</b>\n"
    table_data += "<i>" + " | ".join("-" * column_widths[i] for i in range(len(headers))) + "</i>\n"
    for row in rows:
        change = row[3]
        if "−" in change or "-" in change:  # Check for negative change
            row[3] = f"🔴 {change}"
        else:
            row[3] = f"🟢 {change}"
        table_data += " | ".join(row[i].ljust(column_widths[i]) for i in range(len(headers))) + "\n"

    message = (
        "🌟 Today's Gold Prices in Bangalore 🌟\n\n"
        f"{table_data}\n"
        "<i>Data sourced from <a href=\"https://www.goodreturns.in/gold-rates/bangalore.html\">GoodReturns.in</a></i>"
    )
    return message
# Command handler for /start
async def start(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text("Hello! Use /gold to get today's gold price table in Bangalore.")

# Command handler for /gold
async def gold(update: Update, context: CallbackContext) -> None:
    gold_price_table = get_gold_prices()
    await update.message.reply_text(gold_price_table, parse_mode="HTML")

# Open a port | Flask
def start_dummy_server():
    """Start the dummy HTTP server on port 10000."""
    app.run(host="0.0.0.0", port=10000)

def main() -> None:
    # Start the dummy HTTP server in a separate thread
    server_thread = threading.Thread(target=start_dummy_server)
    server_thread.daemon = True  # Daemonize thread to exit when the main program exits
    server_thread.start()
    # Create an Application object with your bot's token
    application = Application.builder().token(TOKEN).build()

    # Register the /start command handler
    application.add_handler(CommandHandler("start", start))

    # Register the /gold command handler
    application.add_handler(CommandHandler("gold", gold))

    # Start the bot
    application.run_polling()

if __name__ == '__main__':
    main()
