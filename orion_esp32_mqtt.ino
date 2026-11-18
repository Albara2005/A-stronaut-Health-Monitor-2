/*
 * ORION-IV / ESP32 -> MQTT telemetry publisher (Arduino framework)
 * Libraries (Library Manager): PubSubClient, ArduinoJson (v7)
 *
 * Publishes JSON to   orion/<DEVICE_ID>/telemetry   every PUBLISH_MS
 * Listens (optional)  orion/<DEVICE_ID>/decision    (retained summary from the backend)
 *
 * NOTE: not compiled/tested in the build environment. The two read_* functions return
 * placeholder values until you wire your actual sensors (MAX30102 library, temperature sensor).
 */
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <time.h>

// ---- configuration (edit) -----------------------------------------------------
const char* WIFI_SSID   = "YOUR_WIFI";
const char* WIFI_PASS   = "YOUR_PASSWORD";
const char* MQTT_HOST   = "192.168.1.10";   // PC running the broker (not "localhost")
const uint16_t MQTT_PORT = 1883;
const char* MQTT_USER   = "";               // leave empty if the broker allows anonymous
const char* MQTT_PASS   = "";
const char* DEVICE_ID   = "esp32-01";       // only [A-Za-z0-9_.-]
const unsigned long PUBLISH_MS = 2000;
// -------------------------------------------------------------------------------

WiFiClient net;
PubSubClient mqtt(net);
char topicTelemetry[96], topicDecision[96], topicStatus[96];
unsigned long lastPublish = 0;

// TODO: replace with real MAX30102 readings (heart rate / SpO2)
float read_heart_rate_bpm() { return 78.0; }
float read_spo2_percent()   { return 98.0; }
// TODO: replace with your temperature sensor
float read_skin_temp_c()    { return 36.6; }

bool iso_time(char* out, size_t n) {
  time_t now = time(nullptr);
  if (now < 1700000000) return false;            // NTP not synced yet
  struct tm t; gmtime_r(&now, &t);
  strftime(out, n, "%Y-%m-%dT%H:%M:%SZ", &t);
  return true;
}

void on_message(char* topic, byte* payload, unsigned int len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;
  Serial.printf("[decision] %s\n", (const char*)(doc["overall_status"] | "?"));
  // e.g. light a LED when overall_status is CRITICAL / HIGH
}

void connect_wifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print('.'); }
  Serial.printf("\nWiFi OK, IP %s\n", WiFi.localIP().toString().c_str());
  configTime(0, 0, "pool.ntp.org", "time.google.com");
}

void connect_mqtt() {
  while (!mqtt.connected()) {
    // Last Will: broker publishes "offline" if the ESP32 drops without a clean disconnect.
    bool ok = mqtt.connect(DEVICE_ID, MQTT_USER[0] ? MQTT_USER : nullptr,
                           MQTT_USER[0] ? MQTT_PASS : nullptr,
                           topicStatus, 1, true, "offline");
    if (ok) {
      mqtt.publish(topicStatus, "online", true);
      mqtt.subscribe(topicDecision, 1);
    } else {
      Serial.printf("MQTT failed rc=%d, retry in 3s\n", mqtt.state());
      delay(3000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  snprintf(topicTelemetry, sizeof topicTelemetry, "orion/%s/telemetry", DEVICE_ID);
  snprintf(topicDecision,  sizeof topicDecision,  "orion/%s/decision",  DEVICE_ID);
  snprintf(topicStatus,    sizeof topicStatus,    "orion/%s/status",    DEVICE_ID);
  connect_wifi();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(1024);
  mqtt.setCallback(on_message);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) connect_wifi();
  if (!mqtt.connected()) connect_mqtt();
  mqtt.loop();

  if (millis() - lastPublish >= PUBLISH_MS) {
    lastPublish = millis();
    JsonDocument doc;
    char ts[32];
    if (iso_time(ts, sizeof ts)) doc["timestamp"] = ts;   // omitted -> backend uses receive time
    doc["heart_rate_bpm"] = read_heart_rate_bpm();
    doc["spo2_percent"]   = read_spo2_percent();
    doc["skin_temp_c"]    = read_skin_temp_c();
    doc["data_valid"]     = true;

    char buf[512];
    size_t n = serializeJson(doc, buf, sizeof buf);
    mqtt.publish(topicTelemetry, (const uint8_t*)buf, n, false);
  }
}
