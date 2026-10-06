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
            item = candidates.setdefault(value, {"families": set(), "raw": 0})
            item["families"].add(family)
            item["raw"] += 1

        # Golden Code şablonunda kodun bulunduğu siyah kutuya odaklan.
        # Bir ana crop ve iki yakın tolerans crop'u yeterli.
        boxes = [
            (0.07, 0.69, 0.93, 0.91),
            (0.10, 0.71, 0.90, 0.89),
            (0.04, 0.67, 0.96, 0.93),
        ]

        # Toplam 18 Tesseract çağrısı:
        # 3 crop x 2 preprocessing x 3 PSM.
        for crop_i, (x1, y1, x2, y2) in enumerate(boxes):
            crop = rgb.crop((
                int(w * x1), int(h * y1),
                int(w * x2), int(h * y2),
            ))
            gray = ImageOps.autocontrast(ImageOps.grayscale(crop))
            enlarged = gray.resize(
                (gray.width * 5, gray.height * 5),
                Image.Resampling.LANCZOS,
            )
            contrast = ImageEnhance.Contrast(
                ImageEnhance.Sharpness(enlarged).enhance(2.0)
            ).enhance(2.5)

            variants = [
                ("contrast", contrast),
                ("threshold", contrast.point(lambda p: 255 if p > 145 else 0)),
            ]

            for variant_name, variant in variants:
                for psm in (7, 8, 13):
                    family = (crop_i, variant_name, psm)
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
                for score, value, info in ranked[:6]
            )
        )

        top_score, top_value, top_info = ranked[0]
        top_families = len(top_info["families"])
        if top_families < 3:
            self.log("[GOLD][OCR] Güven yetersiz; sonuç kabul edilmedi.")
            return None

        # Yakın ve aynı uzunluktaki adayları karakter bazında oylat.
        peers = []
        for score, value, info in ranked:
            if len(value) == len(top_value) and edit_distance(value, top_value) <= 2:
                peers.append((value, len(info["families"])))

        final_chars = []
        confidences = []
        confusing = [{"O", "0"}, {"I", "1"}, {"S", "5"}, {"Z", "2"}, {"B", "8"}]
        weak_ambiguous = []

        for pos in range(len(top_value)):
            votes = {}
            for value, weight in peers:
                ch = value[pos]
                votes[ch] = votes.get(ch, 0) + weight

            chosen, chosen_votes = max(votes.items(), key=lambda item: item[1])
            total = sum(votes.values())
            confidence = chosen_votes / total if total else 0.0
            final_chars.append(chosen)
            confidences.append(confidence)

            observed = set(votes)
            if any(pair.issubset(observed) for pair in confusing) and confidence < 0.80:
                weak_ambiguous.append(pos + 1)

        # Düşük güvenli karakterleri ve OCR'ın sık karıştırdığı çiftleri
        # kod satırının gerçek renkli glyph'lerinden ayrıca doğrula.
        pair_map = {
            "O": "O0", "0": "O0",
            "I": "I1", "1": "I1",
            "S": "S5", "5": "S5",
            "Z": "Z2", "2": "Z2",
            "B": "B8", "8": "B8",
            "V": "VW", "W": "VW",
        }

        refine_positions = {
            i for i, conf in enumerate(confidences) if conf < 0.80
        }
        refine_positions.update(pos - 1 for pos in weak_ambiguous)
        refine_positions.update(
            i for i, ch in enumerate(final_chars) if ch in pair_map
        )

        if refine_positions and len(top_value) >= 12:
            # Golden Code şablonunda yazı siyah kutunun içinde turuncu.
            # Önce yalnızca bu satırın bulunabileceği güvenli bandı tara;
            # turuncu glyph piksellerinin gerçek bounding box'ını bul.
            search_left = int(w * 0.12)
            search_right = int(w * 0.88)
            search_top = int(h * 0.76)
            search_bottom = int(h * 0.90)

            band = rgb.crop((
                search_left, search_top,
                search_right, search_bottom,
            )).convert("RGB")

            orange_points = []
            px = band.load()
            for yy in range(band.height):
                for xx in range(band.width):
                    rr, gg, bb = px[xx, yy]
                    # Turuncu kod yazısını seç; siyah kutu ve beyaz/dekor alanını at.
                    if (
                        rr > 170
                        and 50 < gg < 220
                        and bb < 120
                        and rr > gg * 1.15
                    ):
                        orange_points.append((xx, yy))

            if orange_points:
                text_left = search_left + min(p[0] for p in orange_points)
                text_right = search_left + max(p[0] for p in orange_points) + 1
                text_top = search_top + min(p[1] for p in orange_points)
                text_bottom = search_top + max(p[1] for p in orange_points) + 1

                self.log(
                    "[GOLD][OCR] Kod yazısı renk maskesiyle bulundu: "
                    "x=%s-%s y=%s-%s"
                    % (text_left, text_right, text_top, text_bottom)
                )

                cell_w = (text_right - text_left) / len(top_value)

                for pos in sorted(refine_positions):
                    current_char = final_chars[pos]
                    whitelist = pair_map.get(
                        current_char,
                        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                    )
                    char_votes = {}

                    # Tek bir crop'a güvenme. Aynı glyph'i çok az değişen
                    # yatay paddinglerle tekrar oku.
                    for pad in (0.00, 0.04, 0.08, 0.12):
                        cx1 = max(
                            0,
                            int(text_left + (pos - pad) * cell_w),
                        )
                        cx2 = min(
                            w,
                            int(text_left + (pos + 1 + pad) * cell_w),
                        )
                        cy1 = max(0, text_top - 4)
                        cy2 = min(h, text_bottom + 4)

                        char_rgb = rgb.crop((cx1, cy1, cx2, cy2)).convert("RGB")

                        # Arka planı tamamen kaldır: yalnızca turuncu glyph siyah,
                        # geri kalan her şey beyaz. Bu, O/0 gibi ayrımlarda
                        # Tesseract'ın kutu kenarlarını harf sanmasını engeller.
                        binary = Image.new("L", char_rgb.size, 255)
                        src = char_rgb.load()
                        dst = binary.load()

                        for yy in range(char_rgb.height):
                            for xx in range(char_rgb.width):
                                rr, gg, bb = src[xx, yy]
                                if (
                                    rr > 170
                                    and 50 < gg < 220
                                    and bb < 120
                                    and rr > gg * 1.15
                                ):
                                    dst[xx, yy] = 0

                        binary = binary.resize(
                            (binary.width * 12, binary.height * 12),
                            Image.Resampling.NEAREST,
                        )

                        for psm in (10, 13):
                            try:
                                raw = pytesseract.image_to_string(
                                    binary,
                                    config=(
                                        f"--oem 1 --psm {psm} "
                                        f"-c tessedit_char_whitelist={whitelist}"
                                    ),
                                )
                            except Exception:
                                continue

                            value = clean(raw)
                            if len(value) == 1:
                                char_votes[value] = char_votes.get(value, 0) + 1

                    if not char_votes:
                        self.log(
                            "[GOLD][OCR] Karakter %s yeniden okunamadı."
                            % (pos + 1)
                        )
                        continue

                    ordered = sorted(
                        char_votes.items(),
                        key=lambda item: item[1],
                        reverse=True,
                    )
                    chosen, chosen_votes = ordered[0]
                    total = sum(char_votes.values())
                    refined_conf = chosen_votes / total if total else 0.0

                    self.log(
                        "[GOLD][OCR] Karakter %s yeniden okuma: %s | "
                        "güven=%%%s | oy=%s"
                        % (
                            pos + 1,
                            chosen,
                            round(refined_conf * 100),
                            "/".join(
                                "%s:%s" % item for item in ordered[:4]
                            ),
                        )
                    )

                    # En az iki başarılı tek-karakter okuması ve güçlü çoğunluk
                    # olmadan ana sonucu değiştirme.
                    if chosen_votes >= 2 and refined_conf >= 0.75:
                        final_chars[pos] = chosen
                        confidences[pos] = max(
                            confidences[pos],
                            refined_conf,
                        )
            else:
                self.log(
                    "[GOLD][OCR] Turuncu kod yazısı renk maskesiyle bulunamadı."
                )

        final_value = "".join(final_chars)
        min_conf = min(confidences) if confidences else 0.0

        # Düşük güvenli kodu panoya göndermiyoruz.
        if min_conf < 0.80:
            self.log(
                "[GOLD][OCR] Nihai karakter güveni düşük (min=%%%s); sonuç kabul edilmedi."
                % round(min_conf * 100)
            )
            return None

        self.log(
            "[GOLD][OCR] Seçilen=%s | aile=%s | min karakter=%%%s"
            % (
                final_value,
                top_families,
                round(min_conf * 100),
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
