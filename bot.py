import os
import logging
import random
import string
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.errors import UserNotParticipant, MessageIdInvalid, ChannelInvalid, ChannelPrivate
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from pymongo import MongoClient
from flask import Flask
from threading import Thread

# --- Flask Web Server (to keep Render active) ---
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "Bot is alive!", 200

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    flask_app.run(host='0.0.0.0', port=port)

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
            "status": "active"
        }
        batches_collection.insert_one(batch)
    return batch

def add_file_to_batch(user_id, file_data):
    """Add a file to user's batch - prevents duplicates"""
    batch = batches_collection.find_one({"user_id": user_id, "status": "active"})
    
    # Check if file already exists in batch (prevent duplicates)
    existing_files = batch.get("file_ids", [])
    for existing_file in existing_files:
        if existing_file.get('message_id') == file_data.get('message_id'):
            return batch["batch_id"]  # File already exists
    
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

def get_file_name(message: Message):
    """Extract file name from message"""
    if message.document:
        return message.document.file_name
    elif message.video:
        return message.video.file_name or "Video File"
    elif message.audio:
        return message.audio.file_name or "Audio File"
    elif message.photo:
        return "Photo.jpg"
    else:
        return "File"

async def forward_to_log_channel(client: Client, message: Message):
    """Safely forward message to log channel with error handling"""
    try:
        # Check if bot has access to log channel
        try:
            await client.get_chat(LOG_CHANNEL)
        except (ChannelInvalid, ChannelPrivate) as e:
            logging.error(f"Bot doesn't have access to log channel {LOG_CHANNEL}: {e}")
            return None
        
        # Forward the message
        forwarded_message = await message.forward(LOG_CHANNEL)
        logging.info(f"Successfully forwarded message to log channel. Message ID: {forwarded_message.id}")
        return forwarded_message
        
    except MessageIdInvalid as e:
        logging.error(f"MessageIdInvalid while forwarding: {e}")
        return None
    except Exception as e:
        logging.error(f"Error forwarding to log channel: {e}")
        return None

# Global dictionary to track user states
user_states = {}

# --- Bot Command Handlers ---

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    user_id = message.from_user.id
    
    # Clear any active batch when user starts fresh
    batches_collection.update_one(
        {"user_id": user_id, "status": "active"},
        {"$set": {"status": "cancelled"}}
    )
    
    # Clear user state completely
    if user_id in user_states:
        # Delete old menu message if exists
        if user_states[user_id].get('menu_message'):
            try:
                await user_states[user_id]['menu_message'].delete()
            except:
                pass
        del user_states[user_id]
    
    if len(message.command) > 1:
        file_id_str = message.command[1]
        
        if not await is_user_member(client, user_id):
            join_button = InlineKeyboardButton("🔗 Join Channel", url=f"https://t.me/{UPDATE_CHANNEL}")
            joined_button = InlineKeyboardButton("✅ I Have Joined", callback_data=f"check_join_{file_id_str}")
            keyboard = InlineKeyboardMarkup([[join_button], [joined_button]])
            
            await message.reply(
                f"👋 **Hello, {message.from_user.first_name}!**\n\nTo access this file, you need to join our update channel first.",
                reply_markup=keyboard
            )
            return

        # Handle batch link
        if file_id_str.startswith("batch_"):
            batch_id = file_id_str.replace("batch_", "")
            file_records = get_batch_files(batch_id)
            
            if file_records:
                status_msg = await message.reply(f"📦 **Sending {len(file_records)} files...**")
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
                
                await status_msg.edit_text(f"✅ **{success_count} files sent successfully!**")
            else:
                await message.reply("🤔 Files not found! The link might be wrong or expired.")
        else:
            # Handle single file link
            file_record = files_collection.find_one({"_id": file_id_str})
            if file_record:
                try:
                    await client.copy_message(
                        chat_id=user_id, 
                        from_chat_id=LOG_CHANNEL, 
                        message_id=file_record['message_id']
                    )
                except Exception as e:
                    await message.reply(f"❌ Sorry, there was an error sending the file.\n`Error: {e}`")
            else:
                await message.reply("🤔 File not found! The link might be wrong or expired.")
    else:
        help_text = """
**📁 File Store Bot**

**How to use me:**

1. **Send Files**: Send me any file, or forward multiple files.

2. **Wait for Processing**: All files will be processed first, then you'll get a menu.

3. **Use the Menu**: After processing, choose:
   - 🔗 **Get Link**: Creates a permanent link for all files
   - ➕ **Add More Files**: Add more files to current batch
   - ❌ **Cancel**: Cancel the current batch

**Available Commands:**
/start - Restart the bot and clear session
/help - Show this help message
        """
        await message.reply(help_text)

@app.on_message(filters.command("help") & filters.private)
async def help_handler(client: Client, message: Message):
    help_text = """
**📁 File Store Bot**

**How to use me:**

1. **Send Files**: Send me any file, or forward multiple files.

2. **Wait for Processing**: All files will be processed first, then you'll get a menu.

3. **Use the Menu**: After processing, choose:
   - 🔗 **Get Link**: Creates a permanent link for all files
   - ➕ **Add More Files**: Add more files to current batch
   - ❌ **Cancel**: Cancel the current batch

**Available Commands:**
/start - Restart the bot and clear session
/help - Show this help message
    """
    await message.reply(help_text)

@app.on_message(filters.private & (filters.document | filters.video | filters.photo | filters.audio))
async def file_handler(client: Client, message: Message):
    bot_mode = await get_bot_mode()
    if bot_mode == "private" and message.from_user.id not in ADMINS:
        await message.reply("😔 **Sorry!** Only admins can upload files in private mode.")
        return

    user_id = message.from_user.id
    
    # Initialize user state if not exists
    if user_id not in user_states:
        user_states[user_id] = {
            'processing': False,
            'pending_files': [],
            'menu_message': None,
            'last_activity': datetime.now()
        }
    
    user_state = user_states[user_id]
    user_state['last_activity'] = datetime.now()
    
    # Add current file to pending list
    user_state['pending_files'].append(message)
    
    # If already processing, just return - the file will be processed in current batch
    if user_state['processing']:
        return
    
    user_state['processing'] = True
    
    try:
        # Wait for 1.5 seconds to collect multiple files
        await asyncio.sleep(1.5)
        
        # Get all pending files
        pending_files = user_state['pending_files'].copy()
        total_files = len(pending_files)
        
        # Delete old menu message if exists
        if user_state['menu_message']:
            try:
                await user_state['menu_message'].delete()
            except:
                pass
        
        # Create processing message (this will be our menu message)
        processing_msg = await message.reply(f"⏳ **Processing {total_files} file(s)...**", quote=True)
        user_state['menu_message'] = processing_msg
        
        # Process all pending files
        processed_count = 0
        batch = get_user_batch(user_id)
        batch_id = batch["batch_id"]
        
        for file_message in pending_files:
            try:
                # Forward file to log channel
                forwarded_message = await forward_to_log_channel(client, file_message)
                
                if forwarded_message:
                    # Generate file ID and save to database
                    file_id_str = generate_random_string()
                    files_collection.insert_one({'_id': file_id_str, 'message_id': forwarded_message.id})
                    
                    # Add file to user's batch
                    file_data = {
                        'file_id': file_id_str,
                        'message_id': forwarded_message.id,
                        'file_name': get_file_name(file_message),
                        'added_at': datetime.now()
                    }
                    batch_id = add_file_to_batch(user_id, file_data)
                    processed_count += 1
                    
            except Exception as e:
                logging.error(f"Error processing file: {e}")
        
        # Clear pending files after processing
        user_state['pending_files'] = []
        
        # Get accurate file count from database
        updated_batch = batches_collection.find_one({"batch_id": batch_id})
        actual_file_count = len(updated_batch.get("file_ids", [])) if updated_batch else 0
        
        # Create menu buttons
        get_link_button = InlineKeyboardButton(f"🔗 Get Link ({actual_file_count} files)", callback_data="get_batch_link")
        add_more_button = InlineKeyboardButton("➕ Add More Files", callback_data="add_more_files")
        cancel_button = InlineKeyboardButton("❌ Cancel", callback_data="cancel_batch")
        keyboard = InlineKeyboardMarkup([[get_link_button], [add_more_button], [cancel_button]])
        
        # Update processing message to show final menu - ONLY ONE MESSAGE
        await processing_msg.edit_text(
            f"✅ **{processed_count} file(s) processed successfully!**\n\n"
            f"📊 **Total in Batch:** {actual_file_count} file(s)\n\n"
            f"**Choose an option:**",
            reply_markup=keyboard
        )
        
    except Exception as e:
        logging.error(f"File handling error: {e}")
        if user_state.get('menu_message'):
            try:
                await user_state['menu_message'].edit_text(f"❌ **Error!**\n\nSomething went wrong. Please try again.\n`Details: {e}`")
            except:
                pass
    
    finally:
        # Always reset processing state
        user_state['processing'] = False

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
    
    # Update the existing message
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
    
    # Clear user state after getting link
    if user_id in user_states:
        del user_states[user_id]

@app.on_callback_query(filters.regex(r"^add_more_files$"))
async def add_more_files_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    batch = batches_collection.find_one({"user_id": user_id, "status": "active"})
    
    if batch:
        file_count = len(batch.get("file_ids", []))
        
        # Update the existing message
        await callback_query.message.edit_text(
            f"✅ **Ready for more files!**\n\n"
            f"📊 **Current Batch:** {file_count} file(s)\n\n"
            f"**You can now send more files.**\n"
            f"Files will be processed together after a short delay.",
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
        "You can start a new batch by sending files."
    )
    
    # Clear user state after cancellation
    if user_id in user_states:
        del user_states[user_id]

# Other handlers remain the same...
@app.on_message(filters.command("settings") & filters.private)
async def settings_handler(client: Client, message: Message):
    if message.from_user.id not in ADMINS:
        await message.reply("❌ You don't have permission to use this command.")
        return
    
    current_mode = await get_bot_mode()
    
    public_button = InlineKeyboardButton("🌍 Public (Anyone)", callback_data="set_mode_public")
    private_button = InlineKeyboardButton("🔒 Private (Admins Only)", callback_data="set_mode_private")
    keyboard = InlineKeyboardMarkup([[public_button], [private_button]])
    
    await message.reply(
        f"⚙️ **Bot Settings**\n\n"
        f"Current file upload mode: **{current_mode.upper()}**\n\n"
        f"**Public:** Anyone can upload files and create links.\n"
        f"**Private:** Only admins can upload files.\n\n"
        f"Select new mode:",
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
    
    await callback_query.answer(f"Mode successfully changed to {new_mode.upper()}!", show_alert=True)
    
    public_button = InlineKeyboardButton("🌍 Public (Anyone)", callback_data="set_mode_public")
    private_button = InlineKeyboardButton("🔒 Private (Admins Only)", callback_data="set_mode_private")
    keyboard = InlineKeyboardMarkup([[public_button], [private_button]])
    
    await callback_query.message.edit_text(
        f"⚙️ **Bot Settings**\n\n"
        f"✅ Bot mode changed to **{new_mode.upper()}**\n\n"
        f"Select new mode:",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex(r"^check_join_"))
async def check_join_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    file_id_str = callback_query.data.split("_", 2)[2]

    if await is_user_member(client, user_id):
        await callback_query.answer("Thanks for joining! Sending files...", show_alert=True)
        
        if file_id_str.startswith("batch_"):
            batch_id = file_id_str.replace("batch_", "")
            file_records = get_batch_files(batch_id)
            
            if file_records:
                await callback_query.message.edit_text(f"📦 **Sending {len(file_records)} files...**")
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
                
                await callback_query.message.edit_text(f"✅ **{success_count} files sent successfully!**")
            else:
                await callback_query.message.edit_text("🤔 Files not found!")
        else:
            file_record = files_collection.find_one({"_id": file_id_str})
            if file_record:
                try:
                    await client.copy_message(
                        chat_id=user_id, 
                        from_chat_id=LOG_CHANNEL, 
                        message_id=file_record['message_id']
                    )
                    await callback_query.message.delete()
                except Exception as e:
                    await callback_query.message.edit_text(f"❌ Error sending file.\n`Error: {e}`")
            else:
                await callback_query.message.edit_text("🤔 File not found!")
    else:
        await callback_query.answer("You haven't joined the channel yet. Please join and try again.", show_alert=True)

# Cleanup function to remove old user states
async def cleanup_user_states():
    while True:
        try:
            current_time = datetime.now()
            users_to_remove = []
            
            for user_id, state in user_states.items():
                # Remove states older than 10 minutes
                if (current_time - state['last_activity']).total_seconds() > 600:
                    users_to_remove.append(user_id)
            
            for user_id in users_to_remove:
                del user_states[user_id]
                logging.info(f"Cleaned up state for user {user_id}")
                
        except Exception as e:
            logging.error(f"Error in cleanup_user_states: {e}")
        
        await asyncio.sleep(300)  # Run every 5 minutes

# --- Start the Bot ---
if __name__ == "__main__":
    if not ADMINS:
        logging.warning("WARNING: ADMIN_IDS is not set. Settings command won't work.")
    
    # Start Flask server in separate thread
    logging.info("Starting Flask web server...")
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Start cleanup task
    asyncio.get_event_loop().create_task(cleanup_user_states())
    
    logging.info("Bot is starting...")
    app.run()
    logging.info("Bot has stopped.")