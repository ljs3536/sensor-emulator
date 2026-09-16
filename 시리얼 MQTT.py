import serial
import threading
import time
import json
import tkinter as tk
from tkinter import ttk, scrolledtext
import paho.mqtt.client as mqtt


# ================= 고정 설정 =================
BAUDRATE = 115200
HEADER = bytes.fromhex("00FF00FF55AA55AA")
GW_ID = "HALOW"

DEFAULT_MQTT_BROKER = "15.165.63.242"
DEFAULT_MQTT_PORT = 1883
# ===========================================


class SerialMQTTApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Serial → MQTT Tool")

        self.running = False
        self.seq_num = 0
        self.buffer = bytearray()

        self.mac_rows = []

        self.ser = None
        self.client = None

        self.create_ui()

    # ================= UI =================
    def create_ui(self):

        frame = ttk.Frame(self.root, padding=10)
        frame.grid(row=0, column=0, sticky="w")

        # ---------------- COM Port ----------------
        ttk.Label(frame, text="COM Port").grid(
            row=0,
            column=0,
            sticky="w",
            padx=5,
            pady=3
        )

        self.com_entry = ttk.Entry(frame, width=25)
        self.com_entry.insert(0, "COM3")
        self.com_entry.grid(
            row=0,
            column=1,
            padx=5,
            pady=3
        )

        # ---------------- MQTT Broker ----------------
        ttk.Label(frame, text="MQTT Broker").grid(
            row=1,
            column=0,
            sticky="w",
            padx=5,
            pady=3
        )

        self.broker_entry = ttk.Entry(frame, width=25)
        self.broker_entry.insert(0, DEFAULT_MQTT_BROKER)
        self.broker_entry.grid(
            row=1,
            column=1,
            padx=5,
            pady=3
        )

        # ---------------- MQTT Port ----------------
        ttk.Label(frame, text="MQTT Port").grid(
            row=2,
            column=0,
            sticky="w",
            padx=5,
            pady=3
        )

        self.port_entry = ttk.Entry(frame, width=25)
        self.port_entry.insert(0, str(DEFAULT_MQTT_PORT))
        self.port_entry.grid(
            row=2,
            column=1,
            padx=5,
            pady=3
        )

        # ---------------- MAC ----------------
        ttk.Label(frame, text="Device MAC").grid(
            row=3,
            column=0,
            sticky="nw",
            padx=5,
            pady=3
        )

        self.mac_frame = ttk.Frame(frame)
        self.mac_frame.grid(
            row=3,
            column=1,
            sticky="w",
            padx=5,
            pady=3
        )

        self.add_mac_row()

        # MAC 추가
        ttk.Button(
            frame,
            text="+ Add MAC",
            command=self.add_mac_row
        ).grid(
            row=4,
            column=1,
            sticky="w",
            padx=5,
            pady=5
        )

        # ---------------- Start / Stop ----------------
        button_frame = ttk.Frame(frame)
        button_frame.grid(
            row=5,
            column=0,
            columnspan=2,
            pady=10
        )

        ttk.Button(
            button_frame,
            text="Start",
            command=self.start,
            width=15
        ).pack(
            side="left",
            padx=5
        )

        ttk.Button(
            button_frame,
            text="Stop",
            command=self.stop,
            width=15
        ).pack(
            side="left",
            padx=5
        )

        # ---------------- 로그 ----------------
        self.log = scrolledtext.ScrolledText(
            self.root,
            width=85,
            height=25
        )

        self.log.grid(
            row=1,
            column=0,
            padx=10,
            pady=10
        )

    # ================= MAC Row =================
    def add_mac_row(self):

        row_frame = ttk.Frame(self.mac_frame)
        row_frame.pack(anchor="w", pady=2)

        entry = ttk.Entry(row_frame, width=25)
        entry.pack(side="left", padx=5)

        btn = ttk.Button(
            row_frame,
            text="X",
            width=3,
            command=lambda: self.remove_mac_row(row_frame)
        )

        btn.pack(side="left")

        self.mac_rows.append(
            (row_frame, entry)
        )

    def remove_mac_row(self, frame):

        for row in self.mac_rows:

            if row[0] == frame:
                self.mac_rows.remove(row)
                break

        frame.destroy()

    # ================= MQTT =================
    def setup_mqtt(self):

        broker = self.broker_entry.get().strip()

        try:
            port = int(self.port_entry.get().strip())

        except ValueError:
            self.log_msg("MQTT Port 값이 올바르지 않습니다.")
            return False

        try:

            self.client = mqtt.Client()

            self.client.on_connect = self.on_mqtt_connect
            self.client.on_disconnect = self.on_mqtt_disconnect
            self.client.on_publish = self.on_mqtt_publish

            self.log_msg(
                f"MQTT Connecting → {broker}:{port}"
            )

            self.client.connect(
                broker,
                port,
                60
            )

            self.client.loop_start()

            return True

        except Exception as e:

            self.log_msg(
                f"MQTT Connect Fail: {e}"
            )

            return False

    def on_mqtt_connect(self, client, userdata, flags, rc):

        if rc == 0:
            self.log_msg("MQTT Connected")

        else:
            self.log_msg(
                f"MQTT Connection Failed (rc={rc})"
            )

    def on_mqtt_disconnect(self, client, userdata, rc):

        self.log_msg(
            f"MQTT Disconnected (rc={rc})"
        )

    def on_mqtt_publish(self, client, userdata, mid):

        self.log_msg(
            f"Publish Success (mid={mid})"
        )

    # ================= 로그 =================
    def log_msg(self, msg):

        def update():

            self.log.insert(
                tk.END,
                f"[{time.strftime('%H:%M:%S')}] {msg}\n"
            )

            self.log.see(tk.END)

        self.root.after(0, update)

    # ================= 패킷 파싱 =================
    def read_packet(self):

        while self.running:

            data = self.ser.read(1024)

            if data:
                self.buffer.extend(data)

            while True:

                start = self.buffer.find(HEADER)

                if start == -1:

                    if len(self.buffer) > 4096:

                        self.buffer = self.buffer[
                            -len(HEADER):
                        ]

                    break

                next_start = self.buffer.find(
                    HEADER,
                    start + len(HEADER)
                )

                if next_start == -1:
                    break

                packet = self.buffer[
                    start:next_start
                ]

                self.buffer = self.buffer[
                    next_start:
                ]

                return packet

        return None

    # ================= 작업 스레드 =================
    def worker(self):

        while self.running:

            try:

                pkt = self.read_packet()

                if pkt is None:
                    continue

                if len(pkt) != 1292:

                    self.log_msg(
                        f"Invalid packet size: {len(pkt)}"
                    )

                    continue

                # MAC 리스트
                mac_list = []

                for _, entry in self.mac_rows:

                    mac = entry.get().strip()

                    if mac:
                        mac_list.append(mac)

                if not mac_list:

                    self.log_msg(
                        "MAC Address가 없습니다."
                    )

                    continue

                # MAC마다 MQTT 전송
                for mac in mac_list:

                    self.seq_num += 1

                    payload = {

                        "gwMacAddr": GW_ID,

                        "dvicMacAddr": mac,

                        "batteryRmin": "100",

                        "seq": str(
                            self.seq_num
                        ),

                        "tick": str(
                            int(time.time() * 1000)
                        ),

                        "sensorData":
                            pkt.hex().upper()
                    }

                    msg = json.dumps(
                        payload
                    )

                    topic = f"D/{mac}"

                    self.log_msg(
                        f"JSON → {mac}"
                    )

                    self.log_msg(
                        msg[:200] + "..."
                    )

                    result = self.client.publish(
                        topic,
                        msg,
                        qos=2
                    )

                    if result.rc != 0:

                        self.log_msg(
                            f"Publish Fail → {topic}"
                        )

                    else:

                        self.log_msg(
                            f"TX seq={self.seq_num}, "
                            f"{topic}"
                        )

            except Exception as e:

                self.log_msg(
                    f"ERROR: {e}"
                )

                time.sleep(1)

    # ================= 시작 =================
    def start(self):

        if self.running:

            self.log_msg(
                "이미 실행 중입니다."
            )

            return

        # -------- Serial Open --------
        try:

            com_port = self.com_entry.get().strip()

            self.ser = serial.Serial(
                com_port,
                BAUDRATE,
                timeout=0.1
            )

            self.log_msg(
                f"Serial Connected → "
                f"{com_port} / {BAUDRATE}"
            )

        except Exception as e:

            self.log_msg(
                f"Serial Open Fail: {e}"
            )

            return

        # -------- MQTT Open --------
        if not self.setup_mqtt():

            try:
                self.ser.close()
            except:
                pass

            return

        # -------- Start --------
        self.running = True

        self.seq_num = 0
        self.buffer = bytearray()

        threading.Thread(
            target=self.worker,
            daemon=True
        ).start()

        self.log_msg("START")

    # ================= 종료 =================
    def stop(self):

        if not self.running:
            return

        self.running = False

        # Serial 종료
        try:

            if self.ser:
                self.ser.close()

        except:
            pass

        # MQTT 종료
        try:

            if self.client:

                self.client.loop_stop()
                self.client.disconnect()

        except:
            pass

        self.log_msg("STOP")


# ================= 실행 =================
if __name__ == "__main__":

    root = tk.Tk()

    app = SerialMQTTApp(root)

    root.mainloop()