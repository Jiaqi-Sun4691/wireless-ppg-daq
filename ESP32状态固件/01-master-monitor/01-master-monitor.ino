#include <WiFi.h>
#include <esp_now.h>

// GUI enhanced protocol. The normal data line remains the original 16 columns.
static const uint8_t PACKET_MAGIC = 0xA7;
static const uint8_t NODE_FINGER = 0;
static const uint8_t NODE_WRIST = 1;
static const uint8_t NODE_OTHER = 2;
static const uint8_t NODE_WHEEL = 3;

static const uint8_t FLAG_PPG_OK = 0x01;
static const uint8_t FLAG_IMU_OK = 0x02;
static const uint8_t FLAG_BATTERY_VALID = 0x04;

// ===== Legacy packet structures (kept for gradual firmware upgrades) =====
struct LegacyPPGData {
  uint8_t channel;
  int value;
};

struct LegacyIMUData {
  int16_t ax, ay, az;
  int16_t gx, gy, gz;
};

struct LegacyWristData {
  uint8_t channel;
  int ppg;
  int16_t ax, ay, az;
  int16_t gx, gy, gz;
};

// ===== Enhanced packets =====
struct __attribute__((packed)) PPGPacketV2 {
  uint8_t magic;
  uint8_t nodeId;
  uint8_t flags;
  uint8_t reserved;
  uint32_t sequence;
  uint16_t batteryMv;
  int32_t ppg;
};

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

struct __attribute__((packed)) WheelPacketV2 {
  uint8_t magic;
  uint8_t nodeId;
  uint8_t flags;
  uint8_t reserved;
  uint32_t sequence;
  uint16_t batteryMv;
  int16_t ax, ay, az;
  int16_t gx, gy, gz;
};

static_assert(sizeof(PPGPacketV2) == 14, "Unexpected PPGPacketV2 padding");
static_assert(sizeof(WristPacketV2) == 26, "Unexpected WristPacketV2 padding");
static_assert(sizeof(WheelPacketV2) == 22, "Unexpected WheelPacketV2 padding");

struct NodeState {
  bool seen;
  uint32_t lastSeenMs;
  uint32_t sequence;
  int16_t batteryMv;
  int16_t flags;
};

NodeState nodeStates[4] = {
  {false, 0, 0, -1, -1},
  {false, 0, 0, -1, -1},
  {false, 0, 0, -1, -1},
  {false, 0, 0, -1, -1}
};

int fingerValue = -1;
int wristValue = -1;
int otherValue = -1;

int16_t wrist_ax = 0, wrist_ay = 0, wrist_az = 0;
int16_t wrist_gx = 0, wrist_gy = 0, wrist_gz = 0;
int16_t wheel_ax = 0, wheel_ay = 0, wheel_az = 0;
int16_t wheel_gx = 0, wheel_gy = 0, wheel_gz = 0;

uint32_t lastSampleTime = 0;
uint32_t lastStatusTime = 0;
uint32_t lastMacTime = 0;
const uint32_t SAMPLE_INTERVAL_MS = 48;
const uint32_t STATUS_INTERVAL_MS = 500;
const uint32_t MAC_INTERVAL_MS = 2000;

bool ppgPlausible(int value) {
  return value > 5 && value < 4090;
}

void updateNode(uint8_t nodeId, uint8_t flags, int batteryMv, uint32_t sequence) {
  if (nodeId > NODE_WHEEL) return;
  nodeStates[nodeId].seen = true;
  nodeStates[nodeId].lastSeenMs = millis();
  nodeStates[nodeId].flags = flags;
  nodeStates[nodeId].batteryMv = batteryMv;
  nodeStates[nodeId].sequence = sequence;
}

void onDataRecv(const esp_now_recv_info *info, const uint8_t *data, int len) {
  // Enhanced Finger / Other PPG packet.
  if (len == sizeof(PPGPacketV2)) {
    PPGPacketV2 incoming;
    memcpy(&incoming, data, sizeof(incoming));
    if (incoming.magic != PACKET_MAGIC) return;
    if (incoming.nodeId == NODE_FINGER) fingerValue = incoming.ppg;
    else if (incoming.nodeId == NODE_OTHER) otherValue = incoming.ppg;
    else return;
    updateNode(incoming.nodeId, incoming.flags, incoming.batteryMv, incoming.sequence);
    return;
  }

  // Enhanced Wrist PPG + IMU packet.
  if (len == sizeof(WristPacketV2)) {
    WristPacketV2 incoming;
    memcpy(&incoming, data, sizeof(incoming));
    if (incoming.magic != PACKET_MAGIC || incoming.nodeId != NODE_WRIST) return;
    wristValue = incoming.ppg;
    wrist_ax = incoming.ax; wrist_ay = incoming.ay; wrist_az = incoming.az;
    wrist_gx = incoming.gx; wrist_gy = incoming.gy; wrist_gz = incoming.gz;
    updateNode(NODE_WRIST, incoming.flags, incoming.batteryMv, incoming.sequence);
    return;
  }

  // Enhanced Wheel / Steering IMU packet.
  if (len == sizeof(WheelPacketV2)) {
    WheelPacketV2 incoming;
    memcpy(&incoming, data, sizeof(incoming));
    if (incoming.magic != PACKET_MAGIC || incoming.nodeId != NODE_WHEEL) return;
    wheel_ax = incoming.ax; wheel_ay = incoming.ay; wheel_az = incoming.az;
    wheel_gx = incoming.gx; wheel_gy = incoming.gy; wheel_gz = incoming.gz;
    updateNode(NODE_WHEEL, incoming.flags, incoming.batteryMv, incoming.sequence);
    return;
  }

  // Legacy packets allow the master firmware to be flashed first.
  if (len == sizeof(LegacyPPGData)) {
    LegacyPPGData incoming;
    memcpy(&incoming, data, sizeof(incoming));
    if (incoming.channel == NODE_FINGER) {
      fingerValue = incoming.value;
      updateNode(NODE_FINGER, ppgPlausible(incoming.value) ? FLAG_PPG_OK : 0, -1, nodeStates[NODE_FINGER].sequence + 1);
    } else if (incoming.channel == NODE_OTHER) {
      otherValue = incoming.value;
      updateNode(NODE_OTHER, ppgPlausible(incoming.value) ? FLAG_PPG_OK : 0, -1, nodeStates[NODE_OTHER].sequence + 1);
    }
    return;
  }

  if (len == sizeof(LegacyIMUData)) {
    LegacyIMUData incoming;
    memcpy(&incoming, data, sizeof(incoming));
    wheel_ax = incoming.ax; wheel_ay = incoming.ay; wheel_az = incoming.az;
    wheel_gx = incoming.gx; wheel_gy = incoming.gy; wheel_gz = incoming.gz;
    updateNode(NODE_WHEEL, FLAG_IMU_OK, -1, nodeStates[NODE_WHEEL].sequence + 1);
    return;
  }

  if (len == sizeof(LegacyWristData)) {
    LegacyWristData incoming;
    memcpy(&incoming, data, sizeof(incoming));
    wristValue = incoming.ppg;
    wrist_ax = incoming.ax; wrist_ay = incoming.ay; wrist_az = incoming.az;
    wrist_gx = incoming.gx; wrist_gy = incoming.gy; wrist_gz = incoming.gz;
    uint8_t flags = FLAG_IMU_OK;
    if (ppgPlausible(incoming.ppg)) flags |= FLAG_PPG_OK;
    updateNode(NODE_WRIST, flags, -1, nodeStates[NODE_WRIST].sequence + 1);
  }
}

uint32_t packetAge(uint8_t nodeId, uint32_t now) {
  if (!nodeStates[nodeId].seen) return UINT32_MAX;
  return now - nodeStates[nodeId].lastSeenMs;
}

void printStatus(uint32_t now) {
  // @STATUS,time,4 ages,4 flags,4 battery mV,4 sequence numbers
  Serial.printf(
    "@STATUS,%lu,%lu,%lu,%lu,%lu,%d,%d,%d,%d,%d,%d,%d,%d,%lu,%lu,%lu,%lu\n",
    (unsigned long)now,
    (unsigned long)packetAge(NODE_FINGER, now),
    (unsigned long)packetAge(NODE_WRIST, now),
    (unsigned long)packetAge(NODE_OTHER, now),
    (unsigned long)packetAge(NODE_WHEEL, now),
    nodeStates[NODE_FINGER].flags,
    nodeStates[NODE_WRIST].flags,
    nodeStates[NODE_OTHER].flags,
    nodeStates[NODE_WHEEL].flags,
    nodeStates[NODE_FINGER].batteryMv,
    nodeStates[NODE_WRIST].batteryMv,
    nodeStates[NODE_OTHER].batteryMv,
    nodeStates[NODE_WHEEL].batteryMv,
    (unsigned long)nodeStates[NODE_FINGER].sequence,
    (unsigned long)nodeStates[NODE_WRIST].sequence,
    (unsigned long)nodeStates[NODE_OTHER].sequence,
    (unsigned long)nodeStates[NODE_WHEEL].sequence
  );
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  WiFi.mode(WIFI_STA);

  // The desktop flashing wizard reads this line and automatically injects
  // the Master STA MAC into every Slave firmware image.
  Serial.print("@MASTER_MAC,");
  Serial.println(WiFi.macAddress());

  if (esp_now_init() != ESP_OK) {
    Serial.println("ESP-NOW init failed");
    return;
  }
  esp_now_register_recv_cb(onDataRecv);
  Serial.println("Master monitor firmware ready");
  Serial.println("timestamp,finger,wrist,other,wrist_ax,wrist_ay,wrist_az,wrist_gx,wrist_gy,wrist_gz,wheel_ax,wheel_ay,wheel_az,wheel_gx,wheel_gy,wheel_gz");
}

void loop() {
  uint32_t now = millis();
  if (now - lastSampleTime >= SAMPLE_INTERVAL_MS) {
    lastSampleTime = now;
    Serial.printf(
      "%lu,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d\n",
      (unsigned long)now,
      fingerValue, wristValue, otherValue,
      wrist_ax, wrist_ay, wrist_az,
      wrist_gx, wrist_gy, wrist_gz,
      wheel_ax, wheel_ay, wheel_az,
      wheel_gx, wheel_gy, wheel_gz
    );
  }
  if (now - lastStatusTime >= STATUS_INTERVAL_MS) {
    lastStatusTime = now;
    printStatus(now);
  }
  if (now - lastMacTime >= MAC_INTERVAL_MS) {
    lastMacTime = now;
    Serial.print("@MASTER_MAC,");
    Serial.println(WiFi.macAddress());
  }
}
