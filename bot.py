import os
import random
import string
import asyncio
import re
from urllib.parse import quote
import aiohttp
import logging
from fastapi import FastAPI, Request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "BOT_TOKENMU")
GROUP_ID = int(os.getenv("GROUP_ID", "-1003928341140"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@esimjwf")
ADMIN_ID = 1294583646

# --- RapidAPI Temporary Gmail Account specific config ---
RAPIDAPI_KEY = (
    os.getenv("RAPIDAPI_KEY")
    or os.getenv("APIKEY_MASTER")
    or os.getenv("RUN2MAIL_API_KEY")
    or os.getenv("TEMPMAIL_API_KEY")
    or ""
).strip()

RAPIDAPI_HOST = (
    os.getenv("RAPIDAPI_HOST")
    or os.getenv("EMAIL_PROVIDER_BASE_URL")
    or os.getenv("RUN2MAIL_BASE_URL")
    or os.getenv("TEMPMAIL_BASE_URL")
    or "temporary-gmail-account.p.rapidapi.com"
).strip().replace("https://", "").replace("http://", "").rstrip("/")

EMAIL_PROVIDER_BASE_URL = f"https://{RAPIDAPI_HOST}"
EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT") or os.getenv("RUN2MAIL_EMAIL") or ""
EMAIL_INBOX_ID = os.getenv("EMAIL_INBOX_ID") or os.getenv("RUN2MAIL_INBOX_ID") or ""

app = FastAPI()
telegram_app = None

# Variabel global untuk melacak status loop aktif per chat/user
active_loops = set()

def sensor_text(text):
    if not text or len(text) <= 3: return "***"
    return text[:-3] + "***"

async def is_user_joined(context, user_id):
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception:
        return False

class Run2MailBot:
    def __init__(self):
        self.base_url = EMAIL_PROVIDER_BASE_URL.rstrip("/")
        self.api_key = RAPIDAPI_KEY
        self.email = EMAIL_ACCOUNT
        self.inbox_id = EMAIL_INBOX_ID
        self.token = os.getenv("TEMP_GMAIL_TOKEN", "")
        self.auth_mode = "rapidapi"
        self.headers = {"Accept": "application/json"}

        if self.api_key:
            self.headers["x-rapidapi-key"] = self.api_key
            self.headers["x-rapidapi-host"] = RAPIDAPI_HOST
            self.headers["Content-Type"] = "application/json"

    def _extract_email_from_payload(self, payload):
        if not isinstance(payload, dict):
            return None
        for key in ("email", "address", "username", "mail", "account", "gmail"):
            value = payload.get(key)
            if isinstance(value, str) and "@" in value:
                return value
        for nested_key in ("data", "result", "account", "accountDetails"):
            nested = payload.get(nested_key)
            if isinstance(nested, dict):
                email = self._extract_email_from_payload(nested)
                if email:
                    return email
        return None

    def _extract_token_from_payload(self, payload):
        if not isinstance(payload, dict):
            return None
        for key in ("token", "accessToken", "generatedToken", "authToken"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        data = payload.get("data")
        if isinstance(data, dict):
            return self._extract_token_from_payload(data)
        return None

    def _extract_messages_from_payload(self, payload):
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        for key in ("messages", "data", "result", "items", "message"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                nested = self._extract_messages_from_payload(val)
                if nested:
                    return nested
        return []

    def _candidate_urls(self, kind):
        base = self.base_url.rstrip("/")
        if kind == "create":
            return [
                f"{base}/GmailGetAccount",
                f"{base}/GetAccount",
                f"{base}/api/v1/account",
                f"{base}/api/v1/emails/create",
            ]
        if kind == "messages":
            return [
                f"{base}/GmailGetMessages",
                f"{base}/GetMessages",
                f"{base}/GmailGetMessage",
                f"{base}/GetMessage",
                f"{base}/api/v1/messages",
            ]
        return [base]

    def _normalize_text(self, value):
        if value is None:
            return ""
        if isinstance(value, list):
            return "\n".join(str(x) for x in value)
        if isinstance(value, dict):
            return str(value)
        return str(value)

    def _extract_items(self, payload):
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []

        data = payload.get("data") if isinstance(payload.get("data"), (dict, list)) else payload
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("messages", "items", "result", "emails", "inboxes"):
                val = data.get(key)
                if isinstance(val, list):
                    return val
            if isinstance(data.get("message"), list):
                return data.get("message")

        for key in ("messages", "items", "result", "emails"):
            if isinstance(payload.get(key), list):
                return payload.get(key)
        return []

    async def _request(self, method, url, json=None, params=None):
        headers = dict(self.headers)
        if json is not None:
            headers["Content-Type"] = "application/json"

        if self.auth_mode == "rapidapi":
            headers.setdefault("x-rapidapi-key", self.api_key)
            headers.setdefault("x-rapidapi-host", self.base_url.replace("https://", "").replace("http://", "").rstrip("/"))

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.request(method, url, json=json, params=params) as r:
                text = await r.text()
                try:
                    return await r.json(content_type=None)
                except Exception:
                    return {"status": r.status, "raw": text}

    async def create_account(self, status_callback=None):
        if self.email:
            logger.info(f"Inbox email yang dipakai: {self.email}")
            return

        if not self.api_key:
            raise RuntimeError("RAPIDAPI_KEY belum diisi di Railway / environment.")

        payload = {"generateNewAccount": 0}
        last_error = None
        attempts = 0

        while attempts < 10:
            attempts += 1
            for url in self._candidate_urls("create"):
                try:
                    resp = await self._request("POST", url, json=payload)
                    email = self._extract_email_from_payload(resp)
                    token = self._extract_token_from_payload(resp)

                    if not email:
                        last_error = resp
                        continue

                    if "+" in email.split("@", 1)[0]:
                        logger.warning(
                            "Email temporari hasil RapidAPI mengandung '+' (%s). Membatalkan dan generate ulang inbox baru.",
                            email,
                        )
                        if status_callback:
                            await status_callback(
                                f"⚠️ [EMAIL] Inbox hasil RapidAPI mengandung '+' ({email}). Membatalkan dan generate ulang inbox baru..."
                            )
                        last_error = {"reason": "plus_sign_detected", "email": email}
                        break

                    if token:
                        self.token = token
                    self.email = email
                    logger.info("Inbox email dibuat dari Temporary Gmail Account: %s", self.email)
                    return
                except Exception as e:
                    last_error = {"error": str(e), "url": url}

            if last_error and isinstance(last_error, dict) and last_error.get("reason") == "plus_sign_detected":
                logger.info("Retry generate account karena email mengandung '+'; percobaan ke-%s/10", attempts)
                if status_callback:
                    await status_callback(
                        f"🔄 [EMAIL] Mencoba generate inbox baru karena email invalid: {last_error.get('email')} (percobaan {attempts}/10)"
                    )
                await asyncio.sleep(1)
                continue

            if last_error and isinstance(last_error, dict) and last_error.get("error"):
                logger.warning("Create account gagal, retrying... detail=%s", last_error)
                if status_callback:
                    await status_callback(f"⚠️ [EMAIL] Generate inbox gagal, retrying... detail: {last_error}")
                await asyncio.sleep(1)
                continue

            break

        raise RuntimeError(f"Temporary Gmail Account gagal membuat inbox. Response: {last_error}")

    async def _get_messages(self):
        if not self.email:
            await self.create_account()

        if not self.token:
            raise RuntimeError("Token Temporary Gmail Account belum tersedia. Silakan cek response dari GmailGetAccount.")

        payload = {"address": self.email, "token": self.token}
        last_error = None

        for url in self._candidate_urls("messages"):
            try:
                resp = await self._request("POST", url, json=payload)
                items = self._extract_messages_from_payload(resp)
                if items:
                    return items
                last_error = resp
            except Exception as e:
                last_error = {"error": str(e), "url": url}

        return []

    async def fetch_otp(self, timeout=60):
        start_time = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start_time) < timeout:
            try:
                messages = await self._get_messages()
                for msg in messages:
                    raw_text = self._normalize_text(msg.get('text') or msg.get('body') or msg.get('content') or msg.get('plain') or '')
                    raw_html = self._normalize_text(msg.get('html') or msg.get('body_html') or msg.get('content_html') or '')
                    subject = self._normalize_text(msg.get('subject') or msg.get('title') or '')
                    combined_content = f"{subject} {raw_text} {raw_html}"

                    match = re.search(r'(?:otp\s*code|kode\s*konfirmasi|otp)[:\s\-]*([A-Za-z0-9]{6})', combined_content, re.IGNORECASE)
                    if match:
                        logger.info(f"OTP berhasil dibaca dari Run2Mail: {match.group(1)}")
                        return match.group(1).strip()

                    words = re.findall(r'\b[A-Z0-9]{6}\b', combined_content)
                    for w in words:
                        if not any(x in w.lower() for x in ['emalupe', 'mail', 'http', 'com', 'co.id', 'xlsmart']):
                            return w
            except Exception as e:
                logger.error(f"Error saat fetch OTP dari Run2Mail: {e}")
            await asyncio.sleep(0.5)
        return None

    async def fetch_xl_confirmation_email(self, timeout=60):
        start_time = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start_time) < timeout:
            try:
                messages = await self._get_messages()
                for msg in messages:
                    raw_text = self._normalize_text(msg.get('text') or msg.get('body') or msg.get('content') or msg.get('plain') or '')
                    raw_html = self._normalize_text(msg.get('html') or msg.get('body_html') or msg.get('content_html') or '')
                    subject = self._normalize_text(msg.get('subject') or msg.get('title') or '')

                    if not raw_text.strip() and raw_html.strip():
                        raw_text = re.sub('<[^<]+?>', '', raw_html)

                    combined_content = f"{subject}\n{raw_text}\n{raw_html}"
                    if 'MSISDN' in combined_content or 'Activation Code' in combined_content or 'eSIM' in combined_content:
                        logger.info("Email eSIM XL ditemukan di Run2Mail, mengekstrak detail...")

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
                logger.error(f"Error saat ekstrak detail email XL dari Run2Mail: {e}")
            await asyncio.sleep(2)
        return f"Email konfirmasi dari Run2Mail belum diterima / timeout, akun terdaftar: {self.email}", None, None, None, None


MailTMBot = Run2MailBot

async def process_xl_esim(chat_id, status_callback):
    temp = MailTMBot()
    await temp.create_account(status_callback=status_callback)

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

                try:
                    await page.locator("input[type='checkbox']").first.check(timeout=10000)
                except Exception:
                    try:
                        await page.get_by_role("checkbox").first.check(timeout=10000)
                    except Exception:
                        await page.evaluate("""() => {
                            const cb = Array.from(document.querySelectorAll('input[type="checkbox"], input[type="radio"], label'))
                                .find(el => {
                                    const text = (el.labels && el.labels.length ? Array.from(el.labels).map(l => l.innerText).join(' ') : el.innerText || '').toLowerCase();
                                    return text.includes('term') || text.includes('syarat') || text.includes('condition') || text.includes('agreement');
                                });
                            if (cb) {
                                if (cb.type === 'checkbox' || cb.type === 'radio') {
                                    cb.checked = true;
                                    cb.click();
                                } else if (cb.tagName === 'LABEL') {
                                    cb.click();
                                }
                            }
                        }""")

            except Exception as e:
                logger.error(f"Error isi data: {e}")
                raise Exception("Error: Form input tidak ditemukan.")

            logger.info("Kirim OTP...")
            await status_callback("📤 [LOG: 4/7] Mengirim permintaan OTP...")
            try:
                await page.get_by_role("button", name="Lanjut").click(timeout=15000)
            except Exception:
                await page.click("button:has-text('Lanjut'), button:has-text('Kirim')")

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
        [InlineKeyboardButton("🚀 Mulai Claim Esim", callback_data="start_claim")],
        [InlineKeyboardButton("🔄 Claim Loop", callback_data="start_claim_loop")],
        [InlineKeyboardButton("💰 Support Owner", callback_data="donation")],
        [InlineKeyboardButton("🎦 Bot Alight Motion", url="https://t.me/amforariey_bot")],
        [InlineKeyboardButton("🗨️ Channel Update", url="https://t.me/forarieyproject")]
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

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == "check_join":
        if await is_user_joined(context, user_id):
            keyboard = [
                [InlineKeyboardButton("🚀 Mulai Claim Esim", callback_data="start_claim")],
                [InlineKeyboardButton("🔄 Claim Loop", callback_data="start_claim_loop")],
                [InlineKeyboardButton("💰 Support Owner", callback_data="donation")],
                [InlineKeyboardButton("🎦 Bot Alight Motion", url="https://t.me/amforariey_bot")],
                [InlineKeyboardButton("🗨️ Channel Update", url="https://t.me/forarieyproject")]
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
    telegram_app = Application.builder().token(TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("stop", stop_command))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    await telegram_app.initialize()
    await telegram_app.start()
    logger.info("Bot Telegram webhook siap menerima koneksi di Railway...")
