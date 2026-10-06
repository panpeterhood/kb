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

        # Doğruluk hızdan daha önemli: farklı kanal, crop, ölçek, rotasyon,
        # kontrast ve threshold kombinasyonlarıyla bağımsız OCR aileleri oluştur.
        crop_boxes = [
            (0, 0, w, h),
            (0, int(h * 0.12), w, int(h * 0.88)),
            (0, int(h * 0.25), w, int(h * 0.82)),
            (0, int(h * 0.35), w, int(h * 0.75)),
            (0, int(h * 0.42), w, int(h * 0.70)),
            (0, int(h * 0.45), w, h),
        ]

        psm_modes = (6, 7, 11, 12, 13)
        rotations = (-1.2, -0.6, 0.0, 0.6, 1.2)
        scales = (3, 4, 5)

        # candidate -> {"raw": int, "families": set(), "confidence": float}
        candidates = {}

        def register_candidate(value, family, confidence=0.0):
            value = re.sub(r"[^A-Z0-9]", "", value.upper())
            if not (12 <= len(value) <= 24):
                return
            if not any(ch.isalpha() for ch in value):
                return
            if not any(ch.isdigit() for ch in value):
                return
            if "GOLDEN" in value or "KEYDROP" in value:
                return

            item = candidates.setdefault(
                value,
                {"raw": 0, "families": set(), "confidence": 0.0}
            )
            item["raw"] += 1
            item["families"].add(family)
            item["confidence"] += max(0.0, float(confidence))

        for crop_index, box in enumerate(crop_boxes):
            crop_rgb = rgb.crop(box)

            # RGB kanallarından biri, renkli arka planda yazıyı gri görüntüden
            # çok daha net ayırabiliyor.
            channel_images = {
                "gray": ImageOps.grayscale(crop_rgb),
                "r": crop_rgb.getchannel("R"),
                "g": crop_rgb.getchannel("G"),
                "b": crop_rgb.getchannel("B"),
            }

            for channel_name, channel in channel_images.items():
                base = ImageOps.autocontrast(channel)

                for rotation in rotations:
                    rotated = base.rotate(
                        rotation,
                        resample=Image.Resampling.BICUBIC,
                        expand=False,
                        fillcolor=255,
                    )

                    for scale in scales:
                        enlarged = rotated.resize(
                            (rotated.width * scale, rotated.height * scale),
                            Image.Resampling.LANCZOS,
                        )
                        sharp = ImageEnhance.Sharpness(enlarged).enhance(2.0)
                        contrast = ImageEnhance.Contrast(sharp).enhance(2.8)

                        variants = {
                            "base": enlarged,
                            "contrast": contrast,
                            "thr110": contrast.point(lambda p: 255 if p > 110 else 0),
                            "thr135": contrast.point(lambda p: 255 if p > 135 else 0),
                            "thr160": contrast.point(lambda p: 255 if p > 160 else 0),
                            "thr185": contrast.point(lambda p: 255 if p > 185 else 0),
                            "invert": ImageOps.invert(contrast),
                        }

                        for variant_name, variant in variants.items():
                            # Aynı görsel varyasyonundaki PSM sonuçlarını tek
                            # bağımsız aile sayıyoruz; böylece aynı OCR hatası
                            # yüzlerce kez tekrar edip sahte güven yaratmaz.
                            family = (
                                crop_index,
                                channel_name,
                                round(rotation, 1),
                                scale,
                                variant_name,
                            )

                            for psm in psm_modes:
                                config = (
                                    f"--oem 1 --psm {psm} "
                                    "-c tessedit_char_whitelist="
                                    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "
                                    "-c preserve_interword_spaces=1"
                                )

                                try:
                                    data = pytesseract.image_to_data(
                                        variant,
                                        config=config,
                                        output_type=pytesseract.Output.DICT,
                                    )
                                except Exception:
                                    continue

                                line_parts = {}
                                line_conf = {}

                                for i, text in enumerate(data.get("text", [])):
                                    text = (text or "").strip().upper()
                                    if not text:
                                        continue

                                    try:
                                        conf = float(data["conf"][i])
                                    except Exception:
                                        conf = 0.0

                                    register_candidate(text, family, conf)

                                    key = (
                                        data["block_num"][i],
                                        data["par_num"][i],
                                        data["line_num"][i],
                                    )
                                    line_parts.setdefault(key, []).append(text)
                                    line_conf.setdefault(key, []).append(conf)

                                # Tesseract kodun arasına boşluk koyarsa satırı
                                # birleştirerek de aday üret.
                                for key, parts in line_parts.items():
                                    joined = "".join(parts)
                                    confs = [x for x in line_conf.get(key, []) if x >= 0]
                                    avg_conf = (
                                        sum(confs) / len(confs) if confs else 0.0
                                    )
                                    register_candidate(joined, family, avg_conf)

        if not candidates:
            self.log("[GOLD][OCR] Uygun kod adayı bulunamadı.")
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

        # Skorun ana bileşeni bağımsız görüntü ailelerinin sayısı.
        # raw tekrarlar yalnızca küçük tie-breaker; OCR confidence da yardımcı.
        ranked = []
        for value, info in candidates.items():
            family_count = len(info["families"])
            raw = info["raw"]
            conf = info["confidence"]

            # Yakın adaylardan gelen destek. Farklı uzunluktaki fakat 1-2 karakter
            # ekleme/silme içeren okumalar da burada hesaba girer.
            neighbor_support = 0.0
            for other, other_info in candidates.items():
                if other == value:
                    continue
                dist = edit_distance(value, other)
                if dist <= 1:
                    neighbor_support += len(other_info["families"]) * 0.35
                elif dist == 2:
                    neighbor_support += len(other_info["families"]) * 0.12

            score = (
                family_count * 10.0
                + neighbor_support
                + min(raw, 20) * 0.20
                + min(conf / 100.0, 20.0) * 0.05
            )
            ranked.append((score, value, info))

        ranked.sort(reverse=True, key=lambda item: item[0])

        # Debug için ilk 8 adayın bağımsız aile ve ham tekrar sayısını göster.
        debug_items = []
        for score, value, info in ranked[:8]:
            debug_items.append(
                "%s [aile=%s ham=%s skor=%.1f]"
                % (value, len(info["families"]), info["raw"], score)
            )
        self.log("[GOLD][OCR] Adaylar: " + ", ".join(debug_items))

        top_score, top_value, top_info = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        top_families = len(top_info["families"])
        margin = (
            (top_score - second_score) / top_score
            if top_score > 0 else 0.0
        )

        # Doğruluk öncelikli: zayıf/kararsız sonucu otomatik kopyalamıyoruz.
        # En iyi aday en az 4 bağımsız görüntü ailesinde görünmeli.
        if top_families < 4:
            self.log(
                "[GOLD][OCR] Güven yetersiz: en iyi aday yalnızca %s bağımsız ailede."
                % top_families
            )
            return None

        # Birbirine çok yakın iki farklı aday varsa ve skor farkı küçükse,
        # yanlış kodu panoya göndermek yerine reddet.
        if len(ranked) > 1:
            _, second_value, second_info = ranked[1]
            distance = edit_distance(top_value, second_value)
            second_families = len(second_info["families"])

            if (
                distance <= 2
                and second_families >= max(3, int(top_families * 0.55))
                and margin < 0.18
            ):
                self.log(
                    "[GOLD][OCR] Belirsiz sonuç: %s ile %s birbirine çok yakın; "
                    "yanlış kod riskinden dolayı kabul edilmedi."
                    % (top_value, second_value)
                )
                return None

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
