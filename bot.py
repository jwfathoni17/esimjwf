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
    """Run2Mail API client.

    Flow:
      1. POST /api/v1/emails/create -> create a temporary inbox.
      2. GET /api/v1/emails/{email}/messages -> poll incoming messages.

    Set RUN2MAIL_API_KEY in the environment. Never hard-code the API key.
    """

    BASE_URL = "https://run2mail.com/api/v1"

    def __init__(self):
        self.email = ""
        self.api_key = os.getenv("RUN2MAIL_API_KEY", "").strip()
        if not self.api_key:
            raise RuntimeError("RUN2MAIL_API_KEY belum diset.")

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _extract_email(data):
        """Extract an email address from the Run2Mail response."""
        if isinstance(data, str):
            candidates = [data]
        else:
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
            match = re.search(
                r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                candidate,
            )
            if match:
                return match.group(0)
        return None

    async def create_account(self):
        url = f"{self.BASE_URL}/emails/create"
        payload = {"type": "random"}

        logger.info("Run2Mail -> POST /emails/create")

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            ) as session:
                async with session.post(
                    url,
                    headers=self._headers(),
                    json=payload,
                ) as r:
                    raw = await r.text()

                    try:
                        data = json.loads(raw)
                    except Exception:
                        data = raw

                    if r.status != 200:
                        raise RuntimeError(
                            f"Run2Mail /emails/create gagal ({r.status}): {raw[:1000]}"
                        )

                    if isinstance(data, dict) and data.get("success") is False:
                        raise RuntimeError(
                            f"Run2Mail menolak pembuatan inbox: {raw[:1000]}"
                        )

                    email = self._extract_email(data)
                    if not email:
                        raise RuntimeError(
                            f"Run2Mail tidak mengembalikan alamat email: {raw[:1000]}"
                        )

                    self.email = email
                    logger.info(f"Run2Mail inbox berhasil dibuat: {self.email}")
                    return self.email

        except aiohttp.ClientError as e:
            raise RuntimeError(f"Koneksi ke Run2Mail gagal: {e}") from e

    async def _get_messages(self, session):
        if not self.email:
            raise RuntimeError("Email Run2Mail belum dibuat.")

        # Email dimasukkan ke URL sebagai path segment.
        from urllib.parse import quote
        encoded_email = quote(self.email, safe="")
        url = f"{self.BASE_URL}/emails/{encoded_email}/messages"

        async with session.get(
            url,
            headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            raw = await r.text()

            try:
                data = json.loads(raw)
            except Exception:
                data = None

            if r.status != 200:
                raise RuntimeError(
                    f"Run2Mail /emails/{{email}}/messages gagal "
                    f"({r.status}): {raw[:1000]}"
                )

            if isinstance(data, list):
                return data

            if isinstance(data, dict):
                # Dokumentasi Run2Mail memakai:
                # {"success": true, "data": {"email": "...", "messages": []}}
                container = data.get("data", data)

                if isinstance(container, dict):
                    messages = container.get("messages")
                    if isinstance(messages, list):
                        return messages

                    # Fallback untuk kemungkinan format API lain.
                    for key in ("emails", "mails", "results"):
                        value = container.get(key)
                        if isinstance(value, list):
                            return value

            return []

    @staticmethod
    def _content(msg):
        if not isinstance(msg, dict):
            return str(msg)

        parts = []
        for key in (
            "sender", "from", "to", "title", "subject",
            "body", "html", "text", "content", "snippet"
        ):
            value = msg.get(key)
            if value:
                parts.append(str(value))

        return "\n".join(parts)

    async def fetch_otp(self, timeout=120):
        if not self.email:
            raise RuntimeError("Email Run2Mail belum dibuat.")

        start_time = asyncio.get_event_loop().time()
        seen = set()
        last_error = None

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=25)
        ) as session:
            poll_no = 0

            while asyncio.get_event_loop().time() - start_time < timeout:
                poll_no += 1

                try:
                    messages = await self._get_messages(session)
                    logger.info(
                        f"Run2Mail poll #{poll_no}: {len(messages)} pesan "
                        f"untuk {self.email}"
                    )

                    for msg in messages:
                        msg_id = ""
                        if isinstance(msg, dict):
                            msg_id = str(
                                msg.get("id")
                                or msg.get("message_id")
                                or msg.get("uid")
                                or ""
                            )

                        if msg_id and msg_id in seen:
                            continue
                        if msg_id:
                            seen.add(msg_id)

                        content = self._content(msg)

                        if isinstance(msg, dict):
                            sender = msg.get("sender", msg.get("from", ""))
                            title = msg.get("title", msg.get("subject", ""))
                            logger.info(
                                f"Run2Mail email: from={sender} subject={title!r}"
                            )

                        # Prioritaskan pola OTP yang eksplisit.
                        match = re.search(
                            r"(?:otp|verification\s*code|security\s*code|"
                            r"kode\s*(?:otp|verifikasi|konfirmasi))"
                            r"\s*(?:is|adalah|:|-)?\s*([0-9]{4,8})",
                            content,
                            re.IGNORECASE,
                        )

                        if match:
                            otp = match.group(1)
                            logger.info(f"OTP Run2Mail ditemukan: {otp}")
                            return otp

                        # Fallback: kode 6 digit.
                        candidates = re.findall(
                            r"(?<!\d)\d{6}(?!\d)",
                            content,
                        )
                        if candidates:
                            logger.info(
                                f"OTP 6 digit Run2Mail ditemukan: {candidates[0]}"
                            )
                            return candidates[0]

                except Exception as e:
                    last_error = e
                    logger.error(f"Error fetch OTP Run2Mail: {e}")

                await asyncio.sleep(3)

        raise TimeoutError(
            f"OTP tidak diterima dalam {timeout} detik untuk {self.email}. "
            f"Error API terakhir: {last_error}"
        )

    async def fetch_xl_confirmation_email(self, timeout=60):
        if not self.email:
            raise RuntimeError("Email Run2Mail belum dibuat.")

        start_time = asyncio.get_event_loop().time()
        last_error = None

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=25)
        ) as session:
            while asyncio.get_event_loop().time() - start_time < timeout:
                try:
                    messages = await self._get_messages(session)

                    for msg in messages:
                        content = self._content(msg)
                        haystack = content.lower()

                        if any(
                            x in haystack
                            for x in ("xl", "xlaxiata", "myxl")
                        ):
                            return content

                except Exception as e:
                    last_error = e
                    logger.error(
                        f"Error membaca email konfirmasi Run2Mail: {e}"
                    )

                await asyncio.sleep(3)

        raise TimeoutError(
            f"Email konfirmasi XL tidak diterima dalam {timeout} detik "
            f"untuk {self.email}. Error terakhir: {last_error}"
        )


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
            await status_callback("☑️ [LOG: 4/7] Data sudah diisi. Mencari & mencentang T&C...")

            # Urutan wajib: isi data -> centang T&C -> klik Lanjut -> baru polling OTP.
            checkbox = page.locator('input[type="checkbox"]').first
            if await page.locator('input[type="checkbox"]').count() == 0:
                # Beberapa build memakai label/custom checkbox. Cari teks persetujuan.
                label = page.locator("label").filter(
                    has_text=re.compile(r"syarat|ketentuan|menyetujui|terms|condition", re.I)
                ).first
                if await label.count() == 0:
                    raise Exception("Checkbox T&C tidak ditemukan setelah data diisi.")
                await label.click(timeout=5000, force=True, no_wait_after=True)
                logger.info("Label T&C diklik.")
            else:
                await checkbox.scroll_into_view_if_needed()
                if not await checkbox.is_checked():
                    await checkbox.check(timeout=5000, force=True)
                logger.info(f"Checkbox T&C checked={await checkbox.is_checked()}")

            # Jangan menekan Lanjut sebelum checkbox benar-benar checked.
            if await page.locator('input[type="checkbox"]').count() > 0:
                if not await page.locator('input[type="checkbox"]').first.is_checked():
                    raise Exception("Checkbox T&C masih belum tercentang; OTP tidak dikirim.")

            await status_callback("☑️ [LOG: 4/7] T&C tercentang. Menekan tombol Lanjut untuk mengirim OTP...")

            # Cari tombol Lanjut yang aktif dan klik tanpa menunggu navigasi penuh.
            buttons = page.get_by_role("button", name=re.compile(r"^Lanjut$", re.I))
            if await buttons.count() == 0:
                buttons = page.locator("button").filter(has_text=re.compile(r"^Lanjut$", re.I))
            if await buttons.count() == 0:
                raise Exception("Tombol Lanjut setelah checkbox T&C tidak ditemukan.")

            lanjut = buttons.last
            try:
                await lanjut.scroll_into_view_if_needed()
            except Exception:
                pass
            try:
                await lanjut.click(timeout=10000, no_wait_after=True)
            except Exception as click_error:
                logger.warning(f"Klik Lanjut normal timeout/gagal: {click_error}; mencoba DOM click.")
                result = await page.evaluate("""() => {
                    const bs = [...document.querySelectorAll('button')];
                    const b = bs.find(x => /^(Lanjut)$/i.test((x.innerText || '').trim()));
                    if (!b) return false;
                    b.click();
                    return true;
                }""")
                if not result:
                    raise Exception("Gagal menekan tombol Lanjut untuk mengirim OTP.")

            logger.info("Tombol Lanjut berhasil dipicu; sekarang baru menunggu OTP.")

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
                
            info = await temp.fetch_xl_confirmation_email(timeout=60)
                ms = pk = sm = ac = None
                
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
