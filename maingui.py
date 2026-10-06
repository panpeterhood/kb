import tkinter as tk
from tkinter import scrolledtext
import threading
import time
import os
import keydrop_bot 

class GiveawayBotGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("")
        self.root.geometry("600x750")
        self.root.configure(bg="#121212")

        self.giveaway_controls = {}
        self.counter_labels = {}
        self.bot_instance = None

        tk.Label(self.root, text="", font=("Segoe UI", 16, "bold"), 
                 bg="#121212", fg="#00e676").pack(pady=20)

        settings_frame = tk.LabelFrame(self.root, text=" Çekiliş Ayarları ", font=("Segoe UI", 10, "bold"),
                                       bg="#1e1e1e", fg="#ffffff", padx=20, pady=20, bd=0)
        settings_frame.pack(pady=10, padx=30, fill=tk.X)

        for name in ["CONTENDER", "AMATEUR"]:
            self._create_row(settings_frame, name)

        btn_frame = tk.Frame(self.root, bg="#121212")
        btn_frame.pack(pady=20)

        # Başlat Butonu
        self.start_btn = tk.Button(btn_frame, text="BOTU BAŞLAT", bg="#00e676", fg="#000000", 
                                   font=("Segoe UI", 10, "bold"), width=18, height=1, 
                                   relief=tk.FLAT, command=self.start_bot)
        self.start_btn.pack(side=tk.LEFT, padx=5)

        # Ayarları Uygula Butonu
        self.apply_btn = tk.Button(btn_frame, text="AYARLARI UYGULA", bg="#ffc107", fg="#000000", 
                                   font=("Segoe UI", 10, "bold"), width=18, height=1, 
                                   relief=tk.FLAT, command=self.apply_settings)
        self.apply_btn.pack(side=tk.LEFT, padx=5)

        # Durdur Butonu
        self.stop_btn = tk.Button(btn_frame, text="DURDUR", bg="#ff1744", fg="#ffffff", 
                                 font=("Segoe UI", 10, "bold"), width=18, height=1, 
                                 relief=tk.FLAT, command=self.stop_bot, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        tk.Label(self.root, text="SİSTEM GÜNLÜĞÜ", font=("Segoe UI", 9, "bold"), bg="#121212", fg="#888888").pack(anchor="w", padx=35)
        self.log_area = scrolledtext.ScrolledText(self.root, height=25, width=70, 
                                                 state="disabled", font=("Consolas", 10), 
                                                 bg="#1e1e1e", fg="#00e676", 
                                                 insertbackground="white", bd=0)
        self.log_area.pack(padx=30, pady=5)
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def _create_row(self, parent, name):
        row = tk.Frame(parent, bg="#1e1e1e", pady=5)
        row.pack(fill=tk.X)
        var = tk.BooleanVar(value=(name == "AMATEUR"))
        cb = tk.Checkbutton(row, text=name, variable=var, font=("Segoe UI", 10), 
                            bg="#1e1e1e", fg="#ffffff", selectcolor="#121212", 
                            activebackground="#1e1e1e", activeforeground="#00e676")
        cb.pack(side=tk.LEFT)
        tk.Label(row, text="Min $:", bg="#1e1e1e", fg="#aaaaaa", font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(20, 5))
        min_e = tk.Entry(row, width=8, bg="#333333", fg="#ffffff", insertbackground="white", bd=0, justify="center")
        min_e.insert(0, "6.0")
        min_e.pack(side=tk.LEFT)
        lbl = tk.Label(row, text="0", fg="#00e676", bg="#1e1e1e", width=10, font=("Segoe UI", 12, "bold"))
        lbl.pack(side=tk.RIGHT)
        self.giveaway_controls[name] = {"active": var, "min_val": min_e}
        self.counter_labels[name] = lbl

    def log(self, message):
        ts = time.strftime("%H:%M:%S")
        self.log_area.config(state="normal")
        self.log_area.insert("end", f"[{ts}] {message}\n")
        self.log_area.see("end")
        self.log_area.config(state="disabled")

    def start_bot(self):
        self.log("[SİSTEM] Bot başlatılıyor...")
        self.bot_instance = keydrop_bot.KeydropBot(self.log, self.get_current_ui_settings(), self.update_counters)
        threading.Thread(target=self.bot_instance.run, daemon=True).start()
        self.start_btn.config(state=tk.DISABLED, bg="#333333")
        self.stop_btn.config(state=tk.NORMAL)

    def get_current_ui_settings(self):
        settings = {}
        for name, ctrl in self.giveaway_controls.items():
            try:
                min_v = float(ctrl["min_val"].get() or 0.0)
                settings[name] = {"active": ctrl["active"].get(), "min_value": min_v}
            except:
                settings[name] = {"active": False, "min_value": 0.0}
        return settings

    def apply_settings(self):
        if self.bot_instance:
            self.bot_instance.update_settings(self.get_current_ui_settings())
            self.log("[SİSTEM] Ayarlar güncellendi.")

    def stop_bot(self):
        if self.bot_instance: self.bot_instance.stop()
        self.start_btn.config(state=tk.NORMAL, bg="#00e676")
        self.stop_btn.config(state=tk.DISABLED)

    def update_counters(self, counters):
        for name, val in counters.items():
            if name in self.counter_labels:
                self.counter_labels[name].config(text=str(val))

    def on_closing(self):
        try:
            self.stop_bot()
        except:
            pass
        self.root.destroy()
        os._exit(0)

if __name__ == "__main__":
    root = tk.Tk()
    app = GiveawayBotGUI(root)
    root.mainloop()