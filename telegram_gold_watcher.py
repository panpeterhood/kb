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

        def register(value, family, bonus=0.0):
            value = clean(value)
            if not (12 <= len(value) <= 24):
                return
            if not any(ch.isalpha() for ch in value) or not any(ch.isdigit() for ch in value):
                return
            if "GOLDEN" in value or "KEYDROP" in value:
                return
            item = candidates.setdefault(value, {"families": set(), "raw": 0, "bonus": 0.0})
            item["families"].add(family)
            item["raw"] += 1
            item["bonus"] += bonus

        def ocr_text(image, psm):
            return pytesseract.image_to_string(
                image,
                config=(
                    f"--oem 1 --psm {psm} "
                    "-c tessedit_char_whitelist="
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                ),
            ).upper()

        # Önce OCR kutularını kullanarak kod benzeri satırların bulunduğu
        # bölgeleri tespit et. Dekoratif alanları tümden OCR'a vermek yerine
        # yalnızca gerçek metin satırlarını ikinci aşamaya taşı.
        locator = ImageOps.grayscale(rgb)
        locator = ImageOps.autocontrast(locator)
        locator = locator.resize((w * 2, h * 2), Image.Resampling.LANCZOS)

        line_boxes = {}
        try:
            data = pytesseract.image_to_data(
                locator,
                config="--oem 1 --psm 11",
                output_type=pytesseract.Output.DICT,
            )
            for i, token in enumerate(data.get("text", [])):
                token_clean = clean(token)
                if len(token_clean) < 4:
                    continue
                key = (
                    data["block_num"][i],
                    data["par_num"][i],
                    data["line_num"][i],
                )
                left = int(data["left"][i])
                top = int(data["top"][i])
                width = int(data["width"][i])
                height = int(data["height"][i])
                entry = line_boxes.setdefault(
                    key,
                    {"left": left, "top": top, "right": left + width,
                     "bottom": top + height, "parts": []},
                )
                entry["left"] = min(entry["left"], left)
                entry["top"] = min(entry["top"], top)
                entry["right"] = max(entry["right"], left + width)
                entry["bottom"] = max(entry["bottom"], top + height)
                entry["parts"].append(token_clean)
        except Exception:
            line_boxes = {}

        regions = []
        for key, entry in line_boxes.items():
            joined = "".join(entry["parts"])
            # Kod satırı çoğunlukla hem harf hem rakam içerir.
            if not (8 <= len(joined) <= 30):
                continue
            if not any(ch.isalpha() for ch in joined) or not any(ch.isdigit() for ch in joined):
                continue

            # locator 2x olduğundan koordinatları orijinale geri çevir.
            l = max(0, entry["left"] // 2)
            t = max(0, entry["top"] // 2)
            r = min(w, (entry["right"] + 1) // 2)
            b = min(h, (entry["bottom"] + 1) // 2)
            pad_x = max(12, int((r - l) * 0.10))
            pad_y = max(8, int((b - t) * 0.55))
            box = (
                max(0, l - pad_x),
                max(0, t - pad_y),
                min(w, r + pad_x),
                min(h, b + pad_y),
            )
            regions.append((joined, box))

        # Locator kaçırırsa kontrollü fallback crop'ları kullan.
        if not regions:
            regions = [
                ("fallback1", (0, int(h * 0.25), w, int(h * 0.82))),
                ("fallback2", (0, int(h * 0.38), w, int(h * 0.76))),
                ("fallback3", (0, int(h * 0.45), w, h)),
            ]

        # En fazla 8 metin bölgesini incele.
        regions = regions[:8]
        self.log("[GOLD][OCR] İncelenen metin bölgesi: %s" % len(regions))

        for region_i, (_, box) in enumerate(regions):
            crop = rgb.crop(box)
            channels = {
                "gray": ImageOps.grayscale(crop),
                "r": crop.getchannel("R"),
                "g": crop.getchannel("G"),
                "b": crop.getchannel("B"),
            }

            for channel_name, channel in channels.items():
                base = ImageOps.autocontrast(channel)

                for scale in (3, 4):
                    enlarged = base.resize(
                        (base.width * scale, base.height * scale),
                        Image.Resampling.LANCZOS,
                    )
                    contrast = ImageEnhance.Contrast(
                        ImageEnhance.Sharpness(enlarged).enhance(2.0)
                    ).enhance(2.8)

                    variants = [
                        ("contrast", contrast),
                        ("thr125", contrast.point(lambda p: 255 if p > 125 else 0)),
                        ("thr150", contrast.point(lambda p: 255 if p > 150 else 0)),
                        ("thr175", contrast.point(lambda p: 255 if p > 175 else 0)),
                    ]

                    for variant_name, variant in variants:
                        for psm in (6, 7, 11, 13):
                            family = (region_i, channel_name, scale, variant_name, psm)
                            try:
                                text = ocr_text(variant, psm)
                            except Exception:
                                continue
                            for match in CODE_RE.findall(text):
                                register(match, family, 1.0)
                            for line in text.splitlines():
                                register(line, family, 0.5)

        if not candidates:
            self.log("[GOLD][OCR] Kod adayı bulunamadı.")
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
            family_count = len(info["families"])
            neighbor = 0.0
            for other, other_info in candidates.items():
                if other == value:
                    continue
                d = edit_distance(value, other)
                if d == 1:
                    neighbor += len(other_info["families"]) * 1.5
                elif d == 2:
                    neighbor += len(other_info["families"]) * 0.45
            score = family_count * 10.0 + neighbor + min(info["raw"], 20) * 0.2 + info["bonus"] * 0.05
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
        if top_families < 3:
            self.log("[GOLD][OCR] Güven yetersiz; sonuç kabul edilmedi.")
            return None

        # Karakter bazlı son doğrulama: en iyi adayla edit-distance <= 1 olan
        # aynı uzunluktaki güçlü okumaları pozisyon pozisyon oylat.
        peers = []
        for score, value, info in ranked:
            if len(value) == len(top_value) and edit_distance(value, top_value) <= 1:
                weight = max(1, len(info["families"]))
                peers.append((value, weight))

        final_chars = []
        char_conf = []
        for pos in range(len(top_value)):
            votes = {}
            for value, weight in peers:
                ch = value[pos]
                votes[ch] = votes.get(ch, 0) + weight
            if not votes:
                final_chars.append(top_value[pos])
                char_conf.append(0.0)
                continue
            ordered = sorted(votes.items(), key=lambda x: x[1], reverse=True)
            chosen, chosen_votes = ordered[0]
            total_votes = sum(votes.values())
            final_chars.append(chosen)
            char_conf.append(chosen_votes / total_votes if total_votes else 0.0)

        final_value = "".join(final_chars)
        weakest = min(char_conf) if char_conf else 0.0

        # O/0 vb. belirsizliklerde karakter oyu zayıfsa yanlış kodu otomatik
        # kabul etmek yerine reddet.
        ambiguous_pairs = [
            {"O", "0"}, {"I", "1"}, {"S", "5"}, {"Z", "2"}, {"B", "8"}
        ]
        ambiguous_positions = []
        for pos in range(len(final_value)):
            observed = {value[pos] for value, _ in peers}
            if any(pair.issubset(observed) for pair in ambiguous_pairs):
                ambiguous_positions.append(pos)

        if ambiguous_positions:
            weak_ambiguous = [
                pos for pos in ambiguous_positions
                if char_conf[pos] < 0.72
            ]
            if weak_ambiguous:
                self.log(
                    "[GOLD][OCR] Karakter belirsizliği var (pozisyonlar: %s); sonuç kabul edilmedi."
                    % ", ".join(str(p + 1) for p in weak_ambiguous)
                )
                return None

        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        margin = (top_score - second_score) / top_score if top_score else 1.0

        self.log(
            "[GOLD][OCR] Seçilen=%s | aile=%s | en zayıf karakter güveni=%%%s | skor farkı=%%%s"
            % (
                final_value,
                top_families,
                round(weakest * 100),
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
