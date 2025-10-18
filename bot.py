import os
import logging
import random
import string
import time
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.errors import UserNotParticipant, MessageIdInvalid, ChannelInvalid, ChannelPrivate, FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from pymongo import MongoClient
from flask import Flask
from threading import Thread

# --- Flask Web Server ---
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "Bot is alive!", 200

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    flask_app.run(host='0.0.0.0', port=port)

# --- Basic Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Load Environment Variables ---
load_dotenv()

# --- Configuration ---
API_ID = int(os.environ.get("API_ID", "123456"))
API_HASH = os.environ.get("API_HASH", "your_api_hash")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "your_bot_token")
MONGO_URI = os.environ.get("MONGO_URI", "your_mongo_uri")
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL", "-1003030414300")) 
UPDATE_CHANNEL = os.environ.get("UPDATE_CHANNEL", "your_channel") 

# Admin configuration
ADMIN_IDS_STR = os.environ.get("ADMIN_IDS", "")
ADMINS = [int(admin_id.strip()) for admin_id in ADMIN_IDS_STR.split(',') if admin_id.strip()]

# --- Database Setup ---
try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.server_info()  # Test connection
    db = mongo_client['file_link_bot']
    files_collection = db['files']
    batches_collection = db['batches']
    settings_collection = db['settings']
    logging.info("✅ MongoDB Connected Successfully!")
except Exception as e:
    logging.error(f"❌ Error connecting to MongoDB: {e}")
    # Continue without MongoDB for now
    files_collection = None
    batches_collection = None
    settings_collection = None

# --- Pyrogram Client with FloodWait handling ---
class FileStoreBot:
    def __init__(self):
        self.app = Client(
            "FileLinkBot",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            sleep_threshold=60,
            max_concurrent_transmissions=1
        )
        self.user_processing = {}
        
    async def start(self):
        """Start the bot with FloodWait handling"""
        try:
            await self.app.start()
            logging.info("✅ Bot started successfully!")
            
            # Get bot info
            me = await self.app.get_me()
            logging.info(f"🤖 Bot: @{me.username} (ID: {me.id})")
            
            # Keep the bot running
            await self.idle()
            
        except FloodWait as e:
            logging.warning(f"⏳ FloodWait: Waiting {e.value} seconds")
            time.sleep(e.value)
            await self.start()
        except Exception as e:
            logging.error(f"❌ Failed to start bot: {e}")
            await self.stop()
    
    async def stop(self):
        """Stop the bot"""
        try:
            await self.app.stop()
            logging.info("✅ Bot stopped successfully!")
        except Exception as e:
            logging.error(f"❌ Error stopping bot: {e}")
    
    async def idle(self):
        """Keep the bot running"""
        logging.info("🔄 Bot is now running...")
        while True:
            await asyncio.sleep(3600)  # Sleep for 1 hour

# Create bot instance
bot_manager = FileStoreBot()
app = bot_manager.app

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
    if not settings_collection:
        return "public"
    try:
        setting = settings_collection.find_one({"_id": "bot_mode"})
        if setting:
            return setting.get("mode", "public")
        settings_collection.update_one({"_id": "bot_mode"}, {"$set": {"mode": "public"}}, upsert=True)
        return "public"
    except:
        return "public"

def get_user_batch(user_id):
    if not batches_collection:
        return {"batch_id": "temp", "file_ids": []}
    try:
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
    except:
        return {"batch_id": "temp", "file_ids": []}

def add_file_to_batch(user_id, file_data):
    if not batches_collection:
        return "temp"
    try:
        batch = batches_collection.find_one({"user_id": user_id, "status": "active"})
        
        # Check if file already exists in batch
        existing_files = batch.get("file_ids", [])
        for existing_file in existing_files:
            if existing_file.get('message_id') == file_data.get('message_id'):
                return batch["batch_id"]
        
        batches_collection.update_one(
            {"user_id": user_id, "status": "active"},
            {"$push": {"file_ids": file_data}}
        )
        return batch["batch_id"]
    except:
        return "temp"

def complete_batch(user_id):
    if batches_collection:
        try:
            batches_collection.update_one(
                {"user_id": user_id, "status": "active"},
                {"$set": {"status": "completed"}}
            )
        except:
            pass

def get_batch_files(batch_id):
    if not batches_collection:
        return []
    try:
        batch = batches_collection.find_one({"batch_id": batch_id})
        return batch.get("file_ids", []) if batch else []
    except:
        return []

def get_file_name(message: Message):
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
    try:
        # Check if bot has access to log channel
        try:
            await client.get_chat(LOG_CHANNEL)
        except (ChannelInvalid, ChannelPrivate) as e:
            logging.error(f"Bot doesn't have access to log channel: {e}")
            return None
        
        # Forward the message
        forwarded_message = await message.forward(LOG_CHANNEL)
        logging.info(f"Successfully forwarded message. Message ID: {forwarded_message.id}")
        return forwarded_message
        
    except FloodWait as e:
        logging.warning(f"⏳ FloodWait in forwarding: Waiting {e.value} seconds")
        await asyncio.sleep(e.value)
        return await forward_to_log_channel(client, message)
    except Exception as e:
        logging.error(f"Error forwarding to log channel: {e}")
        return None

# --- Bot Command Handlers ---

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    user_id = message.from_user.id
    
    # Clear any active batch
    if batches_collection:
        batches_collection.update_one(
            {"user_id": user_id, "status": "active"},
            {"$set": {"status": "cancelled"}}
        )
    
    # Clear user processing state
    if user_id in bot_manager.user_processing:
        del bot_manager.user_processing[user_id]
    
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
                
                # Only show success message if files were actually sent
                if success_count > 0:
                    await status_msg.edit_text(f"✅ **{success_count} files sent successfully!**")
                else:
                    await status_msg.delete()
            else:
                await message.reply("🤔 Files not found! The link might be wrong or expired.")
        else:
            if files_collection:
                file_record = files_collection.find_one({"_id": file_id_str})
                if file_record:
                    try:
                        await client.copy_message(
                            chat_id=user_id, 
                            from_chat_id=LOG_CHANNEL, 
                            message_id=file_record['message_id']
                        )
                    except Exception as e:
                        await message.reply(f"❌ Error sending file.")
                else:
                    await message.reply("🤔 File not found! The link might be wrong or expired.")
            else:
                await message.reply("🤔 File not found! Database not available.")
    else:
        help_text = """
**📁 File Store Bot**

**How to use me:**
1. Send me any file
2. Wait for processing  
3. Get your link

**Commands:**
/start - Restart bot
/help - Show help
        """
        await message.reply(help_text)

@app.on_message(filters.private & (filters.document | filters.video | filters.photo | filters.audio))
async def file_handler(client: Client, message: Message):
    try:
        bot_mode = await get_bot_mode()
        if bot_mode == "private" and message.from_user.id not in ADMINS:
            await message.reply("😔 **Sorry!** Only admins can upload files.")
            return

        user_id = message.from_user.id
        
        # If user is already being processed, ignore this file
        if user_id in bot_manager.user_processing and bot_manager.user_processing[user_id].get('processing'):
            return
        
        # Mark user as being processed
        bot_manager.user_processing[user_id] = {'processing': True, 'last_file': datetime.now()}
        
        # Show processing message
        processing_msg = await message.reply("⏳ **Processing your file...**", quote=True)
        
        try:
            # Forward file to log channel
            forwarded_message = await forward_to_log_channel(client, message)
            
            if not forwarded_message:
                await processing_msg.edit_text("❌ **Error!** Failed to process file. Please try again.")
                # Remove processing flag
                bot_manager.user_processing[user_id]['processing'] = False
                return
            
            # Generate file ID and save to database
            file_id_str = generate_random_string()
            if files_collection:
                files_collection.insert_one({'_id': file_id_str, 'message_id': forwarded_message.id})
            
            # Add file to user's batch
            file_data = {
                'file_id': file_id_str,
                'message_id': forwarded_message.id,
                'file_name': get_file_name(message),
                'added_at': datetime.now()
            }
            batch_id = add_file_to_batch(user_id, file_data)
            
            # Get batch info
            batch = get_user_batch(user_id)
            file_count = len(batch.get("file_ids", []))
            
            # Create menu buttons - ONLY ONE MENU
            get_link_button = InlineKeyboardButton(f"🔗 Get Link ({file_count} files)", callback_data="get_batch_link")
            add_more_button = InlineKeyboardButton("➕ Add More Files", callback_data="add_more_files")
            cancel_button = InlineKeyboardButton("❌ Cancel", callback_data="cancel_batch")
            keyboard = InlineKeyboardMarkup([[get_link_button], [add_more_button], [cancel_button]])
            
            # Delete processing message first
            await processing_msg.delete()
            
            # Send final menu - ONLY ONE MESSAGE
            menu_msg = await message.reply(
                f"✅ **File added successfully!**\n\n"
                f"📊 **Total Files:** {file_count}\n\n"
                f"**Choose an option:**",
                reply_markup=keyboard,
                quote=True
            )
            
            # Store menu message reference
            bot_manager.user_processing[user_id]['menu_message'] = menu_msg
            
        except FloodWait as e:
            await processing_msg.edit_text(f"⏳ Please wait {e.value} seconds and try again.")
        except Exception as e:
            logging.error(f"File handling error: {e}")
            try:
                await processing_msg.edit_text("❌ **Error!** Failed to process file. Please try again.")
            except:
                pass
        finally:
            # Always remove processing flag
            if user_id in bot_manager.user_processing:
                bot_manager.user_processing[user_id]['processing'] = False
                
    except FloodWait as e:
        logging.warning(f"⏳ FloodWait in file handler: {e}")
    except Exception as e:
        logging.error(f"Error in file handler: {e}")

# ... (rest of the callback handlers remain the same as previous code)
@app.on_callback_query(filters.regex(r"^get_batch_link$"))
async def get_batch_link_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    batch = get_user_batch(user_id)
    
    if not batch or not batch.get("file_ids"):
        await callback_query.answer("No files in your batch!", show_alert=True)
        return
    
    # Complete the batch
    complete_batch(user_id)
    
    # Generate shareable link
    bot_username = (await client.get_me()).username
    share_link = f"https://t.me/{bot_username}?start=batch_{batch['batch_id']}"
    
    await callback_query.message.edit_text(
        f"✅ **Link Created!**\n\n"
        f"📦 **Files:** {len(batch['file_ids'])}\n"
        f"🔗 **Link:** `{share_link}`\n\n"
        f"**Share this link:**",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Share Link", url=f"https://t.me/share/url?url={share_link}")]
        ])
    )
    
    # Clear user processing state
    if user_id in bot_manager.user_processing:
        del bot_manager.user_processing[user_id]

@app.on_callback_query(filters.regex(r"^add_more_files$"))
async def add_more_files_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    
    # Update the message to show ready for more files
    await callback_query.message.edit_text(
        "✅ **Ready for more files!**\n\n"
        "**Send your files now.**\n"
        "All files will be added to the same batch.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Get Link Now", callback_data="get_batch_link")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_batch")]
        ])
    )
    
    # Ensure processing flag is reset so new files can be processed
    if user_id in bot_manager.user_processing:
        bot_manager.user_processing[user_id]['processing'] = False

@app.on_callback_query(filters.regex(r"^cancel_batch$"))
async def cancel_batch_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    complete_batch(user_id)
    
    await callback_query.message.edit_text(
        "❌ **Batch cancelled!**\n\n"
        "Send files to start new batch."
    )
    
    # Clear user processing state
    if user_id in bot_manager.user_processing:
        del bot_manager.user_processing[user_id]

# ... (other handlers remain the same)

# --- Start the Bot ---
if __name__ == "__main__":
    logging.info("🚀 Starting File Store Bot...")
    
    # Start Flask server in separate thread
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    logging.info("✅ Flask server started")
    
    # Start the bot with proper error handling
    try:
        asyncio.run(bot_manager.start())
    except KeyboardInterrupt:
        logging.info("⏹️ Bot stopped by user")
    except Exception as e:
        logging.error(f"❌ Bot crashed: {e}")