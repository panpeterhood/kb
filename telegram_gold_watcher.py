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

        # İki aşamalı yaklaşım:
        # 1) Az sayıda güçlü kombinasyonla adayları çıkar.
        # 2) Sadece en iyi adayların çevresinde daha yoğun doğrulama yap.
        candidates = {}

        def register(value, family, weight=1.0):
            value = re.sub(r"[^A-Z0-9]", "", (value or "").upper())
            if not (12 <= len(value) <= 24):
                return
            if not any(ch.isalpha() for ch in value) or not any(ch.isdigit() for ch in value):
                return
            if "GOLDEN" in value or "KEYDROP" in value:
                return
            item = candidates.setdefault(value, {"families": set(), "raw": 0, "weight": 0.0})
            item["families"].add(family)
            item["raw"] += 1
            item["weight"] += weight

        def run_ocr(image, family_prefix, psm_modes):
            for psm in psm_modes:
                family = family_prefix + (psm,)
                try:
                    text = pytesseract.image_to_string(
                        image,
                        config=(
                            f"--oem 1 --psm {psm} "
                            "-c tessedit_char_whitelist="
                            "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                        ),
                    ).upper()
                except Exception:
                    continue

                for match in CODE_RE.findall(text):
                    register(match, family)
                for line in text.splitlines():
                    register(line, family)

        # Aşama 1: yaklaşık 70-100 OCR çağrısı.
        stage1_boxes = [
            (0, 0, w, h),
            (0, int(h * 0.25), w, int(h * 0.82)),
            (0, int(h * 0.38), w, int(h * 0.76)),
            (0, int(h * 0.45), w, h),
        ]

        for crop_i, box in enumerate(stage1_boxes):
            crop = rgb.crop(box)
            channels = {
                "gray": ImageOps.grayscale(crop),
                "r": crop.getchannel("R"),
                "g": crop.getchannel("G"),
                "b": crop.getchannel("B"),
            }
            for channel_name, channel in channels.items():
                base = ImageOps.autocontrast(channel)
                enlarged = base.resize(
                    (base.width * 3, base.height * 3),
                    Image.Resampling.LANCZOS,
                )
                contrast = ImageEnhance.Contrast(enlarged).enhance(2.6)
                variants = {
                    "contrast": contrast,
                    "thr140": contrast.point(lambda p: 255 if p > 140 else 0),
                }
                for variant_name, variant in variants.items():
                    run_ocr(
                        variant,
                        ("s1", crop_i, channel_name, variant_name),
                        (6, 7, 11),
                    )

        if not candidates:
            self.log("[GOLD][OCR] İlk aşamada kod adayı bulunamadı.")
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

        def base_score(value, info):
            return len(info["families"]) * 10.0 + min(info["raw"], 20) * 0.25 + info["weight"] * 0.05

        stage1_ranked = sorted(
            candidates.items(),
            key=lambda item: base_score(item[0], item[1]),
            reverse=True,
        )
        seeds = [value for value, _ in stage1_ranked[:6]]

        # Aşama 2: farklı ölçek/rotasyon/threshold ile doğrulama.
        focus_boxes = [
            (0, int(h * 0.28), w, int(h * 0.82)),
            (0, int(h * 0.36), w, int(h * 0.76)),
            (0, int(h * 0.42), w, int(h * 0.72)),
        ]

        for crop_i, box in enumerate(focus_boxes):
            crop = rgb.crop(box)
            for channel_name, channel in {
                "gray": ImageOps.grayscale(crop),
                "r": crop.getchannel("R"),
                "g": crop.getchannel("G"),
                "b": crop.getchannel("B"),
            }.items():
                base = ImageOps.autocontrast(channel)

                for rotation in (-0.8, 0.0, 0.8):
                    rotated = base.rotate(
                        rotation,
                        resample=Image.Resampling.BICUBIC,
                        expand=False,
                        fillcolor=255,
                    )
                    enlarged = rotated.resize(
                        (rotated.width * 4, rotated.height * 4),
                        Image.Resampling.LANCZOS,
                    )
                    contrast = ImageEnhance.Contrast(
                        ImageEnhance.Sharpness(enlarged).enhance(2.0)
                    ).enhance(2.8)

                    for threshold in (120, 150, 180):
                        variant = contrast.point(
                            lambda p, t=threshold: 255 if p > t else 0
                        )
                        run_ocr(
                            variant,
                            ("s2", crop_i, channel_name, rotation, threshold),
                            (7, 11, 13),
                        )

        # Nihai skor: bağımsız aile desteği + edit-distance komşu desteği.
        ranked = []
        for value, info in candidates.items():
            families = len(info["families"])
            neighbor = 0.0
            for other, other_info in candidates.items():
                if other == value:
                    continue
                dist = edit_distance(value, other)
                if dist == 1:
                    neighbor += len(other_info["families"]) * 1.2
                elif dist == 2:
                    neighbor += len(other_info["families"]) * 0.35

            seed_bonus = 2.0 if any(edit_distance(value, seed) <= 1 for seed in seeds) else 0.0
            score = families * 10.0 + neighbor + seed_bonus + min(info["raw"], 30) * 0.15
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

        if top_families < 4:
            self.log("[GOLD][OCR] Güven yetersiz; sonuç kabul edilmedi.")
            return None

        if len(ranked) > 1:
            second_score, second_value, second_info = ranked[1]
            margin = (top_score - second_score) / top_score if top_score else 0.0
            distance = edit_distance(top_value, second_value)

            if (
                distance <= 2
                and len(second_info["families"]) >= max(3, int(top_families * 0.55))
                and margin < 0.12
            ):
                self.log(
                    "[GOLD][OCR] Belirsiz: %s / %s. Yanlış kod riskinden dolayı kabul edilmedi."
                    % (top_value, second_value)
                )
                return None
        else:
            margin = 1.0

        self.log(
            "[GOLD][OCR] Seçilen=%s | bağımsız aile=%s | skor farkı=%%%s"
            % (top_value, top_families, round(margin * 100))
        )
        return top_value

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
