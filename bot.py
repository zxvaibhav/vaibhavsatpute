import os
import logging
import random
import string
from datetime import datetime
from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.errors import UserNotParticipant
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from pymongo import MongoClient
from flask import Flask
from threading import Thread

# --- Flask Web Server (Render ko busy rakhne ke liye) ---
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "Bot is alive!", 200

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    flask_app.run(host='0.0.0.0', port=port)
# --- Web Server ka code yahan khatam ---

# --- Basic Logging ---
logging.basicConfig(level=logging.INFO)

# --- Load Environment Variables ---
load_dotenv()

# --- Configuration ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL")) 
UPDATE_CHANNEL = os.environ.get("UPDATE_CHANNEL") 

# Admin configuration
ADMIN_IDS_STR = os.environ.get("ADMIN_IDS", "")
ADMINS = [int(admin_id.strip()) for admin_id in ADMIN_IDS_STR.split(',') if admin_id]

# --- Database Setup ---
try:
    client = MongoClient(MONGO_URI)
    db = client['file_link_bot']
    files_collection = db['files']
    batches_collection = db['batches']
    settings_collection = db['settings']
    logging.info("MongoDB Connected Successfully!")
except Exception as e:
    logging.error(f"Error connecting to MongoDB: {e}")
    exit()

# --- Pyrogram Client ---
app = Client("FileLinkBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Helper Functions ---
def generate_random_string(length=6):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

async def is_user_member(client: Client, user_id: int) -> bool:
    try:
        await client.get_chat_member(chat_id=f"@{UPDATE_CHANNEL}", user_id=user_id)
        return True
    except UserNotParticipant:
        return False
    except Exception as e:
        logging.error(f"Error checking membership for {user_id}: {e}")
        return False

async def get_bot_mode() -> str:
    setting = settings_collection.find_one({"_id": "bot_mode"})
    if setting:
        return setting.get("mode", "public")
    settings_collection.update_one({"_id": "bot_mode"}, {"$set": {"mode": "public"}}, upsert=True)
    return "public"

def get_user_batch(user_id):
    """Get or create a batch for the user"""
    batch = batches_collection.find_one({"user_id": user_id, "status": "active"})
    if not batch:
        batch_id = generate_random_string(8)
        batch = {
            "batch_id": batch_id,
            "user_id": user_id,
            "file_ids": [],
            "created_at": datetime.now(),
            "status": "active"  # active, completed
        }
        batches_collection.insert_one(batch)
    return batch

def add_file_to_batch(user_id, file_data):
    """Add a file to user's batch"""
    batch = get_user_batch(user_id)
    batches_collection.update_one(
        {"user_id": user_id, "status": "active"},
        {"$push": {"file_ids": file_data}}
    )
    return batch["batch_id"]

def complete_batch(user_id):
    """Mark batch as completed"""
    batches_collection.update_one(
        {"user_id": user_id, "status": "active"},
        {"$set": {"status": "completed"}}
    )

def get_batch_files(batch_id):
    """Get all files in a batch"""
    batch = batches_collection.find_one({"batch_id": batch_id})
    return batch.get("file_ids", []) if batch else []

# --- Bot Command Handlers ---

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    # Clear any active batch when user starts fresh
    batches_collection.update_one(
        {"user_id": message.from_user.id, "status": "active"},
        {"$set": {"status": "cancelled"}}
    )
    
    if len(message.command) > 1:
        file_id_str = message.command[1]
        
        if not await is_user_member(client, message.from_user.id):
            join_button = InlineKeyboardButton("🔗 Join Channel", url=f"https://t.me/{UPDATE_CHANNEL}")
            joined_button = InlineKeyboardButton("✅ I Have Joined", callback_data=f"check_join_{file_id_str}")
            keyboard = InlineKeyboardMarkup([[join_button], [joined_button]])
            
            await message.reply(
                f"👋 **Hello, {message.from_user.first_name}!**\n\nYe file access karne ke liye, aapko hamara update channel join karna hoga.",
                reply_markup=keyboard
            )
            return

        # Handle batch link
        if file_id_str.startswith("batch_"):
            batch_id = file_id_str.replace("batch_", "")
            file_records = get_batch_files(batch_id)
            
            if file_records:
                success_count = 0
                for file_record in file_records:
                    try:
                        await client.copy_message(
                            chat_id=message.from_user.id, 
                            from_chat_id=LOG_CHANNEL, 
                            message_id=file_record['message_id']
                        )
                        success_count += 1
                    except Exception as e:
                        logging.error(f"Error sending file: {e}")
                
                if success_count > 0:
                    await message.reply(f"✅ **{success_count} files successfully sent!**")
                else:
                    await message.reply("❌ Koi bhi file bhej nahi paya. Files expire ho gayi hain.")
            else:
                await message.reply("🤔 Files not found! Ho sakta hai link galat ya expire ho gaya ho.")
        else:
            # Handle single file link (backward compatibility)
            file_record = files_collection.find_one({"_id": file_id_str})
            if file_record:
                try:
                    await client.copy_message(chat_id=message.from_user.id, from_chat_id=LOG_CHANNEL, message_id=file_record['message_id'])
                except Exception as e:
                    await message.reply(f"❌ Sorry, file bhejte waqt ek error aa gaya.\n`Error: {e}`")
            else:
                await message.reply("🤔 File not found! Ho sakta hai link galat ya expire ho gaya ho.")
    else:
        help_text = """
**📁 Multi-File Link Bot**

**Here's how to use me:**

1. **Send Files**: Send me any file, or forward multiple files at once.

2. **Use the Menu**: After you send a file, a menu will appear:

   - 🔗 **Get Free Link**: Creates a permanent link for all files in your batch.

   - ➕ **Add More Files**: Allows you to send more files to the current batch.

**Available Commands:**
/start - Restart the bot and clear any session.
/editlink - Edit an existing link you created.
/help - Show this help message.
        """
        await message.reply(help_text)

@app.on_message(filters.command("help") & filters.private)
async def help_handler(client: Client, message: Message):
    help_text = """
**📁 Multi-File Link Bot**

**Here's how to use me:**

1. **Send Files**: Send me any file, or forward multiple files at once.

2. **Use the Menu**: After you send a file, a menu will appear:

   - 🔗 **Get Free Link**: Creates a permanent link for all files in your batch.

   - ➕ **Add More Files**: Allows you to send more files to the current batch.

**Available Commands:**
/start - Restart the bot and clear any session.
/editlink - Edit an existing link you created.
/help - Show this help message.
    """
    await message.reply(help_text)

@app.on_message(filters.private & (filters.document | filters.video | filters.photo | filters.audio))
async def file_handler(client: Client, message: Message):
    bot_mode = await get_bot_mode()
    if bot_mode == "private" and message.from_user.id not in ADMINS:
        await message.reply("😔 **Sorry!** Abhi sirf Admins hi files upload kar sakte hain.")
        return

    status_msg = await message.reply("⏳ Please wait, file process kar raha hu...", quote=True)
    
    try:
        # Forward file to log channel
        forwarded_message = await message.forward(LOG_CHANNEL)
        
        # Generate file ID and save to database
        file_id_str = generate_random_string()
        files_collection.insert_one({'_id': file_id_str, 'message_id': forwarded_message.id})
        
        # Add file to user's batch
        file_data = {
            'file_id': file_id_str,
            'message_id': forwarded_message.id,
            'file_name': getattr(message.document or message.video or message.audio, 'file_name', 'File'),
            'added_at': datetime.now()
        }
        batch_id = add_file_to_batch(message.from_user.id, file_data)
        
        # Get current batch info
        batch = batches_collection.find_one({"batch_id": batch_id})
        file_count = len(batch.get("file_ids", []))
        
        # Create menu buttons
        get_link_button = InlineKeyboardButton(f"🔗 Get Link ({file_count} files)", callback_data="get_batch_link")
        add_more_button = InlineKeyboardButton("➕ Add More Files", callback_data="add_more_files")
        cancel_button = InlineKeyboardButton("❌ Cancel", callback_data="cancel_batch")
        keyboard = InlineKeyboardMarkup([[get_link_button], [add_more_button], [cancel_button]])
        
        await status_msg.edit_text(
            f"✅ **File added successfully!**\n\n"
            f"📊 **Current Batch:** {file_count} files\n\n"
            f"**Choose an option:**",
            reply_markup=keyboard
        )
        
    except Exception as e:
        logging.error(f"File handling error: {e}")
        await status_msg.edit_text(f"❌ **Error!**\n\nKuch galat ho gaya. Please try again.\n`Details: {e}`")

@app.on_callback_query(filters.regex(r"^get_batch_link$"))
async def get_batch_link_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    batch = batches_collection.find_one({"user_id": user_id, "status": "active"})
    
    if not batch or not batch.get("file_ids"):
        await callback_query.answer("No files in your batch!", show_alert=True)
        return
    
    # Complete the batch
    complete_batch(user_id)
    
    # Generate shareable link
    bot_username = (await client.get_me()).username
    share_link = f"https://t.me/{bot_username}?start=batch_{batch['batch_id']}"
    
    await callback_query.message.edit_text(
        f"✅ **Batch Link Created Successfully!**\n\n"
        f"📦 **Total Files:** {len(batch['file_ids'])}\n"
        f"🔗 **Your Link:** `{share_link}`\n\n"
        f"**Share this link to share all files at once!**",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Share Link", url=f"https://t.me/share/url?url={share_link}")]
        ])
    )

@app.on_callback_query(filters.regex(r"^add_more_files$"))
async def add_more_files_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    batch = batches_collection.find_one({"user_id": user_id, "status": "active"})
    
    if batch:
        file_count = len(batch.get("file_ids", []))
        await callback_query.message.edit_text(
            f"✅ **Ready for more files!**\n\n"
            f"📊 **Current Batch:** {file_count} files\n\n"
            f"**Ab aap aur files bhej sakte hain.**\n"
            f"Har naye file ke baad menu dikhega jahan aap:\n"
            f"• 🔗 Link ban sakte hain\n"
            f"• ➕ Aur files add kar sakte hain",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Get Link Now", callback_data="get_batch_link")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_batch")]
            ])
        )
    else:
        await callback_query.answer("No active batch found!", show_alert=True)

@app.on_callback_query(filters.regex(r"^cancel_batch$"))
async def cancel_batch_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    batches_collection.update_one(
        {"user_id": user_id, "status": "active"},
        {"$set": {"status": "cancelled"}}
    )
    
    await callback_query.message.edit_text(
        "❌ **Batch cancelled!**\n\n"
        "Aap naye files bhej kar naya batch start kar sakte hain."
    )

@app.on_message(filters.command("editlink") & filters.private)
async def edit_link_handler(client: Client, message: Message):
    await message.reply(
        "✏️ **Edit Link Feature**\n\n"
        "Yeh feature jald hi available hoga!\n\n"
        "Aap filhaal naye files bhej kar naya link bana sakte hain."
    )

@app.on_message(filters.command("settings") & filters.private)
async def settings_handler(client: Client, message: Message):
    if message.from_user.id not in ADMINS:
        await message.reply("❌ Aapke paas is command ko use karne ki permission nahi hai.")
        return
    
    current_mode = await get_bot_mode()
    
    public_button = InlineKeyboardButton("🌍 Public (Anyone)", callback_data="set_mode_public")
    private_button = InlineKeyboardButton("🔒 Private (Admins Only)", callback_data="set_mode_private")
    keyboard = InlineKeyboardMarkup([[public_button], [private_button]])
    
    await message.reply(
        f"⚙️ **Bot Settings**\n\n"
        f"Abhi bot ka file upload mode **{current_mode.upper()}** hai.\n\n"
        f"**Public:** Koi bhi file bhej kar link bana sakta hai.\n"
        f"**Private:** Sirf admins hi file bhej sakte hain.\n\n"
        f"Naya mode select karein:",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex(r"^set_mode_"))
async def set_mode_callback(client: Client, callback_query: CallbackQuery):
    if callback_query.from_user.id not in ADMINS:
        await callback_query.answer("Permission Denied!", show_alert=True)
        return
        
    new_mode = callback_query.data.split("_")[2]
    
    settings_collection.update_one(
        {"_id": "bot_mode"},
        {"$set": {"mode": new_mode}},
        upsert=True
    )
    
    await callback_query.answer(f"Mode successfully {new_mode.upper()} par set ho gaya hai!", show_alert=True)
    
    public_button = InlineKeyboardButton("🌍 Public (Anyone)", callback_data="set_mode_public")
    private_button = InlineKeyboardButton("🔒 Private (Admins Only)", callback_data="set_mode_private")
    keyboard = InlineKeyboardMarkup([[public_button], [private_button]])
    
    await callback_query.message.edit_text(
        f"⚙️ **Bot Settings**\n\n"
        f"✅ Bot ka file upload mode ab **{new_mode.upper()}** hai.\n\n"
        f"Naya mode select karein:",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex(r"^check_join_"))
async def check_join_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    file_id_str = callback_query.data.split("_", 2)[2]

    if await is_user_member(client, user_id):
        await callback_query.answer("Thanks for joining! File bhej raha hu...", show_alert=True)
        
        if file_id_str.startswith("batch_"):
            batch_id = file_id_str.replace("batch_", "")
            file_records = get_batch_files(batch_id)
            
            if file_records:
                success_count = 0
                for file_record in file_records:
                    try:
                        await client.copy_message(
                            chat_id=user_id, 
                            from_chat_id=LOG_CHANNEL, 
                            message_id=file_record['message_id']
                        )
                        success_count += 1
                    except Exception as e:
                        logging.error(f"Error sending file: {e}")
                
                if success_count > 0:
                    await callback_query.message.delete()
                else:
                    await callback_query.message.edit_text("❌ Koi bhi file bhej nahi paya.")
            else:
                await callback_query.message.edit_text("🤔 Files not found!")
        else:
            file_record = files_collection.find_one({"_id": file_id_str})
            if file_record:
                try:
                    await client.copy_message(chat_id=user_id, from_chat_id=LOG_CHANNEL, message_id=file_record['message_id'])
                    await callback_query.message.delete()
                except Exception as e:
                    await callback_query.message.edit_text(f"❌ File bhejte waqt error aa gaya.\n`Error: {e}`")
            else:
                await callback_query.message.edit_text("🤔 File not found!")
    else:
        await callback_query.answer("Aapne abhi tak channel join nahi kiya hai. Please join karke dobara try karein.", show_alert=True)

# --- Bot ko Start Karo ---
if __name__ == "__main__":
    if not ADMINS:
        logging.warning("WARNING: ADMIN_IDS is not set. Settings command kaam nahi karega.")
    
    # Flask server ko ek alag thread me start karo
    logging.info("Starting Flask web server...")
    flask_thread = Thread(target=run_flask)
    flask_thread.start()
    
    logging.info("Bot is starting...")
    app.run()
    logging.info("Bot has stopped.")