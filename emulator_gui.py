import json
import time
import threading
import random
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
MAC_DEFAULT = "035415641614"

class DynamicVibrationEmulatorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("다이나믹 진동 센서 에뮬레이터 (PIEZO / ADXL)")
        self.root.geometry("750x620")

        # 센서 설정 상태
        self.device_config = {
            "COMMPATH": "0",
            "SENSORTYPE": "1",  # 0: STOP, 1: PIEZO, 2: ADXL
            "PIEZO_GAIN": "255",
            "PIEZO_SAMPLE_RATE": "25600",
            "PIEZO_SAMPLE_COUNT": "1024",
            "MEMS_SAMPLE_RATE": "3200",
            "MEMS_SAMPLE_COUNT": "1024",
            "MEMS_G_RANGE": "2",
            "SENSING_PERIOD": "1000"
        }

        # MQTT 관련
        self.mqtt_client = None
        self.is_connected = False
        self.seq_frame_counter = 1
        self.pub_thread = None

        self._build_ui()

    # =========================================================
    # 다이나믹 파형 생성기 (실제 센서 특성 반영)
    # =========================================================
    def generate_dynamic_hex_sensor_data(self, sample_count, base_freq=15, noise_level=120, shock_prob=0.15):
        """
        다이나믹 신호 생성:
        1. 다중 주파수 합성 (1차, 2차, 3차 조화파)
        2. 랜던 가우시안 노이즈
        3. 간헐적 기계 충격(Spike/Transient) 발생
        """
        t = np.linspace(0, 1.0, sample_count, endpoint=False)
        
        # 1. 복합 주파수 합성
        signal = (300 * np.sin(2 * np.pi * base_freq * t) +
                  150 * np.sin(2 * np.pi * (base_freq * 2.5) * t) +
                  80 * np.sin(2 * np.pi * (base_freq * 5.1) * t))

        # 2. 강한 가우시안 노이즈 추가 (난잡한 파형 연출)
        noise = np.random.normal(0, noise_level, sample_count)
        combined = signal + noise

        # 3. 간헐적 순간 충격(Spike) 발생
        if random.random() < shock_prob:
            spike_idx = random.randint(0, sample_count - 50)
            spike_len = random.randint(10, 40)
            spike_amp = random.choice([1, -1]) * random.randint(1500, 3500)
            combined[spike_idx:spike_idx + spike_len] += spike_amp * np.exp(-np.linspace(0, 3, spike_len))

        # short int16 범위 클리핑 (-32768 ~ 32767)
        raw_samples = np.clip(combined, -32768, 32767).astype(int)

        # HEX 4자리 Big-Endian 변환
        hex_data_list = [f"{val & 0xFFFF:04X}" for val in raw_samples]
        return "".join(hex_data_list)

    # =========================================================
    # UI 구성
    # =========================================================
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        # 1. Connection Frame
        conn_frame = ttk.LabelFrame(main, text="1. MQTT 브로커 및 디바이스 설정", padding=10)
        conn_frame.pack(fill="x", pady=5)

        ttk.Label(conn_frame, text="Broker").grid(row=0, column=0, sticky="w")
        self.ent_broker = ttk.Entry(conn_frame, width=16)
        self.ent_broker.insert(0, BROKER_DEFAULT)
        self.ent_broker.grid(row=0, column=1, padx=5)

        ttk.Label(conn_frame, text="Port").grid(row=0, column=2)
        self.ent_port = ttk.Entry(conn_frame, width=6)
        self.ent_port.insert(0, str(PORT_DEFAULT))
        self.ent_port.grid(row=0, column=3, padx=5)

        ttk.Label(conn_frame, text="MAC Addr").grid(row=0, column=4)
        self.ent_mac = ttk.Entry(conn_frame, width=16)
        self.ent_mac.insert(0, MAC_DEFAULT)
        self.ent_mac.grid(row=0, column=5, padx=5)

        self.btn_connect = ttk.Button(conn_frame, text="에뮬레이터 시작", command=self.start_emulator)
        self.btn_connect.grid(row=0, column=6, padx=10)

        # 2. Control Frame
        ctrl_frame = ttk.LabelFrame(main, text="2. 센서 실시간 수동 제어", padding=10)
        ctrl_frame.pack(fill="x", pady=10)

        ttk.Label(ctrl_frame, text="센서 동작 모드").grid(row=0, column=0, sticky="w")
        self.var_sensor_type = tk.StringVar(value="1")
        rb_stop = ttk.Radiobutton(ctrl_frame, text="STOP (0)", variable=self.var_sensor_type, value="0", command=self.update_config_from_ui)
        rb_piezo = ttk.Radiobutton(ctrl_frame, text="PIEZO (1)", variable=self.var_sensor_type, value="1", command=self.update_config_from_ui)
        rb_adxl = ttk.Radiobutton(ctrl_frame, text="ADXL/MEMS 3축 (2)", variable=self.var_sensor_type, value="2", command=self.update_config_from_ui)
        rb_stop.grid(row=0, column=1, padx=5)
        rb_piezo.grid(row=0, column=2, padx=5)
        rb_adxl.grid(row=0, column=3, padx=5)

        ttk.Label(ctrl_frame, text="전송 주기(ms)").grid(row=1, column=0, sticky="w", pady=5)
        self.ent_period = ttk.Entry(ctrl_frame, width=10)
        self.ent_period.insert(0, "1000")
        self.ent_period.grid(row=1, column=1, padx=5, pady=5)

        btn_apply_period = ttk.Button(ctrl_frame, text="주기 적용", command=self.update_config_from_ui)
        btn_apply_period.grid(row=1, column=2, padx=5)

        # 3. Waveform Dynamic Control (파형 제어)
        wave_frame = ttk.LabelFrame(main, text="3. 다이나믹 파형 노이즈/충격 제어", padding=10)
        wave_frame.pack(fill="x", pady=5)

        ttk.Label(wave_frame, text="기본 진동수 (Hz)").grid(row=0, column=0, sticky="w")
        self.scale_freq = ttk.Scale(wave_frame, from_=5, to=100, value=20)
        self.scale_freq.grid(row=0, column=1, sticky="ew", padx=5)

        ttk.Label(wave_frame, text="노이즈 수준").grid(row=1, column=0, sticky="w")
        self.scale_noise = ttk.Scale(wave_frame, from_=10, to=500, value=150)
        self.scale_noise.grid(row=1, column=1, sticky="ew", padx=5)

        self.btn_shock = ttk.Button(wave_frame, text="⚡ 순간 진동 충격(Spike) 강제 발생", command=self.trigger_shock)
        self.btn_shock.grid(row=2, column=0, columnspan=2, fill="x", pady=5)

        # 4. Log Box
        log_frame = ttk.LabelFrame(main, text="4. 에뮬레이터 송수신 로그", padding=10)
        log_frame.pack(fill="both", expand=True, pady=5)

        self.txt_log = tk.Text(log_frame, wrap="none", height=10)
        self.txt_log.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.txt_log.yview)
        scroll.pack(side="right", fill="y")
        self.txt_log.configure(yscrollcommand=scroll.set)

    # =========================================================
    # 제어 및 MQTT 로직
    # =========================================================
    def trigger_shock(self):
        self._log("⚡ [GUI] 강제 충격(Spike) 진동 신호 발생!", "SHOCK")
        self.force_shock = True

    def update_config_from_ui(self):
        self.device_config["SENSORTYPE"] = self.var_sensor_type.get()
        self.device_config["SENSING_PERIOD"] = self.ent_period.get().strip() or "1000"
        self._log(f"⚙️ [CONFIG UPDATE] SENSORTYPE={self.device_config['SENSORTYPE']}, PERIOD={self.device_config['SENSING_PERIOD']}ms", "SYS")

    def _log(self, msg, tag="INFO"):
        ts = time.strftime("%H:%M:%S")
        self.txt_log.insert("end", f"[{ts}] [{tag}] {msg}\n")
        self.txt_log.see("end")

    def start_emulator(self):
        if self.is_connected:
            messagebox.showinfo("알림", "이미 에뮬레이터가 동작 중입니다.")
            return

        broker = self.ent_broker.get().strip()
        port = int(self.ent_port.get().strip())
        mac = self.ent_mac.get().strip()
        topic = f"VIB/{mac}"

        try:
            self.mqtt_client = mqtt_client.Client(
                callback_api_version=CallbackAPIVersion.VERSION2,
                client_id=f"Dynamic_Emulator_{mac}"
            )
            self.mqtt_client.on_connect = lambda c, u, f, rc, p=None: self._log(f"✅ Connected to Broker. Subscribed: {topic}", "MQTT")
            self.mqtt_client.on_message = self.on_mqtt_message

            self.mqtt_client.connect(broker, port)
            self.mqtt_client.subscribe(topic)
            self.mqtt_client.loop_start()

            self.is_connected = True
            self.btn_connect.config(state="disabled")
            self._log(f"🚀 에뮬레이터 시작! Target Topic: {topic}", "SYS")

            # 송신 스레드 실행
            self.pub_thread = threading.Thread(target=self.publish_loop, args=(topic,), daemon=True)
            self.pub_thread.start()

        except Exception as e:
            messagebox.showerror("오류", f"MQTT 연결 실패: {e}")

    def on_mqtt_message(self, client, userdata, msg):
        """백엔드로부터 제어 명령 수신 처리"""
        try:
            payload_str = msg.payload.decode('utf-8').strip()
            req_json = json.loads(payload_str)

            # 자기가 발행한 패킷 무시
            if "sensorData" in req_json or ("COMMPATH" in req_json and "gwMacAddr" in req_json):
                return

            self._log(f"📩 [Command Received] {payload_str}", "CMD")

            # 설정값 변경 요청 시 동기화
            for k, v in req_json.items():
                if k in self.device_config:
                    self.device_config[k] = str(v)
            
            self.var_sensor_type.set(self.device_config.get("SENSORTYPE", "1"))
            self.ent_period.delete(0, "end")
            self.ent_period.insert(0, self.device_config.get("SENSING_PERIOD", "1000"))

        except Exception as e:
            self._log(f"명령 파싱 에러: {e}", "ERR")

    def publish_loop(self, topic):
        """데이터 주기적 발행 루프[cite: 6]"""
        self.force_shock = False

        while self.is_connected:
            sensor_type = self.device_config.get("SENSORTYPE", "1")
            
            if sensor_type == "0":  # STOP
                time.sleep(0.5)
                continue

            frame_hex = f"{self.seq_frame_counter:X}"
            base_freq = self.scale_freq.get()
            noise_lvl = self.scale_noise.get()
            shock_p = 0.8 if self.force_shock else 0.1
            self.force_shock = False  # 1회 발생 후 초기화

            # -------------------------------------------------
            # CASE 1: PIEZO (1채널)[cite: 6]
            # -------------------------------------------------
            if sensor_type == "1":
                seq_str = f"0{frame_hex}"
                sample_count = int(self.device_config.get("PIEZO_SAMPLE_COUNT", "1024"))
                hex_data = self.generate_dynamic_hex_sensor_data(sample_count, base_freq, noise_lvl, shock_p)

                payload = {
                    "gwMacAddr": "HALOW", "dvicMacAddr": self.ent_mac.get().strip(),
                    "batteryRmin": "5", "seq": seq_str, "tick": str(int(time.time() * 1000)),
                    "sensorData": hex_data
                }
                self.mqtt_client.publish(topic, json.dumps(payload))
                self._log(f"PUB PIEZO -> seq: {seq_str}, len: {len(hex_data)}", "PUB")

            # -------------------------------------------------
            # CASE 2: ADXL / MEMS (3축 낱개전송)[cite: 6]
            # -------------------------------------------------
            elif sensor_type == "2":
                sample_count = int(self.device_config.get("ADXL_SAMPLE_COUNT", "1024"))

                for axis_idx, axis_name in enumerate(["X", "Y", "Z"], start=1):
                    seq_axis = f"{axis_idx}{frame_hex}"
                    axis_freq = base_freq * (1.0 + axis_idx * 0.3)
                    hex_data = self.generate_dynamic_hex_sensor_data(sample_count, axis_freq, noise_lvl, shock_p)

                    payload = {
                        "gwMacAddr": "HALOW", "dvicMacAddr": self.ent_mac.get().strip(),
                        "batteryRmin": "5", "seq": seq_axis, "tick": str(int(time.time() * 1000)),
                        "sensorData": hex_data
                    }
                    self.mqtt_client.publish(topic, json.dumps(payload))

                self._log(f"PUB ADXL 3-AXIS -> frame: {frame_hex}, samples: {sample_count}", "PUB")

            # 시퀀스 및 대기 시간[cite: 6]
            self.seq_frame_counter = 1 if self.seq_frame_counter >= 15 else self.seq_frame_counter + 1
            period_ms = int(self.device_config.get("SENSING_PERIOD", "1000"))
            time.sleep(max(0.01, period_ms / 1000.0))


if __name__ == "__main__":
    root = tk.Tk()
    app = DynamicVibrationEmulatorGUI(root)
    root.mainloop()