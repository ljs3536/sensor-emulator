import json
import time
import threading
import struct
import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox
from paho.mqtt import client as mqtt_client
from paho.mqtt.enums import CallbackAPIVersion

# =========================================================
# 기본 CONFIG 및 MQTT 설정
# =========================================================
BROKER_DEFAULT = "15.165.63.242"
PORT_DEFAULT = 1883
NUM_SLOTS = 10  # 10개의 개별 슬롯 생성

class MultiUltrasonicEmulatorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("초음파(Ultrasonic) 센서 [10슬롯 개별 제어] 에뮬레이터")
        self.root.geometry("1150x700")

        self.device_config = {
            "SENSING_PERIOD": "1000"
        }

        self.slots = []
        self.is_leaking = False  # 💡 전체 누출 상태 토글 변수

        self._build_ui()

    # ---------------------------------------------------------
    # 데이터 생성 로직 (초음파 전용 헤더 + 가스 누출 시그널)
    # ---------------------------------------------------------
    def generate_ultrasonic_hex_data(self, is_leak=False):
        size = 320
        base_noise = np.random.normal(loc=0.002, scale=0.0005, size=size)
        
        if not is_leak:
            # 정상 상태: 미세한 스파이크 발생
            spike_indices = np.random.choice(size, 8, replace=False)
            base_noise[spike_indices] += np.random.uniform(0.005, 0.015, 8)
            signal = base_noise
        else:
            # 누출 상태: 특정 대역에 가파른 피크(가스 새는 소리) 발생 및 전체 소음 상승
            x = np.linspace(0, size - 1, size)
            leak_bump = 0.08 * np.exp(-((x - 180) ** 2) / (2 * 20 ** 2))
            signal = base_noise + leak_bump + np.random.uniform(0.002, 0.01, size)

        signal = np.clip(signal, 0.0001, 1.0)
        
        # 초음파 센서 24자리 헤더 주입
        header_hex = "0000000000000000280A0000"
        data_hex = "".join([struct.pack('<f', val).hex().upper() for val in signal])
        
        return header_hex + data_hex

    # ---------------------------------------------------------
    # UI 빌드 (좌/우 분할)
    # ---------------------------------------------------------
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        # ====== [왼쪽 패널] Broker 설정 및 10개 MAC 슬롯 ======
        left_frame = ttk.Frame(main)
        left_frame.pack(side="left", fill="y", padx=(0, 10))

        conn_frame = ttk.LabelFrame(left_frame, text="1. 브로커 설정 및 전체 제어", padding=10)
        conn_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(conn_frame, text="Broker").grid(row=0, column=0, sticky="w")
        self.ent_broker = ttk.Entry(conn_frame, width=15)
        self.ent_broker.insert(0, BROKER_DEFAULT)
        self.ent_broker.grid(row=0, column=1, padx=5)

        ttk.Label(conn_frame, text="Port").grid(row=0, column=2)
        self.ent_port = ttk.Entry(conn_frame, width=5)
        self.ent_port.insert(0, str(PORT_DEFAULT))
        self.ent_port.grid(row=0, column=3, padx=5)

        ttk.Button(conn_frame, text="▶ 전체 시작", command=self.start_all).grid(row=1, column=0, columnspan=2, pady=5, sticky="we")
        ttk.Button(conn_frame, text="⏹ 전체 정지", command=self.stop_all).grid(row=1, column=2, columnspan=2, pady=5, sticky="we")

        slot_frame = ttk.LabelFrame(left_frame, text="2. 개별 센서 (MAC) 제어", padding=10)
        slot_frame.pack(fill="both", expand=True)

        for i in range(NUM_SLOTS):
            row_frame = ttk.Frame(slot_frame)
            row_frame.pack(fill="x", pady=4)
            
            ttk.Label(row_frame, text=f"#{i+1:02d}").pack(side="left")
            
            ent_mac = ttk.Entry(row_frame, width=16)
            ent_mac.insert(0, f"1A253C466F{i+1:02d}")  # 💡 초음파 센서 기본 MAC 세팅
            ent_mac.pack(side="left", padx=5)

            btn_start = ttk.Button(row_frame, text="▶ 시작", width=6, command=lambda idx=i: self.start_slot(idx))
            btn_start.pack(side="left", padx=2)

            btn_stop = ttk.Button(row_frame, text="⏹ 정지", width=6, state="disabled", command=lambda idx=i: self.stop_slot(idx))
            btn_stop.pack(side="left", padx=2)
            
            status_lbl = ttk.Label(row_frame, text="OFF", foreground="gray", width=4)
            status_lbl.pack(side="left", padx=5)

            self.slots.append({
                "mac_entry": ent_mac,
                "btn_start": btn_start,
                "btn_stop": btn_stop,
                "status_lbl": status_lbl,
                "client": None,
                "is_connected": False,
                "seq": 1,
                "thread": None
            })

        # ====== [오른쪽 패널] 센서 설정 및 로그 ======
        right_frame = ttk.Frame(main)
        right_frame.pack(side="right", fill="both", expand=True)

        ctrl_frame = ttk.LabelFrame(right_frame, text="3. 공통 센서 동작 모드 설정", padding=10)
        ctrl_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(ctrl_frame, text="전송 주기(ms)").grid(row=0, column=0, sticky="w", pady=10)
        self.ent_period = ttk.Entry(ctrl_frame, width=10)
        self.ent_period.insert(0, "1000")
        self.ent_period.grid(row=0, column=1, pady=10, padx=10)
        ttk.Button(ctrl_frame, text="주기 적용", command=self.update_config).grid(row=0, column=2)

        wave_frame = ttk.LabelFrame(right_frame, text="4. 누출(Leak) 시뮬레이션 제어", padding=10)
        wave_frame.pack(fill="x", pady=10)

        # 💡 토글 형태의 누출 상태 제어 버튼
        self.btn_leak = tk.Button(wave_frame, text="🟢 현재 상태: 정상 (누출 없음)", bg="lightgreen", font=("Arial", 11, "bold"), command=self.toggle_leak)
        self.btn_leak.pack(fill="x", pady=15, ipadx=20, ipady=10)

        log_frame = ttk.LabelFrame(right_frame, text="5. 에뮬레이터 통합 로그", padding=10)
        log_frame.pack(fill="both", expand=True)

        self.txt_log = tk.Text(log_frame, wrap="none", height=15)
        self.txt_log.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.txt_log.yview)
        scroll.pack(side="right", fill="y")
        self.txt_log.configure(yscrollcommand=scroll.set)

    # ---------------------------------------------------------
    # 컨트롤 함수들
    # ---------------------------------------------------------
    def toggle_leak(self):
        self.is_leaking = not self.is_leaking
        if self.is_leaking:
            self.btn_leak.config(text="🚨 현재 상태: 가스/에어 누출 발생 중 (10대 일괄 적용)!!", bg="salmon")
            self._log("⚠️ 누출(Leak) 모드 ON! 실행 중인 모든 센서에서 고주파 이상 데이터가 전송됩니다.", "WARN")
        else:
            self.btn_leak.config(text="🟢 현재 상태: 정상 (누출 없음)", bg="lightgreen")
            self._log("✅ 정상(Normal) 모드 ON! 잔잔한 백그라운드 노이즈로 복귀합니다.", "INFO")

    def update_config(self):
        self.device_config["SENSING_PERIOD"] = self.ent_period.get().strip() or "1000"
        self._log(f"⚙️ 주기 설정 변경: PERIOD={self.device_config['SENSING_PERIOD']}ms", "SYS")

    def _log(self, msg, tag="INFO"):
        ts = time.strftime("%H:%M:%S")
        self.txt_log.insert("end", f"[{ts}] [{tag}] {msg}\n")
        self.txt_log.see("end")

    # ---------------------------------------------------------
    # 개별 슬롯 제어 로직
    # ---------------------------------------------------------
    def start_all(self):
        for i in range(NUM_SLOTS):
            if not self.slots[i]["is_connected"]:
                self.start_slot(i)

    def stop_all(self):
        for i in range(NUM_SLOTS):
            if self.slots[i]["is_connected"]:
                self.stop_slot(i)

    def start_slot(self, idx):
        slot = self.slots[idx]
        if slot["is_connected"]: return

        broker = self.ent_broker.get().strip()
        port = int(self.ent_port.get().strip())
        mac = slot["mac_entry"].get().strip()
        
        if not mac:
            messagebox.showwarning("경고", f"#{idx+1} 슬롯의 MAC 주소가 비어있습니다.")
            return

        try:
            client = mqtt_client.Client(callback_api_version=CallbackAPIVersion.VERSION2, client_id=f"Sim_Ultra_{mac}")
            
            def on_connect(c, u, f, rc, p=None, m=mac):
                self._log(f"✅ #{idx+1} 연결됨 (MAC: {m})", "MQTT")

            client.on_connect = on_connect
            client.connect(broker, port)
            client.loop_start()

            slot["client"] = client
            slot["is_connected"] = True
            slot["mac_entry"].config(state="disabled")
            slot["btn_start"].config(state="disabled")
            slot["btn_stop"].config(state="normal")
            slot["status_lbl"].config(text="ON", foreground="green")

            t = threading.Thread(target=self.publish_loop, args=(idx, mac), daemon=True)
            slot["thread"] = t
            t.start()

        except Exception as e:
            self._log(f"❌ #{idx+1} 연결 실패: {e}", "ERR")

    def stop_slot(self, idx):
        slot = self.slots[idx]
        if not slot["is_connected"]: return

        slot["is_connected"] = False
        if slot["client"]:
            slot["client"].loop_stop()
            slot["client"].disconnect()

        slot["mac_entry"].config(state="normal")
        slot["btn_start"].config(state="normal")
        slot["btn_stop"].config(state="disabled")
        slot["status_lbl"].config(text="OFF", foreground="gray")
        self._log(f"🛑 #{idx+1} ({slot['mac_entry'].get()}) 정지됨", "SYS")

    def publish_loop(self, idx, mac):
        slot = self.slots[idx]
        # 💡 초음파 센서 토픽 규격 (D/)
        topic = f"D/{mac}" 
        
        while slot["is_connected"]:
            hex_data = self.generate_ultrasonic_hex_data(self.is_leaking)
            
            payload = {
                "gwMacAddr": "HALOW",
                "dvicMacAddr": mac,
                "batteryRmin": "10",
                "seq": f"{slot['seq']:02X}",
                "tick": str(int(time.time() * 1000)),
                "sensorData": hex_data
            }
            slot["client"].publish(topic, json.dumps(payload))

            # 초음파는 로그 폭주를 막기 위해 Sequence 1일 때만 살짝 로그를 찍어줌
            if slot["seq"] == 1:
                status_txt = "🚨 LEAK" if self.is_leaking else "🟢 NORMAL"
                self._log(f"PUB [#{idx+1}] {mac} 초음파 데이터 전송 중... [{status_txt}]", "PUB")

            # 초음파 센서는 진동(15)과 달리 255(FF)에서 롤오버
            slot["seq"] = 1 if slot["seq"] >= 255 else slot["seq"] + 1
            
            period_ms = int(self.device_config.get("SENSING_PERIOD", "1000"))
            time.sleep(max(0.01, period_ms / 1000.0))

if __name__ == "__main__":
    root = tk.Tk()
    app = MultiUltrasonicEmulatorGUI(root)
    root.mainloop()