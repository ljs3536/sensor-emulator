# VERSION: V3 - PIEZO 1CH / MEMS 3CH dynamic layout + filtered serial log
import json
import re
import struct
import time
import threading
import math
from collections import deque

import tkinter as tk
from tkinter import ttk, messagebox

import serial
from serial.tools import list_ports

# matplotlib (Tk 내장)
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator, FuncFormatter, MultipleLocator


# ===================== Serial 기본 설정 =====================
SERIAL_BAUD_DEFAULT = 230400
SERIAL_BAUD_OPTIONS = [
    "9600", "19200", "38400", "57600", "115200",
    "230400", "460800", "921600",
]

# ===================== 펌웨어에서 fs/fftN을 안 보내면 여기 값 사용 =====================
FS_HZ_DEFAULT = 25600
FFT_N_DEFAULT = 8192

# ===================== Plot 옵션 =====================
FRAMES_ON_SCREEN = 30
MAX_BINS_TO_PLOT = 0
Y_MARGIN_RATIO = 0.10

# FFT peak 표시 옵션
EXCLUDE_DC_FOR_PEAK = True   # True면 0번 bin(0Hz) 제외하고 peak 탐색

# ===================== CONFIG 옵션 정의 =====================
# MQTT를 제거했으므로 CONFIG의 COMMPATH는 SERIAL(1)로 고정한다.
SERIAL_COMMPATH_VALUE = "1"
SENSORTYPE_OPTIONS = [("STOP", "0"), ("PIEZO", "1"), ("MEMS(ADXL)", "2")]

PIEZO_SAMPLE_RATE_OPTIONS = ["512", "1280", "2560", "5120", "12800", "25600"]
PIEZO_SAMPLE_COUNT_OPTIONS = ["1024", "2048", "4096", "8192"]

ADXL_SAMPLE_RATE_OPTIONS = ["100", "200", "400", "800", "1600", "3200", "0x0F"]
ADXL_SAMPLE_COUNT_OPTIONS = ["256", "512", "1024", "2048", "4096", "8192"]
ADXL_G_RANGE_OPTIONS = ["2", "4", "8", "16"]


def now_iso():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ===================== sensorData 파싱 유틸 =====================
def clean_hex(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    return re.sub(r"\s+", "", s)


def hex_to_u16_list(hex_str: str):
    """4 hex chars -> uint16 list"""
    hex_str = clean_hex(hex_str)
    if not hex_str:
        return []

    n = (len(hex_str) // 4) * 4
    hex_str = hex_str[:n]

    out = []
    for i in range(0, n, 4):
        out.append(int(hex_str[i:i + 4], 16))
    return out


def hex_to_f32_list_be(hex_str: str):
    """
    8 hex chars -> float32 list (BIG-ENDIAN)
    """
    hex_str = clean_hex(hex_str)
    if not hex_str:
        return []

    n = (len(hex_str) // 8) * 8
    hex_str = hex_str[:n]

    out = []
    for i in range(0, n, 8):
        u = int(hex_str[i:i + 8], 16)
        b = u.to_bytes(4, byteorder="big")
        f = struct.unpack(">f", b)[0]
        out.append(f)
    return out


def normalize_adxl_fs_from_packet(fs_value):
    """
    ADXL 쪽 패킷 fs를 FFT 계산용 Hz로 변환
    아래는 모두 3200Hz로 취급
      - 15
      - "15"
      - 0x0F
      - "0x0F"
    """
    if fs_value is None:
        return None

    if isinstance(fs_value, str):
        s = fs_value.strip().lower()
        if s in ("15", "0x0f"):
            return 3200
        try:
            return int(s, 0)
        except Exception:
            return None

    if isinstance(fs_value, int):
        if fs_value == 15 or fs_value == 0x0F:
            return 3200
        return fs_value

    return None


def normalize_adxl_rate_from_config(rate_value):
    """
    GUI 설정값(MEMS_SAMPLE_RATE) 기준 Hz 변환
    """
    if rate_value is None:
        return None

    s = str(rate_value).strip().lower()
    if s in ("15", "0x0f"):
        return 3200

    try:
        return int(s, 0)
    except Exception:
        return None


def seq_to_axis(seq):
    """
    seq 십의 자리로 축 구분
    11,12,13 -> X
    21,22,23 -> Y
    31,32,33 -> Z
    """
    try:
        seq = int(seq)
    except Exception:
        return None

    axis_digit = (seq // 10) % 10
    if axis_digit == 1:
        return "X"
    elif axis_digit == 2:
        return "Y"
    elif axis_digit == 3:
        return "Z"
    return None


def json_brace_delta(text: str) -> int:
    """
    JSON 조각 안의 { } 개수 차이를 계산한다.
    문자열 내부의 중괄호는 제외한다.

    시리얼에서는 JSON이 여러 줄로 들어올 수 있으므로
    완전한 JSON 객체가 끝났는지 판단하기 위해 사용한다.
    """
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


class SerialGui:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("SERIAL CONFIG + FFT Plot V3 - PIEZO 1CH / MEMS 3CH + LOG")
        self.root.geometry("1500x920")

        # Serial
        self.ser = None
        self.connected = False
        self.rx_thread = None
        self.rx_stop = threading.Event()

        # 여러 줄 JSON 조립용
        self._json_accum = ""
        self._json_depth = 0
        self._json_collecting = False

        # RX lock
        self._rx_lock = threading.Lock()

        # 단일 채널 fallback
        self.frames = deque(maxlen=FRAMES_ON_SCREEN)
        self.latest_meta = {
            "seq": None,
            "tick": None,
            "batteryRmin": None,
            "fs": None,
            "fftN": None,
            "pkt_format": None,
            "axis": None,
        }

        # ADXL 3축용
        self.axis_frames = {
            "X": deque(maxlen=FRAMES_ON_SCREEN),
            "Y": deque(maxlen=FRAMES_ON_SCREEN),
            "Z": deque(maxlen=FRAMES_ON_SCREEN),
        }
        self.axis_meta = {
            "X": {"seq": None, "tick": None, "batteryRmin": None, "fs": None, "fftN": None, "pkt_format": None},
            "Y": {"seq": None, "tick": None, "batteryRmin": None, "fs": None, "fftN": None, "pkt_format": None},
            "Z": {"seq": None, "tick": None, "batteryRmin": None, "fs": None, "fftN": None, "pkt_format": None},
        }

        # Plot cache
        self.axis_plot_cache = {
            "X": {"x": [], "y": [], "is_fft": False, "df": None},
            "Y": {"x": [], "y": [], "is_fft": False, "df": None},
            "Z": {"x": [], "y": [], "is_fft": False, "df": None},
        }

        self._build_ui()
        self.refresh_ports()

        self.root.after(50, self._update_plot)
        self.root.protocol("WM_DELETE_WINDOW", self.on_quit)

    # ===================== UI =====================
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        # ===== Connection =====
        conn = ttk.LabelFrame(main, text="Serial Connection", padding=10)
        conn.pack(fill="x")

        self.var_serial_port = tk.StringVar(value="")
        self.var_baud = tk.StringVar(value=str(SERIAL_BAUD_DEFAULT))

        ttk.Label(conn, text="COM Port").grid(row=0, column=0, sticky="w")
        self.cb_port = ttk.Combobox(
            conn,
            textvariable=self.var_serial_port,
            values=[],
            width=16,
            state="readonly",
        )
        self.cb_port.grid(row=0, column=1, padx=6)

        self.btn_refresh = ttk.Button(conn, text="Refresh", command=self.refresh_ports)
        self.btn_refresh.grid(row=0, column=2, padx=6)

        ttk.Label(conn, text="Baud").grid(row=0, column=3, padx=(18, 0))
        ttk.Combobox(
            conn,
            textvariable=self.var_baud,
            values=SERIAL_BAUD_OPTIONS,
            width=10,
            state="readonly",
        ).grid(row=0, column=4, padx=6)

        self.lbl_status = ttk.Label(conn, text="DISCONNECTED")
        self.lbl_status.grid(row=0, column=5, padx=12)

        self.btn_connect = ttk.Button(conn, text="Connect", command=self.on_connect)
        self.btn_connect.grid(row=0, column=6, padx=6)

        self.btn_disconnect = ttk.Button(conn, text="Disconnect", command=self.on_disconnect, state="disabled")
        self.btn_disconnect.grid(row=0, column=7, padx=6)

        # ===== CONFIG =====
        cfg = ttk.LabelFrame(main, text="CONFIG Builder", padding=10)
        cfg.pack(fill="x", pady=10)

        self.var_sensortype = tk.StringVar(value="1")
        self.var_piezo_gain = tk.StringVar(value="255")
        self.var_piezo_rate = tk.StringVar(value="25600")
        self.var_piezo_count = tk.StringVar(value="8192")
        self.var_adxl_rate = tk.StringVar(value="3200")
        self.var_adxl_count = tk.StringVar(value="8192")
        self.var_adxl_grange = tk.StringVar(value="2")
        self.var_period = tk.StringVar(value="10")

        ttk.Label(cfg, text="COMMPATH").grid(row=0, column=0, sticky="w")
        ttk.Label(cfg, text="SERIAL=1").grid(row=0, column=1, padx=6, sticky="w")

        ttk.Label(cfg, text="SENSORTYPE").grid(row=0, column=2, padx=(18, 0))
        self.cb_sensor = ttk.Combobox(
            cfg,
            values=[f"{n}={v}" for n, v in SENSORTYPE_OPTIONS],
            width=14,
            state="readonly",
        )
        self.cb_sensor.current(1)
        self.cb_sensor.grid(row=0, column=3, padx=6)
        self.cb_sensor.bind("<<ComboboxSelected>>", self._on_sensor_type_changed)

        ttk.Label(cfg, text="SENSING_PERIOD(ms)").grid(row=0, column=4, padx=(18, 0))
        ttk.Entry(cfg, textvariable=self.var_period, width=10).grid(row=0, column=5, padx=6)

        self.var_mode_text = tk.StringVar(value="MODE: PIEZO / 1 CHANNEL")
        self.lbl_mode = ttk.Label(cfg, textvariable=self.var_mode_text)
        self.lbl_mode.grid(row=0, column=6, padx=(20, 0), sticky="w")

        ttk.Separator(cfg, orient="horizontal").grid(row=1, column=0, columnspan=7, sticky="ew", pady=8)

        self.lbl_piezo_gain = ttk.Label(cfg, text="PIEZO_GAIN")
        self.lbl_piezo_gain.grid(row=2, column=0)
        self.ent_piezo_gain = ttk.Entry(cfg, textvariable=self.var_piezo_gain, width=10)
        self.ent_piezo_gain.grid(row=2, column=1, padx=6)

        self.lbl_piezo_rate = ttk.Label(cfg, text="PIEZO_RATE")
        self.lbl_piezo_rate.grid(row=2, column=2)
        self.cb_piezo_rate = ttk.Combobox(
            cfg,
            textvariable=self.var_piezo_rate,
            values=PIEZO_SAMPLE_RATE_OPTIONS,
            width=10,
            state="readonly",
        )
        self.cb_piezo_rate.grid(row=2, column=3, padx=6)

        self.lbl_piezo_count = ttk.Label(cfg, text="PIEZO_COUNT")
        self.lbl_piezo_count.grid(row=2, column=4)
        self.cb_piezo_count = ttk.Combobox(
            cfg,
            textvariable=self.var_piezo_count,
            values=PIEZO_SAMPLE_COUNT_OPTIONS,
            width=10,
            state="readonly",
        )
        self.cb_piezo_count.grid(row=2, column=5, padx=6)

        ttk.Separator(cfg, orient="horizontal").grid(row=3, column=0, columnspan=7, sticky="ew", pady=8)

        self.lbl_mems_rate = ttk.Label(cfg, text="MEMS_RATE")
        self.lbl_mems_rate.grid(row=4, column=0)
        self.cb_mems_rate = ttk.Combobox(
            cfg,
            textvariable=self.var_adxl_rate,
            values=ADXL_SAMPLE_RATE_OPTIONS,
            width=10,
            state="readonly",
        )
        self.cb_mems_rate.grid(row=4, column=1, padx=6)

        self.lbl_mems_count = ttk.Label(cfg, text="MEMS_COUNT")
        self.lbl_mems_count.grid(row=4, column=2)
        self.cb_mems_count = ttk.Combobox(
            cfg,
            textvariable=self.var_adxl_count,
            values=ADXL_SAMPLE_COUNT_OPTIONS,
            width=10,
            state="readonly",
        )
        self.cb_mems_count.grid(row=4, column=3, padx=6)

        self.lbl_mems_grange = ttk.Label(cfg, text="MEMS_G_RANGE")
        self.lbl_mems_grange.grid(row=4, column=4)
        self.cb_mems_grange = ttk.Combobox(
            cfg,
            textvariable=self.var_adxl_grange,
            values=ADXL_G_RANGE_OPTIONS,
            width=10,
            state="readonly",
        )
        self.cb_mems_grange.grid(row=4, column=5, padx=6)

        btns = ttk.Frame(cfg)
        btns.grid(row=5, column=0, columnspan=7, pady=10, sticky="w")

        self.btn_send_cfg = ttk.Button(btns, text="Send CONFIG", command=self.send_config, state="disabled")
        self.btn_send_cfg.pack(side="left", padx=6)

        self.btn_query_cfg = ttk.Button(btns, text="Query CONFIG", command=self.query_config, state="disabled")
        self.btn_query_cfg.pack(side="left", padx=6)

        # ===== Middle: Plot + filtered Log =====
        mid = ttk.PanedWindow(main, orient="horizontal")
        mid.pack(fill="both", expand=True)

        plotf = ttk.LabelFrame(mid, text="Sensor Plot", padding=8)
        logf = ttk.LabelFrame(mid, text="Serial Log (sensorData body hidden)", padding=8)
        mid.add(plotf, weight=4)
        mid.add(logf, weight=1)

        plot_container = ttk.Frame(plotf)
        plot_container.pack(fill="both", expand=True)

        self.fig = Figure(figsize=(12.0, 9.2), dpi=100)

        # 축/그래프 객체는 센서 타입에 따라 다시 만든다.
        # PIEZO = 1개 subplot(111), MEMS = 3개 subplot(311/312/313)
        self.axes = {}
        self.lines = {}
        self.title_texts = {}
        self.peak_markers = {}
        self.peak_annots = {}

        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_container)
        self.canvas_widget = self.canvas.get_tk_widget()
        self.canvas_widget.pack(fill="both", expand=True)

        self.toolbar = NavigationToolbar2Tk(self.canvas, plot_container)
        self.toolbar.update()

        # 초기값 PIEZO에 맞춰 실제 subplot 자체를 1개만 생성
        self._rebuild_plot_axes(self.var_sensortype.get())

        # 로그 제어: 센서 패킷은 기본적으로 본문을 숨기고 필요할 때 요약만 표시
        log_ctrl = ttk.Frame(logf)
        log_ctrl.pack(fill="x", pady=(0, 6))

        self.var_show_data_summary = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            log_ctrl,
            text="Show DATA summary",
            variable=self.var_show_data_summary,
        ).pack(side="left")

        ttk.Button(log_ctrl, text="Clear Log", command=self.clear_log).pack(side="right")

        log_body = ttk.Frame(logf)
        log_body.pack(fill="both", expand=True)

        self.txt_log = tk.Text(log_body, wrap="none", width=42)
        self.txt_log.pack(side="left", fill="both", expand=True)

        log_scroll = ttk.Scrollbar(log_body, orient="vertical", command=self.txt_log.yview)
        log_scroll.pack(side="right", fill="y")
        self.txt_log.configure(yscrollcommand=log_scroll.set)

        # 맨 아래에는 마지막 이벤트도 한 줄로 유지
        self.var_event_status = tk.StringVar(value="Ready - V3")
        self.lbl_event_status = ttk.Label(main, textvariable=self.var_event_status, anchor="w")
        self.lbl_event_status.pack(fill="x", pady=(6, 0))

        # 시작 기본값 PIEZO에 맞춰 설정 항목도 PIEZO만 표시
        self._apply_sensor_type_ui(clear_data=False)

    # ===================== Sensor type UI =====================
    def _on_sensor_type_changed(self, event=None):
        idx = self.cb_sensor.current()
        if idx < 0:
            return

        self.var_sensortype.set(SENSORTYPE_OPTIONS[idx][1])
        self._apply_sensor_type_ui(clear_data=True)

    def _clear_plot_buffers(self):
        with self._rx_lock:
            self.frames.clear()
            for axis_name in ("X", "Y", "Z"):
                self.axis_frames[axis_name].clear()

    def _make_axis_artists(self, axis_name, ax, display_name=None):
        """한 축에 필요한 line/title/peak 객체를 만든다."""
        name = display_name or axis_name

        ax.set_ylabel(f"{name} Mag", fontsize=11)
        ax.grid(True, linestyle="--", linewidth=0.7, alpha=0.75)
        ax.tick_params(axis="y", which="major", labelsize=10)
        ax.tick_params(axis="x", which="major", labelsize=11, labelbottom=True, pad=6)
        ax.yaxis.set_major_locator(MaxNLocator(nbins=6))
        ax.set_xlabel("Frequency (Hz) / Sample Index", fontsize=11, labelpad=8)

        line, = ax.plot([], [], linewidth=1.2)
        self.lines[axis_name] = line

        txt = ax.text(
            0.01, 0.97, f"{name}: No data",
            transform=ax.transAxes,
            va="top", ha="left",
            fontsize=10,
        )
        self.title_texts[axis_name] = txt

        peak_marker, = ax.plot([], [], marker="o", linestyle="", markersize=7, visible=False)
        self.peak_markers[axis_name] = peak_marker

        peak_annot = ax.annotate(
            "",
            xy=(0, 0),
            xytext=(12, 12),
            textcoords="offset points",
            bbox=dict(boxstyle="round", fc="w", alpha=0.90),
            arrowprops=dict(arrowstyle="->"),
        )
        peak_annot.set_visible(False)
        self.peak_annots[axis_name] = peak_annot

    def _rebuild_plot_axes(self, sensor_type):
        """
        센서 타입에 따라 Figure 자체를 다시 구성한다.
        PIEZO(1) : subplot 1개
        MEMS(2)  : X/Y/Z subplot 3개
        STOP(0)  : 빈 subplot 1개
        """
        self.fig.clear()
        self.axes = {}
        self.lines = {}
        self.title_texts = {}
        self.peak_markers = {}
        self.peak_annots = {}

        if sensor_type == "2":
            self.ax_x = self.fig.add_subplot(311)
            self.ax_y = self.fig.add_subplot(312)
            self.ax_z = self.fig.add_subplot(313)
            self.axes = {"X": self.ax_x, "Y": self.ax_y, "Z": self.ax_z}

            for axis_name, ax in self.axes.items():
                self._make_axis_artists(axis_name, ax, axis_name)

            self.ax_x.set_title("MEMS - X / Y / Z (3 CHANNEL)", fontsize=14, pad=10)
            self.fig.subplots_adjust(
                left=0.08, right=0.985, top=0.95, bottom=0.08, hspace=0.50
            )
        else:
            self.ax_x = self.fig.add_subplot(111)
            self.ax_y = None
            self.ax_z = None
            self.axes = {"X": self.ax_x}

            display_name = "PIEZO" if sensor_type == "1" else "STOP"
            self._make_axis_artists("X", self.ax_x, display_name)

            if sensor_type == "1":
                self.ax_x.set_title("PIEZO - 1 CHANNEL", fontsize=14, pad=10)
            else:
                self.ax_x.set_title("SENSOR STOP", fontsize=13, pad=10)

            self.fig.subplots_adjust(
                left=0.08, right=0.985, top=0.93, bottom=0.10
            )

        if hasattr(self, "canvas"):
            self.canvas.draw_idle()

    def _apply_sensor_type_ui(self, clear_data=True):
        sensor_type = self.var_sensortype.get()

        if clear_data:
            self._clear_plot_buffers()

        piezo_enabled = (sensor_type == "1")
        mems_enabled = (sensor_type == "2")

        piezo_widgets = (
            self.lbl_piezo_gain, self.ent_piezo_gain,
            self.lbl_piezo_rate, self.cb_piezo_rate,
            self.lbl_piezo_count, self.cb_piezo_count,
        )
        mems_widgets = (
            self.lbl_mems_rate, self.cb_mems_rate,
            self.lbl_mems_count, self.cb_mems_count,
            self.lbl_mems_grange, self.cb_mems_grange,
        )

        if piezo_enabled:
            for w in piezo_widgets:
                w.grid()
            for w in mems_widgets:
                w.grid_remove()
        elif mems_enabled:
            for w in piezo_widgets:
                w.grid_remove()
            for w in mems_widgets:
                w.grid()
        else:
            for w in piezo_widgets + mems_widgets:
                w.grid_remove()

        if sensor_type == "1":
            self.var_mode_text.set("MODE: PIEZO / 1 CHANNEL")
        elif sensor_type == "2":
            self.var_mode_text.set("MODE: MEMS / 3 CHANNEL (X/Y/Z)")
        else:
            self.var_mode_text.set("MODE: STOP")

        # 핵심: subplot 자체를 1개/3개로 재생성한다.
        self._rebuild_plot_axes(sensor_type)

    # ===================== Peak 표시 =====================
    def _hide_peak(self, axis_name):
        self.peak_markers[axis_name].set_visible(False)
        self.peak_annots[axis_name].set_visible(False)

    def _show_peak(self, axis_name, x, y, text):
        marker = self.peak_markers[axis_name]
        annot = self.peak_annots[axis_name]

        marker.set_data([x], [y])
        marker.set_visible(True)

        annot.xy = (x, y)
        annot.set_text(text)
        annot.set_visible(True)

    # ===================== Serial =====================
    def refresh_ports(self):
        ports = [p.device for p in list_ports.comports()]
        self.cb_port["values"] = ports

        current = self.var_serial_port.get()
        if current in ports:
            self.var_serial_port.set(current)
        elif ports:
            self.var_serial_port.set(ports[0])
        else:
            self.var_serial_port.set("")

    def on_connect(self):
        port = self.var_serial_port.get().strip()
        if not port:
            messagebox.showerror("Error", "COM Port를 선택하세요.")
            return

        try:
            baud = int(self.var_baud.get())
        except Exception:
            messagebox.showerror("Error", "Baud rate가 올바르지 않습니다.")
            return

        try:
            self.ser = serial.Serial(
                port=port,
                baudrate=baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.20,
                write_timeout=1.0,
            )
        except Exception as e:
            messagebox.showerror("Error", f"Serial connect failed:\n{e}")
            self.ser = None
            return

        self.rx_stop.clear()
        self.rx_thread = threading.Thread(target=self._serial_rx_worker, daemon=True)
        self.rx_thread.start()

        self._set_connected(True)
        self._log(f"CONNECTED {port} @ {baud} 8N1", "SYS")

    def on_disconnect(self):
        self.rx_stop.set()

        ser = self.ser
        self.ser = None

        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

        self._set_connected(False)
        self._log("DISCONNECTED", "SYS")

    def _serial_rx_worker(self):
        """
        백그라운드 스레드에서 시리얼을 읽는다.
        Tk 위젯은 worker thread에서 직접 건드리면 안 되므로 root.after()로 넘긴다.
        """
        while not self.rx_stop.is_set():
            ser = self.ser
            if ser is None or not ser.is_open:
                break

            try:
                raw_bytes = ser.readline()
            except Exception as e:
                if not self.rx_stop.is_set():
                    self.root.after(0, self._serial_error, str(e))
                break

            if not raw_bytes:
                continue

            raw = raw_bytes.decode("utf-8", errors="replace")
            self.root.after(0, self._handle_serial_fragment, raw)

    def _serial_error(self, err):
        self._log(f"SERIAL ERROR: {err}", "SYS")
        self.on_disconnect()

    def _handle_serial_fragment(self, raw: str):
        """
        시리얼 문자열에서 JSON 객체만 조립한다.
        센서 데이터 원문은 Log에 계속 출력하지 않는다.
        한 줄 JSON과 여러 줄(JSON pretty print) 둘 다 대응한다.
        """
        if raw is None:
            return

        # 대용량 sensorData 원문은 Log에 출력하지 않고 파싱만 한다.
        text = raw

        # JSON 수집 전이면 첫 '{'를 찾는다.
        if not self._json_collecting:
            start = text.find("{")
            if start < 0:
                # JSON이 아닌 펌웨어 printf/debug 문자열은 로그에 그대로 표시한다.
                plain = text.strip()
                if plain:
                    self._log(plain, "RX")
                return

            # '{' 앞에 일반 문자열이 같이 붙어 있으면 그 부분은 로그로 남긴다.
            prefix = text[:start].strip()
            if prefix:
                self._log(prefix, "RX")

            text = text[start:]
            self._json_collecting = True
            self._json_accum = ""
            self._json_depth = 0

        self._json_accum += text
        self._json_depth += json_brace_delta(text)

        # 아직 JSON이 끝나지 않았다.
        if self._json_depth > 0:
            return

        # 완성 후보
        candidate = self._json_accum.strip()

        self._json_collecting = False
        self._json_accum = ""
        self._json_depth = 0

        if not candidate:
            return

        try:
            pkt = json.loads(candidate)
        except Exception as e:
            # 센서 로그 등 일반 문자열은 이미 위에서 표시했으므로,
            # JSON처럼 시작했지만 깨진 경우만 오류 표시
            self._log(f"JSON PARSE FAIL: {e}", "SYS")
            return

        self._process_packet(pkt)

    def _process_packet(self, pkt):
        """기존 MQTT _on_message()의 sensorData 처리 부분을 그대로 분리한 함수."""
        if not isinstance(pkt, dict):
            return

        sensor_hex = pkt.get("sensorData", "")
        if not sensor_hex:
            # 센서 데이터가 아닌 CONFIG/상태 응답 정도만 간단히 표시한다.
            self._log(json.dumps(pkt, ensure_ascii=False), "RX")
            return

        is_fft_packet = ("fftN" in pkt) or ("fs" in pkt)

        # 15 / "15" / 0x0F / "0x0F" => 3200
        fs = normalize_adxl_fs_from_packet(pkt.get("fs", None))
        fftN = pkt.get("fftN", None)
        seq = pkt.get("seq", None)

        try:
            fftN = int(fftN) if fftN is not None else None
        except Exception:
            fftN = None

        sensor_type = self.var_sensortype.get()
        axis = seq_to_axis(seq) if sensor_type == "2" else None

        try:
            if is_fft_packet:
                data = hex_to_f32_list_be(sensor_hex)
                pkt_format = "f32"
            else:
                data = hex_to_u16_list(sensor_hex)
                pkt_format = "u16"
        except Exception as e:
            self._log(f"sensorData parse failed: {e}", "SYS")
            return

        if not data:
            return

        if MAX_BINS_TO_PLOT and len(data) > MAX_BINS_TO_PLOT:
            data = data[:MAX_BINS_TO_PLOT]

        # 긴 sensorData HEX 자체는 로그에 표시하지 않는다.
        # 필요할 때만 체크박스로 패킷 요약을 볼 수 있다.
        if hasattr(self, "var_show_data_summary") and self.var_show_data_summary.get():
            if sensor_type == "2":
                channel_name = axis if axis in ("X", "Y", "Z") else "MEMS?"
            else:
                channel_name = "PIEZO"
            data_kind = "FFT" if pkt_format == "f32" else "RAW"
            self._log(
                f"DATA {channel_name} {data_kind} seq={seq} fs={fs} fftN={fftN} samples={len(data)}",
                "RX",
            )

        with self._rx_lock:
            if sensor_type == "2" and axis in ("X", "Y", "Z"):
                # MEMS는 seq를 기준으로 X/Y/Z 각각 분리한다.
                self.axis_frames[axis].append(data)
                self.axis_meta[axis]["seq"] = seq
                self.axis_meta[axis]["tick"] = pkt.get("tick")
                self.axis_meta[axis]["batteryRmin"] = pkt.get("batteryRmin")
                self.axis_meta[axis]["fs"] = fs
                self.axis_meta[axis]["fftN"] = fftN
                self.axis_meta[axis]["pkt_format"] = pkt_format
            else:
                # PIEZO는 seq 값과 관계없이 항상 단일 채널로 취급한다.
                self.frames.append(data)
                self.latest_meta["seq"] = seq
                self.latest_meta["tick"] = pkt.get("tick")
                self.latest_meta["batteryRmin"] = pkt.get("batteryRmin")
                self.latest_meta["fs"] = fs
                self.latest_meta["fftN"] = fftN
                self.latest_meta["pkt_format"] = pkt_format
                self.latest_meta["axis"] = None

    def _set_connected(self, v: bool):
        self.connected = v
        self.btn_connect.config(state="disabled" if v else "normal")
        self.btn_disconnect.config(state="normal" if v else "disabled")
        self.btn_refresh.config(state="disabled" if v else "normal")
        self.btn_send_cfg.config(state="normal" if v else "disabled")
        self.btn_query_cfg.config(state="normal" if v else "disabled")
        self.cb_port.config(state="disabled" if v else "readonly")
        self.lbl_status.config(text="CONNECTED" if v else "DISCONNECTED")

    # ===================== Commands =====================
    def build_config_json(self):
        return {
            "COMMPATH": SERIAL_COMMPATH_VALUE,
            "SENSORTYPE": self.var_sensortype.get(),
            "PIEZO_GAIN": self.var_piezo_gain.get(),
            "PIEZO_SAMPLE_RATE": self.var_piezo_rate.get(),
            "PIEZO_SAMPLE_COUNT": self.var_piezo_count.get(),
            "MEMS_SAMPLE_RATE": self.var_adxl_rate.get(),
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
        except Exception as e:
            messagebox.showerror("Serial", f"Serial send failed:\n{e}")
            return False

        return True

    def send_config(self):
        cfg = self.build_config_json()
        if self._serial_send_json(cfg):
            # 설정값은 실제로 무엇을 보냈는지 확인할 수 있도록 전체를 로그에 남긴다.
            self._log("CONFIG " + json.dumps(cfg, ensure_ascii=False), "TX")

    def query_config(self):
        if self._serial_send_json({"CONFIG": "?"}):
            self._log("CONFIG query sent", "TX")

    # ===================== Plot helper =====================
    def _plot_one_axis(self, axis_name, data, meta):
        ax = self.axes[axis_name]
        line = self.lines[axis_name]
        title_text = self.title_texts[axis_name]

        if data is None or meta is None:
            line.set_data([], [])
            title_text.set_text(f"{axis_name}: No data")
            ax.set_xlim(0, 1)
            ax.set_ylim(-1, 1)

            self.axis_plot_cache[axis_name] = {
                "x": [],
                "y": [],
                "is_fft": False,
                "df": None,
            }
            self._hide_peak(axis_name)
            return

        pkt_format = meta.get("pkt_format") or "u16"

        if pkt_format == "u16":
            y_i16 = [v - 65536 if v >= 32768 else v for v in data]
            x = list(range(len(y_i16)))

            line.set_data(x, y_i16)
            ax.set_xlim(0, max(1, len(x) - 1))
            display_name = "PIEZO" if (self.var_sensortype.get() == "1" and axis_name == "X") else axis_name
            ax.set_ylabel(f"{display_name} Raw", fontsize=11)

            self.axis_plot_cache[axis_name] = {
                "x": x,
                "y": y_i16,
                "is_fft": False,
                "df": None,
            }

            ax.xaxis.set_major_locator(MaxNLocator(nbins=10, integer=True))
            ax.xaxis.set_major_formatter(FuncFormatter(lambda xv, pos: f"{int(round(xv))}"))

            ymin = min(y_i16) if y_i16 else -1
            ymax = max(y_i16) if y_i16 else 1
            if ymin == ymax:
                ymin -= 1
                ymax += 1
            pad = max(1.0, (ymax - ymin) * 0.10)
            ax.set_ylim(ymin - pad, ymax + pad)

            seq = meta.get("seq")
            tick = meta.get("tick")
            batt = meta.get("batteryRmin")
            title_text.set_text(
                f"{display_name} RAW  seq={seq}  tick={tick}  batt={batt}  samples={len(y_i16)}"
            )

            # RAW에서는 자동 peak 표시 안 함
            self._hide_peak(axis_name)

        else:
            fs = meta.get("fs")
            fftN = meta.get("fftN") if meta.get("fftN") else FFT_N_DEFAULT

            if fs is None:
                if self.var_sensortype.get() == "1":
                    try:
                        fs = int(self.var_piezo_rate.get())
                    except Exception:
                        fs = None
                elif self.var_sensortype.get() == "2":
                    fs = normalize_adxl_rate_from_config(self.var_adxl_rate.get())

            if fs is None:
                fs = FS_HZ_DEFAULT

            df = fs / fftN if fftN else 1.0
            x = [i * df for i in range(len(data))]

            line.set_data(x, data)
            display_name = "PIEZO" if (self.var_sensortype.get() == "1" and axis_name == "X") else axis_name
            ax.set_ylabel(f"{display_name} Mag", fontsize=11)

            self.axis_plot_cache[axis_name] = {
                "x": x,
                "y": data,
                "is_fft": True,
                "df": df,
            }

            x_max = x[-1] if x else (fs / 2.0)
            ax.set_xlim(0, max(1.0, x_max))

            if fs <= 3200:
                ax.xaxis.set_major_locator(MultipleLocator(200))
            elif fs <= 6400:
                ax.xaxis.set_major_locator(MultipleLocator(500))
            elif fs <= 12800:
                ax.xaxis.set_major_locator(MultipleLocator(1000))
            else:
                ax.xaxis.set_major_locator(MaxNLocator(nbins=10))

            ax.xaxis.set_major_formatter(
                FuncFormatter(lambda xv, pos: f"{xv:.1f}" if abs(xv) < 100 else f"{xv:.0f}")
            )

            yy = [v for v in data if isinstance(v, (int, float)) and not math.isnan(v) and not math.isinf(v)]
            yy2 = [v for v in yy if v >= 0.0]
            ymax = max(yy2) if yy2 else 1.0
            ymax = max(1e-12, ymax)
            ax.set_ylim(0, ymax * (1.0 + Y_MARGIN_RATIO))

            seq = meta.get("seq")
            tick = meta.get("tick")
            batt = meta.get("batteryRmin")

            # ===== 자동 peak 탐색 =====
            peak_idx = None
            peak_val = None

            start_idx = 1 if (EXCLUDE_DC_FOR_PEAK and len(data) > 1) else 0

            for i in range(start_idx, len(data)):
                v = data[i]
                if not isinstance(v, (int, float)):
                    continue
                if math.isnan(v) or math.isinf(v):
                    continue
                if peak_val is None or v > peak_val:
                    peak_val = v
                    peak_idx = i

            if peak_idx is not None:
                peak_freq = x[peak_idx]
                self._show_peak(
                    axis_name,
                    peak_freq,
                    peak_val,
                    f"PEAK\nbin {peak_idx}\n{peak_freq:.3f} Hz\n{peak_val:.6g}",
                )
                peak_info = f"  peak=bin{peak_idx} ({peak_freq:.3f}Hz, {peak_val:.6g})"
            else:
                self._hide_peak(axis_name)
                peak_info = ""

            title_text.set_text(
                f"{display_name} FFT  seq={seq}  tick={tick}  batt={batt}  "
                f"Fs={fs}Hz  N={fftN}  Δf={df:.3f}Hz  bins={len(data)}  ymax={ymax:.6g}{peak_info}"
            )

    # ===================== Plot update =====================
    def _update_plot(self):
        with self._rx_lock:
            axis_data = {}
            axis_meta = {}

            for axis_name in ("X", "Y", "Z"):
                if len(self.axis_frames[axis_name]) > 0:
                    axis_data[axis_name] = self.axis_frames[axis_name][-1]
                    axis_meta[axis_name] = dict(self.axis_meta[axis_name])
                else:
                    axis_data[axis_name] = None
                    axis_meta[axis_name] = None

            single_data = self.frames[-1] if len(self.frames) > 0 else None
            single_meta = dict(self.latest_meta) if len(self.frames) > 0 else None

        sensor_type = self.var_sensortype.get()

        if sensor_type == "2":
            # MEMS: 항상 X/Y/Z 세 축을 각각 갱신한다.
            for axis_name in ("X", "Y", "Z"):
                self._plot_one_axis(axis_name, axis_data[axis_name], axis_meta[axis_name])
        elif sensor_type == "1":
            # PIEZO: 단일 채널만 X 위치의 큰 그래프 하나에 표시한다.
            self._plot_one_axis("X", single_data, single_meta)
        else:
            self._plot_one_axis("X", None, None)

        self.canvas.draw_idle()
        self.root.after(50, self._update_plot)

    # ===================== Log / Status =====================
    def clear_log(self):
        if hasattr(self, "txt_log"):
            self.txt_log.delete("1.0", "end")

    def _log(self, s, tag):
        # sensorData 본문은 _process_packet()에서 호출하지 않으므로 여기에는
        # 연결/설정/일반 디버그/오류/선택적 DATA 요약만 들어온다.
        msg = str(s).replace("\r", " ").replace("\n", " ").strip()
        if not msg:
            return

        # 한 줄이 지나치게 길어 UI가 무거워지는 것을 방지한다.
        if len(msg) > 1200:
            msg = msg[:1197] + "..."

        line = f"[{now_iso()}] {tag}  {msg}\n"

        if hasattr(self, "txt_log"):
            self.txt_log.insert("end", line)
            self.txt_log.see("end")

            # 로그가 무한정 쌓이지 않도록 최근 약 1000줄만 유지한다.
            try:
                line_count = int(self.txt_log.index("end-1c").split(".")[0])
                if line_count > 1000:
                    self.txt_log.delete("1.0", f"{line_count - 1000}.0")
            except Exception:
                pass

        if hasattr(self, "var_event_status"):
            short = msg if len(msg) <= 180 else msg[:177] + "..."
            self.var_event_status.set(f"[{now_iso()}] {tag}: {short}")

    # ===================== Quit =====================
    def on_quit(self):
        self.rx_stop.set()

        ser = self.ser
        self.ser = None

        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

        self.root.destroy()


def main():
    root = tk.Tk()
    SerialGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
