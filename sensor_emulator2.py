import time
import json
import threading
import numpy as np
from paho.mqtt import client as mqtt_client
from paho.mqtt.enums import CallbackAPIVersion

BROKER = '127.0.0.1'
PORT = 1883
DATA_TOPIC = "VIB/PIEZO/01"
CMD_TOPIC = "VIB/PIEZO/01/config/set"   # 서버가 보낸 명령을 받는 토픽 (Subscribe)
RES_TOPIC = "VIB/PIEZO/01/config/res"   # 서버로 디바이스 상태를 응답하는 토픽 (Publish)

# 디바이스 기본 설정값 (전원 재부팅 후에도 유지되는 백업 레지스터 가정)
device_config = {
    "COMMPATH": "0",             # 0: MQTT, 1: Serial
    "SENSORTYPE": "1",           # 0: STOP, 1: PIEZO, 2: ADXL
    "PIEZO_GAIN": "255",
    "PIEZO_SAMPLE_RATE": "25600",
    "PIEZO_SAMPLE_COUNT": "1024",# 테스트 편의상 1024 설정 (8192로 변경 가능)
    "ADXL_SAMPLE_RATE": "3200",  # (명세서의 MEMS/ADXL 혼용 명칭 지원)
    "ADXL_SAMPLE_COUNT": "1024",
    "ADXL_G_RANGE": "2",
    "SENSING_PERIOD": "1000"     # ms 단위 (1000ms = 1초)
}

# 시퀀스 번호 순환 관리 (1의 자리: 1~F / 1~15)
seq_frame_counter = 1

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

def generate_hex_sensor_data(sample_count, freq=10, amplitude=500):
    """
    파형 샘플을 생성하여 Signed 16진수 4자리(short) Hex 문자열로 변환
    """
    t = np.linspace(0, 1.0, sample_count, endpoint=False)
    signal = amplitude * np.sin(2 * np.pi * freq * t)
    noise = np.random.normal(0, 30, sample_count)
    raw_samples = (signal + noise).astype(int)

    hex_data_list = []
    for val in raw_samples:
        # Int16 범위를 4자리 16진수로 변환 (Signed 2's complement)
        hex_str = f"{val & 0xFFFF:04X}"
        hex_data_list.append(hex_str)

    return "".join(hex_data_list)

def on_connect(client, userdata, flags, rc, properties=None):
    print(f"✅ Connected to MQTT Broker ({BROKER}:{PORT})")
    # 제어 명령 수신 전용 토픽 구독
    client.subscribe(CMD_TOPIC)
    print(f"📡 Subscribed to Command Topic: {CMD_TOPIC}")

def on_message(client, userdata, msg):
    global device_config
    try:
        payload_str = msg.payload.decode('utf-8').strip()
        print(f"\n📩 [Command Received] Topic: {msg.topic}, Payload: {payload_str}")
        
        req_json = json.loads(payload_str)
        
        # 1. 설정값 조회 요청 (CONFIG: ?)
        if req_json.get("CONFIG") == "?":
            print(">>> [CONFIG Query] Returning Device Config...")

        # 2. 설정값 변경 요청
        else:
            print(">>> [CONFIG Update] Applying New Settings...")
            # 화면에서 날아온 설정값들(PIEZO_SAMPLE_RATE, ADXL_SAMPLE_COUNT 등)을 
            # device_config 전역 객체에 전부 갱신
            for key, val in req_json.items():
                device_config[key] = str(val)

            print(f"✅ [Applied Config] SENSORTYPE={device_config.get('SENSORTYPE')}, "
                  f"PIEZO_CNT={device_config.get('PIEZO_SAMPLE_COUNT')}, "
                  f"ADXL_CNT={device_config.get('ADXL_SAMPLE_COUNT')}, "
                  f"PERIOD={device_config.get('SENSING_PERIOD')}ms")

        # 결과/현재 상태 응답 (RES_TOPIC으로 전송하여 화면과 동기화)
        res_payload = {
            "COMMPATH": device_config.get("COMMPATH", "0"),
            "SENSORTYPE": device_config.get("SENSORTYPE", "1"),
            "PIEZO_GAIN": device_config.get("PIEZO_GAIN", "255"),
            "PIEZO_SAMPLE_RATE": device_config.get("PIEZO_SAMPLE_RATE", "25600"),
            "PIEZO_SAMPLE_COUNT": device_config.get("PIEZO_SAMPLE_COUNT", "1024"),
            "ADXL_SAMPLE_RATE": device_config.get("ADXL_SAMPLE_RATE", "0x0F"),
            "ADXL_SAMPLE_COUNT": device_config.get("ADXL_SAMPLE_COUNT", "1024"),
            "ADXL_G_RANGE": device_config.get("ADXL_G_RANGE", "2"),
            "SENSING_PERIOD": device_config.get("SENSING_PERIOD", "1000")
        }
        client.publish(RES_TOPIC, json.dumps(res_payload))

    except Exception as e:
        print(f"❌ Command Error: {e}")

def run_vibration_publisher(client):
    global seq_frame_counter
    print("[Vibration Data Publisher Loop Started]")

    while True:
        sensor_type = device_config.get("SENSORTYPE", "1")
        
        # 0: STOP 상태이면 송신 안 함 (1초 대기)
        if sensor_type == "0":
            time.sleep(1)
            continue

        # 하위 1의 자리 프레임 (Hex 1 ~ F)
        frame_hex = f"{seq_frame_counter:X}" 

        # ====================================================
        # CASE 1: PIEZO 센서 (상위 비트 0 -> seq: 01 ~ 0F)
        # ====================================================
        if sensor_type == "1":
            seq_str = f"0{frame_hex}"
            
            # 🎯 변경된 PIEZO_SAMPLE_COUNT 실시간 적용!
            sample_count = int(device_config.get("PIEZO_SAMPLE_COUNT", "1024"))
            hex_data = generate_hex_sensor_data(sample_count, freq=10, amplitude=600)

            payload = {
                "gwMacAddr": "HALOW",
                "dvicMacAddr": "PIEZO_SENSOR_01",
                "batteryRmin": "5",
                "seq": seq_str,
                "tick": str(int(time.time() * 1000)),
                "sensorData": hex_data
            }
            client.publish(DATA_TOPIC, json.dumps(payload))
            print(f"[PUBLISH PIEZO] seq: {seq_str}, samples: {sample_count}")

        # ====================================================
        # CASE 2: ADXL 3축 센서 (X: 1x, Y: 2x, Z: 3x 낱개 전송)
        # ====================================================
        elif sensor_type == "2":
            # 🎯 변경된 ADXL_SAMPLE_COUNT 실시간 적용!
            sample_count = int(device_config.get("ADXL_SAMPLE_COUNT", "1024"))

            # 1) X축 전송 (상위 비트 1)
            seq_x = f"1{frame_hex}"
            hex_x = generate_hex_sensor_data(sample_count, freq=15, amplitude=300)
            client.publish(DATA_TOPIC, json.dumps({
                "gwMacAddr": "HALOW", "dvicMacAddr": "PIEZO_SENSOR_01",
                "batteryRmin": "5", "seq": seq_x, "tick": str(int(time.time()*1000)),
                "sensorData": hex_x
            }))

            # 2) Y축 전송 (상위 비트 2)
            seq_y = f"2{frame_hex}"
            hex_y = generate_hex_sensor_data(sample_count, freq=30, amplitude=450)
            client.publish(DATA_TOPIC, json.dumps({
                "gwMacAddr": "HALOW", "dvicMacAddr": "PIEZO_SENSOR_01",
                "batteryRmin": "5", "seq": seq_y, "tick": str(int(time.time()*1000)),
                "sensorData": hex_y
            }))

            # 3) Z축 전송 (상위 비트 3)
            seq_z = f"3{frame_hex}"
            hex_z = generate_hex_sensor_data(sample_count, freq=60, amplitude=200)
            client.publish(DATA_TOPIC, json.dumps({
                "gwMacAddr": "HALOW", "dvicMacAddr": "PIEZO_SENSOR_01",
                "batteryRmin": "5", "seq": seq_z, "tick": str(int(time.time()*1000)),
                "sensorData": hex_z
            }))

            print(f"[PUBLISH ADXL 3-AXIS] frame: {frame_hex}, samples: {sample_count}")

        # 프레임 번호 순환 (1 ~ 15 / Hex 1 ~ F)
        seq_frame_counter = 1 if seq_frame_counter >= 15 else seq_frame_counter + 1

        # 🎯 변경된 SENSING_PERIOD (ms) 실시간 적용! (0일 경우 0.01초 연속 송신)
        period_ms = int(device_config.get("SENSING_PERIOD", "1000"))
        time.sleep(0.01 if period_ms == 0 else (period_ms / 1000.0))

def main():
    client = mqtt_client.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id="Vibration_Sensor_Emulator"
    )
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(BROKER, PORT)
    except Exception as e:
        print(f"Failed to connect to Broker: {e}")
        return

    # 송신 루프 스레드 실행
    pub_thread = threading.Thread(target=run_vibration_publisher, args=(client,))
    pub_thread.daemon = True
    pub_thread.start()

    t1 = threading.Thread(target=run_ultrasonic_emulator, args=(client,))
    t1.daemon = True
    t1.start()

    # 수신 루프 (Blocking)
    client.loop_forever()

if __name__ == '__main__':
    main()