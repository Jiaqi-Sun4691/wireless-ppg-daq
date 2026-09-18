#include <WiFi.h>
#include <esp_now.h>
#include <Wire.h>

uint8_t receiverMac[] = {0x44, 0x1D, 0x64, 0xD4, 0xF2, 0x30};

#define WRIST_PPG_PIN 35
#define IMU_ADDR 0x23
#define SDA_PIN 21
#define SCL_PIN 22
#define REG_ACCEL_X_L 0x04
#define REG_GYRO_X_L 0x0A

// Battery voltage must NEVER be connected directly to an ESP32 ADC pin.
#define BATTERY_MONITOR_ENABLED 0
#define BATTERY_ADC_PIN 34
#define BATTERY_DIVIDER_RATIO 2.0f

static const uint8_t PACKET_MAGIC = 0xA7;
static const uint8_t NODE_WRIST = 1;
static const uint8_t FLAG_PPG_OK = 0x01;
static const uint8_t FLAG_IMU_OK = 0x02;
static const uint8_t FLAG_BATTERY_VALID = 0x04;

struct __attribute__((packed)) WristPacketV2 {
  uint8_t magic;
  uint8_t nodeId;
  uint8_t flags;
  uint8_t reserved;
  uint32_t sequence;
  uint16_t batteryMv;
  int32_t ppg;
  int16_t ax, ay, az;
  int16_t gx, gy, gz;
};

static_assert(sizeof(WristPacketV2) == 26, "Unexpected WristPacketV2 padding");
WristPacketV2 packet = {};
uint32_t lastSampleTime = 0;
uint32_t lastBatteryReadMs = 0;
uint16_t cachedBatteryMv = 0;

bool readBytes(uint8_t reg, uint8_t *buffer, uint8_t len) {
  Wire.beginTransmission(IMU_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  int count = Wire.requestFrom((int)IMU_ADDR, (int)len);
  if (count != len) return false;
  for (uint8_t i = 0; i < len; i++) buffer[i] = Wire.read();
  return true;
}

int16_t bytesToInt16(uint8_t lowByte, uint8_t highByte) {
  return (int16_t)((highByte << 8) | lowByte);
}

uint16_t readBatteryMv(uint32_t now) {
#if BATTERY_MONITOR_ENABLED
  if (now - lastBatteryReadMs >= 1000 || lastBatteryReadMs == 0) {
    lastBatteryReadMs = now;
    cachedBatteryMv = (uint16_t)(analogReadMilliVolts(BATTERY_ADC_PIN) * BATTERY_DIVIDER_RATIO);
  }
  return cachedBatteryMv;
#else
  return 0;
#endif
}

void setup() {
  Serial.begin(115200);
  delay(500);
  pinMode(WRIST_PPG_PIN, INPUT);
  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000);
#if BATTERY_MONITOR_ENABLED
  pinMode(BATTERY_ADC_PIN, INPUT);
  analogSetPinAttenuation(BATTERY_ADC_PIN, ADC_11db);
#endif
  WiFi.mode(WIFI_STA);
  if (esp_now_init() != ESP_OK) {
    Serial.println("ESP-NOW init failed");
    return;
  }
  esp_now_peer_info_t peerInfo = {};
  memcpy(peerInfo.peer_addr, receiverMac, 6);
  peerInfo.channel = 0;
  peerInfo.encrypt = false;
  if (esp_now_add_peer(&peerInfo) != ESP_OK) {
    Serial.println("Add peer failed");
    return;
  }
  packet.magic = PACKET_MAGIC;
  packet.nodeId = NODE_WRIST;
  Serial.println("Wrist monitor firmware ready");
}

void loop() {
  uint32_t now = millis();
  if (now - lastSampleTime < 40) return;
  lastSampleTime = now;

  packet.sequence++;
  packet.flags = 0;
  packet.ppg = analogRead(WRIST_PPG_PIN);
  if (packet.ppg > 5 && packet.ppg < 4090) packet.flags |= FLAG_PPG_OK;

  uint8_t accBuf[6];
  uint8_t gyroBuf[6];
  bool accOK = readBytes(REG_ACCEL_X_L, accBuf, 6);
  bool gyroOK = readBytes(REG_GYRO_X_L, gyroBuf, 6);
  if (accOK && gyroOK) {
    packet.ax = bytesToInt16(accBuf[0], accBuf[1]);
    packet.ay = bytesToInt16(accBuf[2], accBuf[3]);
    packet.az = bytesToInt16(accBuf[4], accBuf[5]);
    packet.gx = bytesToInt16(gyroBuf[0], gyroBuf[1]);
    packet.gy = bytesToInt16(gyroBuf[2], gyroBuf[3]);
    packet.gz = bytesToInt16(gyroBuf[4], gyroBuf[5]);
    packet.flags |= FLAG_IMU_OK;
  }

#if BATTERY_MONITOR_ENABLED
  packet.flags |= FLAG_BATTERY_VALID;
#endif
  packet.batteryMv = readBatteryMv(now);

  // Send even after an IMU read failure, so the GUI can show:
  // "ESP32 online / IMU failed" instead of incorrectly reporting it offline.
  esp_now_send(receiverMac, (uint8_t *)&packet, sizeof(packet));
}
