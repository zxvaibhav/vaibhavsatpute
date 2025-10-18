import os
import logging
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Create client
app = Client("file_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

print("🤖 Bot starting...")

@app.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    print(f"📩 Start from user: {message.from_user.id}")
    await message.reply("🎉 **Bot is working!**\n\nSend me any file to get a link.")

@app.on_message(filters.document & filters.private)
async def handle_file(client, message):
    print(f"📄 File received: {message.document.file_name}")
    
    # Show processing
    processing_msg = await message.reply("⏳ Processing your file...")
    
    try:
        # Create buttons
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Get Download Link", callback_data="get_link")],
            [InlineKeyboardButton("➕ Add More Files", callback_data="add_more")]
        ])
        
        # Delete processing message
        await processing_msg.delete()
        
        # Send menu
        await message.reply(
            f"✅ **File Received!**\n\n"
            f"📁 **Name:** {message.document.file_name}\n"
            f"📦 **Size:** {message.document.file_size} bytes\n\n"
            f"**Choose option:**",
            reply_markup=keyboard
        )
        
    except Exception as e:
        print(f"❌ Error: {e}")
        await message.reply("❌ Error processing file")

@app.on_callback_query(filters.regex("get_link"))
async def get_link_callback(client, callback_query):
    await callback_query.answer()
    await callback_query.message.edit_text(
        "🔗 **Download Link:**\n\n"
        "📁 **File:** Your_File.txt\n"
        "⬇️ **Link:** `https://example.com/file.txt`\n\n"
        "Share this link with others!"
    )

@app.on_callback_query(filters.regex("add_more"))
async def add_more_callback(client, callback_query):
    await callback_query.answer()
    await callback_query.message.edit_text(
        "✅ **Ready for more files!**\n\n"
        "Send me another file to add to your batch."
    )

if __name__ == "__main__":
    print("🚀 Starting bot...")
    app.run()
    print("🛑 Bot stopped")