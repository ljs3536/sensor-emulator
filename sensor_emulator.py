import time
import json
import random
import threading
import numpy as np
from paho.mqtt import client as mqtt_client
from paho.mqtt.enums import CallbackAPIVersion

BROKER = '127.0.0.1'  # Docker MQTT 브로커 IP
PORT = 1883

# 1. 초음파 센서 에뮬레이터 스레드 (기존 서식 모사)
def run_ultrasonic_emulator(client):
    topic = "D/0102030405060708"
    seq = 1
    print("[Ultrasonic Emulator] Started...")
    
    while True:
        # 기존에 들어오던 문자열 형태 모사
        payload_data = {
            "gwMacAddr": "HALOW",
            "dvicMacAddr": "0102030405060708",
            "batteryRmin": "5",
            "seq": str(seq),
            "sensorData": "00FF00FF55AA55AA19C800001305BB3A0B82F83AFEB1D83AABE6733B2EC6D83A"
        }
        
        # 기존 수신 메시지에 들어있던 "D/... b'{...}'" 포맷 모사
        message_str = f"D/0102030405060708 b'{json.dumps(payload_data)}'\r\n'"
        
        client.publish(topic, message_str)
        print(f"[Ultrasonic] Published seq:{seq} to {topic}")
        
        seq += 1
        time.sleep(3)  # 3초마다 송신

# 2. 진동 센서(Piezo/ADXL) 에뮬레이터 스레드
def run_vibration_emulator(client):
    topic = "VIB/PIEZO/01"
    seq = 1
    print("[Vibration Emulator] Started...")
    
    while True:
        # 1024개 샘플 데이터 생성 (10Hz, 50Hz 파형 합성)
        t = np.linspace(0, 1.0, 1024, endpoint=False)
        signal = 1.5 * np.sin(2 * np.pi * 10 * t) + 0.8 * np.sin(2 * np.pi * 50 * t)
        noise = np.random.normal(0, 0.1, 1024)
        raw_samples = (signal + noise).tolist()
        
        payload_data = {
            "dvicMacAddr": "PIEZO_SENSOR_01",
            "sensorType": "PIEZO",
            "samplingRate": 1024,
            "seq": seq,
            "data": raw_samples
        }
        
        client.publish(topic, json.dumps(payload_data))
        print(f"[Vibration] Published seq:{seq} to {topic}")
        
        seq += 1
        time.sleep(1)  # 1초마다 송신

def main():
    client = mqtt_client.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id="Integrated_Sensor_Emulator"
    )
    try:
        client.connect(BROKER, PORT)
        print(f"Connected to MQTT Broker ({BROKER}:{PORT})")
    except Exception as e:
        print(f"Failed to connect to MQTT Broker: {e}")
        return

    # 두 센서 시뮬레이터를 독립된 스레드로 실행
    t1 = threading.Thread(target=run_ultrasonic_emulator, args=(client,))
    t2 = threading.Thread(target=run_vibration_emulator, args=(client,))
    
    t1.daemon = True
    t2.daemon = True
    
    t1.start()
    t2.start()

    # 메인 스레드 대기
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Emulator Stopped.")

if __name__ == '__main__':
    main()