#include <WiFi.h>
#include <esp_now.h>

uint8_t receiverMac[] = {0x44, 0x1D, 0x64, 0xD4, 0xF2, 0x30};

#define OTHER_PPG_PIN 35
#define BATTERY_MONITOR_ENABLED 0
#define BATTERY_ADC_PIN 34
#define BATTERY_DIVIDER_RATIO 2.0f

static const uint8_t PACKET_MAGIC = 0xA7;
static const uint8_t NODE_OTHER = 2;
static const uint8_t FLAG_PPG_OK = 0x01;
static const uint8_t FLAG_BATTERY_VALID = 0x04;

struct __attribute__((packed)) PPGPacketV2 {
  uint8_t magic;
  uint8_t nodeId;
  uint8_t flags;
  uint8_t reserved;
  uint32_t sequence;
  uint16_t batteryMv;
  int32_t ppg;
};

static_assert(sizeof(PPGPacketV2) == 14, "Unexpected PPGPacketV2 padding");
uint32_t sequenceNumber = 0;
uint16_t cachedBatteryMv = 0;
uint32_t lastBatteryReadMs = 0;

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
  pinMode(OTHER_PPG_PIN, INPUT);
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
  Serial.println("Other monitor firmware ready");
}

void loop() {
  uint32_t now = millis();
  int ppg = analogRead(OTHER_PPG_PIN);
  PPGPacketV2 packet = {};
  packet.magic = PACKET_MAGIC;
  packet.nodeId = NODE_OTHER;
  packet.sequence = ++sequenceNumber;
  packet.ppg = ppg;
  if (ppg > 5 && ppg < 4090) packet.flags |= FLAG_PPG_OK;
#if BATTERY_MONITOR_ENABLED
  packet.flags |= FLAG_BATTERY_VALID;
#endif
  packet.batteryMv = readBatteryMv(now);
  esp_now_send(receiverMac, (uint8_t *)&packet, sizeof(packet));
  delay(23);
}
