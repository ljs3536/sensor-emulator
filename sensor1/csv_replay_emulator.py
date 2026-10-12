import os
import json
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from paho.mqtt import client as mqtt_client
from paho.mqtt.enums import CallbackAPIVersion
from datetime import datetime
import glob
import pandas as pd # 💡 CSV 처리를 위해 pandas 사용

# =========================================================
# 기본 통신 설정
# =========================================================
BROKER_DEFAULT = "15.165.63.242"
PORT_DEFAULT = 1883

class CsvReplayGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("센서 실측 패킷(CSV) 완벽 재생 에뮬레이터")
        self.root.geometry("850x600")

        self.client = None
        self.is_playing = False
        self.play_thread = None

        self.csv_df = None      # pandas DataFrame 버퍼
        self.total_rows = 0
        self.current_idx = 0    # 현재 재생 위치(행 번호)

        self._build_ui()
        self.scan_csv_files()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        # 1. 브로커 설정
        conn_frame = ttk.LabelFrame(main, text="1. MQTT 브로커 설정", padding=10)
        conn_frame.pack(fill="x", pady=5)

        ttk.Label(conn_frame, text="Broker:").grid(row=0, column=0, padx=5, pady=5)
        self.ent_broker = ttk.Entry(conn_frame, width=15)
        self.ent_broker.insert(0, BROKER_DEFAULT)
        self.ent_broker.grid(row=0, column=1, padx=5)

        ttk.Label(conn_frame, text="Port:").grid(row=0, column=2, padx=5)
        self.ent_port = ttk.Entry(conn_frame, width=5)
        self.ent_port.insert(0, str(PORT_DEFAULT))
        self.ent_port.grid(row=0, column=3, padx=5)

        # 2. CSV 로드 및 설정
        csv_frame = ttk.LabelFrame(main, text="2. 실측 패킷(CSV) 로드 및 재생 설정", padding=10)
        csv_frame.pack(fill="x", pady=5)

        ttk.Label(csv_frame, text="재생할 CSV 파일:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.cb_csv = ttk.Combobox(csv_frame, values=[], state="readonly", width=50)
        self.cb_csv.grid(row=0, column=1, columnspan=2, padx=5)
        ttk.Button(csv_frame, text="🔄 새로고침", command=self.scan_csv_files).grid(row=0, column=3, padx=5)

        ttk.Label(csv_frame, text="패킷 전송 간격(초):").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.ent_delay = ttk.Entry(csv_frame, width=10)
        self.ent_delay.insert(0, "2.0")
        self.ent_delay.grid(row=1, column=1, sticky="w", padx=5)

        # 3. 제어 버튼
        ctrl_frame = ttk.Frame(main)
        ctrl_frame.pack(fill="x", pady=10)
        self.btn_play = tk.Button(ctrl_frame, text="▶ 측정 패킷(CSV) 서버로 전송 시작", bg="lightgreen", font=("Arial", 12, "bold"), command=self.toggle_play)
        self.btn_play.pack(fill="x", ipady=10)

        # 4. 재생 진행률 바
        self.progress_var = tk.DoubleVar()
        self.progress = ttk.Progressbar(main, variable=self.progress_var, maximum=100)
        self.progress.pack(fill="x", pady=5)
        self.lbl_status = ttk.Label(main, text="대기 중...")
        self.lbl_status.pack(pady=2)

        # 5. 로그 영역
        log_frame = ttk.LabelFrame(main, text="3. 전송 로그", padding=10)
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

        self._log(f"[{filename}] 패킷 분석 중...")
        
        try:
            # 💡 [핵심] pandas로 CSV 통째로 읽기 및 결측치 처리
            df = pd.read_csv(filename)
            df = df.fillna('')
            
            if 'sensorData_hex' not in df.columns:
                messagebox.showerror("오류", "'sensorData_hex' 컬럼이 없는 파일입니다. 형식을 확인하세요.")
                return False

            self.csv_df = df
            self.total_rows = len(df)
            self.current_idx = 0
            self.progress_var.set(0)
            
            self._log(f"총 {self.total_rows:,}개의 패킷(Row) 로드 완료!")
            return True

        except Exception as e:
            messagebox.showerror("오류", f"CSV 읽기 실패: {e}")
            return False

    def toggle_play(self):
        if not self.is_playing:
            if not self.load_csv_data(): return
            
            broker = self.ent_broker.get().strip()
            port = int(self.ent_port.get().strip())

            try:
                # 클라이언트 ID 중복 방지
                client_id = f"Sim_CSV_Replay_{int(time.time())}"
                self.client = mqtt_client.Client(callback_api_version=CallbackAPIVersion.VERSION2, client_id=client_id)
                self.client.on_connect = lambda c, u, f, rc, p=None: self._log("MQTT 브로커 연결 성공!")
                self.client.connect(broker, port)
                self.client.loop_start()

                self.is_playing = True
                self.btn_play.config(text="⏹ 전송 중지 (Stop)", bg="salmon")
                
                self.play_thread = threading.Thread(target=self.play_loop, daemon=True)
                self.play_thread.start()
            except Exception as e:
                self._log(f"MQTT 연결 실패: {e}", "ERR")
        else:
            self.is_playing = False
            if self.client:
                self.client.loop_stop()
                self.client.disconnect()
            self.btn_play.config(text="▶ 측정 패킷(CSV) 서버로 전송 시작", bg="lightgreen")
            self._log("패킷 전송이 중지되었습니다.")

    def play_loop(self):
        delay_sec = float(self.ent_delay.get() or 2.0)
        
        while self.is_playing and self.current_idx < self.total_rows:
            row = self.csv_df.iloc[self.current_idx]
            
            mac = str(row['dvicMacAddr']).strip()
            topic = f"V/{mac}"
            
            seq_val = str(row['seq']).strip()
            if not seq_val: seq_val = "01"
                
            battery = str(row['batteryRmin']).strip()
            if not battery or battery == "0.0": battery = "0"
            
            current_tick = str(int(time.time() * 1000))

            # 💡 CSV에 있는 배터리, 샘플레이트, 샘플개수 원본 그대로 주입
            payload = {
                "gwMacAddr": str(row.get('gwMacAddr', 'HALOW')).strip(),
                "dvicMacAddr": mac,
                "batteryRmin": battery,
                "seq": seq_val.zfill(2),
                "samplerate": str(row.get('samplerate', '25600')).strip(),
                "numofsample": str(row.get('numofsample', '8192')).strip(),
                "tick": current_tick,
                "sensorData": str(row['sensorData_hex']).strip()
            }
            
            self.client.publish(topic, json.dumps(payload))

            # UI 갱신
            self.current_idx += 1
            pct = (self.current_idx / self.total_rows) * 100
            self.progress_var.set(pct)
            self.lbl_status.config(text=f"전송 중... ({self.current_idx:,} / {self.total_rows:,} 패킷 완료)")

            self._log(f"PUB [{mac}] 패킷 전송 (Seq: {payload['seq']}, Batt: {battery})", "REPLAY")
            
            # 다음 패킷 전송 전 대기
            if self.current_idx < self.total_rows:
                time.sleep(delay_sec)

        # 모두 전송 시 자동 종료 처리
        if self.current_idx >= self.total_rows:
            self._log("CSV 끝에 도달하여 전송을 종료합니다.", "DONE")
            # tkinter 메인 스레드에서 버튼 상태 원복
            self.root.after(0, self.reset_play_state)

    def reset_play_state(self):
        self.is_playing = False
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
        self.btn_play.config(text="▶ 측정 패킷(CSV) 서버로 전송 시작", bg="lightgreen")

if __name__ == "__main__":
    root = tk.Tk()
    app = CsvReplayGUI(root)
    root.mainloop()