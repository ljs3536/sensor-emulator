import os
import json
import time
import threading
import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox
from paho.mqtt import client as mqtt_client
from paho.mqtt.enums import CallbackAPIVersion
from datetime import datetime
import glob

# =========================================================
# 기본 통신 설정
# =========================================================
BROKER_DEFAULT = "15.165.63.242"
PORT_DEFAULT = 1883
DEFAULT_MAC = "035415641614"
SAMPLE_COUNT = 8192

class CsvRawStreamGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("RAW 진동 데이터(Pump_DE_Y 등) 스트리밍 에뮬레이터")
        self.root.geometry("850x650")

        self.client = None
        self.is_playing = False
        self.play_thread = None
        self.seq = 1

        self.csv_data = []      
        self.current_idx = 0    

        self._build_ui()
        self.scan_csv_files()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        # 1. 통신 및 추가 파라미터 설정
        conn_frame = ttk.LabelFrame(main, text="1. 통신 및 센서 정보 설정", padding=10)
        conn_frame.pack(fill="x", pady=5)

        ttk.Label(conn_frame, text="Broker:").grid(row=0, column=0, padx=5, pady=5, sticky="e")
        self.ent_broker = ttk.Entry(conn_frame, width=15)
        self.ent_broker.insert(0, BROKER_DEFAULT)
        self.ent_broker.grid(row=0, column=1, padx=5, sticky="w")

        ttk.Label(conn_frame, text="Port:").grid(row=0, column=2, padx=5, sticky="e")
        self.ent_port = ttk.Entry(conn_frame, width=8)
        self.ent_port.insert(0, str(PORT_DEFAULT))
        self.ent_port.grid(row=0, column=3, padx=5, sticky="w")

        ttk.Label(conn_frame, text="MAC 주소:").grid(row=0, column=4, padx=5, sticky="e")
        self.ent_mac = ttk.Entry(conn_frame, width=15)
        self.ent_mac.insert(0, DEFAULT_MAC)
        self.ent_mac.grid(row=0, column=5, padx=5, sticky="w")

        # 💡 [추가] 배터리 및 샘플레이트 UI
        ttk.Label(conn_frame, text="배터리 잔량:").grid(row=1, column=0, padx=5, pady=5, sticky="e")
        self.ent_batt = ttk.Entry(conn_frame, width=15)
        self.ent_batt.insert(0, "10")
        self.ent_batt.grid(row=1, column=1, padx=5, sticky="w")

        ttk.Label(conn_frame, text="샘플레이트:").grid(row=1, column=2, padx=5, sticky="e")
        self.cb_sr = ttk.Combobox(conn_frame, values=["3200", "12800", "25600"], width=8)
        self.cb_sr.set("25600")
        self.cb_sr.grid(row=1, column=3, padx=5, sticky="w")

        # 2. CSV 로드 및 설정
        csv_frame = ttk.LabelFrame(main, text="2. 실측 데이터(CSV) 로드 및 스트리밍 설정", padding=10)
        csv_frame.pack(fill="x", pady=5)

        ttk.Label(csv_frame, text="재생할 CSV 파일:").grid(row=0, column=0, sticky="e", padx=5, pady=5)
        self.cb_csv = ttk.Combobox(csv_frame, values=[], state="readonly", width=40)
        self.cb_csv.grid(row=0, column=1, columnspan=2, padx=5, sticky="w")
        ttk.Button(csv_frame, text="🔄 새로고침", command=self.scan_csv_files).grid(row=0, column=3, padx=5)

        ttk.Label(csv_frame, text="스케일 팩터(배율):").grid(row=1, column=0, sticky="e", padx=5, pady=5)
        self.ent_scale = ttk.Entry(csv_frame, width=10)
        self.ent_scale.insert(0, "10000")
        self.ent_scale.grid(row=1, column=1, sticky="w", padx=5)
        ttk.Label(csv_frame, text="* 소수점(G값)을 16bit 정수 Hex로 변환하기 위한 증폭치", foreground="gray").grid(row=1, column=2, columnspan=2, sticky="w")

        ttk.Label(csv_frame, text="측정 주기(ms):").grid(row=2, column=0, sticky="e", padx=5, pady=5)
        self.ent_period = ttk.Entry(csv_frame, width=10)
        self.ent_period.insert(0, "1000")
        self.ent_period.grid(row=2, column=1, sticky="w", padx=5)

        # 3. 제어 버튼
        ctrl_frame = ttk.Frame(main)
        ctrl_frame.pack(fill="x", pady=10)
        self.btn_play = tk.Button(ctrl_frame, text="▶ 측정 데이터(CSV) 실시간 스트리밍 시작", bg="lightgreen", font=("Arial", 12, "bold"), command=self.toggle_play)
        self.btn_play.pack(fill="x", ipady=10)

        # 4. 재생 진행률 바
        self.progress_var = tk.DoubleVar()
        self.progress = ttk.Progressbar(main, variable=self.progress_var, maximum=100)
        self.progress.pack(fill="x", pady=5)
        self.lbl_status = ttk.Label(main, text="대기 중...")
        self.lbl_status.pack(pady=2)

        # 5. 로그 영역
        log_frame = ttk.LabelFrame(main, text="3. 스트리밍 로그", padding=10)
        log_frame.pack(fill="both", expand=True, pady=5)

        self.txt_log = tk.Text(log_frame, wrap="none", height=10)
        self.txt_log.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.txt_log.yview)
        scroll.pack(side="right", fill="y")
        self.txt_log.configure(yscrollcommand=scroll.set)

    def scan_csv_files(self):
        files = glob.glob("*.csv")
        self.cb_csv['values'] = files
        if files:
            self.cb_csv.set(files[0])
            self._log(f"CSV 파일 {len(files)}개 발견됨.")
        else:
            self.cb_csv.set("")
            self._log("현재 폴더에 CSV 파일이 없습니다.", "WARN")

    def _log(self, msg, tag="INFO"):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.txt_log.insert("end", f"[{ts}] [{tag}] {msg}\n")
        self.txt_log.see("end")

    def load_csv_data(self):
        filename = self.cb_csv.get()
        if not filename or not os.path.exists(filename):
            messagebox.showerror("오류", "CSV 파일을 선택해주세요.")
            return False

        self._log(f"[{filename}] 파일 파싱 시작...")
        
        data_values = []
        try:
            with open(filename, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    # 💡 [핵심] 헤더에 Sample Rate 정보가 있으면 자동으로 UI에 반영
                    if "Sample Rate" in line and ":" in line:
                        try:
                            sr_val = line.split(":")[1].strip()
                            self.cb_sr.set(sr_val)
                            self._log(f"💡 CSV 헤더에서 샘플레이트 자동 감지 및 적용: {sr_val} Hz")
                        except:
                            pass
                            
                    parts = line.strip().split(',')
                    # 데이터 라인(시간, 값)일 경우 Y값 추출
                    if len(parts) >= 2:
                        try:
                            val = float(parts[1])
                            data_values.append(val)
                        except ValueError:
                            pass # "time(sec),aiChannel1" 같은 문자열 헤더는 자연스럽게 무시됨
            
            if not data_values:
                messagebox.showerror("오류", "데이터를 추출하지 못했습니다. 형식을 확인하세요.")
                return False

            self.csv_data = data_values
            self.current_idx = 0
            self.progress_var.set(0)
            self._log(f"총 {len(data_values):,}개의 측정 샘플 로드 완료!")
            return True

        except Exception as e:
            messagebox.showerror("오류", f"CSV 읽기 실패: {e}")
            return False

    def toggle_play(self):
        if not self.is_playing:
            if not self.load_csv_data(): return
            
            broker = self.ent_broker.get().strip()
            port = int(self.ent_port.get().strip())
            mac = self.ent_mac.get().strip()

            try:
                self.client = mqtt_client.Client(callback_api_version=CallbackAPIVersion.VERSION2, client_id=f"Sim_CSV_{mac}")
                self.client.on_connect = lambda c, u, f, rc, p=None: self._log("MQTT 브로커 연결 성공!")
                self.client.connect(broker, port)
                self.client.loop_start()

                self.is_playing = True
                self.btn_play.config(text="⏹ 재생 중지 (Stop)", bg="salmon")
                
                self.play_thread = threading.Thread(target=self.play_loop, args=(mac,), daemon=True)
                self.play_thread.start()
            except Exception as e:
                self._log(f"MQTT 연결 실패: {e}", "ERR")
        else:
            self.is_playing = False
            if self.client:
                self.client.loop_stop()
                self.client.disconnect()
            self.btn_play.config(text="▶ 측정 데이터(CSV) 실시간 스트리밍 시작", bg="lightgreen")
            self._log("재생이 중지되었습니다.")

    def play_loop(self, mac):
        topic = f"V/{mac}"
        scale_factor = float(self.ent_scale.get() or 10000) 
        period_ms = int(self.ent_period.get() or 1000)
        
        total_samples = len(self.csv_data)
        
        while self.is_playing:
            # 💡 [추가] UI에서 배터리와 샘플레이트 값 실시간으로 읽기
            battery_val = self.ent_batt.get().strip() or "10"
            samplerate_val = self.cb_sr.get().strip() or "25600"
            
            end_idx = self.current_idx + SAMPLE_COUNT
            chunk = self.csv_data[self.current_idx:end_idx]
            
            if len(chunk) < SAMPLE_COUNT:
                remainder = SAMPLE_COUNT - len(chunk)
                chunk.extend(self.csv_data[:remainder])
                self.current_idx = remainder
            else:
                self.current_idx = end_idx

            np_chunk = np.array(chunk) * scale_factor
            raw_samples = np.clip(np_chunk, -32768, 32767).astype(int)
            hex_data = "".join([f"{val & 0xFFFF:04X}" for val in raw_samples])

            frame_hex = f"{self.seq:X}"
            seq_str = f"0{frame_hex}"
            current_tick = str(int(time.time() * 1000))

            # 💡 [적용] 하드코딩 대신 UI에서 받아온 변수 삽입
            payload = {
                "gwMacAddr": "HALOW", 
                "dvicMacAddr": mac, 
                "batteryRmin": battery_val, 
                "seq": seq_str, 
                "samplerate": samplerate_val, 
                "numofsample": str(SAMPLE_COUNT), 
                "tick": current_tick, 
                "sensorData": hex_data
            }
            
            self.client.publish(topic, json.dumps(payload))

            pct = (self.current_idx / total_samples) * 100
            self.progress_var.set(pct)
            self.lbl_status.config(text=f"스트리밍 중... ({self.current_idx:,} / {total_samples:,} 샘플)")

            self._log(f"PUB [{mac}] {SAMPLE_COUNT}개 전송 완료 (Batt: {battery_val}, SR: {samplerate_val})", "STREAM")

            self.seq = 1 if self.seq >= 15 else self.seq + 1
            time.sleep(max(0.01, period_ms / 1000.0))

if __name__ == "__main__":
    root = tk.Tk()
    app = CsvRawStreamGUI(root)
    root.mainloop()