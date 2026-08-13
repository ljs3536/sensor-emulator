import json
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import serial
from serial.tools import list_ports
import paho.mqtt.client as mqtt

# ===================== 기본 설정 =====================
SERIAL_BAUD_DEFAULT = 230400
SERIAL_BAUD_OPTIONS = [
    "9600", "19200", "38400", "57600", "115200",
    "230400", "460800", "921600",
]

MQTT_BROKER_DEFAULT = "mqtt.r-lab.co.kr"
MQTT_PORT_DEFAULT = 1883
# Spring Framework에서 구독 중인 토픽으로 기본값 지정
MQTT_TOPIC_DEFAULT = "V/035415641614"

SERIAL_COMMPATH_VALUE = "1"
SENSORTYPE_OPTIONS = [("STOP", "0"), ("PIEZO", "1"), ("MEMS(ADXL)", "2")]

PIEZO_SAMPLE_RATE_OPTIONS = ["512", "1280", "2560", "5120", "12800", "25600"]
PIEZO_SAMPLE_COUNT_OPTIONS = ["1024", "2048", "4096", "8192"]

ADXL_SAMPLE_RATE_OPTIONS = ["100", "200", "400", "800", "1600", "3200", "0x0F"]
ADXL_SAMPLE_COUNT_OPTIONS = ["256", "512", "1024", "2048", "4096", "8192"]
ADXL_G_RANGE_OPTIONS = ["2", "4", "8", "16"]


def now_iso():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def json_brace_delta(text: str) -> int:
    delta = 0
    in_string = False
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_string:
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if not in_string:
            if ch == "{":
                delta += 1
            elif ch == "}":
                delta -= 1
    return delta


class LightSerialMqttBridgeGui:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Light Serial to MQTT Bridge (No Graph)")
        self.root.geometry("900x700")

        # Serial & MQTT
        self.ser = None
        self.connected = False
        self.rx_thread = None
        self.rx_stop = threading.Event()

        self.mqtt_client = None
        self.mqtt_connected = False

        self._json_accum = ""
        self._json_depth = 0
        self._json_collecting = False

        self._build_ui()
        self.refresh_ports()
        self.root.protocol("WM_DELETE_WINDOW", self.on_quit)

    # ===================== UI Build =====================
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        # ===== Connection =====
        conn = ttk.LabelFrame(main, text="1. Connection Setup", padding=10)
        conn.pack(fill="x")

        self.var_serial_port = tk.StringVar(value="")
        self.var_baud = tk.StringVar(value=str(SERIAL_BAUD_DEFAULT))
        self.var_mqtt_host = tk.StringVar(value=MQTT_BROKER_DEFAULT)
        self.var_mqtt_port = tk.StringVar(value=str(MQTT_PORT_DEFAULT))
        self.var_mqtt_topic = tk.StringVar(value=MQTT_TOPIC_DEFAULT)

        # Serial Row
        ttk.Label(conn, text="COM Port").grid(row=0, column=0, sticky="w")
        self.cb_port = ttk.Combobox(conn, textvariable=self.var_serial_port, values=[], width=12, state="readonly")
        self.cb_port.grid(row=0, column=1, padx=4)

        self.btn_refresh = ttk.Button(conn, text="Refresh", command=self.refresh_ports)
        self.btn_refresh.grid(row=0, column=2, padx=4)

        ttk.Label(conn, text="Baud").grid(row=0, column=3, padx=(10, 0))
        ttk.Combobox(conn, textvariable=self.var_baud, values=SERIAL_BAUD_OPTIONS, width=9, state="readonly").grid(row=0, column=4, padx=4)

        # MQTT Row
        ttk.Label(conn, text="MQTT Host").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(conn, textvariable=self.var_mqtt_host, width=15).grid(row=1, column=1, padx=4, pady=(6, 0))

        ttk.Label(conn, text="Port").grid(row=1, column=2, pady=(6, 0))
        ttk.Entry(conn, textvariable=self.var_mqtt_port, width=6).grid(row=1, column=3, padx=4, pady=(6, 0), sticky="w")

        ttk.Label(conn, text="Topic (Spring 구독용)").grid(row=1, column=4, pady=(6, 0))
        ttk.Entry(conn, textvariable=self.var_mqtt_topic, width=20).grid(row=1, column=5, padx=4, pady=(6, 0))

        # Status & Buttons
        self.lbl_status = ttk.Label(conn, text="DISCONNECTED", font=("맑은 고딕", 10, "bold"), foreground="red")
        self.lbl_status.grid(row=0, column=6, rowspan=2, padx=12)

        self.btn_connect = ttk.Button(conn, text="Connect All", command=self.on_connect)
        self.btn_connect.grid(row=0, column=7, rowspan=2, padx=4)

        self.btn_disconnect = ttk.Button(conn, text="Disconnect", command=self.on_disconnect, state="disabled")
        self.btn_disconnect.grid(row=0, column=8, rowspan=2, padx=4)

        # ===== CONFIG Builder =====
        cfg = ttk.LabelFrame(main, text="2. Device Trigger Control", padding=10)
        cfg.pack(fill="x", pady=10)

        self.var_sensortype = tk.StringVar(value="1")
        self.var_piezo_gain = tk.StringVar(value="255")
        self.var_piezo_rate = tk.StringVar(value="25600")
        self.var_piezo_count = tk.StringVar(value="8192")
        self.var_adxl_rate = tk.StringVar(value="3200")
        self.var_adxl_count = tk.StringVar(value="8192")
        self.var_adxl_grange = tk.StringVar(value="2")
        self.var_period = tk.StringVar(value="10")

        ttk.Label(cfg, text="SENSORTYPE").grid(row=0, column=0, sticky="w")
        self.cb_sensor = ttk.Combobox(cfg, values=[f"{n}={v}" for n, v in SENSORTYPE_OPTIONS], width=14, state="readonly")
        self.cb_sensor.current(1)
        self.cb_sensor.grid(row=0, column=1, padx=6)
        self.cb_sensor.bind("<<ComboboxSelected>>", self._on_sensor_type_changed)

        ttk.Label(cfg, text="SENSING_PERIOD(ms)").grid(row=0, column=2, padx=(18, 0))
        ttk.Entry(cfg, textvariable=self.var_period, width=10).grid(row=0, column=3, padx=6)

        ttk.Separator(cfg, orient="horizontal").grid(row=1, column=0, columnspan=6, sticky="ew", pady=8)

        # PIEZO Controls
        self.lbl_piezo_gain = ttk.Label(cfg, text="PIEZO_GAIN")
        self.lbl_piezo_gain.grid(row=2, column=0)
        self.ent_piezo_gain = ttk.Entry(cfg, textvariable=self.var_piezo_gain, width=10)
        self.ent_piezo_gain.grid(row=2, column=1, padx=6)

        self.lbl_piezo_rate = ttk.Label(cfg, text="PIEZO_RATE")
        self.lbl_piezo_rate.grid(row=2, column=2)
        self.cb_piezo_rate = ttk.Combobox(cfg, textvariable=self.var_piezo_rate, values=PIEZO_SAMPLE_RATE_OPTIONS, width=10, state="readonly")
        self.cb_piezo_rate.grid(row=2, column=3, padx=6)

        self.lbl_piezo_count = ttk.Label(cfg, text="PIEZO_COUNT")
        self.lbl_piezo_count.grid(row=2, column=4)
        self.cb_piezo_count = ttk.Combobox(cfg, textvariable=self.var_piezo_count, values=PIEZO_SAMPLE_COUNT_OPTIONS, width=10, state="readonly")
        self.cb_piezo_count.grid(row=2, column=5, padx=6)

        # MEMS Controls
        self.lbl_mems_rate = ttk.Label(cfg, text="MEMS_RATE")
        self.lbl_mems_rate.grid(row=4, column=0)
        self.cb_mems_rate = ttk.Combobox(cfg, textvariable=self.var_adxl_rate, values=ADXL_SAMPLE_RATE_OPTIONS, width=10, state="readonly")
        self.cb_mems_rate.grid(row=4, column=1, padx=6)

        self.lbl_mems_count = ttk.Label(cfg, text="MEMS_COUNT")
        self.lbl_mems_count.grid(row=4, column=2)
        self.cb_mems_count = ttk.Combobox(cfg, textvariable=self.var_adxl_count, values=ADXL_SAMPLE_COUNT_OPTIONS, width=10, state="readonly")
        self.cb_mems_count.grid(row=4, column=3, padx=6)

        self.lbl_mems_grange = ttk.Label(cfg, text="MEMS_G_RANGE")
        self.lbl_mems_grange.grid(row=4, column=4)
        self.cb_mems_grange = ttk.Combobox(cfg, textvariable=self.var_adxl_grange, values=ADXL_G_RANGE_OPTIONS, width=10, state="readonly")
        self.cb_mems_grange.grid(row=4, column=5, padx=6)

        btns = ttk.Frame(cfg)
        btns.grid(row=5, column=0, columnspan=6, pady=10, sticky="w")

        self.btn_send_cfg = ttk.Button(btns, text="Send CONFIG (데이터 시작)", command=self.send_config, state="disabled")
        self.btn_send_cfg.pack(side="left", padx=6)

        self.btn_query_cfg = ttk.Button(btns, text="Query CONFIG", command=self.query_config, state="disabled")
        self.btn_query_cfg.pack(side="left", padx=6)

        # ===== Log Box =====
        logf = ttk.LabelFrame(main, text="3. Serial Received & MQTT Relay Monitor", padding=8)
        logf.pack(fill="both", expand=True)

        log_ctrl = ttk.Frame(logf)
        log_ctrl.pack(fill="x", pady=(0, 6))

        self.var_enable_mqtt = tk.BooleanVar(value=True)
        ttk.Checkbutton(log_ctrl, text="MQTT 중계 (Publish) 활성화", variable=self.var_enable_mqtt).pack(side="left")

        self.var_show_raw_hex = tk.BooleanVar(value=False)
        ttk.Checkbutton(log_ctrl, text="센서 Hex 본문 포함 전체 출력", variable=self.var_show_raw_hex).pack(side="left", padx=15)

        ttk.Button(log_ctrl, text="Clear Log", command=self.clear_log).pack(side="right")

        log_body = ttk.Frame(logf)
        log_body.pack(fill="both", expand=True)

        self.txt_log = tk.Text(log_body, wrap="none", height=15)
        self.txt_log.pack(side="left", fill="both", expand=True)

        log_scroll = ttk.Scrollbar(log_body, orient="vertical", command=self.txt_log.yview)
        log_scroll.pack(side="right", fill="y")
        self.txt_log.configure(yscrollcommand=log_scroll.set)

        self._apply_sensor_type_ui()

    # ===================== UI Events =====================
    def _on_sensor_type_changed(self, event=None):
        idx = self.cb_sensor.current()
        if idx >= 0:
            self.var_sensortype.set(SENSORTYPE_OPTIONS[idx][1])
            self._apply_sensor_type_ui()

    def _apply_sensor_type_ui(self):
        sensor_type = self.var_sensortype.get()
        piezo_widgets = (self.lbl_piezo_gain, self.ent_piezo_gain, self.lbl_piezo_rate, self.cb_piezo_rate, self.lbl_piezo_count, self.cb_piezo_count)
        mems_widgets = (self.lbl_mems_rate, self.cb_mems_rate, self.lbl_mems_count, self.cb_mems_count, self.lbl_mems_grange, self.cb_mems_grange)

        if sensor_type == "1":
            for w in piezo_widgets: w.grid()
            for w in mems_widgets: w.grid_remove()
        elif sensor_type == "2":
            for w in piezo_widgets: w.grid_remove()
            for w in mems_widgets: w.grid()
        else:
            for w in piezo_widgets + mems_widgets: w.grid_remove()

    # ===================== Connection =====================
    def refresh_ports(self):
        ports = [p.device for p in list_ports.comports()]
        self.cb_port["values"] = ports
        if ports:
            self.var_serial_port.set(ports[0])

    def on_connect(self):
        port = self.var_serial_port.get().strip()
        if not port:
            messagebox.showerror("Error", "COM Port를 선택하세요.")
            return

        # 1. Serial Connect
        try:
            baud = int(self.var_baud.get())
            self.ser = serial.Serial(port=port, baudrate=baud, timeout=0.20, write_timeout=1.0)
        except Exception as e:
            messagebox.showerror("Error", f"Serial connect failed:\n{e}")
            return

        # 2. MQTT Connect
        try:
            self.mqtt_client = mqtt.Client()
            self.mqtt_client.connect(self.var_mqtt_host.get(), int(self.var_mqtt_port.get()), 60)
            self.mqtt_client.loop_start()
            self.mqtt_connected = True
        except Exception as e:
            self._log(f"MQTT Connect Failed: {e}", "SYS")
            self.mqtt_connected = False

        # 3. Start RX Thread
        self.rx_stop.clear()
        self.rx_thread = threading.Thread(target=self._serial_rx_worker, daemon=True)
        self.rx_thread.start()

        self._set_connected(True)
        self._log(f"CONNECTED Serial({port}) & MQTT({self.var_mqtt_host.get()})", "SYS")

    def on_disconnect(self):
        self.rx_stop.set()
        if self.ser:
            try: self.ser.close()
            except Exception: pass
            self.ser = None

        if self.mqtt_client:
            try:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
            except Exception: pass
            self.mqtt_client = None
            self.mqtt_connected = False

        self._set_connected(False)
        self._log("DISCONNECTED", "SYS")

    def _serial_rx_worker(self):
        while not self.rx_stop.is_set():
            if not self.ser or not self.ser.is_open:
                break
            try:
                raw_bytes = self.ser.readline()
                if raw_bytes:
                    raw = raw_bytes.decode("utf-8", errors="replace")
                    self.root.after(0, self._handle_serial_fragment, raw)
            except Exception as e:
                if not self.rx_stop.is_set():
                    self.root.after(0, self._serial_error, str(e))
                break

    def _serial_error(self, err):
        self._log(f"SERIAL ERROR: {err}", "SYS")
        self.on_disconnect()

    def _handle_serial_fragment(self, raw: str):
        if not raw: return
        text = raw

        if not self._json_collecting:
            start = text.find("{")
            if start < 0:
                plain = text.strip()
                if plain: self._log(plain, "RX")
                return
            prefix = text[:start].strip()
            if prefix: self._log(prefix, "RX")

            text = text[start:]
            self._json_collecting = True
            self._json_accum = ""
            self._json_depth = 0

        self._json_accum += text
        self._json_depth += json_brace_delta(text)

        if self._json_depth > 0:
            return

        candidate = self._json_accum.strip()
        self._json_collecting, self._json_accum, self._json_depth = False, "", 0

        if not candidate: return

        try:
            pkt = json.loads(candidate)
        except Exception as e:
            self._log(f"JSON PARSE FAIL: {e}", "SYS")
            return

        self._process_packet(pkt, raw_json_str=candidate)

    def _process_packet(self, pkt: dict, raw_json_str: str):
        seq = pkt.get("seq", "N/A")
        tick = pkt.get("tick", "N/A")
        sensor_data_len = len(pkt.get("sensorData", ""))

        # 1. 시리얼 수신 요약로그 출력
        if self.var_show_raw_hex.get():
            self._log(f"SERIAL RECV -> {raw_json_str}", "RX")
        else:
            self._log(f"SERIAL RECV -> seq={seq}, tick={tick}, data_len={sensor_data_len}", "RX")

        # 2. MQTT로 Publish (중계)
        if self.mqtt_connected and self.var_enable_mqtt.get():
            topic = self.var_mqtt_topic.get().strip()
            # 서버 수신에 완결성을 주기 위해 개행(\r\n) 포함하여 Publish
            payload = raw_json_str + "\r\n"
            self.mqtt_client.publish(topic, payload, qos=1)
            self._log(f"MQTT PUBLISHED! -> Topic: [{topic}] (Length: {len(payload)})", "MQTT")
        elif not self.mqtt_connected:
            self._log("MQTT 연결이 꺼져있어 전송하지 않았습니다.", "WARN")

    def _set_connected(self, v: bool):
        self.connected = v
        self.btn_connect.config(state="disabled" if v else "normal")
        self.btn_disconnect.config(state="normal" if v else "disabled")
        self.btn_refresh.config(state="disabled" if v else "normal")
        self.btn_send_cfg.config(state="normal" if v else "disabled")
        self.btn_query_cfg.config(state="normal" if v else "disabled")
        self.cb_port.config(state="disabled" if v else "readonly")
        self.lbl_status.config(text="CONNECTED" if v else "DISCONNECTED", foreground="green" if v else "red")

    # ===================== Command Sending =====================
    def build_config_json(self):
        mems_rate = self.var_adxl_rate.get()
        # 3200Hz 입력 시 펌웨어 인식용 HEX 코드로 자동 변환
        if mems_rate == "3200":
            mems_rate = "0x0F"

        return {
            "COMMPATH": SERIAL_COMMPATH_VALUE,
            "SENSORTYPE": self.var_sensortype.get(),
            "PIEZO_GAIN": self.var_piezo_gain.get(),
            "PIEZO_SAMPLE_RATE": self.var_piezo_rate.get(),
            "PIEZO_SAMPLE_COUNT": self.var_piezo_count.get(),
            "MEMS_SAMPLE_RATE": mems_rate,          # ADXL 대신 MEMS 키 사용
            "MEMS_SAMPLE_COUNT": self.var_adxl_count.get(),
            "MEMS_G_RANGE": self.var_adxl_grange.get(),
            "SENSING_PERIOD": self.var_period.get(),
        }

    def _serial_send_json(self, obj):
        if not self.ser or not self.connected or not self.ser.is_open:
            messagebox.showwarning("Serial", "Serial이 연결되어 있지 않습니다.")
            return False

        payload = json.dumps(obj, ensure_ascii=False) + "\r\n"
        try:
            self.ser.write(payload.encode("utf-8"))
            self.ser.flush()
            return True
        except Exception as e:
            messagebox.showerror("Serial", f"Serial send failed:\n{e}")
            return False

    def send_config(self):
        cfg = self.build_config_json()
        if self._serial_send_json(cfg):
            self._log("CONFIG SENT (Trigger) -> " + json.dumps(cfg, ensure_ascii=False), "TX")

    def query_config(self):
        if self._serial_send_json({"CONFIG": "?"}):
            self._log("CONFIG QUERY SENT", "TX")

    # ===================== Log & Quit =====================
    def clear_log(self):
        self.txt_log.delete("1.0", "end")

    def _log(self, s, tag):
        msg = str(s).replace("\r", " ").replace("\n", " ").strip()
        if not msg: return

        line = f"[{now_iso()}] [{tag}] {msg}\n"
        self.txt_log.insert("end", line)
        self.txt_log.see("end")

    def on_quit(self):
        self.on_disconnect()
        self.root.destroy()


def main():
    root = tk.Tk()
    LightSerialMqttBridgeGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()