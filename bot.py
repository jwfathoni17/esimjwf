import os
import random
import string
import asyncio
import re
import aiohttp
import logging
from fastapi import FastAPI, Request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GROUP_ID= -1001234567890
CHANNEL_USERNAME= "@esimjwf"
ADMIN_ID= 1294583646
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "")

app = FastAPI()
telegram_app = None

# Variabel global untuk melacak status loop aktif per chat/user
active_loops = set()
manual_email_pending = set()

def sensor_text(text):
    if not text or len(text) <= 3: return "***"
    return text[:-3] + "***"

def claim_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Mulai Claim Esim", callback_data="start_claim")],
        [InlineKeyboardButton("✍️ Manual Esim", callback_data="manual_esim")],
        [InlineKeyboardButton("🔄 Claim Loop", callback_data="start_claim_loop")],
        [InlineKeyboardButton("💰 Support Owner", callback_data="donation")],
        [InlineKeyboardButton("🎦 Bot Alight Motion", url="https://t.me/amforariey_bot")],
        [InlineKeyboardButton("🗨️ Channel Update", url="https://t.me/forarieyproject")]
    ])

async def dismiss_cookie_popup(page):
    cookie_button = page.get_by_role("button", name=re.compile(r"^\s*Setuju\s*$", re.IGNORECASE)).first
    if await cookie_button.count() == 0:
        cookie_button = page.locator("button").filter(
            has_text=re.compile(r"^\s*Setuju\s*$", re.IGNORECASE)
        ).first

    if await cookie_button.count() > 0 and await cookie_button.is_visible():
        try:
            await cookie_button.click(force=True, timeout=5000)
            await asyncio.sleep(0.5)
            logger.info("Popup cookie ditutup")
        except Exception as e:
            logger.warning(f"Popup cookie ditemukan tetapi gagal ditutup: {e}")

async def is_user_joined(context, user_id):
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception:
        return False

class GmailifyBot:
    def __init__(self):
        self.api_url = "https://api.apify.com/v2/acts/dev00~gmailify-premium-temp-gmail-generator/run-sync-get-dataset-items"
        self.email = ""

    async def _call_api(self, payload):
        if not APIFY_API_TOKEN:
            raise RuntimeError("APIFY_API_TOKEN belum dikonfigurasi")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.api_url,
                json=payload,
                params={"token": APIFY_API_TOKEN}
            ) as r:
                data = await r.json()
                if r.status >= 400:
                    error = data.get("error") if isinstance(data, dict) else None
                    raise RuntimeError(error or f"Apify HTTP {r.status}")

                items = data if isinstance(data, list) else [data]
                return [item for item in items if item.get("success", True)]

    async def create_account(self):
        result = await self._call_api({"endpoint": "generate", "provider": "gmail"})
        if result:
            self.email = result[0].get('email', '')
        if not self.email:
            raise RuntimeError("Gmailify tidak mengembalikan alamat email")
        logger.info(f"Akun Gmailify dibuat: {self.email}")

    async def fetch_otp(self, timeout=60):
        start_time = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start_time) < timeout:
            try:
                result = await self._call_api({"endpoint": "check-emails", "email": self.email})
                if result:
                    for item in result:
                        subject = item.get('subject', '') or ''
                        match = re.search(r'(?:otp\s*code|kode\s*konfirmasi|otp|verification\s*code)(?:\s+is)?[:\s\-]*([A-Za-z0-9]{6})', subject, re.IGNORECASE)
                        if match:
                            logger.info(f"OTP berhasil dibaca: {match.group(1)}")
                            return match.group(1).strip()
                        words = re.findall(r'\b[A-Z0-9]{6}\b', subject)
                        if words:
                            for w in words:
                                if not any(x in w.lower() for x in ['emalupe', 'mail', 'http', 'com', 'co.id', 'xlsmart']):
                                    return w
            except Exception as e:
                logger.error(f"Error saat fetch OTP: {e}")
            await asyncio.sleep(2)
        return None

    async def fetch_xl_confirmation_email(self, timeout=60):
        start_time = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start_time) < timeout:
            try:
                result = await self._call_api({"endpoint": "check-emails", "email": self.email})
                if result:
                    for item in result:
                        subject = item.get('subject', '') or ''
                        sender = item.get('sender', '') or ''
                        combined_content = " ".join(str(item.get(key, '') or '') for key in (
                            'subject', 'sender', 'email_address', 'body', 'text'
                        ))
                        
                        if 'MSISDN' in combined_content or 'Activation Code' in combined_content or 'eSIM' in combined_content or 'XL' in combined_content:
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

async def process_xl_esim(chat_id, status_callback, email=None):
    temp = GmailifyBot()
    if email:
        temp.email = email
        logger.info(f"Menggunakan email manual: {temp.email}")
    else:
        await temp.create_account()

    full_name = f"mhmdsari{''.join(random.choices(string.ascii_lowercase + string.digits, k=4))}xlstore"
    whatsapp = "08" + ''.join(random.choices(string.digits, k=9))
    
    screenshot_path = f"esim_{chat_id}.png"
    debug_path = f"debug_{chat_id}.png"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page(viewport={"width": 1366, "height": 768})
        
        try:
            logger.info("Membuka halaman XL...")
            await status_callback("🌐 [LOG: 1/7] Membuka halaman XL eSIM Trial...")
            await page.goto("https://www.xl.co.id/esim-trial/claim", timeout=90000, wait_until="domcontentloaded")
            await dismiss_cookie_popup(page)

            logger.info("Klik mulai...")
            await status_callback("🖱️ [LOG: 2/7] Klik tombol mulai...")
            try:
                await page.wait_for_selector("text=Mulai Isi Data", timeout=20000)
                await page.get_by_text("Mulai Isi Data").first.click()
            except Exception:
                await page.click("button:has-text('Mulai Isi Data')", timeout=5000)
            
            await asyncio.sleep(2)

            logger.info("Isi data...")
            await status_callback("📝 [LOG: 3/7] Mengisi data diri otomatis...")
            try:
                inputs = await page.locator("input").all()
                if len(inputs) >= 3:
                    await inputs[0].fill(full_name)
                    await inputs[1].fill(temp.email)
                    await inputs[2].fill(whatsapp)
                else:
                    raise Exception("Gagal mendeteksi input form")
            except Exception as e:
                logger.error(f"Error isi data: {e}")
                raise Exception("Error: Form input tidak ditemukan.")

            logger.info("Menyetujui Syarat & Ketentuan serta Kebijakan Privasi...")
            try:
                checkbox = page.locator("input[type='checkbox']").first
                if await checkbox.count() == 0:
                    checkbox = page.get_by_role("checkbox").first
                if await checkbox.count() == 0:
                    raise Exception("Checkbox persetujuan tidak ditemukan")
                await checkbox.check(force=True)
            except Exception as e:
                logger.error(f"Error mencentang persetujuan: {e}")
                raise Exception("Error: Checklist Syarat & Ketentuan/Kebijakan Privasi tidak ditemukan.")

            logger.info("Kirim OTP...")
            await status_callback("📤 [LOG: 4/7] Mengirim permintaan OTP...")
            try:
                await dismiss_cookie_popup(page)
                lanjut_button = page.locator("button.btn-claim.claim-cta").last
                if await lanjut_button.count() == 0:
                    lanjut_button = page.get_by_role(
                        "button", name=re.compile(r"^\s*Lanjut\s*$", re.IGNORECASE)
                    ).last
                if await lanjut_button.count() == 0:
                    raise Exception("Tombol Lanjut tidak ditemukan")

                await lanjut_button.wait_for(state="visible", timeout=15000)
                await lanjut_button.scroll_into_view_if_needed()
                if not await lanjut_button.is_enabled():
                    raise Exception("Tombol Lanjut masih nonaktif")

                button_box = await lanjut_button.bounding_box()
                if not button_box:
                    raise Exception("Posisi tombol Lanjut tidak tersedia")

                await lanjut_button.evaluate("button => button.click()")
                await asyncio.sleep(1)

                if await lanjut_button.is_visible():
                    logger.warning("Klik DOM belum berpindah halaman, mencoba klik koordinat tombol")
                    await page.mouse.click(
                        button_box["x"] + button_box["width"] / 2,
                        button_box["y"] + button_box["height"] / 2
                    )
                    await asyncio.sleep(1)

                if await lanjut_button.is_visible():
                    raise Exception("Event tombol Lanjut tidak mengubah halaman")
            except Exception as e:
                button_html = "tidak tersedia"
                if await lanjut_button.count() > 0:
                    button_html = await lanjut_button.evaluate("button => button.outerHTML")
                logger.error(f"Error menekan tombol Lanjut: {e}; HTML: {button_html}")
                await page.screenshot(path=debug_path)
                raise Exception("Error: Tombol Lanjut tidak dapat ditekan.")

            logger.info("Menunggu OTP...")
            await status_callback(f"⏳ [LOG: 5/7] Menunggu OTP masuk ke `{temp.email}`...")
            otp = await temp.fetch_otp(timeout=60)
            
            if not otp: 
                await page.screenshot(path=debug_path)
                raise Exception("Error: Waktu tunggu OTP habis (Timeout).")
            
            logger.info(f"Input OTP: {otp}")
            await status_callback(f"✅ [LOG: OTP OK] Kode: `{otp}`. Memasukkan ke sistem...")
            
            try:
                await page.locator("input").first.click()
            except Exception:
                pass
            
            await page.keyboard.type(otp, delay=150)

            logger.info("Konfirmasi OTP...")
            await status_callback("📤 [LOG: Konfirmasi OTP] Menekan tombol Lanjut...")
            await asyncio.sleep(1.5)
            try:
                await page.get_by_role("button", name="Lanjut").click(timeout=10000)
            except Exception:
                await page.click("button:has-text('Lanjut'), button:has-text('Konfirmasi')")

            logger.info("Pilih nomor...")
            await status_callback("📱 [LOG: 6/7] Menunggu dan memilih nomor eSIM...")
            
            try:
                await page.wait_for_selector('input[type="radio"], label, .number-card, text=/08/', timeout=30000)
            except Exception:
                logger.warning("Timeout menunggu elemen pilihan nomor, mencoba lanjut paksa via evaluate...")

            await asyncio.sleep(3) 
            
            await page.evaluate("""() => {
                const radios = Array.from(document.querySelectorAll('input[type="radio"]'));
                if (radios.length > 0) {
                    radios[0].checked = true;
                    radios[0].click();
                    radios[0].dispatchEvent(new Event('change', { bubbles: true }));
                    return;
                }
                const candidates = Array.from(document.querySelectorAll('div, label, span, button')).filter(el => {
                    const text = el.innerText ? el.innerText.trim() : '';
                    return text.startsWith('08') && text.length >= 10 && text.length <= 15 && el.children.length <= 2;
                });
                if (candidates.length > 0) {
                    candidates[0].click();
                }
            }""")

            logger.info("Lanjut ke QR...")
            await status_callback("📤 [LOG: 7/7] Menekan tombol Lanjut...")
            await asyncio.sleep(2)

            await page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button, div[role="button"]'));
                const target = btns.find(b => b.innerText && (b.innerText.toLowerCase().includes('lanjut') || b.innerText.toLowerCase().includes('konfirmasi') || b.innerText.toLowerCase().includes('pilih')));
                if (target) {
                    target.click();
                }
            }""")

            logger.info("Proses akhir QR...")
            await status_callback("⏳ Sedang memproses eSIM di server XL (Menunggu QR & Email)...")
            await asyncio.sleep(10) 
            
            await status_callback("✨ QR Code berhasil dimuat! Mengambil screenshot & membaca detail email...")
            await page.screenshot(path=screenshot_path, full_page=True)
            await browser.close()
            
            if os.path.exists(debug_path):
                os.remove(debug_path)
                
            info, ms, pk, sm, ac = await temp.fetch_xl_confirmation_email(timeout=60)
                
            return screenshot_path, info, ms, pk, sm, ac

        except Exception as e:
            logger.error(f"Error di proses utama: {e}")
            try:
                await page.screenshot(path=debug_path)
                await browser.close()
            except Exception:
                pass
            return debug_path, str(e), None, None, None, None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    manual_email_pending.discard(update.effective_chat.id)
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

    await update.message.reply_text("👋 <b>Selamat datang di Bot Claim eSIM XL!</b>\nSilakan pilih menu di bawah:", 
                                   reply_markup=claim_menu(), parse_mode="HTML")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_loops:
        active_loops.remove(chat_id)
        await update.message.reply_text("🛑 <b>Claim Loop berhasil dihentikan!</b>", parse_mode="HTML")
    else:
        await update.message.reply_text("⚠️ Tidak ada proses Claim Loop yang sedang berjalan.", parse_mode="HTML")

async def manual_email_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in manual_email_pending:
        return

    email = update.message.text.strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        await update.message.reply_text("⚠️ Format email tidak valid. Kirim email yang benar, atau ketik /start untuk membatalkan.")
        return

    manual_email_pending.remove(chat_id)
    msg = await update.message.reply_text("🚀 Memproses Manual Esim dengan email yang kamu masukkan...")

    async def update_status(text):
        try:
            await context.bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=msg.message_id, parse_mode="HTML"
            )
        except Exception:
            pass

    user = update.effective_user
    username = f"@{user.username}" if user.username else user.first_name
    path, info, ms, pk, sm, ac = await process_xl_esim(chat_id, update_status, email=email)

    if path and "esim_" in path and os.path.exists(path):
        keyboard_claim = [[InlineKeyboardButton("🧩 Register Biometrik", url="https://registrasi.xl.co.id")]]
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=open(path, "rb"),
            caption=info,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard_claim)
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
            await context.bot.send_message(
                chat_id=GROUP_ID, text=grup_text, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard_claim)
            )
        os.remove(path)
    elif path and os.path.exists(path):
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=open(path, "rb"),
            caption=f"❌ <b>Gagal Memproses:</b>\n<pre>{info}</pre>",
            parse_mode="HTML"
        )
        os.remove(path)
    else:
        await context.bot.send_message(
            chat_id=chat_id, text=f"❌ <b>Gagal Memproses:</b>\n<pre>{info}</pre>", parse_mode="HTML"
        )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == "check_join":
        if await is_user_joined(context, user_id):
            await query.edit_message_text("✅ <b>Terima kasih!</b> Anda telah bergabung.\nSilakan gunakan fitur bot:", 
                                          reply_markup=claim_menu(), parse_mode="HTML")
        else:
            await query.answer("❌ Anda belum bergabung di channel, silakan klik tombol Join!", show_alert=True)

    elif query.data == "manual_esim":
        if not await is_user_joined(context, user_id):
            await query.message.reply_text("⚠️ Silakan join channel @forarieyproject terlebih dahulu!")
            return

        manual_email_pending.add(query.message.chat.id)
        await query.message.reply_text(
            "✍️ <b>Manual Esim</b>\n\nKirim email yang ingin digunakan untuk menerima OTP.\n"
            "Email ini akan dipakai tanpa generate email baru.\n\nKetik /start untuk membatalkan.",
            parse_mode="HTML"
        )

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
        raise RuntimeError("TELEGRAM_BOT_TOKEN belum dikonfigurasi")
    if not APIFY_API_TOKEN:
        raise RuntimeError("APIFY_API_TOKEN belum dikonfigurasi")

    telegram_app = Application.builder().token(TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("stop", stop_command))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manual_email_handler))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    await telegram_app.initialize()
    await telegram_app.start()
    logger.info("Bot Telegram webhook siap menerima koneksi di Railway...")
