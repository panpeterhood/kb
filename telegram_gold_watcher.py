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

        rgb = Image.open(io.BytesIO(response.content)).convert("RGB")
        w, h = rgb.size
        candidates = {}

        def clean(value):
            return re.sub(r"[^A-Z0-9]", "", (value or "").upper())

        def register(value, family):
            value = clean(value)
            if not (12 <= len(value) <= 24):
                return
            if not any(ch.isalpha() for ch in value) or not any(ch.isdigit() for ch in value):
                return
            if "GOLDEN" in value or "KEYDROP" in value:
                return
            item = candidates.setdefault(value, {"families": set(), "raw": 0})
            item["families"].add(family)
            item["raw"] += 1

        # Golden Code görselleri sabit şablon kullanıyor. Kod, siyah yuvarlatılmış
        # kutunun içinde ve yaklaşık olarak görselin x=%6-94, y=%69-91 bandında.
        # Birkaç yakın crop kullanmak, Telegram yeniden ölçeklese bile tolerans sağlar.
        boxes = [
            (0.05, 0.67, 0.95, 0.92),
            (0.07, 0.70, 0.93, 0.90),
            (0.10, 0.72, 0.90, 0.88),
        ]

        for crop_i, (x1, y1, x2, y2) in enumerate(boxes):
            crop = rgb.crop((
                int(w * x1), int(h * y1),
                int(w * x2), int(h * y2),
            ))

            channels = {
                "gray": ImageOps.grayscale(crop),
                "r": crop.getchannel("R"),
                "g": crop.getchannel("G"),
            }

            for channel_name, channel in channels.items():
                base = ImageOps.autocontrast(channel)

                for scale in (4, 6):
                    enlarged = base.resize(
                        (base.width * scale, base.height * scale),
                        Image.Resampling.LANCZOS,
                    )
                    sharp = ImageEnhance.Sharpness(enlarged).enhance(2.2)
                    contrast = ImageEnhance.Contrast(sharp).enhance(2.4)

                    variants = [
                        ("plain", enlarged),
                        ("contrast", contrast),
                        ("thr100", contrast.point(lambda p: 255 if p > 100 else 0)),
                        ("thr130", contrast.point(lambda p: 255 if p > 130 else 0)),
                        ("thr160", contrast.point(lambda p: 255 if p > 160 else 0)),
                        ("invert", ImageOps.invert(contrast)),
                    ]

                    for variant_name, variant in variants:
                        for psm in (7, 8, 13):
                            family = (crop_i, channel_name, scale, variant_name, psm)
                            try:
                                text = pytesseract.image_to_string(
                                    variant,
                                    config=(
                                        f"--oem 1 --psm {psm} "
                                        "-c tessedit_char_whitelist="
                                        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                                    ),
                                )
                            except Exception:
                                continue
                            register(text, family)
                            for match in CODE_RE.findall(text.upper()):
                                register(match, family)

        if not candidates:
            self.log("[GOLD][OCR] Kod kutusunda uygun aday bulunamadı.")
            return None

        def edit_distance(a, b):
            previous = list(range(len(b) + 1))
            for i, ca in enumerate(a, 1):
                current = [i]
                for j, cb in enumerate(b, 1):
                    current.append(min(
                        current[-1] + 1,
                        previous[j] + 1,
                        previous[j - 1] + (ca != cb),
                    ))
                previous = current
            return previous[-1]

        ranked = []
        for value, info in candidates.items():
            families = len(info["families"])
            neighbor = 0.0
            for other, other_info in candidates.items():
                if other == value:
                    continue
                d = edit_distance(value, other)
                if d == 1:
                    neighbor += len(other_info["families"]) * 1.5
                elif d == 2:
                    neighbor += len(other_info["families"]) * 0.4
            score = families * 10.0 + neighbor + min(info["raw"], 20) * 0.2
            ranked.append((score, value, info))

        ranked.sort(reverse=True, key=lambda item: item[0])
        self.log(
            "[GOLD][OCR] Adaylar: " +
            ", ".join(
                "%s [aile=%s ham=%s skor=%.1f]"
                % (value, len(info["families"]), info["raw"], score)
                for score, value, info in ranked[:8]
            )
        )

        top_score, top_value, top_info = ranked[0]
        top_families = len(top_info["families"])

        # Kod kutusundan gelmesine rağmen tek/iki varyasyonda görünen sonucu kabul etme.
        if top_families < 4:
            self.log("[GOLD][OCR] Güven yetersiz; sonuç kabul edilmedi.")
            return None

        # Aynı uzunluktaki yakın adayları karakter bazında oylat.
        peers = []
        for score, value, info in ranked:
            if len(value) == len(top_value) and edit_distance(value, top_value) <= 2:
                peers.append((value, len(info["families"])))

        final_chars = []
        confidences = []
        ambiguity = []
        confusing = [
            {"O", "0"}, {"I", "1"}, {"S", "5"}, {"Z", "2"}, {"B", "8"}
        ]

        for pos in range(len(top_value)):
            votes = {}
            for value, weight in peers:
                ch = value[pos]
                votes[ch] = votes.get(ch, 0) + weight

            ordered = sorted(votes.items(), key=lambda item: item[1], reverse=True)
            chosen, chosen_votes = ordered[0]
            total = sum(votes.values())
            confidence = chosen_votes / total if total else 0.0
            final_chars.append(chosen)
            confidences.append(confidence)

            observed = set(votes)
            if any(pair.issubset(observed) for pair in confusing):
                ambiguity.append((pos + 1, votes, confidence))

        final_value = "".join(final_chars)

        # Özellikle O/0 gibi çiftlerde iki taraf da anlamlı destek alıyorsa
        # tahmin yürütme; kodu reddet.
        weak = [
            pos for pos, votes, conf in ambiguity
            if conf < 0.80
        ]
        if weak:
            self.log(
                "[GOLD][OCR] Karakter belirsizliği (pozisyon %s); sonuç kabul edilmedi."
                % ", ".join(map(str, weak))
            )
            return None

        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        margin = (top_score - second_score) / top_score if top_score else 1.0

        self.log(
            "[GOLD][OCR] Seçilen=%s | aile=%s | min karakter=%%%s | skor farkı=%%%s"
            % (
                final_value,
                top_families,
                round(min(confidences) * 100) if confidences else 0,
                round(margin * 100),
            )
        )
        return final_value

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
