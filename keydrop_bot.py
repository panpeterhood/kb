import time
import random
import re
import subprocess
import os
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

class KeydropBot:
    def __init__(self, log_callback, settings=None, update_counters_callback=None):
        self.log = log_callback
        self.settings = settings or {}
        self.update_counters_callback = update_counters_callback
        self.driver = None
        self.running = False
        self.counters = {"AMATEUR": 0, "CONTENDER": 0}
        self.joined_urls = set()
        
        # Çekilişlerin gerçek bitiş zamanlarını tutacak olan sözlük
        self.last_joined_end_time = {"AMATEUR": 0, "CONTENDER": 0}
        # Başarısız katılım denemelerinde spamı önlemek için koruma zamanı
        self.last_attempt_time = {"AMATEUR": 0, "CONTENDER": 0}

        # Donma kontrolü için
        self.last_seen_url = None
        self.last_change_time = time.time()

        # Random restart için
        self.next_restart_time = time.time() + random.randint(480, 720)  # 8-12 dk
        self.profile_path = r"C:\selenum\ChromeProfile"

    def update_settings(self, new_settings):
        self.settings = new_settings

    def _ensure_dir(self):
        if not os.path.exists(self.profile_path):
            os.makedirs(self.profile_path)

    def start_driver(self):
        self._ensure_dir()
        
        # 1. Önceki kalıntıları temizle
        os.system("taskkill /f /im chrome.exe /t >nul 2>&1")
        time.sleep(2)
        
        chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        
        # 2. Tırnak işaretlerini Windows komut satırına en uygun hale getiriyoruz
        flags = [
            f'--remote-debugging-port=9222',
            f'--user-data-dir="{self.profile_path}"',
            '--start-maximized',
            '--no-sandbox',
            '--disable-background-timer-throttling',
            '--disable-backgrounding-occluded-windows',
            '--disable-renderer-backgrounding',
            '--disable-features=CalculateNativeWinOcclusion',
            '--no-first-run'
        ]
        
        chrome_cmd = f'start "" "{chrome_path}" ' + " ".join(flags)
        
        try:
            subprocess.Popen(chrome_cmd, shell=True)
            time.sleep(8) 
            
            opts = Options()
            opts.add_experimental_option("debuggerAddress", "127.0.0.1:9222")
            
            service = Service(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=opts)
            
            _ = self.driver.current_url
            self.log("[BAŞARILI] Tarayıcıya bağlanıldı.")
            return True
        except Exception as e:
            self.log(f"[HATA] Bağlantı: {str(e)}")
            os.system("taskkill /f /im chrome.exe /t >nul 2>&1")
            return False

    def get_price(self, card):
        try:
            # Doğrudan lime-300 sınıfındaki fiyat etiketini hedefle
            price_element = card.find_element(By.CSS_SELECTOR, "span.text-lime-300")
            price_text = self.driver.execute_script("return arguments[0].innerText;", price_element).strip()
            
            # $ simgesini temizleyip float sayıya çevir
            clean_price = price_text.replace("$", "").replace(",", "")
            return float(clean_price)
        except Exception:
            # Alternatif yöntem: Regex ile kart içindeki en güvenilir fiyat değerini çek
            try:
                card_text = self.driver.execute_script("return arguments[0].innerText;", card)
                prices = re.findall(r'\$(\d+(?:\.\d+)?)', card_text)
                if prices:
                    return float(prices[0])
            except Exception:
                pass
        return 0.0

    def get_latest_card_url(self):
        try:
            cards = self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='div-active-giveaways-list-single-card']")
            if not cards:
                return None

            card = cards[0]
            btn = card.find_element(By.CSS_SELECTOR, "a[data-testid='btn-single-card-giveaway-join']")
            return btn.get_attribute("href")
        except:
            return None

    def check_and_join(self):
        try:
            cards = self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='div-active-giveaways-list-single-card']")
            
            for card in cards:
                try:
                    # Kartın tüm metnini çekiyoruz (Kategori, Sayaç vs.)
                    card_full_text = self.driver.execute_script("return arguments[0].innerText;", card).upper()
                    
                    category = "AMATEUR" if "AMATEUR" in card_full_text else "CONTENDER" if "CONTENDER" in card_full_text else None
                    if not category or not self.settings.get(category, {}).get("active"):
                        continue

                    # KONTROL 1: Katıldığımız çekilişin süresi hala dolmadıysa bu kartı pas geç
                    if time.time() < self.last_joined_end_time[category] - 3:
                        continue

                    # KONTROL 2: Son başarısız denemenin üzerinden 15 saniye geçmediyse pas geç
                    if time.time() - self.last_attempt_time.get(category, 0) < 15:
                        continue

                    btn = card.find_element(By.CSS_SELECTOR, "a[data-testid='btn-single-card-giveaway-join']")
                    price = self.get_price(card)
                    
                    if price >= self.settings[category].get("min_value", 0.0):
                        self.log(f"[HEDEF] {category} (${price}) algılandı. Sızılıyor...")
                        
                        # Son deneme zamanını güncelle (spam koruması)
                        self.last_attempt_time[category] = time.time()
                        
                        # Dışarıdaki butona tıkla
                        self.driver.execute_script("arguments[0].click();", btn)
                        time.sleep(2.5)
                        
                        # İçerideki "Join the giveaway" onay butonuna tıkla
                        success = self.driver.execute_script("""
                            var joinBtn = document.querySelector("button[data-testid='btn-giveaway-join-the-giveaway']");
                            if(joinBtn) {
                                joinBtn.click();
                                return true;
                            }
                            return false;
                        """)
                        
                        if success:
                            time.sleep(2)
                            
                            # Kart metninden geri sayım sayacını bul (Örn: 02:45)
                            timer_match = re.search(r'(\d{2}):(\d{2})', card_full_text)
                            if timer_match:
                                mins = int(timer_match.group(1))
                                secs = int(timer_match.group(2))
                                rem_seconds = (mins * 60) + secs
                            else:
                                rem_seconds = 180 # Sayaç okunamazsa varsayılan olarak 3 dakika kilit koy
                                
                            # Bu çekilişin biteceği tam zamanı kaydet
                            self.last_joined_end_time[category] = time.time() + rem_seconds
                            
                            self.counters[category] += 1
                            if self.update_counters_callback:
                                self.update_counters_callback(self.counters)
                            self.log(f"[BAŞARILI] {category} çekilişine girildi. (Bitişine {rem_seconds} sn var)")
                        
                        # İşlem bitince ana sayfaya temiz dönüş yap
                        self.driver.execute_script("window.location.href = 'https://key-drop.gg/en/giveaways';")
                        time.sleep(2)
                        return True 
                except:
                    continue
        except Exception:
            pass
        return False

    def run(self):
        if not self.start_driver(): return
        self.running = True
        self.driver.get("https://key-drop.gg/en/giveaways")
        
        while self.running:
            now = time.time()

            if now > self.next_restart_time:
                self.log("[SİSTEM] Sayfa yenileniyor (Cloudflare reset)...")
                try:
                    self.driver.get("https://key-drop.gg/en/giveaways")
                    time.sleep(5)
                except Exception as e:
                    self.log(f"[HATA] Sayfa yenilenemedi: {str(e)}")

                self.last_seen_url = None
                self.last_change_time = time.time()
                self.next_restart_time = time.time() + random.randint(480, 720)
                continue

            try:
                _ = self.driver.current_url
            except:
                self.log("[KRİTİK] Tarayıcı dondu, yeniden başlatılıyor...")
                self.start_driver()
                continue

            self.check_and_join()
            time.sleep(1.5)

    def stop(self):
        self.running = False
        self.log("[DURDURULDU]")