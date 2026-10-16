import json
import time
import threading
import random
import struct
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

class UnifiedSensorEmulatorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("통합 센서 에뮬레이터 (지속형 고장 모드 지원)")
        self.root.geometry("1500x850") 

        self.slots = []
        self.next_slot_id = 1 

        self._build_ui()
        
        default_types = ["1: PIEZO", "1: PIEZO", "2: ADXL", "2: ADXL", "3: ULTRASONIC"]
        for t in default_types:
            self.add_slot(default_type=t)

    # ---------------------------------------------------------
    # 데이터 생성 1: 진동 (선택된 '고장 모드'에 따른 지속적 파형 생성)
    # ---------------------------------------------------------
    def generate_realistic_vibration(self, sample_count, fault_state, sensor_type, axis=None, rpm=1800.0, gear_teeth=0, sample_rate=25600.0):
        # 1. 입력된 RPM 기반으로 1X(기본 회전 주파수) 도출
        base_freq = rpm / 60.0 if rpm > 0 else 30.0
        rate = float(sample_rate) if sample_rate and float(sample_rate) > 0 else 25600.0
        t = np.arange(sample_count, dtype=np.float64) / rate
        
        amp_mod = 1.0 + 0.1 * np.sin(2 * np.pi * 2.0 * t)
        noise = np.random.normal(0, 100, sample_count)
        
        # 2. 기본 축 방향 정상 진동 셋업
        if sensor_type == "PIEZO" or axis == "X":
            amplitude = 2000
            harmonic = 150 * np.sin(2 * np.pi * (base_freq * 2) * t)
        elif axis == "Y":
            amplitude = 600
            harmonic = 50 * np.sin(2 * np.pi * (base_freq * 2) * t)
        else:
            amplitude = 50
            harmonic = 0

        phase = 0 if (sensor_type == "PIEZO" or axis == "X") else (np.pi/3 if axis == "Y" else np.pi/1.5)
        signal = (amplitude * amp_mod * np.sin(2 * np.pi * base_freq * t + phase)) + harmonic + noise

        # 3. 톱니가 있다면 평상시에도 약한 GMF(기어맞물림) 진동이 깔림
        if gear_teeth > 0:
            gmf_hz = base_freq * gear_teeth
            signal += 300 * np.sin(2 * np.pi * gmf_hz * t)

        # 💡 4. 고장 모드 지속 적용 (버튼 클릭 1회성이 아닌 상태 유지)
        if fault_state == "축 정렬 불량":
            # 2X, 3X 주파수 지속적 상승 모사
            fault_amp = 15000 if (sensor_type == "PIEZO" or axis == "X") else (8000 if axis == "Y" else 1000)
            signal += fault_amp * np.sin(2 * np.pi * (base_freq * 2) * t)
            signal += (fault_amp * 0.5) * np.sin(2 * np.pi * (base_freq * 3) * t)
            signal += np.random.normal(0, 500, sample_count) # 마찰 노이즈 증가

        elif fault_state == "기어 톱니 손상" and gear_teeth > 0:
            # GMF 및 사이드밴드(Sidebands) 지속적 상승 모사
            gmf_hz = base_freq * gear_teeth
            fault_amp = 18000 if (sensor_type == "PIEZO" or axis == "X") else (10000 if axis == "Y" else 2000)
            signal += fault_amp * np.sin(2 * np.pi * gmf_hz * t)
            signal += (fault_amp * 0.6) * np.sin(2 * np.pi * (gmf_hz - base_freq) * t)
            signal += (fault_amp * 0.6) * np.sin(2 * np.pi * (gmf_hz + base_freq) * t)
            signal += np.random.normal(0, 800, sample_count) # 갈리는 노이즈 증가

        raw_samples = np.clip(signal, -32768, 32767).astype(int)
        return "".join([f"{val & 0xFFFF:04X}" for val in raw_samples])

    def generate_ultrasonic_hex_data(self, is_leak=False):
        size = 320
        base_noise = np.random.normal(loc=0.002, scale=0.0005, size=size)
        if not is_leak:
            spike_indices = np.random.choice(size, 8, replace=False)
            base_noise[spike_indices] += np.random.uniform(0.005, 0.015, 8)
            signal = base_noise
        else:
            x = np.linspace(0, size - 1, size)
            leak_bump = 0.08 * np.exp(-((x - 180) ** 2) / (2 * 20 ** 2))
            signal = base_noise + leak_bump + np.random.uniform(0.002, 0.01, size)

        signal = np.clip(signal, 0.0001, 1.0)
        header_hex = "0000000000000000280A0000"
        data_hex = "".join([struct.pack('<f', val).hex().upper() for val in signal])
        return header_hex + data_hex

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        conn_frame = ttk.LabelFrame(main, text="1. 브로커 설정 및 전체 제어", padding=10)
        conn_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(conn_frame, text="Broker:").pack(side="left")
        self.ent_broker = ttk.Entry(conn_frame, width=15)
        self.ent_broker.insert(0, BROKER_DEFAULT)
        self.ent_broker.pack(side="left", padx=5)

        ttk.Label(conn_frame, text="Port:").pack(side="left")
        self.ent_port = ttk.Entry(conn_frame, width=5)
        self.ent_port.insert(0, str(PORT_DEFAULT))
        self.ent_port.pack(side="left", padx=5)

        ttk.Button(conn_frame, text="▶ 전체 시작", command=self.start_all).pack(side="left", padx=20)
        ttk.Button(conn_frame, text="⏹ 전체 정지", command=self.stop_all).pack(side="left")

        slot_outer_frame = ttk.LabelFrame(main, text="2. 개별 센서 동적 설정 관리", padding=10)
        slot_outer_frame.pack(fill="both", expand=True, pady=(0, 10))

        ctrl_panel = ttk.Frame(slot_outer_frame)
        ctrl_panel.pack(fill="x", pady=(0, 5))
        ttk.Button(ctrl_panel, text="➕ 센서 슬롯 1개 추가", command=lambda: self.add_slot()).pack(side="left")

        self.canvas = tk.Canvas(slot_outer_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(slot_outer_frame, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)
        self.scrollable_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        log_frame = ttk.LabelFrame(main, text="3. 통합 로그 모니터", padding=10)
        log_frame.pack(fill="x", expand=False)

        self.txt_log = tk.Text(log_frame, wrap="none", height=12)
        self.txt_log.pack(side="left", fill="both", expand=True)
        scroll_log = ttk.Scrollbar(log_frame, orient="vertical", command=self.txt_log.yview)
        scroll_log.pack(side="right", fill="y")
        self.txt_log.configure(yscrollcommand=scroll_log.set)

    def add_slot(self, default_type="1: PIEZO"):
        curr_id = self.next_slot_id
        self.next_slot_id += 1
        
        row_frame = ttk.Frame(self.scrollable_frame)
        row_frame.pack(fill="x", pady=5)
        
        ttk.Label(row_frame, text=f"#{curr_id:02d}").pack(side="left", padx=(0, 5))
        
        ent_mac = ttk.Entry(row_frame, width=15)
        ent_mac.insert(0, f"0354156416{curr_id+9:02d}") 
        ent_mac.pack(side="left", padx=5)

        ttk.Label(row_frame, text="배터리:").pack(side="left")
        ent_batt = ttk.Entry(row_frame, width=4)
        ent_batt.insert(0, "10") 
        ent_batt.pack(side="left", padx=(0, 5))

        var_type = tk.StringVar(value=default_type)
        cb_type = ttk.Combobox(row_frame, textvariable=var_type, values=["0: STOP", "1: PIEZO", "2: ADXL", "3: ULTRASONIC"], state="readonly", width=12)
        cb_type.pack(side="left", padx=5)

        dynamic_frame = ttk.Frame(row_frame)
        dynamic_frame.pack(side="left", fill="x", expand=True, padx=10)

        btn_trigger = tk.Button(row_frame, text="-", width=12)
        btn_trigger.pack(side="left", padx=5)

        btn_start = ttk.Button(row_frame, text="▶", width=3)
        btn_start.pack(side="left", padx=2)
        btn_stop = ttk.Button(row_frame, text="⏹", width=3, state="disabled")
        btn_stop.pack(side="left", padx=2)
        status_lbl = ttk.Label(row_frame, text="OFF", foreground="gray", width=4)
        status_lbl.pack(side="left", padx=5)
        btn_del = ttk.Button(row_frame, text="❌", width=3)
        btn_del.pack(side="left", padx=5)

        slot = {
            "id": curr_id,
            "frame": row_frame,
            "mac_entry": ent_mac,
            "batt_entry": ent_batt,
            "var_type": var_type,
            "dynamic_frame": dynamic_frame,
            "btn_trigger": btn_trigger,
            "btn_start": btn_start,
            "btn_stop": btn_stop,
            "status_lbl": status_lbl,
            "is_connected": False,
            "client": None,
            "seq": 1,
            "is_leaking": False,
            "ui_vars": {}
        }

        cb_type.bind("<<ComboboxSelected>>", lambda e, s=slot: self.update_dynamic_ui(s))
        btn_trigger.config(command=lambda s=slot: self.on_trigger(s))
        btn_start.config(command=lambda s=slot: self.start_slot(s))
        btn_stop.config(command=lambda s=slot: self.stop_slot(s))
        btn_del.config(command=lambda s=slot: self.remove_slot(s))

        self.slots.append(slot)
        self.update_dynamic_ui(slot) 
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def update_dynamic_ui(self, slot):
        for widget in slot["dynamic_frame"].winfo_children():
            widget.destroy()
        
        slot["ui_vars"].clear()
        stype = slot["var_type"].get()[0]

        if stype in ["1", "2"]: # PIEZO or ADXL
            # 💡 [추가] 기계 스펙 및 고장 상태 제어 추가
            ttk.Label(slot["dynamic_frame"], text="RPM:", foreground="blue").pack(side="left")
            ent_rpm = ttk.Entry(slot["dynamic_frame"], width=5)
            ent_rpm.insert(0, "1800")
            ent_rpm.pack(side="left", padx=(2, 5))

            ttk.Label(slot["dynamic_frame"], text="톱니:", foreground="blue").pack(side="left")
            ent_teeth = ttk.Entry(slot["dynamic_frame"], width=3)
            ent_teeth.insert(0, "0")
            ent_teeth.pack(side="left", padx=(2, 5))

            ttk.Label(slot["dynamic_frame"], text="모드:", foreground="red").pack(side="left")
            cb_fault = ttk.Combobox(slot["dynamic_frame"], values=["정상", "축 정렬 불량", "기어 톱니 손상"], width=12, state="readonly")
            cb_fault.set("정상")
            cb_fault.pack(side="left", padx=(2, 10))

            if stype == "1":
                cb_sr = ttk.Combobox(slot["dynamic_frame"], values=["25600", "12800"], width=6)
                cb_sr.set("25600")
                cb_sr.pack(side="left", padx=(2, 2))
                cb_sc = ttk.Combobox(slot["dynamic_frame"], values=["8192", "4096"], width=5)
                cb_sc.set("8192")
                cb_sc.pack(side="left", padx=(2, 5))
                slot["ui_vars"] = {"rpm": ent_rpm, "teeth": ent_teeth, "fault": cb_fault, "sr": cb_sr, "sc": cb_sc}
                
            else:
                cb_sr = ttk.Combobox(slot["dynamic_frame"], values=["0x0F (3200Hz)"], width=13)
                cb_sr.set("0x0F (3200Hz)")
                cb_sr.pack(side="left", padx=(2, 2))
                cb_sc = ttk.Combobox(slot["dynamic_frame"], values=["8192", "4096"], width=5)
                cb_sc.set("8192")
                cb_sc.pack(side="left", padx=(2, 5))
                slot["ui_vars"] = {"rpm": ent_rpm, "teeth": ent_teeth, "fault": cb_fault, "sr": cb_sr, "sc": cb_sc}

            # 진동은 상태(Combobox)로 제어하므로 버튼 비활성화
            slot["btn_trigger"].config(text="상태로 제어", state="disabled", bg="SystemButtonFace")

        elif stype == "3": # ULTRASONIC
            ttk.Label(slot["dynamic_frame"], text="측정 주기(ms):").pack(side="left")
            ent_period = ttk.Entry(slot["dynamic_frame"], width=6)
            ent_period.insert(0, "1000")
            ent_period.pack(side="left", padx=(2, 10))
            slot["ui_vars"] = {"period": ent_period}
            self._update_leak_btn(slot)
        else: 
            slot["btn_trigger"].config(text="---", state="disabled", bg="SystemButtonFace")

    def _update_leak_btn(self, slot):
        txt = "🚨 누출 끄기" if slot["is_leaking"] else "🟢 누출 켜기"
        bg_color = "salmon" if slot["is_leaking"] else "lightgreen"
        slot["btn_trigger"].config(text=txt, bg=bg_color, state="normal")

    def on_trigger(self, slot):
        stype = slot["var_type"].get()[0]
        if stype == "3":
            slot["is_leaking"] = not slot["is_leaking"]
            self._update_leak_btn(slot)
            self._log(f"⚠️ [#{slot['id']:02d}] 초음파 누출 {'발생' if slot['is_leaking'] else '해제'}!", "ULTRA")

    def remove_slot(self, slot):
        if slot["is_connected"]: self.stop_slot(slot)
        slot["frame"].destroy() 
        if slot in self.slots: self.slots.remove(slot)
        self._log(f"🗑️ #{slot['id']:02d} 슬롯 삭제", "SYS")

    def _log(self, msg, tag="INFO"):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.txt_log.insert("end", f"[{ts}] [{tag}] {msg}\n")
        self.txt_log.see("end")

    def start_all(self):
        for slot in self.slots:
            if not slot["is_connected"]: self.start_slot(slot)

    def stop_all(self):
        for slot in self.slots:
            if slot["is_connected"]: self.stop_slot(slot)

    def start_slot(self, slot):
        if slot["is_connected"]: return
        broker, port = self.ent_broker.get().strip(), int(self.ent_port.get().strip())
        mac = slot["mac_entry"].get().strip()
        if not mac: return

        try:
            client = mqtt_client.Client(callback_api_version=CallbackAPIVersion.VERSION2, client_id=f"Sim_{mac}_{random.randint(10,99)}")
            client.on_connect = lambda c, u, f, rc, p=None, m=mac, i=slot["id"]: self._log(f"✅ #{i:02d} 연결됨 ({m})", "MQTT")
            client.connect(broker, port)
            client.loop_start()

            slot["client"] = client
            slot["is_connected"] = True
            slot["mac_entry"].config(state="disabled")
            slot["var_type"].trace_add('write', lambda *args: None)
            slot["btn_start"].config(state="disabled")
            slot["btn_stop"].config(state="normal")
            slot["status_lbl"].config(text="ON", foreground="green")

            t = threading.Thread(target=self.publish_loop, args=(slot, mac), daemon=True)
            slot["thread"] = t
            t.start()
        except Exception as e:
            self._log(f"❌ #{slot['id']:02d} 연결 실패: {e}", "ERR")

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
        self._log(f"🛑 #{slot['id']:02d} 정지됨", "SYS")

    def publish_loop(self, slot, mac):
        s_id = slot["id"]
        
        while slot["is_connected"]:
            stype = slot["var_type"].get()[0]
            if stype == "0":
                time.sleep(0.5)
                continue

            current_tick = str(int(time.time() * 1000))
            ui = slot["ui_vars"]
            current_battery = slot["batt_entry"].get().strip() or "10"
            period_ms = 1000 

            if stype in ["1", "2"]:
                topic = f"V/{mac}"
                frame_hex = f"{slot['seq']:X}"
                
                sc_str = ui.get("sc").get() if "sc" in ui else "8192"
                sc_int = int(sc_str) if sc_str.isdigit() else 8192
                
                rpm_str = ui.get("rpm").get() if "rpm" in ui else "1800"
                rpm = float(rpm_str) if rpm_str.replace('.', '', 1).isdigit() else 1800.0
                
                teeth_str = ui.get("teeth").get() if "teeth" in ui else "0"
                teeth = int(teeth_str) if teeth_str.isdigit() else 0
                
                # 💡 선택된 고장 모드 읽어오기
                fault_state = ui.get("fault").get() if "fault" in ui else "정상"
                
                if stype == "1":
                    sr_str = ui.get("sr").get() if "sr" in ui else "25600"
                    sr_value = float(sr_str) if str(sr_str).replace(".", "", 1).isdigit() else 25600.0
                    hex_data = self.generate_realistic_vibration(sc_int, fault_state, "PIEZO", None, rpm, teeth, sr_value)
                    payload = { "gwMacAddr": "HALOW", "dvicMacAddr": mac, "batteryRmin": current_battery, "seq": f"0{frame_hex}", "samplerate": sr_str, "numofsample": sc_str, "tick": current_tick, "sensorData": hex_data }
                    slot["client"].publish(topic, json.dumps(payload))

                elif stype == "2": 
                    sr_raw = ui.get("sr").get() if "sr" in ui else "3200"
                    sr_str = "3200" if "3200" in sr_raw else "1600" if "1600" in sr_raw else "3200"
                    
                    for axis_idx, axis_name in enumerate(["X", "Y", "Z"], start=1):
                        hex_data = self.generate_realistic_vibration(sc_int, fault_state, "ADXL", axis_name, rpm, teeth, float(sr_str))
                        payload = { "gwMacAddr": "HALOW", "dvicMacAddr": mac, "batteryRmin": current_battery, "seq": f"{axis_idx}{frame_hex}", "samplerate": sr_str, "numofsample": sc_str, "tick": current_tick, "sensorData": hex_data }
                        slot["client"].publish(topic, json.dumps(payload))

                self._log(f"PUB [#{s_id:02d}] 진동 전송 (모드:{fault_state})", "PUB-VIB")
                slot["seq"] = 1 if slot["seq"] >= 15 else slot["seq"] + 1

            elif stype == "3":
                topic = f"D/{mac}"
                hex_data = self.generate_ultrasonic_hex_data(slot["is_leaking"])
                payload = { "gwMacAddr": "HALOW", "dvicMacAddr": mac, "batteryRmin": current_battery, "seq": f"{slot['seq']:02X}", "tick": current_tick, "sensorData": hex_data }
                slot["client"].publish(topic, json.dumps(payload))

                self._log(f"PUB [#{s_id:02d}] 초음파 전송", "PUB-ULT")
                slot["seq"] = 1 if slot["seq"] >= 255 else slot["seq"] + 1
            
            time.sleep(max(0.01, period_ms / 1000.0))

if __name__ == "__main__":
    root = tk.Tk()
    app = UnifiedSensorEmulatorGUI(root)
    root.mainloop()