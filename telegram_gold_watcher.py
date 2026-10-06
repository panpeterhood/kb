import io
import re
import time

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageEnhance, ImageOps
import pytesseract

CHANNEL = "keydropcomofficial"
CHANNEL_URL = "https://t.me/s/" + CHANNEL
POLL_SECONDS = 60
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
        response = self.session.get(CHANNEL_URL, timeout=20)
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
        response = self.session.get(image_url, timeout=20)
        response.raise_for_status()
        image = Image.open(io.BytesIO(response.content)).convert("L")
        w, h = image.size
        variants = [image, image.crop((0, int(h * 0.45), w, h))]
        candidates = []
        for variant in variants:
            variant = ImageOps.autocontrast(variant)
            variant = ImageEnhance.Contrast(variant).enhance(2.0)
            variant = variant.resize((variant.width * 2, variant.height * 2))
            text = pytesseract.image_to_string(
                variant,
                config="--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
            ).upper()
            candidates.extend(CODE_RE.findall(text))
        valid = [
            value for value in candidates
            if any(c.isalpha() for c in value)
            and any(c.isdigit() for c in value)
            and "GOLDEN" not in value
        ]
        return max(valid, key=len) if valid else None

    def check_once(self):
        posts = self._posts()
        if not posts:
            return
        newest_id = posts[-1][0]
        if self.last_post_id is None:
            self.last_post_id = newest_id
            self.log("[GOLD] Telegram takibi hazır. Son gönderi #%s." % newest_id)
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

    def run(self):
        self.running = True
        self.log("[GOLD] Telegram arka planda 60 saniyede bir kontrol ediliyor.")
        while self.running:
            try:
                self.check_once()
            except Exception as exc:
                self.log("[GOLD] Telegram kontrol hatası: %s" % exc)
            for _ in range(self.poll_seconds):
                if not self.running:
                    return
                time.sleep(1)
