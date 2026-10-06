import io
import re
import time

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageEnhance, ImageOps
import pytesseract

# Tesseract OCR executable (Windows)
pytesseract.pytesseract.tesseract_cmd = r"C:\\Program Files\\Tesseract-OCR\\tesseract.exe"

CHANNEL = "keydropcomofficial"
CHANNEL_URL = "https://t.me/s/" + CHANNEL
POLL_SECONDS = 300
RETRY_SECONDS = 30
HTTP_TIMEOUT = (30, 45)  # connect, read
CODE_RE = re.compile(r"\b[A-Z0-9]{12,24}\b")


class TelegramGoldWatcher:
    def __init__(self, log_callback, code_callback, poll_seconds=POLL_SECONDS):
        self.log = log_callback
        self.code_callback = code_callback
        self.poll_seconds = poll_seconds
        self.running = False
        self.last_post_id = None
        self.seen_codes = set()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})

    def stop(self):
        self.running = False

    def _posts(self):
        response = self.session.get(CHANNEL_URL, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        posts = []
        for wrap in soup.select(".tgme_widget_message_wrap"):
            msg = wrap.select_one(".tgme_widget_message")
            if not msg:
                continue
            ref = msg.get("data-post", "")
            try:
                post_id = int(ref.rsplit("/", 1)[1])
            except (ValueError, IndexError):
                continue
            text_el = wrap.select_one(".tgme_widget_message_text")
            text = text_el.get_text(" ", strip=True) if text_el else ""
            photo = wrap.select_one(".tgme_widget_message_photo_wrap")
            image_url = None
            if photo:
                match = re.search(r"url\(['\"]?(.*?)['\"]?\)", photo.get("style", ""))
                if match:
                    image_url = match.group(1).replace("&amp;", "&")
            posts.append((post_id, text, image_url))
        return sorted(posts, key=lambda item: item[0])

    def _ocr(self, image_url):
        response = self.session.get(image_url, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        original = Image.open(io.BytesIO(response.content)).convert("L")
        w, h = original.size

        # Golden Code farklı tasarımlarda farklı yüksekliklerde olabildiği için
        # tam görsel + yatay bölgeleri ayrı ayrı deniyoruz.
        crops = [
            original,
            original.crop((0, int(h * 0.20), w, int(h * 0.80))),
            original.crop((0, int(h * 0.35), w, int(h * 0.75))),
            original.crop((0, int(h * 0.45), w, h)),
        ]

        texts = []
        configs = [
            "--psm 6",
            "--psm 11",
            "--psm 7",
            "--psm 13",
        ]

        for crop in crops:
            base = ImageOps.autocontrast(crop)
            enlarged = base.resize((base.width * 3, base.height * 3))

            # Birden fazla ön işleme varyasyonu.
            variants = [
                enlarged,
                ImageEnhance.Contrast(enlarged).enhance(2.5),
                enlarged.point(lambda p: 255 if p > 145 else 0),
                enlarged.point(lambda p: 255 if p > 180 else 0),
                ImageOps.invert(enlarged),
            ]

            for variant in variants:
                for psm in configs:
                    try:
                        text = pytesseract.image_to_string(
                            variant,
                            config=(
                                psm
                                + " -c tessedit_char_whitelist="
                                + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                            ),
                        ).upper()
                        if text.strip():
                            texts.append(text)
                    except Exception:
                        continue

        # Önce Tesseract'ın doğal olarak tek parça okuduğu adayları topla.
        candidates = []
        for text in texts:
            candidates.extend(CODE_RE.findall(text))

            # OCR karakterler arasına boşluk koyduysa satırı birleştirip tekrar dene.
            for line in text.splitlines():
                compact = re.sub(r"[^A-Z0-9]", "", line.upper())
                if 12 <= len(compact) <= 24:
                    candidates.append(compact)

        # Aynı adayın kaç farklı OCR denemesinde çıktığını da hesaba kat.
        counts = {}
        for value in candidates:
            if (
                any(ch.isalpha() for ch in value)
                and any(ch.isdigit() for ch in value)
                and "GOLDEN" not in value
                and "KEYDROP" not in value
            ):
                counts[value] = counts.get(value, 0) + 1

        if not counts:
            preview = " | ".join(
                re.sub(r"\\s+", " ", text).strip()[:80]
                for text in texts[:4]
                if text.strip()
            )
            if preview:
                self.log("[GOLD][OCR DEBUG] Ham okuma: %s" % preview)
            return None

        # Tekrarlanan OCR sonucu öncelikli; eşitlikte daha uzun aday seçilir.
        return max(counts, key=lambda value: (counts[value], len(value)))

    def check_once(self):
        posts = self._posts()
        if not posts:
            return
        newest_id = posts[-1][0]
        if self.last_post_id is None:
            # İlk açılışta mevcut en son Golden Code gönderisini de işle.
            self.last_post_id = newest_id
            golden_posts = [
                p for p in posts
                if "golden code" in p[1].lower() and p[2]
            ]
            self.log("[GOLD] Telegram takibi hazır. Son gönderi #%s." % newest_id)
            if golden_posts:
                post_id, text, image_url = golden_posts[-1]
                try:
                    code = self._ocr(image_url)
                    if code and code not in self.seen_codes:
                        self.seen_codes.add(code)
                        self.code_callback(code, post_id)
                    elif not code:
                        self.log("[GOLD] #%s görselinde kod okunamadı." % post_id)
                except Exception as exc:
                    self.log("[GOLD] OCR hatası #%s: %s" % (post_id, exc))
            return

        new_posts = [p for p in posts if p[0] > self.last_post_id]
        self.last_post_id = max(self.last_post_id, newest_id)
        for post_id, text, image_url in new_posts:
            if "golden code" not in text.lower() or not image_url:
                continue
            try:
                code = self._ocr(image_url)
                if not code:
                    self.log("[GOLD] #%s görselinde kod okunamadı." % post_id)
                    continue
                if code in self.seen_codes:
                    continue
                self.seen_codes.add(code)
                self.code_callback(code, post_id)
            except Exception as exc:
                self.log("[GOLD] OCR hatası #%s: %s" % (post_id, exc))

    def _wait(self, seconds):
        for _ in range(seconds):
            if not self.running:
                return False
            time.sleep(1)
        return self.running

    def run(self):
        self.running = True
        self.log("[GOLD] Telegram arka planda 300 saniyede bir kontrol ediliyor.")
        while self.running:
            try:
                self.check_once()
                wait_seconds = self.poll_seconds
            except requests.RequestException as exc:
                self.log(
                    "[GOLD] Telegram bağlantı hatası: %s. 30 saniye sonra tekrar denenecek."
                    % exc
                )
                wait_seconds = RETRY_SECONDS
            except Exception as exc:
                self.log("[GOLD] Telegram kontrol hatası: %s" % exc)
                wait_seconds = RETRY_SECONDS

            if not self._wait(wait_seconds):
                return
