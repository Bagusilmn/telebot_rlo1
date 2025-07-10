import logging
import os
import json
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# --- Konfigurasi Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Memuat Konfigurasi dari Environment Variables ---
try:
    TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
    CHATBOT_PRODUCT_API = os.environ['CHATBOT_PRODUCT_API']
    CHATBOT_TICKET_API = os.environ['CHATBOT_TICKET_API']
    SPREADSHEET_NAME = os.environ['SPREADSHEET_NAME']
    ORDER_SHEET_NAME = os.environ['ORDER_SHEET_NAME']
    LOG_SHEET_NAME = os.environ['LOG_SHEET_NAME']
    GOOGLE_CREDENTIALS_PATH = os.environ['GOOGLE_CREDENTIALS_JSON_PATH']
except KeyError as e:
    logger.error(f"Environment variable {e} tidak ditemukan! Harap atur sebelum menjalankan.")
    exit()

# --- State Management (Pengganti PropertiesService) ---
# Untuk produksi, disarankan menggunakan database (misal: SQLite) daripada dictionary
user_states = {}

# --- Koneksi ke Google Sheets ---
try:
    scope = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/spreadsheets',
             "https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_PATH, scope)
    client = gspread.authorize(creds)
    spreadsheet = client.open(SPREADSHEET_NAME)
    order_sheet = spreadsheet.worksheet(ORDER_SHEET_NAME)
    log_sheet = spreadsheet.worksheet(LOG_SHEET_NAME)
    logger.info("Berhasil terhubung ke Google Sheets.")
except Exception as e:
    logger.error(f"Gagal terhubung ke Google Sheets: {e}")
    exit()

# =================================================================
# FUNGSI-FUNGSI UTAMA HANDLER
# =================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mengirim menu utama saat pengguna mengirim /start."""
    keyboard = [
        [InlineKeyboardButton("1. Chatbot Product Knowledge", callback_data="chatbot_product")],
        [InlineKeyboardButton("2. Chatbot Ticket Alignment", callback_data="chatbot_ticket")],
        [InlineKeyboardButton("3. Tiket (Input & Cek Resi)", callback_data="ticket_system")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Selamat datang! Silakan pilih layanan yang Anda butuhkan:", reply_markup=reply_markup)

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mengeluarkan pengguna dari mode saat ini."""
    chat_id = update.effective_chat.id
    if chat_id in user_states:
        del user_states[chat_id]
        await update.message.reply_text("Anda telah keluar dari mode saat ini. Kirim /start untuk memulai lagi.")
    else:
        await update.message.reply_text("Anda sedang tidak dalam mode apa pun. Kirim /start untuk memulai.")

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menangani klik tombol dari inline keyboard."""
    query = update.callback_query
    await query.answer()
    
    chat_id = query.message.chat_id
    selection = query.data
    user_states[chat_id] = selection

    mode_text = selection.replace("_", " ").title()
    await query.edit_message_text(text=f"Pilihan Anda: {mode_text}")
    
    reply_text = ""
    if selection == 'chatbot_product':
      reply_text = "Anda sekarang dalam mode <b>Chatbot Product Knowledge</b>.\n\nSilakan ajukan pertanyaan Anda. Kirim /stop untuk keluar."
    elif selection == 'chatbot_ticket':
      reply_text = "Anda sekarang dalam mode <b>Chatbot Ticket Alignment</b>.\n\nSilakan ajukan pertanyaan Anda. Kirim /stop untuk keluar."
    elif selection == 'ticket_system':
      reply_text = "Anda sekarang dalam mode <b>Tiket</b>.\n\nKirim <code>cari [nomor resi]</code> untuk mencari data atau kirim data dengan format yang ditentukan. Kirim /stop untuk keluar."
      
    if reply_text:
        await context.bot.send_message(chat_id=chat_id, text=reply_text, parse_mode='HTML')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatcher utama untuk pesan teks berdasarkan state pengguna."""
    chat_id = update.effective_chat.id
    mode = user_states.get(chat_id)

    if mode == 'chatbot_product':
        await handle_chatbot(update, context, CHATBOT_PRODUCT_API)
    elif mode == 'chatbot_ticket':
        await handle_chatbot(update, context, CHATBOT_TICKET_API)
    elif mode == 'ticket_system':
        await handle_ticket_system(update, context)
    else:
        await update.message.reply_text("Perintah tidak dikenali. Silakan kirim /start untuk melihat menu utama.")

# =================================================================
# FUNGSI-FUNGSI LOGIKA BISNIS
# =================================================================

async def handle_chatbot(update: Update, context: ContextTypes.DEFAULT_TYPE, api_url: str):
    """Meneruskan pesan ke API chatbot dan mengirim balasannya."""
    question = update.message.text
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action='typing')

    try:
        payload = {"question": question, "user_id": str(chat_id)}
        response = requests.post(api_url, json=payload, timeout=30)
        response.raise_for_status()
        response_json = response.json()
        reply = response_json.get("result") or response_json.get("message") or response_json.get("answer", "Maaf, chatbot tidak dapat merespons saat ini.")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error saat akses chatbot API {api_url}: {e}")
        reply = f"⚠ Terjadi kesalahan saat mengakses chatbot."
    
    await update.message.reply_text(reply)

async def handle_ticket_system(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menangani logika untuk input data dan cek resi."""
    message_text = update.message.text
    text_lower = message_text.lower()
    reply_text = ""

    if text_lower.startswith('cari '):
        resi = message_text[5:].strip()
        reply_text = await cek_resi_sheets(resi)
    elif 'nama:' in text_lower and 'kode barang:' in text_lower:
        parsed_data = parse_order_message(message_text)
        if parsed_data:
            parsed_data['chatId'] = update.effective_chat.id
            id_order = await input_data_sheets(parsed_data)
            reply_text = f"Data berhasil disimpan dengan ID Order <b>{id_order}</b>" if id_order else "Data gagal disimpan."
        else:
            reply_text = "Format data yang Anda kirim tidak lengkap atau salah. Data tidak dapat disimpan."
    else:
        reply_text = ("Perintah tidak dikenali dalam mode Tiket.\n\n"
                      "Gunakan format:\n- <code>cari [nomor resi]</code>\n"
                      "- atau kirim data order lengkap.\n\n"
                      "Kirim /stop untuk keluar dari mode ini.")

    await update.message.reply_html(reply_text)

# =================================================================
# FUNGSI-FUNGSI PEMBANTU (HELPERS)
# =================================================================

def parse_order_message(message: str) -> dict or None:
    """Mem-parsing pesan teks untuk mendapatkan data order."""
    data = {}
    for line in message.split('\n'):
        parts = line.split(':', 1)
        if len(parts) == 2:
            key = parts[0].strip().lower()
            value = parts[1].strip()
            if 'nama' in key: data['nama'] = value
            if 'kode barang' in key: data['kodeBarang'] = value
            if 'alamat' in key: data['alamat'] = value
            if 'resi' in key: data['resi'] = value
    
    return data if all(k in data for k in ['nama', 'kodeBarang', 'alamat', 'resi']) else None

async def cek_resi_sheets(resi: str) -> str:
    """Mencari data di Google Sheet berdasarkan resi."""
    if not resi:
        return "Format pencarian tidak valid. Gunakan: <code>cari [nomor resi]</code>"
    try:
        all_data = order_sheet.get_all_records() # Mengambil data sebagai list of dictionaries
        found_data = None
        for row in all_data:
            if str(row.get('resi')).lower() == resi.lower():
                found_data = row
                break
        
        if found_data:
            # Pastikan tanggal diformat dengan benar
            try:
                order_date = datetime.strptime(found_data.get('tanggal_order', '').split(" ")[0], '%Y-%m-%d').strftime('%d %b %Y')
            except (ValueError, TypeError):
                order_date = found_data.get('tanggal_order', 'N/A')

            return (f"Info Resi <b>{resi}</b>\n\n"
                    f"ID Order: {found_data.get('id_order', 'N/A')}\n"
                    f"Tanggal Order: {order_date}\n"
                    f"Nama: {found_data.get('nama', 'N/A')}\n"
                    f"Kode Barang: {found_data.get('kode_barang', 'N/A')}\n"
                    f"Alamat: {found_data.get('alamat', 'N/A')}\n"
                    f"Status Pengiriman: <b>{found_data.get('status_pengiriman', 'N/A')}</b>")
        else:
            return f"Resi <b>{resi}</b> tidak ditemukan."
    except Exception as e:
        logger.error(f"Error di cek_resi_sheets: {e}")
        await log_to_sheet(f"Error di cek_resi_sheets: {e}")
        return f"Terjadi kesalahan saat mencari resi {resi}."

async def input_data_sheets(data: dict) -> str or None:
    """Memasukkan data order baru ke Google Sheet."""
    try:
        last_row_num = len(order_sheet.get_all_values())
        id_order = f"ORD-{last_row_num}"
        today = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        new_row = [
            last_row_num, id_order, today, data['nama'], data['kodeBarang'],
            data['alamat'], data['resi'], 'Sedang dikemas', data['chatId']
        ]
        order_sheet.append_row(new_row)
        return id_order
    except Exception as e:
        logger.error(f"Error di input_data_sheets: {e}")
        await log_to_sheet(f"Error di input_data_sheets: {e}")
        return None

async def log_to_sheet(log_message: str):
    """Mencatat pesan log ke dalam Google Sheet."""
    try:
        today = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_sheet.append_row([today, log_message])
    except Exception as e:
        logger.error(f"Gagal menulis log ke sheet: {e}")

def main() -> None:
    """Fungsi utama untuk menjalankan bot."""
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # --- Menambahkan Handlers ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CallbackQueryHandler(button_click))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # --- Menjalankan Bot ---
    logger.info("Bot mulai berjalan...")
    application.run_polling()

if __name__ == '__main__':
    main()
