import json
import time
import threading
import random
import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox
from paho.mqtt import client as mqtt_client
from paho.mqtt.enums import CallbackAPIVersion
from datetime import datetime

# =========================================================
# 기본 CONFIG 및 MQTT 설정
# =========================================================
BROKER_DEFAULT = "15.165.63.242"
PORT_DEFAULT = 1883

class MultiVibrationEmulatorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("다이나믹 진동 센서 [무한 확장형] 에뮬레이터")
        self.root.geometry("1150x750")

        self.device_config = {
            "SENSORTYPE": "1",  # 0: STOP, 1: PIEZO, 2: ADXL
            "SENSING_PERIOD": "1000"
        }

        # 동적 슬롯 관리 리스트 및 ID 발급용 변수
        self.slots = []
        self.next_slot_id = 1 
        self.force_shock = False

        self._build_ui()
        
        # 프로그램 시작 시 기본적으로 5개의 센서 슬롯 세팅
        for _ in range(5):
            self.add_slot()

    # ---------------------------------------------------------
    # 데이터 생성 로직 (기존과 동일)
    # ---------------------------------------------------------
    def generate_realistic_vibration(self, sample_count, base_freq, noise_level, shock_prob, dc_offset, sensor_type, axis=None):
        t = np.linspace(0, 1.0, sample_count, endpoint=False)
        
        if sensor_type == "PIEZO":
            signal = (20 * np.sin(2 * np.pi * base_freq * t) + 
                      10 * np.sin(2 * np.pi * (base_freq * 3.5) * t) + 
                      5 * np.sin(2 * np.pi * (base_freq * 7.2) * t))
            noise = np.random.normal(0, noise_level * 0.3, sample_count)
            micro_spikes = np.random.choice([0, 1, -1], size=sample_count, p=[0.99, 0.005, 0.005]) * (noise_level * 1.5)
            combined = dc_offset + signal + noise + micro_spikes
        else:
            phase_shift = random.uniform(0, 2 * np.pi)
            if axis == "X":
                signal = 30 * np.sin(2 * np.pi * base_freq * t + phase_shift)
                noise = np.random.normal(0, noise_level * 0.2, sample_count)
            elif axis == "Y":
                signal = 25 * np.sin(2 * np.pi * (base_freq * 1.2) * t + phase_shift + np.pi/4)
                noise = np.random.normal(0, noise_level * 0.25, sample_count)
            else: 
                signal = 50 * np.sin(2 * np.pi * base_freq * t + phase_shift) + 15 * np.sin(2 * np.pi * (base_freq * 2.1) * t)
                noise = np.random.normal(0, noise_level * 0.3, sample_count)
            combined = dc_offset + signal + noise

        if random.random() < shock_prob:
            spike_idx = random.randint(0, max(1, sample_count - 100))
            spike_len = random.randint(20, 60)
            spike_amp = random.choice([1, -1]) * random.randint(300, 800)
            damping = np.exp(-np.linspace(0, 4, spike_len))
            shock_wave = spike_amp * np.sin(2 * np.pi * np.linspace(0, 2.5, spike_len)) * damping
            combined[spike_idx:spike_idx + spike_len] += shock_wave

        window = np.ones(3) / 3.0
        combined = np.convolve(combined, window, mode='same')
        raw_samples = np.clip(combined, -32768, 32767).astype(int)
        hex_data_list = [f"{val & 0xFFFF:04X}" for val in raw_samples]
        return "".join(hex_data_list)
    
    # ---------------------------------------------------------
    # UI 빌드 (좌/우 분할 및 동적 스크롤 적용)
    # ---------------------------------------------------------
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        left_frame = ttk.Frame(main)
        left_frame.pack(side="left", fill="y", padx=(0, 10))

        # 1. Broker 설정 영역
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

        # 2. 동적 센서 (MAC) 제어 영역 (스크롤바 포함)
        slot_outer_frame = ttk.LabelFrame(left_frame, text="2. 개별 센서 (MAC) 제어", padding=10)
        slot_outer_frame.pack(fill="both", expand=True)

        # 상단 추가 버튼
        ctrl_panel = ttk.Frame(slot_outer_frame)
        ctrl_panel.pack(fill="x", pady=(0, 5))
        ttk.Button(ctrl_panel, text="➕ 센서 슬롯 1개 추가", command=self.add_slot).pack(side="left")

        # 캔버스를 이용한 스크롤 가능한 프레임 구현
        self.canvas = tk.Canvas(slot_outer_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(slot_outer_frame, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)

        self.scrollable_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # ====== [오른쪽 패널] 센서 설정 및 로그 ======
        right_frame = ttk.Frame(main)
        right_frame.pack(side="right", fill="both", expand=True)

        ctrl_frame = ttk.LabelFrame(right_frame, text="3. 공통 센서 동작 모드 설정", padding=10)
        ctrl_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(ctrl_frame, text="센서 종류").grid(row=0, column=0, sticky="w")
        self.var_sensor_type = tk.StringVar(value="1")
        ttk.Radiobutton(ctrl_frame, text="STOP (0)", variable=self.var_sensor_type, value="0", command=self.update_config).grid(row=0, column=1)
        ttk.Radiobutton(ctrl_frame, text="PIEZO (1)", variable=self.var_sensor_type, value="1", command=self.update_config).grid(row=0, column=2)
        ttk.Radiobutton(ctrl_frame, text="ADXL 3축 (2)", variable=self.var_sensor_type, value="2", command=self.update_config).grid(row=0, column=3)

        ttk.Label(ctrl_frame, text="전송 주기(ms)").grid(row=1, column=0, sticky="w", pady=10)
        self.ent_period = ttk.Entry(ctrl_frame, width=10)
        self.ent_period.insert(0, "1000")
        self.ent_period.grid(row=1, column=1, pady=10)
        ttk.Button(ctrl_frame, text="적용", command=self.update_config).grid(row=1, column=2)

        wave_frame = ttk.LabelFrame(right_frame, text="4. 다이나믹 파형 노이즈/충격 제어", padding=10)
        wave_frame.pack(fill="x", pady=10)
        wave_frame.columnconfigure(1, weight=1)

        ttk.Label(wave_frame, text="기본 진동수 (Hz)").grid(row=0, column=0, sticky="w")
        self.scale_freq = ttk.Scale(wave_frame, from_=5, to=100, value=20)
        self.scale_freq.grid(row=0, column=1, sticky="ew", padx=5)

        ttk.Label(wave_frame, text="노이즈 수준").grid(row=1, column=0, sticky="w")
        self.scale_noise = ttk.Scale(wave_frame, from_=10, to=500, value=150)
        self.scale_noise.grid(row=1, column=1, sticky="ew", padx=5)

        self.btn_shock = tk.Button(wave_frame, text="⚡ 실행 중인 모든 센서에 충격(Spike) 발생!", bg="yellow", font=("Arial", 10, "bold"), command=self.trigger_shock)
        self.btn_shock.grid(row=2, column=0, columnspan=2, sticky="ew", pady=10, ipady=5)

        log_frame = ttk.LabelFrame(right_frame, text="5. 에뮬레이터 통합 로그", padding=10)
        log_frame.pack(fill="both", expand=True)

        self.txt_log = tk.Text(log_frame, wrap="none", height=15)
        self.txt_log.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.txt_log.yview)
        scroll.pack(side="right", fill="y")
        self.txt_log.configure(yscrollcommand=scroll.set)

    # ---------------------------------------------------------
    # 동적 슬롯 관리 기능 (추가 / 삭제)
    # ---------------------------------------------------------
    def add_slot(self):
        curr_id = self.next_slot_id
        self.next_slot_id += 1
        
        row_frame = ttk.Frame(self.scrollable_frame)
        row_frame.pack(fill="x", pady=4)
        
        ttk.Label(row_frame, text=f"#{curr_id:02d}").pack(side="left")
        
        ent_mac = ttk.Entry(row_frame, width=16)
        ent_mac.insert(0, f"0354156416{curr_id+9:02d}")  # 겹치지 않는 MAC 주소 임시 생성
        ent_mac.pack(side="left", padx=5)

        # 객체 참조 기반의 슬롯 딕셔너리 생성
        slot = {
            "id": curr_id,
            "frame": row_frame,
            "mac_entry": ent_mac,
            "is_connected": False,
            "client": None,
            "seq": 1,
            "thread": None
        }

        # 람다식 파라미터(s=slot)로 캡처링 방지
        btn_start = ttk.Button(row_frame, text="▶", width=3, command=lambda s=slot: self.start_slot(s))
        btn_start.pack(side="left", padx=2)

        btn_stop = ttk.Button(row_frame, text="⏹", width=3, state="disabled", command=lambda s=slot: self.stop_slot(s))
        btn_stop.pack(side="left", padx=2)
        
        status_lbl = ttk.Label(row_frame, text="OFF", foreground="gray", width=4)
        status_lbl.pack(side="left", padx=5)
        
        btn_del = ttk.Button(row_frame, text="❌", width=3, command=lambda s=slot: self.remove_slot(s))
        btn_del.pack(side="left", padx=5)

        slot["btn_start"] = btn_start
        slot["btn_stop"] = btn_stop
        slot["status_lbl"] = status_lbl
        
        self.slots.append(slot)
        
        # 슬롯 추가 후 자동으로 맨 아래로 스크롤
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def remove_slot(self, slot):
        if slot["is_connected"]:
            self.stop_slot(slot)
        
        slot["frame"].destroy() # UI에서 삭제
        
        if slot in self.slots:
            self.slots.remove(slot)
            
        self._log(f"🗑️ #{slot['id']:02d} 센서 슬롯 삭제됨", "SYS")

    # ---------------------------------------------------------
    # 컨트롤 함수들
    # ---------------------------------------------------------
    def trigger_shock(self):
        self._log("⚡ [GUI] 구동 중인 모든 센서에 강제 충격 장전!", "SHOCK")
        self.force_shock = True

    def update_config(self):
        self.device_config["SENSORTYPE"] = self.var_sensor_type.get()
        self.device_config["SENSING_PERIOD"] = self.ent_period.get().strip() or "1000"
        self._log(f"⚙️ 설정 변경 적용: TYPE={self.device_config['SENSORTYPE']}, PERIOD={self.device_config['SENSING_PERIOD']}ms", "SYS")

    def _log(self, msg, tag="INFO"):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]  # HH:MM:SS.mmm 형식으로 밀리초까지 표현
        self.txt_log.insert("end", f"[{ts}] [{tag}] {msg}\n")
        self.txt_log.see("end")

    # ---------------------------------------------------------
    # 개별/전체 슬롯 제어 로직
    # ---------------------------------------------------------
    def start_all(self):
        for slot in self.slots:
            if not slot["is_connected"]:
                self.start_slot(slot)

    def stop_all(self):
        for slot in self.slots:
            if slot["is_connected"]:
                self.stop_slot(slot)

    def start_slot(self, slot):
        if slot["is_connected"]: return

        broker = self.ent_broker.get().strip()
        port = int(self.ent_port.get().strip())
        mac = slot["mac_entry"].get().strip()
        s_id = slot["id"]
        
        if not mac:
            messagebox.showwarning("경고", f"#{s_id:02d} 슬롯의 MAC 주소가 비어있습니다.")
            return

        try:
            # Client ID 중복 방지를 위해 난수 결합
            client = mqtt_client.Client(callback_api_version=CallbackAPIVersion.VERSION2, client_id=f"Sim_Vibe_{mac}_{random.randint(10,99)}")
            
            def on_connect(c, u, f, rc, p=None, m=mac, i=s_id):
                self._log(f"✅ #{i:02d} 연결됨 (MAC: {m})", "MQTT")

            client.on_connect = on_connect
            client.connect(broker, port)
            client.loop_start()

            slot["client"] = client
            slot["is_connected"] = True
            slot["mac_entry"].config(state="disabled")
            slot["btn_start"].config(state="disabled")
            slot["btn_stop"].config(state="normal")
            slot["status_lbl"].config(text="ON", foreground="green")

            t = threading.Thread(target=self.publish_loop, args=(slot, mac), daemon=True)
            slot["thread"] = t
            t.start()

        except Exception as e:
            self._log(f"❌ #{s_id:02d} 연결 실패: {e}", "ERR")

    def stop_slot(self, slot):
        if not slot["is_connected"]: return

        slot["is_connected"] = False
        if slot["client"]:
            slot["client"].loop_stop()
            slot["client"].disconnect()

        slot["mac_entry"].config(state="normal")
        slot["btn_start"].config(state="normal")
        slot["btn_stop"].config(state="disabled")
        slot["status_lbl"].config(text="OFF", foreground="gray")
        self._log(f"🛑 #{slot['id']:02d} ({slot['mac_entry'].get()}) 정지됨", "SYS")

    def publish_loop(self, slot, mac):
        topic = f"V/{mac}"
        s_id = slot["id"]
        
        while slot["is_connected"]:
            sensor_type = self.device_config.get("SENSORTYPE", "1")
            if sensor_type == "0":
                time.sleep(0.5)
                continue

            frame_hex = f"{slot['seq']:X}"
            base_freq = self.scale_freq.get() + random.uniform(-1, 1) 
            noise_lvl = self.scale_noise.get()
            shock_p = 1.0 if self.force_shock else 0.05
            
            # 💡 현재 발송 시점의 정확한 밀리초 타임스탬프 (DB의 TICK 컬럼에 그대로 들어갈 값)
            current_tick = str(int(time.time() * 1000))
            
            if sensor_type == "1": 
                seq_str = f"0{frame_hex}"
                hex_data = self.generate_realistic_vibration(8192, base_freq, noise_lvl, shock_p, dc_offset=-32040, sensor_type="PIEZO")
                payload = {
                    "gwMacAddr": "HALOW", "dvicMacAddr": mac, "batteryRmin": "10", 
                    "seq": seq_str, "samplerate": "25600", "numofsample": "8192",
                    "tick": current_tick, "sensorData": hex_data
                }
                slot["client"].publish(topic, json.dumps(payload))

            elif sensor_type == "2": 
                for axis_idx, axis_name in enumerate(["X", "Y", "Z"], start=1):
                    seq_axis = f"{axis_idx}{frame_hex}"
                    hex_data = self.generate_realistic_vibration(8192, base_freq, noise_lvl, shock_p, dc_offset=16384, sensor_type="ADXL", axis=axis_name)
                    payload = {
                        "gwMacAddr": "HALOW", "dvicMacAddr": mac, "batteryRmin": "10", 
                        "seq": seq_axis, "samplerate": "3200", "numofsample": "8192",
                        "tick": current_tick, "sensorData": hex_data
                    }
                    slot["client"].publish(topic, json.dumps(payload))

            # 💡 프레임 1번일 때 로그 출력 + DB 비교를 위한 Tick(밀리초) 표기
            self._log(f"PUB [#{s_id:02d}] {mac} 데이터 전송 (Tick: {current_tick})", "PUB")

            slot["seq"] = 1 if slot["seq"] >= 15 else slot["seq"] + 1
            
            period_ms = int(self.device_config.get("SENSING_PERIOD", "1000"))
            time.sleep(max(0.01, period_ms / 1000.0))

        self.force_shock = False

if __name__ == "__main__":
    root = tk.Tk()
    app = MultiVibrationEmulatorGUI(root)
    root.mainloop()