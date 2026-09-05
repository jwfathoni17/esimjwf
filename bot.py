import os
import random
import string
import asyncio
import re
import aiohttp
import logging
from fastapi import FastAPI, Request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
GROUP_ID = int(os.getenv("GROUP_ID", "-1001234567890"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@esimjwf").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "1294583646"))

app = FastAPI()
telegram_app = None

# Variabel global untuk melacak status loop aktif per chat/user
active_loops = set()
stop_email_flags = set()

def sensor_text(text):
    if not text or len(text) <= 3: return "***"
    return text[:-3] + "***"

async def is_user_joined(context, user_id):
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception:
        return False

class MailTMBot:
    def __init__(self):
        self.base_url = "https://temp.tf"
        self.email = ""
        self.token = ""

    async def create_account(self):
        params = {"dot": 1, "providers": "gmail"}
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.base_url}/api/account", params=params, timeout=30) as r:
                data = await r.json(content_type=None)
                self.email = data.get("email")
                if not self.email:
                    raise RuntimeError(f"temp.tf gagal membuat email. Response: {data}")
        logger.info(f"Akun temp.tf dibuat: {self.email}")

    async def fetch_otp(self, timeout=60):
        headers = {"Authorization": f"Bearer {self.token}"}
        start_time = asyncio.get_event_loop().time()
        
        async with aiohttp.ClientSession(headers=headers) as session:
            while (asyncio.get_event_loop().time() - start_time) < timeout:
                try:
                    async with session.get(f"{self.base_url}/messages", headers={**headers, "Cache-Control": "no-cache"}) as r:
                        data = await r.json()
                        if data.get('hydra:totalItems', 0) > 0:
                            for msg in data['hydra:member']:
                                msg_id = msg['id']
                                async with session.get(f"{self.base_url}/messages/{msg_id}", headers=headers) as r2:
                                    msg_detail = await r2.json()
                                    raw_text = msg_detail.get('text', '') or ''
                                    raw_html = msg_detail.get('html', '') or ''
                                    subject = msg_detail.get('subject', '') or ''
                                    
                                    if isinstance(raw_text, list): raw_text = "\n".join(str(x) for x in raw_text)
                                    if isinstance(raw_html, list): raw_html = "\n".join(str(x) for x in raw_html)
                                    if isinstance(subject, list): subject = " ".join(str(x) for x in subject)

                                    combined_content = f"{subject} {raw_text} {raw_html}"
                                    
                                    match = re.search(r'(?:otp\s*code|kode\s*konfirmasi|otp)[:\s\-]*([A-Za-z0-9]{6})', combined_content, re.IGNORECASE)
                                    if match:
                                        logger.info(f"OTP berhasil dibaca: {match.group(1)}")
                                        return match.group(1).strip()
                                    words = re.findall(r'\b[A-Z0-9]{6}\b', combined_content)
                                    if words:
                                        for w in words:
                                            if not any(x in w.lower() for x in ['emalupe', 'mail', 'http', 'com', 'co.id', 'xlsmart']):
                                                return w
                except Exception as e:
                    logger.error(f"Error saat fetch OTP: {e}")
                await asyncio.sleep(0.5)
        return None

    async def fetch_xl_confirmation_email(self, timeout=60):
        headers = {"Authorization": f"Bearer {self.token}"}
        start_time = asyncio.get_event_loop().time()
        
        async with aiohttp.ClientSession(headers=headers) as session:
            logger.info("Menunggu email konfirmasi eSIM dari XL...")
            while (asyncio.get_event_loop().time() - start_time) < timeout:
                try:
                    async with session.get(f"{self.base_url}/messages", headers={**headers, "Cache-Control": "no-cache"}) as r:
                        data = await r.json()
                        if data.get('hydra:totalItems', 0) > 0:
                            for msg in data['hydra:member']:
                                msg_id = msg['id']
                                async with session.get(f"{self.base_url}/messages/{msg_id}", headers=headers) as r2:
                                    msg_detail = await r2.json()
                                    raw_text = msg_detail.get('text', '') or ''
                                    raw_html = msg_detail.get('html', '') or ''
                                    subject = msg_detail.get('subject', '') or ''
                                    
                                    if isinstance(raw_text, list): raw_text = "\n".join(str(x) for x in raw_text)
                                    if isinstance(raw_html, list): raw_html = "\n".join(str(x) for x in raw_html)
                                    if isinstance(subject, list): subject = " ".join(str(x) for x in subject)

                                    if not raw_text.strip() and raw_html.strip():
                                        raw_text = re.sub('<[^<]+?>', '', raw_html)

                                    combined_content = f"{subject}\n{raw_text}\n{raw_html}"
                                    
                                    if 'MSISDN' in combined_content or 'Activation Code' in combined_content or 'eSIM' in combined_content:
                                        logger.info("Email sukses eSIM XL ditemukan, mengekstrak detail...")
                                        
                                        msisdn = re.search(r'MSISDN\s*[:\s\-]*([0-9\+\s]+)', combined_content, re.IGNORECASE)
                                        puk = re.search(r'(?:Kode\s*PUK|PUK)\s*[:\s\-]*([0-9\s]+)', combined_content, re.IGNORECASE)
                                        smdp = re.search(r'SM-DP\+?\s*Address\s*[:\s\-]*([a-zA-Z0-9\.\_\-]+)', combined_content, re.IGNORECASE)
                                        act_code = re.search(r'Activation\s*Code\s*[:\s\-]*([a-zA-Z0-9\-]+)', combined_content, re.IGNORECASE)
                                        
                                        clean_msisdn = msisdn.group(1).strip() if msisdn else '-'
                                        clean_puk = puk.group(1).strip() if puk else '-'
                                        clean_smdp = smdp.group(1).strip() if smdp else '-'
                                        clean_act = act_code.group(1).strip() if act_code else '-'
                                        
                                        extracted_info = (
                                            "✅ <b>Berhasil Claim Esim 50GB 7Hari</b>\n\n"
                                            "<b>Detail Esim Private Kamu</b>\n"
                                            "<pre>MSISDN     : " + clean_msisdn + "\n"
                                            "Kode PUK   : " + clean_puk + "\n"
                                            "Address    : " + clean_smdp + "\n"
                                            "Activation : " + clean_act + "\n\n"
                                            "CREATED    : @forariey</pre>"
                                        )
                                        return extracted_info, clean_msisdn, clean_puk, clean_smdp, clean_act
                except Exception as e:
                    logger.error(f"Error saat ekstrak detail email XL: {e}")
                await asyncio.sleep(2)
        return f"Email konfirmasi dari XL belum diterima / timeout, akun terdaftar: {self.email}", None, None, None, None

async def process_xl_esim(chat_id, status_callback):
    full_name = f"jkowi{''.join(random.choices(string.ascii_lowercase + string.digits, k=4))}tokoxl"
    whatsapp = "08" + ''.join(random.choices(string.digits, k=9))
    email_retry_count = 0
    stop_email_flags.discard(chat_id)

    while True:
        if chat_id in stop_email_flags:
            logger.info(f"Proses email dibatalkan oleh user di chat {chat_id}")
            await status_callback("🛑 [STOP EMAIL] Proses dibatalkan oleh user.")
            return None, "Proses dibatalkan oleh user via /stopemail.", None, None, None, None

        email_retry_count += 1
        temp = MailTMBot()
        await temp.create_account()

        screenshot_path = f"esim_{chat_id}.png"
        debug_path = f"debug_{chat_id}.png"

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            context = await browser.new_context(viewport={"width": 1366, "height": 768}, locale="id-ID")
            page = await context.new_page()

            try:
                logger.info("Membersihkan cache dan session browser sebelum membuka halaman XL...")
                await context.clear_cookies()
                await context.add_init_script("""
                    try {
                        Object.defineProperty(window, 'navigator', {
                            value: Object.create(window.navigator),
                            configurable: true
                        });
                    } catch (e) {}
                """)
                await status_callback("🌐 [LOG: 1/7] Membersihkan sesi browser dan membuka halaman XL eSIM Trial...")
                await page.goto("https://www.xl.co.id/esim-trial/claim", timeout=90000, wait_until="domcontentloaded")
                await asyncio.sleep(2.0)

                logger.info("Membuka halaman dan menunggu form siap...")
                await status_callback("🌐 [LOG: 2/7] Halaman dibuka dan menunggu form siap...")
                await asyncio.sleep(2.5)

                logger.info("Klik mulai isi data...")
                await status_callback("🖱️ [LOG: 3/7] Klik tombol mulai isi data...")
                try:
                    start_btn = page.get_by_text("Mulai Isi Data", exact=True).first
                    if await start_btn.is_visible(timeout=10000):
                        await start_btn.click(timeout=15000)
                        await asyncio.sleep(2.0)
                except Exception:
                    try:
                        await page.click("button:has-text('Mulai Isi Data')", timeout=15000)
                        await asyncio.sleep(2.0)
                    except Exception:
                        pass

                logger.info("Isi form nama, email, nomor telepon...")
                await status_callback("📝 [LOG: 4/7] Mengisi form nama, email, dan nomor telepon...")
                try:
                    inputs = await page.locator("input").all()
                    if len(inputs) >= 3:
                        await inputs[0].fill(full_name)
                        await asyncio.sleep(0.6)
                        await inputs[1].fill(temp.email)
                        await asyncio.sleep(0.6)
                        await inputs[2].fill(whatsapp)
                        await asyncio.sleep(1.0)
                    else:
                        raise Exception("Gagal mendeteksi input form")
                except Exception as e:
                    logger.error(f"Error isi data: {e}")
                    raise Exception("Error: Form input tidak ditemukan.")

                logger.info("Centang syarat dan ketentuan")
                await status_callback("✅ [LOG: 5/7] Mencentang syarat & ketentuan...")
                try:
                    checkbox = page.locator("input[type='checkbox']").last
                    await checkbox.check(timeout=15000)
                    await asyncio.sleep(1.2)
                except Exception:
                    try:
                        await page.locator("label:has-text('Syarat'), label:has-text('Ketentuan')").first.click(timeout=15000)
                        await asyncio.sleep(1.2)
                    except Exception:
                        pass

                logger.info("Tekan lanjut...")
                await status_callback("📤 [LOG: 6/7] Menekan tombol lanjut...")
                try:
                    await page.get_by_role("button", name="Lanjut").click(timeout=10000)
                    await asyncio.sleep(1.5)
                except Exception:
                    await page.click("button:has-text('Lanjut'), button:has-text('Kirim')", timeout=10000)
                    await asyncio.sleep(1.5)

                logger.info("Ambil screenshot setelah klik lanjut")
                await status_callback("📸 [LOG: 7/7] Mengambil screenshot setelah tombol lanjut...")
                await page.screenshot(path=screenshot_path, full_page=True)

                if os.path.exists(debug_path):
                    os.remove(debug_path)

                return screenshot_path, "Selesai sampai tahap setelah klik lanjut dan screenshot berhasil diambil.", None, None, None, None

            except Exception as e:
                logger.error(f"Error di proses utama: {e}")
                try:
                    await page.screenshot(path=debug_path)
                except Exception:
                    pass
                return debug_path, str(e), None, None, None, None

            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

        # end while


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_user_joined(context, user_id):
        keyboard = [
            [InlineKeyboardButton("📢 Join Channel", url="https://t.me/forarieyproject")],
            [InlineKeyboardButton("🔄 Cek Status Join", callback_data="check_join")]
        ]
        await update.message.reply_text(
            "⚠️ <b>Akses Ditolak!</b>\n\n"
            "Anda belum bergabung di channel kami. Silakan klik tombol di bawah untuk join:",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )
        return

    keyboard = [
        [InlineKeyboardButton("🚀 Mulai Claim Esim", callback_data="start_claim")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("👋 <b>Selamat datang di Bot Claim eSIM XL!</b>\nSilakan pilih menu di bawah:", 
                                   reply_markup=reply_markup, parse_mode="HTML")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_loops:
        active_loops.remove(chat_id)
        await update.message.reply_text("🛑 <b>Claim Loop berhasil dihentikan!</b>", parse_mode="HTML")
    else:
        await update.message.reply_text("⚠️ Tidak ada proses Claim Loop yang sedang berjalan.", parse_mode="HTML")

async def stop_email_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    stop_email_flags.add(chat_id)
    if chat_id in active_loops:
        await update.message.reply_text("🛑 <b>Proses retry email dihentikan.</b> Bot akan berhenti setelah langkah saat ini selesai.", parse_mode="HTML")
    else:
        await update.message.reply_text("🛑 <b>Stop email aktif.</b> Proses retry email akan dibatalkan pada percobaan berikutnya.", parse_mode="HTML")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == "check_join":
        if await is_user_joined(context, user_id):
            keyboard = [
                [InlineKeyboardButton("🚀 Mulai Claim Esim", callback_data="start_claim")]
            ]
            await query.edit_message_text("✅ <b>Terima kasih!</b> Anda telah bergabung.\nSilakan gunakan fitur bot:", 
                                          reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        else:
            await query.answer("❌ Anda belum bergabung di channel, silakan klik tombol Join!", show_alert=True)

    elif query.data == "donation":
        await query.message.reply_text("Dana : 082151916181\nShopeepay : 082151916181")
        
    elif query.data == "start_claim_loop":
        if not await is_user_joined(context, user_id):
            await query.message.reply_text("⚠️ Silakan join channel @forarieyproject terlebih dahulu!")
            return

        chat_id = query.message.chat.id
        if chat_id in active_loops:
            await query.message.reply_text("⚠️ Claim Loop sudah berjalan di chat ini! Kirim /stop untuk menghentikan.")
            return

        active_loops.add(chat_id)
        await query.message.reply_text("🔄 <b>Claim Loop diaktifkan!</b> Bot akan melakukan klaim eSIM secara terus-menerus.\nKetik /stop kapan saja untuk menghentikan.", parse_mode="HTML")

        loop_count = 1
        while chat_id in active_loops:
            msg = await context.bot.send_message(chat_id=chat_id, text=f"🚀 <b>[Loop ke-{loop_count}]</b> Memulai proses klaim eSIM...", parse_mode="HTML")
            
            async def update_status(text):
                try:
                    await context.bot.edit_message_text(text=f"<b>[Loop ke-{loop_count}]</b>\n{text}", chat_id=chat_id, message_id=msg.message_id, parse_mode="HTML")
                except Exception:
                    pass

            user = query.from_user
            username = f"@{user.username}" if user.username else user.first_name
            path, info, ms, pk, sm, ac = await process_xl_esim(chat_id, update_status)
            
            if chat_id not in active_loops:
                break

            if path and "esim_" in path and os.path.exists(path):
                caption = info
                keyboard_claim = [[InlineKeyboardButton("🧩 Register Biometrik", url="https://registrasi.xl.co.id")]]
                reply_markup_claim = InlineKeyboardMarkup(keyboard_claim)
                await context.bot.send_photo(
                    chat_id=chat_id, 
                    photo=open(path, 'rb'), 
                    caption=caption, 
                    parse_mode="HTML",
                    reply_markup=reply_markup_claim
                )
                
                if ms:
                    grup_text = (
                        f"👤 Halo {username}\n\n"
                        "✅ <b>Esim Berhasil Dibuat</b>\n\n"
                        "<b>Detail Esim Private Kamu</b>\n"
                        "<pre>MSISDN     : " + sensor_text(ms) + "\n"
                        "Kode PUK   : " + sensor_text(pk) + "\n"
                        "Address    : " + sm + "\n"
                        "Activation : " + sensor_text(ac) + "\n\n"
                        "CREATED    : @forariey\n"
                        "Donation   : 082151916181</pre>"
                    )
                    await context.bot.send_message(chat_id=GROUP_ID, text=grup_text, parse_mode="HTML", reply_markup=reply_markup_claim)
                    
                try:
                    os.remove(path)
                except Exception:
                    pass
            else:
                if path and os.path.exists(path):
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=open(path, 'rb'),
                        caption=f"❌ <b>Gagal Memproses (Loop {loop_count}):</b>\n<pre>{info}</pre>",
                        parse_mode="HTML"
                    )
                    os.remove(path)
                else:
                    await context.bot.send_message(chat_id=chat_id, text=f"❌ <b>Gagal Memproses (Loop {loop_count}):</b>\n<pre>{info}</pre>", parse_mode="HTML")

            loop_count += 1
            if chat_id in active_loops:
                await asyncio.sleep(5) # Jeda antar loop agar tidak terlalu spam

    elif query.data == "start_claim":
        if not await is_user_joined(context, user_id):
            await query.message.reply_text("⚠️ Silakan join channel @forarieyproject terlebih dahulu!")
            return

        chat_id = query.message.chat.id
        msg = await query.message.reply_text("🚀 Bot Telegram aktif! Memproses klaim eSIM...")
        
        async def update_status(text):
            try:
                await context.bot.edit_message_text(text=text, chat_id=chat_id, message_id=msg.message_id, parse_mode="HTML")
            except Exception:
                pass

        user = query.from_user
        username = f"@{user.username}" if user.username else user.first_name
        path, info, ms, pk, sm, ac = await process_xl_esim(chat_id, update_status)
        
        if path and "esim_" in path and os.path.exists(path):
            caption = info
            keyboard_claim = [[InlineKeyboardButton("🧩 Register Biometrik", url="https://registrasi.xl.co.id")]]
            reply_markup_claim = InlineKeyboardMarkup(keyboard_claim)
            await context.bot.send_photo(
                chat_id=chat_id, 
                photo=open(path, 'rb'), 
                caption=caption, 
                parse_mode="HTML",
                reply_markup=reply_markup_claim
            )
            
            if ms:
                grup_text = (
                    f"👤 Halo {username}\n\n"
                    "✅ <b>Esim Berhasil Dibuat</b>\n\n"
                    "<b>Detail Esim Private Kamu</b>\n"
                    "<pre>MSISDN     : " + sensor_text(ms) + "\n"
                    "Kode PUK   : " + sensor_text(pk) + "\n"
                    "Address    : " + sm + "\n"
                    "Activation : " + sensor_text(ac) + "\n\n"
                    "CREATED    : @forariey\n"
                    "Donation   : 082151916181</pre>"
                )
                await context.bot.send_message(chat_id=GROUP_ID, text=grup_text, parse_mode="HTML", reply_markup=reply_markup_claim)
                
            try:
                os.remove(path)
            except Exception:
                pass
        else:
            if path and os.path.exists(path):
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=open(path, 'rb'),
                    caption=f"❌ <b>Gagal Memproses:</b>\n<pre>{info}</pre>",
                    parse_mode="HTML"
                )
                os.remove(path)
            else:
                await context.bot.send_message(chat_id=chat_id, text=f"❌ <b>Gagal Memproses:</b>\n<pre>{info}</pre>", parse_mode="HTML")

@app.post("/")
async def webhook(request: Request):
    global telegram_app
    try:
        data = await request.json()
        if "message" in data and "text" in data["message"]:
            update = Update.de_json(data, telegram_app.bot)
            if update and update.message:
                await telegram_app.process_update(update)
        elif "callback_query" in data:
            update = Update.de_json(data, telegram_app.bot)
            if update and update.callback_query:
                await telegram_app.process_update(update)
    except Exception as e:
        logger.error(f"Error pada webhook: {e}")
    return {"status": "ok"}

@app.get("/")
async def health_check():
    return Response(content="Bot is running smoothly!", status_code=200)

@app.on_event("startup")
async def startup_event():
    global telegram_app
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN belum diisi di environment Railway.")
    telegram_app = Application.builder().token(TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("stop", stop_command))
    telegram_app.add_handler(CommandHandler("stopemail", stop_email_command))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    await telegram_app.initialize()
    await telegram_app.start()
    logger.info("Bot Telegram webhook siap menerima koneksi di Railway...")
