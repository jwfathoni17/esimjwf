import os
import random
import string
import asyncio
import json
import re
import aiohttp
import logging
from datetime import datetime
from fastapi import FastAPI, Request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN = "8877836310:AAHeAf-cAx2ho6MttbnHdA302fTl8b4UzBU"
GROUP_ID = -1003928341140
CHANNEL_USERNAME = "@esimjwf" 
ADMIN_ID = 1294583646

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

class MailTMBot:
    """RapidAPI Temp Gmail API client.

    ONLY /get-email-dot is used to create addresses.
    The daily counter is informational and never blocks requests.
    """

    RAPIDAPI_KEY = "b9932d4795msh62a785bac6c469dp1da41ajsn7d45cb7133cf"
    RAPIDAPI_HOST = "temp-gmail-api.p.rapidapi.com"
    BASE_URL = "https://temp-gmail-api.p.rapidapi.com"

    def __init__(self):
        self.email = ""
        self.daily_count = self._load_counter()

    def _counter_path(self):
        return os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "rapidapi_usage.json"
        )

    def _load_counter(self):
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with open(self._counter_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == today:
                return {"date": today, "count": int(data.get("count", 0))}
        except Exception:
            pass
        return {"date": today, "count": 0}

    def _save_counter(self):
        try:
            with open(self._counter_path(), "w", encoding="utf-8") as f:
                json.dump(self.daily_count, f, indent=2)
        except Exception as e:
            logger.warning(f"Gagal menyimpan counter RapidAPI: {e}")

    def _increment_counter(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self.daily_count.get("date") != today:
            self.daily_count = {"date": today, "count": 0}
        self.daily_count["count"] += 1
        self._save_counter()

    def usage_text(self):
        today = datetime.now().strftime("%d-%m-%Y")
        count = self.daily_count.get("count", 0)
        return (
            "📊 <b>RapidAPI Temp Gmail</b>\n"
            "━━━━━━━━━━━━━━\n"
            f"📧 Email /get-email-dot: <b>{count}</b>\n"
            "🎯 Batas referensi: <b>10</b>\n"
            "⚠️ Monitoring saja — <b>tidak membatasi</b>\n"
            f"📅 {today}\n"
            "━━━━━━━━━━━━━━"
        )

    def _headers(self):
        return {
            "X-RapidAPI-Key": self.RAPIDAPI_KEY,
            "X-RapidAPI-Host": self.RAPIDAPI_HOST,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _extract_email(data):
        candidates = []

        def walk(value):
            if isinstance(value, str):
                candidates.append(value.strip())
            elif isinstance(value, dict):
                for v in value.values():
                    walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)

        walk(data)

        for candidate in candidates:
            if re.fullmatch(r"[A-Za-z0-9._-]+@gmail\.com", candidate) and "+" not in candidate:
                return candidate
        return None

    async def create_account(self):
        # IMPORTANT: this is deliberately the ONLY email-generation endpoint.
        url = f"{self.BASE_URL}/get-email-dot"

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20)
        ) as session:
            async with session.get(url, headers=self._headers()) as r:
                raw = await r.text()

                if r.status != 200:
                    raise RuntimeError(
                        f"RapidAPI /get-email-dot gagal ({r.status}): {raw[:500]}"
                    )

                try:
                    data = json.loads(raw)
                except Exception:
                    data = raw

                email = self._extract_email(data)

                if not email:
                    raise RuntimeError(
                        "RapidAPI /get-email-dot tidak mengembalikan Gmail dot "
                        f"yang valid: {raw[:1000]}"
                    )

                # Extra safety: never allow plus aliases into the XL form.
                if "+" in email or not email.lower().endswith("@gmail.com"):
                    raise RuntimeError(f"Email ditolak karena bukan Gmail dot: {email}")

                self.email = email
                self._increment_counter()

                logger.info(
                    f"RapidAPI /get-email-dot berhasil: {self.email} | "
                    f"pemakaian hari ini: {self.daily_count['count']}"
                )
                return self.email

    async def _get_messages(self, session):
        url = f"{self.BASE_URL}/get-mails"
        payload = {"email": self.email, "count": 10}

        async with session.post(
            url,
            headers=self._headers(),
            json=payload,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            raw = await r.text()

            if r.status != 200:
                raise RuntimeError(
                    f"RapidAPI /get-mails gagal ({r.status}): {raw[:500]}"
                )

            try:
                data = json.loads(raw)
            except Exception:
                return []

            if isinstance(data, list):
                return data

            if isinstance(data, dict):
                for key in ("emails", "mails", "messages", "results", "data"):
                    value = data.get(key)
                    if isinstance(value, list):
                        return value

            return []

    @staticmethod
    def _content(msg):
        if not isinstance(msg, dict):
            return str(msg)

        parts = []
        for key in ("sender", "from", "title", "subject", "body", "html", "text", "content"):
            value = msg.get(key)
            if value:
                parts.append(str(value))
        return "\n".join(parts)

    async def fetch_otp(self, timeout=120):
        if not self.email:
            raise RuntimeError("Email RapidAPI belum dibuat.")

        start_time = asyncio.get_event_loop().time()
        seen = set()

        async with aiohttp.ClientSession() as session:
            poll_no = 0

            while asyncio.get_event_loop().time() - start_time < timeout:
                poll_no += 1

                try:
                    messages = await self._get_messages(session)
                    logger.info(
                        f"RapidAPI poll #{poll_no}: {len(messages)} pesan "
                        f"untuk {self.email}"
                    )

                    for msg in messages:
                        msg_id = str(msg.get("id", "")) if isinstance(msg, dict) else ""

                        if msg_id and msg_id in seen:
                            continue
                        if msg_id:
                            seen.add(msg_id)

                        content = self._content(msg)

                        if isinstance(msg, dict):
                            sender = msg.get("sender", msg.get("from", ""))
                            title = msg.get("title", msg.get("subject", ""))
                            logger.info(
                                f"RapidAPI email: from={sender} subject={title!r}"
                            )

                        match = re.search(
                            r"(?:otp|verification\s*code|security\s*code|"
                            r"kode\s*(?:otp|verifikasi|konfirmasi))"
                            r"\s*(?:is|adalah|:)?\s*([0-9]{4,8})",
                            content,
                            re.IGNORECASE,
                        )

                        if match:
                            otp = match.group(1)
                            logger.info(f"OTP RapidAPI ditemukan: {otp}")
                            return otp

                        candidates = re.findall(r"(?<!\d)\d{6}(?!\d)", content)
                        if candidates:
                            logger.info(f"OTP 6 digit RapidAPI ditemukan: {candidates[0]}")
                            return candidates[0]

                except Exception as e:
                    logger.error(f"Error fetch OTP RapidAPI: {e}")

                await asyncio.sleep(3)

        raise TimeoutError(
            f"OTP tidak diterima dalam {timeout} detik untuk {self.email}"
        )

    async def fetch_xl_confirmation_email(self, timeout=120):
        if not self.email:
            raise RuntimeError("Email RapidAPI belum dibuat.")

        start_time = asyncio.get_event_loop().time()

        async with aiohttp.ClientSession() as session:
            while asyncio.get_event_loop().time() - start_time < timeout:
                try:
                    messages = await self._get_messages(session)

                    for msg in messages:
                        content = self._content(msg)
                        haystack = content.lower()

                        if any(x in haystack for x in ("xl", "xlaxiata", "myxl")):
                            return content

                except Exception as e:
                    logger.error(f"Error membaca email konfirmasi RapidAPI: {e}")

                await asyncio.sleep(3)

        raise TimeoutError("Email konfirmasi XL tidak diterima.")


async def process_xl_esim(chat_id, status_callback):
    temp = MailTMBot()
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

            # Banner cookie dapat menutupi tombol form dan membuat Playwright
            # menunggu sampai timeout. Tutup/terima bila muncul.
            try:
                cookie_btn = page.get_by_role("button", name=re.compile(r"^(Setuju|Accept|I agree)$", re.I))
                if await cookie_btn.count() > 0:
                    await cookie_btn.first.click(timeout=3000, no_wait_after=True)
                    await asyncio.sleep(0.5)
                    logger.info("Banner cookie ditutup.")
            except Exception:
                pass

            logger.info("Klik mulai...")
            await status_callback("🖱️ [LOG: 2/7] Klik tombol mulai...")
            try:
                await page.wait_for_selector("text=Mulai Isi Data", timeout=20000)
                await page.get_by_text("Mulai Isi Data").first.click(timeout=10000, no_wait_after=True)
            except Exception:
                await page.locator("button").filter(has_text=re.compile(r"Mulai Isi Data", re.I)).first.click(timeout=5000, no_wait_after=True)
            
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

            logger.info("Kirim OTP...")
            await status_callback("📤 [LOG: 4/7] Menyetujui Syarat & Ketentuan lalu mengirim OTP...")

            # Form XL memiliki checkbox persetujuan T&C sebelum tombol Lanjut/Kirim
            # dapat diproses. Cari checkbox yang terlihat dan belum dicentang,
            # lalu centang melalui Playwright. Jika checkbox tidak ditemukan,
            # lanjutkan karena pada sebagian versi halaman consent bisa sudah tersimpan.
            try:
                checkboxes = page.locator('input[type="checkbox"]')
                checkbox_count = await checkboxes.count()
                checked_one = False

                for i in range(checkbox_count):
                    cb = checkboxes.nth(i)
                    try:
                        if await cb.is_visible() and not await cb.is_checked():
                            await cb.check(timeout=5000, force=True)
                            checked_one = True
                            logger.info(f"Checkbox T&C ditemukan dan dicentang (index={i}).")
                            break
                    except Exception as cb_error:
                        logger.warning(f"Checkbox index={i} gagal dicentang: {cb_error}")

                # Fallback: klik label terkait checkbox jika input tersembunyi/styled.
                if not checked_one:
                    labels = page.locator("label")
                    for i in range(await labels.count()):
                        label = labels.nth(i)
                        try:
                            if not await label.is_visible():
                                continue
                            label_text = (await label.inner_text()).strip().lower()
                            if any(key in label_text for key in (
                                "syarat", "ketentuan", "terms", "condition",
                                "saya telah membaca", "menyetujui"
                            )):
                                await label.click(timeout=5000, force=True, no_wait_after=True)
                                checked_one = True
                                logger.info("Label checkbox T&C ditemukan dan diklik.")
                                break
                        except Exception as label_error:
                            logger.warning(f"Label T&C gagal diklik: {label_error}")

                await asyncio.sleep(0.7)
            except Exception as consent_error:
                logger.warning(f"Proses checkbox T&C gagal/tidak diperlukan: {consent_error}")

            # Pastikan banner cookie/consent tidak menghalangi tombol.
            try:
                for name in [r"^(Setuju|Accept|I agree)$", r"^Saya setuju$"]:
                    cb = page.get_by_role("button", name=re.compile(name, re.I))
                    if await cb.count() > 0:
                        await cb.first.click(timeout=2000, no_wait_after=True)
                        await asyncio.sleep(0.5)
                        break
            except Exception:
                pass

            # Verifikasi singkat: bila masih ada checkbox T&C yang terlihat dan belum
            # dicentang, centang sekali lagi sebelum menekan tombol OTP.
            try:
                for i in range(await page.locator('input[type="checkbox"]').count()):
                    cb = page.locator('input[type="checkbox"]').nth(i)
                    if await cb.is_visible() and not await cb.is_checked():
                        await cb.check(timeout=3000, force=True)
                        logger.info(f"Checkbox T&C masih unchecked; dicentang ulang (index={i}).")
            except Exception as verify_error:
                logger.warning(f"Verifikasi checkbox T&C dilewati: {verify_error}")

            # Klik tombol pengiriman OTP tanpa menunggu navigasi penuh.
            # Situs XL dapat menahan request/navigation sehingga click() default
            # Playwright bisa berakhir dengan "Timeout 30000ms exceeded".
            clicked = False
            for locator in [
                page.get_by_role("button", name=re.compile(r"^(Lanjut|Kirim)$", re.I)),
                page.locator("button").filter(has_text=re.compile(r"^(Lanjut|Kirim)$", re.I)),
                page.locator("button").filter(has_text=re.compile(r"Lanjut|Kirim", re.I)),
            ]:
                try:
                    await locator.first.click(timeout=8000, no_wait_after=True)
                    clicked = True
                    break
                except Exception as click_error:
                    logger.warning(f"Percobaan klik tombol OTP gagal: {click_error}")
            if not clicked:
                # Fallback JS click, tidak menunggu navigation.
                result = await page.evaluate("""() => {
                    const btn = [...document.querySelectorAll('button')]
                      .find(b => /^(Lanjut|Kirim)$/i.test((b.innerText || '').trim()));
                    if (btn) { btn.click(); return true; }
                    return false;
                }""")
                if not result:
                    raise Exception("Tombol Lanjut/Kirim untuk meminta OTP tidak ditemukan.")

            logger.info("Menunggu OTP...")
            await status_callback(f"⏳ [LOG: 5/7] Menunggu OTP masuk ke `{temp.email}` (maks. 120 detik)...")
            otp = await temp.fetch_otp(timeout=120)
            
            if not otp: 
                await page.screenshot(path=debug_path, timeout=15000)
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
            clicked = False
            for locator in [
                page.get_by_role("button", name=re.compile(r"^(Lanjut|Konfirmasi)$", re.I)),
                page.locator("button").filter(has_text=re.compile(r"Lanjut|Konfirmasi", re.I)),
            ]:
                try:
                    await locator.first.click(timeout=8000, no_wait_after=True)
                    clicked = True
                    break
                except Exception as click_error:
                    logger.warning(f"Klik konfirmasi OTP gagal: {click_error}")
            if not clicked:
                result = await page.evaluate("""() => {
                    const btn = [...document.querySelectorAll('button')]
                      .find(b => /Lanjut|Konfirmasi/i.test((b.innerText || '').trim()));
                    if (btn) { btn.click(); return true; }
                    return false;
                }""")
                if not result:
                    raise Exception("Tombol konfirmasi OTP tidak ditemukan.")

            logger.info("Pilih nomor...")
            await status_callback("📱 [LOG: 6/7] Menunggu dan memilih nomor eSIM...")
            
            try:
                await page.wait_for_selector('input[type="radio"], label, .number-card, text=/08/', timeout=12000)
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
            await page.screenshot(path=screenshot_path, full_page=True, timeout=30000)
            await browser.close()
            
            if os.path.exists(debug_path):
                os.remove(debug_path)
                
            info, ms, pk, sm, ac = await temp.fetch_xl_confirmation_email(timeout=60)
                
            return screenshot_path, info, ms, pk, sm, ac

        except Exception as e:
            logger.error(f"Error di proses utama: {e}")
            try:
                await page.screenshot(path=debug_path, timeout=15000)
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
        [InlineKeyboardButton("🚀 Mulai Claim eSIM", callback_data="start_claim")],
        [InlineKeyboardButton("🔄 Claim Loop eSIM", callback_data="start_claim_loop")]
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
                [InlineKeyboardButton("🚀 Mulai Claim eSIM", callback_data="start_claim")],
                [InlineKeyboardButton("🔄 Claim Loop eSIM", callback_data="start_claim_loop")]
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
