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
MAC_DEFAULT = "1A253C466F70" # 초음파 센서 MAC

class UltrasonicEmulatorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("초음파(Ultrasonic) 센서 에뮬레이터 (실제 데이터 완벽 모사형)")
        self.root.geometry("640x500")

        self.mqtt_client = None
        self.is_connected = False
        self.pub_thread = None
        self.is_leaking = False

        self._build_ui()

    def generate_ultrasonic_hex_data(self, is_leak=False):
        """
        자바가 기대하는 실제 하드웨어 규격:
        [24자리 헤더] + [320개의 Float -> Little Endian Hex(8자리씩)]
        """
        size = 320
        
        # 💡 핵심: 대리님이 주신 실제 데이터 값(0.001~0.003)과 완벽하게 일치하는 베이스 노이즈
        base_noise = np.random.normal(loc=0.002, scale=0.0005, size=size)
        
        if not is_leak:
            # [정상 상태] 이따금씩 0.005 ~ 0.015 수준의 미세 피크가 발생 (사용자 실제 데이터 반영)
            spike_indices = np.random.choice(size, 8, replace=False)
            base_noise[spike_indices] += np.random.uniform(0.005, 0.015, 8)
            signal = base_noise
        else:
            # [누출 상태] 누출 시 특정 대역(인덱스 180 부근)에서 0.08 수준의 가파른 피크 발생
            x = np.linspace(0, size - 1, size)
            leak_bump = 0.08 * np.exp(-((x - 180) ** 2) / (2 * 20 ** 2))
            
            # 전체적인 소음 대역도 미세하게 상승
            signal = base_noise + leak_bump + np.random.uniform(0.002, 0.01, size)

        # 절대 음수나 0이 나오지 않게 하한선 보정
        signal = np.clip(signal, 0.0001, 1.0)
        
        # 2. 자바가 파싱하는 24자리 헤더 강제 주입
        # 인덱스 16~17: minKhz (예: 40kHz -> 0x28)
        # 인덱스 18~19: intervalHz (예: 10Hz -> 0x0A)
        # 0000000000000000(16) + 28(2) + 0A(2) + 0000(4) = 24자리
        header_hex = "0000000000000000280A0000"
        
        # 3. Float 배열을 Little Endian Hex (8자리) 문자열로 팩킹 (자바 ByteBuffer와 완벽 호환)
        data_hex = "".join([struct.pack('<f', val).hex().upper() for val in signal])
        
        return header_hex + data_hex

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        conn_frame = ttk.LabelFrame(main, text="1. MQTT 설정", padding=10)
        conn_frame.pack(fill="x", pady=5)

        ttk.Label(conn_frame, text="Broker").grid(row=0, column=0, sticky="w")
        self.ent_broker = ttk.Entry(conn_frame, width=15)
        self.ent_broker.insert(0, BROKER_DEFAULT)
        self.ent_broker.grid(row=0, column=1, padx=5)

        ttk.Label(conn_frame, text="Port").grid(row=0, column=2)
        self.ent_port = ttk.Entry(conn_frame, width=5)
        self.ent_port.insert(0, str(PORT_DEFAULT))
        self.ent_port.grid(row=0, column=3, padx=5)

        ttk.Label(conn_frame, text="MAC Addr").grid(row=0, column=4)
        self.ent_mac = ttk.Entry(conn_frame, width=15)
        self.ent_mac.insert(0, MAC_DEFAULT)
        self.ent_mac.grid(row=0, column=5, padx=5)

        self.btn_connect = ttk.Button(conn_frame, text="▶ 전송 시작", command=self.start_emulator)
        self.btn_connect.grid(row=0, column=6, padx=5)

        self.btn_stop = ttk.Button(conn_frame, text="⏹ 전송 정지", command=self.stop_emulator, state="disabled")
        self.btn_stop.grid(row=0, column=7, padx=5)

        ctrl_frame = ttk.LabelFrame(main, text="2. 초음파 센서 제어", padding=10)
        ctrl_frame.pack(fill="x", pady=10)

        ttk.Label(ctrl_frame, text="전송 주기(ms): ").grid(row=0, column=0, sticky="w")
        self.ent_period = ttk.Entry(ctrl_frame, width=10)
        self.ent_period.insert(0, "1000")
        self.ent_period.grid(row=0, column=1, padx=5)

        self.btn_leak = tk.Button(ctrl_frame, text="🟢 현재 상태: 정상 (누출 없음)", bg="lightgreen", font=("Arial", 11, "bold"), command=self.toggle_leak)
        self.btn_leak.grid(row=1, column=0, columnspan=2, pady=15, ipadx=20, ipady=10)

        log_frame = ttk.LabelFrame(main, text="3. 전송 로그", padding=10)
        log_frame.pack(fill="both", expand=True, pady=5)

        self.txt_log = tk.Text(log_frame, wrap="none", height=10)
        self.txt_log.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.txt_log.yview)
        scroll.pack(side="right", fill="y")
        self.txt_log.configure(yscrollcommand=scroll.set)

    def toggle_leak(self):
        self.is_leaking = not self.is_leaking
        if self.is_leaking:
            self.btn_leak.config(text="🚨 현재 상태: 가스/에어 누출 발생 중!!", bg="salmon")
            self._log("⚠️ 누출(Leak) 모드 ON! 고주파 소음 데이터가 전송됩니다.", "WARN")
        else:
            self.btn_leak.config(text="🟢 현재 상태: 정상 (누출 없음)", bg="lightgreen")
            self._log("✅ 정상(Normal) 모드 ON! 잔잔한 백그라운드 노이즈 전송.", "INFO")

    def _log(self, msg, tag="INFO"):
        ts = time.strftime("%H:%M:%S")
        self.txt_log.insert("end", f"[{ts}] [{tag}] {msg}\n")
        self.txt_log.see("end")

    def start_emulator(self):
        if self.is_connected:
            return

        broker = self.ent_broker.get().strip()
        port = int(self.ent_port.get().strip())
        mac = self.ent_mac.get().strip()
        
        topic = f"D/{mac}" 

        try:
            self.mqtt_client = mqtt_client.Client(
                callback_api_version=CallbackAPIVersion.VERSION2,
                client_id=f"UltraSonic_Emulator_{mac}"
            )
            self.mqtt_client.on_connect = lambda c, u, f, rc, p=None: self._log(f"✅ Broker 연결됨. Topic: {topic}", "MQTT")
            
            self.mqtt_client.connect(broker, port)
            self.mqtt_client.loop_start()

            self.is_connected = True
            
            self.btn_connect.config(state="disabled")
            self.btn_stop.config(state="normal")

            self.pub_thread = threading.Thread(target=self.publish_loop, args=(topic, mac), daemon=True)
            self.pub_thread.start()

        except Exception as e:
            messagebox.showerror("오류", f"MQTT 연결 실패: {e}")

    def stop_emulator(self):
        if not self.is_connected:
            return
        
        self.is_connected = False
        
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            
        self.btn_connect.config(state="normal")
        self.btn_stop.config(state="disabled")
        
        self._log("🛑 데이터 전송이 정지되었습니다. (MQTT 연결 해제)", "SYS")

    def publish_loop(self, topic, mac):
        seq = 1
        while self.is_connected:
            period_ms = int(self.ent_period.get().strip() or "1000")
            
            # 실제 데이터에 맞춘 아주 미세한 Hex 시그널 생성
            sensor_data_hex = self.generate_ultrasonic_hex_data(self.is_leaking)
            
            payload = {
                "gwMacAddr": "HALOW",
                "dvicMacAddr": mac,
                "batteryRmin": "10",
                "seq": f"{seq:02X}",
                "tick": str(int(time.time() * 1000)),
                "sensorData": sensor_data_hex
            }
            
            try:
                self.mqtt_client.publish(topic, json.dumps(payload))
            except Exception as e:
                self._log(f"Publish 에러: {e}", "ERR")
                break
            
            status_txt = "LEAK" if self.is_leaking else "NORMAL"
            self._log(f"PUB [{status_txt}] -> seq:{seq:02X}, data[:32]: {sensor_data_hex[:32]}...", "PUB")
            
            seq = 1 if seq >= 255 else seq + 1
            time.sleep(max(0.01, period_ms / 1000.0))

if __name__ == "__main__":
    root = tk.Tk()
    app = UltrasonicEmulatorGUI(root)
    root.mainloop()