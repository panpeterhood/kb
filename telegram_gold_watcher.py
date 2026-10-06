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

        crops = [
            original,
            original.crop((0, int(h * 0.15), w, int(h * 0.85))),
            original.crop((0, int(h * 0.30), w, int(h * 0.78))),
            original.crop((0, int(h * 0.40), w, int(h * 0.72))),
            original.crop((0, int(h * 0.45), w, h)),
        ]
        configs = ["--psm 6", "--psm 11", "--psm 7", "--psm 13"]
        votes = {}

        def add_candidate(value):
            value = re.sub(r"[^A-Z0-9]", "", value.upper())
            if not (12 <= len(value) <= 24):
                return
            if not any(ch.isalpha() for ch in value) or not any(ch.isdigit() for ch in value):
                return
            if "GOLDEN" in value or "KEYDROP" in value:
                return
            votes[value] = votes.get(value, 0) + 1

        for crop in crops:
            base = ImageOps.autocontrast(crop)
            for scale in (2, 3, 4):
                enlarged = base.resize((base.width * scale, base.height * scale))
                contrast = ImageEnhance.Contrast(enlarged).enhance(2.5)
                variants = [
                    enlarged,
                    contrast,
                    contrast.point(lambda p: 255 if p > 115 else 0),
                    contrast.point(lambda p: 255 if p > 145 else 0),
                    contrast.point(lambda p: 255 if p > 175 else 0),
                    ImageOps.invert(contrast),
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
                        except Exception:
                            continue

                        for match in CODE_RE.findall(text):
                            add_candidate(match)
                        for line in text.splitlines():
                            add_candidate(line)

        if not votes:
            self.log("[GOLD][OCR] Uygun kod adayı bulunamadı.")
            return None

        # Önce aynı uzunluktaki yakın sonuçları kümeliyoruz. Böylece örn.
        # I/T, O/0, S/5 gibi tek-karakter OCR sapmaları consensus'a dahil olur.
        by_length = {}
        for value, count in votes.items():
            by_length.setdefault(len(value), []).append((value, count))

        def hamming(a, b):
            return sum(x != y for x, y in zip(a, b))

        best_seed = max(votes, key=lambda v: votes[v])
        same_len = by_length[len(best_seed)]
        cluster = [(v, n) for v, n in same_len if hamming(v, best_seed) <= 3]

        # Karakter bazında ağırlıklı çoğunluk oyu.
        consensus = []
        confidences = []
        for pos in range(len(best_seed)):
            char_votes = {}
            total = 0
            for value, count in cluster:
                ch = value[pos]
                char_votes[ch] = char_votes.get(ch, 0) + count
                total += count
            ch, count = max(char_votes.items(), key=lambda item: item[1])
            consensus.append(ch)
            confidences.append(count / total if total else 0.0)

        result = "".join(consensus)
        support = sum(n for _, n in cluster)
        min_conf = min(confidences) if confidences else 0.0
        avg_conf = sum(confidences) / len(confidences) if confidences else 0.0

        top = sorted(votes.items(), key=lambda item: item[1], reverse=True)[:5]
        self.log(
            "[GOLD][OCR] Adaylar: "
            + ", ".join("%s x%s" % (value, count) for value, count in top)
        )
        self.log(
            "[GOLD][OCR] Consensus=%s | destek=%s | min=%%%.0f | ort=%%%.0f"
            % (result, support, min_conf * 100, avg_conf * 100)
        )

        # Şüpheli sonucu otomatik olarak panoya göndermiyoruz.
        if support < 3 or min_conf < 0.60 or avg_conf < 0.78:
            self.log("[GOLD][OCR] Güven seviyesi yetersiz; kod kabul edilmedi.")
            return None

        return result

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
