import json
import time
import serial
import paho.mqtt.client as mqtt

# 1. 설정
PORT = "COM5"          # 하드웨어 팀에서 지정해줄 COM 포트
BAUDRATE = 230400      # 통신 속도 (하드웨어팀과 맞춤)

MQTT_BROKER = "mqtt.r-lab.co.kr"
MQTT_PORT = 1883
MQTT_TOPIC = "V/035415641614"

# 2. MQTT 연결
client = mqtt.Client()
client.connect(MQTT_BROKER, MQTT_PORT, 60)
client.loop_start()

# 3. Serial 연결
try:
    ser = serial.Serial(PORT, BAUDRATE, timeout=1.0)
    print(f"[BRIDGE] Connected to {PORT} at {BAUDRATE} baud")
except Exception as e:
    print(f"[ERROR] Serial connection failed: {e}")
    exit(1)

# 4. 중계 무한 루프
try:
    while True:
        if ser.in_waiting:
            # 시리얼에서 한 줄 읽기 (장비 송신 포맷에 따라 read/readline 결정)
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            
            if line:
                # 필요시 JSON 변환 또는 데이터 검증 후 Publish
                print(f"[SERIAL -> MQTT] {line}")
                client.publish(MQTT_TOPIC, line, qos=1)

        time.sleep(0.01)

except KeyboardInterrupt:
    print("\n[BRIDGE] Stopped")
    ser.close()
    client.loop_stop()
    client.disconnect()