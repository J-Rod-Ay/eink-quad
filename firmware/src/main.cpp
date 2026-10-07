// Quad board (forked from the note box firmware): long-poll the quad server
// and play whatever the server sends. The server renders every pixel, including animation, as a "reel" of
// changed rectangles; this firmware only copies bytes and picks refresh modes.
//
// Two ways of living, chosen by the server per request (X-Mode):
//   live  -- on USB: WiFi stays up, long-polls, buttons answer instantly,
//            Inky gets idle animations.
//   sleep -- on battery: deep sleep between checks, any button wakes it.

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <driver/rtc_io.h>

#include "board.h"
#include "display.h"
#include "secrets.h"
#include "sensors.h"
#include "wifisetup.h"

RTC_DATA_ATTR static uint32_t rtcVersion = 0;   // what the glass shows, per the server
RTC_DATA_ATTR static uint32_t bootCount = 0;
RTC_DATA_ATTR static uint8_t failures = 0;

static const uint32_t POLL_WAIT_S = 25;
static const uint32_t WIFI_TIMEOUT_MS = 20000;
static const uint32_t PORTAL_IDLE_MS = 10UL * 60UL * 1000UL;
static const size_t MAX_REEL = 3 * 1024 * 1024;

static bool bufferValid = false;   // does display::buffer() match the glass?
static uint32_t partialsSinceFull = 0;
static uint32_t lastPartialMs = 0;
static uint32_t cleanAfterS = 90;
static bool unread = false;
static bool liveMode = true;
static uint32_t nextSleepS = 300;

static uint8_t *reel = nullptr;

// ---------------------------------------------------------------- buttons

static volatile int8_t pendingBtn = -1;
static volatile uint32_t lastBtnMs = 0;

static void IRAM_ATTR onKey(int which) {
  uint32_t now = millis();
  if (now - lastBtnMs < 250) return;
  lastBtnMs = now;
  if (pendingBtn < 0) pendingBtn = which;
}
static void IRAM_ATTR isr0() { onKey(0); }
static void IRAM_ATTR isr1() { onKey(1); }
static void IRAM_ATTR isr2() { onKey(2); }

static int8_t takeBtn() {
  int8_t b = pendingBtn;
  pendingBtn = -1;
  return b;
}

static bool keyHeld(int pin, uint32_t ms) {
  uint32_t t0 = millis();
  while (millis() - t0 < ms) {
    if (digitalRead(pin) == HIGH) return false;
    delay(10);
  }
  return true;
}

// ---------------------------------------------------------------- led / buzzer

static void led(bool on) { digitalWrite(LED_PIN, on ? LOW : HIGH); }

static void chime() {
  // a little "ding-da-ding"
  const uint16_t notes[][2] = {{1319, 90}, {0, 30}, {1568, 90}, {0, 30}, {2093, 160}};
  for (auto &n : notes) {
    if (n[0]) tone(BUZZER_PIN, n[0], n[1]);   // tone() with a duration blocks until done
    else delay(n[1]);
  }
}

static void blip() {
  tone(BUZZER_PIN, 2400, 18);
}

// ---------------------------------------------------------------- screens

static void ownScreen(const char *title, const display::MsgLine *lines, int n,
                      const char *footer) {
  rtcVersion = 0;   // anything we draw ourselves is not a server version
  bufferValid = true;
  display::drawMessage(title, lines, n, footer);
}

static void sleepNow(uint32_t seconds) {
  Serial.printf("[sleep] %u s\n", seconds);
  Serial.flush();
  display::sleep();
  led(false);
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  uint64_t mask = (1ULL << KEY0_PIN) | (1ULL << KEY1_PIN) | (1ULL << KEY2_PIN);
  for (int p : {KEY0_PIN, KEY1_PIN, KEY2_PIN}) {
    rtc_gpio_pullup_en((gpio_num_t)p);
    rtc_gpio_pulldown_dis((gpio_num_t)p);
  }
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH, ESP_PD_OPTION_ON);
  esp_sleep_enable_ext1_wakeup(mask, ESP_EXT1_WAKEUP_ANY_LOW);
  esp_sleep_enable_timer_wakeup((uint64_t)seconds * 1000000ULL);
  esp_deep_sleep_start();
}

static void enterPortal(const char *joinLine) {
  const display::MsgLine lines[] = {
      {joinLine, false},
      {wifisetup::apName(), true},
      {"2. A setup page should open. If not, browse to:", false},
      {"http://192.168.4.1", true},
      {"3. Pick your Wi-Fi and type its password.", false},
  };
  char footer[80];
  snprintf(footer, sizeof(footer), "The dashboard appears when it works.    %s", FW_BUILD);
  ownScreen("Set up the quad board", lines, 5, footer);
  wifisetup::runPortal(PORTAL_IDLE_MS);
  const display::MsgLine l2[] = {{"Nobody finished setup. Press any button to retry.", false}};
  ownScreen("Setup paused", l2, 1, nullptr);
  sleepNow(3600);
}

// ---------------------------------------------------------------- reel player

static inline uint16_t rd16(const uint8_t *p) { return p[0] | (p[1] << 8); }

// Returns false if the reel is malformed.
static bool playReel(const uint8_t *r, size_t len, bool baseIsGlass) {
  if (len < 7 || memcmp(r, "REEL", 4) != 0 || r[4] != 1) return false;
  uint16_t n = rd16(r + 5);
  size_t off = 7;
  uint8_t *buf = display::buffer();
  for (uint16_t i = 0; i < n; i++) {
    if (off + 11 > len) return false;
    uint16_t x0 = rd16(r + off), y0 = rd16(r + off + 2), x1 = rd16(r + off + 4),
             y1 = rd16(r + off + 6), dly = rd16(r + off + 8);
    uint8_t flags = r[off + 10];
    off += 11;
    if (x0 % 8 || x1 % 8 || x1 > display::W || y1 > display::H || x1 <= x0 || y1 <= y0)
      return false;
    size_t rowBytes = (x1 - x0) / 8;
    size_t need = rowBytes * (y1 - y0);
    // flags bit1 = grey: a full-screen frame whose 1-bit rows are followed by
    // the same image at 2bpp for a 4-level full refresh.
    bool gray = (flags & 2) && x0 == 0 && y0 == 0 && x1 == display::W && y1 == display::H;
    size_t extra = gray ? display::GRAY_BYTES : 0;
    if (off + need + extra > len) return false;

    bool same = bufferValid;
    for (uint16_t y = y0; y < y1; y++) {
      uint8_t *dst = buf + (size_t)y * display::STRIDE + x0 / 8;
      const uint8_t *src = r + off + (size_t)(y - y0) * rowBytes;
      if (same && memcmp(dst, src, rowBytes) != 0) same = false;
      memcpy(dst, src, rowBytes);
    }
    off += need;

    if (i == 0 && !bufferValid && baseIsGlass && !(flags & 1)) {
      // Woke from deep sleep: RAM is gone but the glass still shows frame 0.
      bufferValid = true;
      continue;
    }
    if (gray) {
      uint32_t t0 = millis();
      display::fullGray(r + off);
      off += extra;
      partialsSinceFull = 0;
      Serial.printf("[reel] %u/%u grey full %lums\n", i + 1, n, millis() - t0);
    } else if (!same || (flags & 1)) {
      uint32_t t0 = millis();
      if (flags & 1) {
        display::full();
        partialsSinceFull = 0;
      } else {
        display::partial(x0, y0, x1, y1);
        partialsSinceFull++;
        lastPartialMs = millis();
      }
      Serial.printf("[reel] %u/%u %ux%u %s %lums\n", i + 1, n, x1 - x0, y1 - y0,
                    (flags & 1) ? "full" : "part", millis() - t0);
    }
    bufferValid = true;
    if (dly) delay(dly);
  }
  return true;
}

// ---------------------------------------------------------------- HTTP

static WiFiClientSecure tls;

struct Resp {
  int code = 0;
  size_t len = 0;
  uint32_t version = 0;
  bool chime = false, unread = false, base = false;
  String mode;
  uint32_t next = 0, clean = 90;
  bool aborted = false;
};

static bool readLine(String &out, uint32_t deadline) {
  out = "";
  while (millis() < deadline) {
    while (tls.available()) {
      char c = tls.read();
      if (c == '\n') return true;
      if (c != '\r') out += c;
    }
    if (!tls.connected() && !tls.available()) return false;
    delay(2);
  }
  return false;
}

// One poll. While waiting for the first byte, a button press aborts it so the
// caller can re-ask with the button attached -- the long-poll must never make
// a press feel ignored.
static Resp poll(uint32_t waitS, int8_t btn, bool boot, bool abortable) {
  Resp r;
  if (!tls.connected()) {
    tls.stop();
    tls.setInsecure();   // hostname is pinned in firmware; this is a toy, not a bank
    tls.setHandshakeTimeout(15);
    uint32_t t0 = millis();
    if (!tls.connect(NOTE_HOST, NOTE_PORT)) {
      Serial.println("[http] connect failed");
      return r;
    }
    Serial.printf("[http] connected in %lums\n", millis() - t0);
  }

  float t = 0, rh = 0;
  bool haveT = sensors::readSHT4x(&t, &rh);
  float vbat = sensors::readVBat();
  char path[256];
  int n = snprintf(path, sizeof(path),
                   "%s?k=%s&v=%u&wait=%u&vbat=%.3f&rssi=%d&g=1%s",
                   NOTE_PATH, NOTE_TOKEN, rtcVersion, waitS, vbat, WiFi.RSSI(), boot ? "&boot=1" : "");
  if (haveT) n += snprintf(path + n, sizeof(path) - n, "&t=%.1f&rh=%.0f", t, rh);
  if (btn >= 0) n += snprintf(path + n, sizeof(path) - n, "&btn=%d", btn);
  if (!bufferValid) n += snprintf(path + n, sizeof(path) - n, "&fresh=1");

  tls.printf("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: quadboard\r\nConnection: keep-alive\r\n\r\n",
             path, NOTE_HOST);

  // wait for the first byte (this is where the long-poll sits)
  uint32_t deadline = millis() + (waitS + 20) * 1000;
  uint32_t blinkAt = millis();
  bool ledOn = false;
  while (!tls.available()) {
    if (!tls.connected() || millis() > deadline) {
      Serial.println("[http] dropped while waiting");
      tls.stop();
      return r;
    }
    if (abortable && pendingBtn >= 0) {
      tls.stop();
      r.aborted = true;
      led(false);
      return r;
    }
    if (unread && millis() - blinkAt > 700) {
      blinkAt = millis();
      ledOn = !ledOn;
      led(ledOn);
    }
    delay(15);
  }
  led(false);

  deadline = millis() + 20000;
  String line;
  if (!readLine(line, deadline)) { tls.stop(); return r; }
  // "HTTP/1.1 200 OK"
  int sp = line.indexOf(' ');
  r.code = sp > 0 ? line.substring(sp + 1, sp + 4).toInt() : 0;
  bool closeAfter = false;
  while (readLine(line, deadline) && line.length()) {
    int c = line.indexOf(':');
    if (c < 0) continue;
    String k = line.substring(0, c);
    String v = line.substring(c + 1);
    v.trim();
    k.toLowerCase();
    if (k == "content-length") r.len = v.toInt();
    else if (k == "x-version") r.version = v.toInt();
    else if (k == "x-chime") r.chime = v.toInt();
    else if (k == "x-unread") r.unread = v.toInt();
    else if (k == "x-mode") r.mode = v;
    else if (k == "x-next") r.next = v.toInt();
    else if (k == "x-clean") r.clean = v.toInt();
    else if (k == "x-base") r.base = v.toInt();
    else if (k == "connection" && v.equalsIgnoreCase("close")) closeAfter = true;
  }

  if (r.len) {
    if (r.len > MAX_REEL) { tls.stop(); r.code = 0; return r; }
    size_t got = 0;
    while (got < r.len && millis() < deadline) {
      int a = tls.available();
      if (a > 0) {
        got += tls.read(reel + got, min((size_t)a, r.len - got));
        deadline = millis() + 20000;
      } else if (!tls.connected()) {
        break;
      } else {
        delay(2);
      }
    }
    if (got != r.len) {
      Serial.printf("[http] short body %u/%u\n", (unsigned)got, (unsigned)r.len);
      tls.stop();
      r.code = 0;
      return r;
    }
  }
  if (closeAfter) tls.stop();
  return r;
}

// ---------------------------------------------------------------- main

static void handle(const Resp &r) {
  if (r.mode.length()) liveMode = (r.mode == "live");
  if (r.next) nextSleepS = r.next;
  cleanAfterS = r.clean;
  unread = r.unread;
  if (r.code == 200 && r.len) {
    if (r.chime) chime();
    uint32_t t0 = millis();
    if (!playReel(reel, r.len, r.base)) Serial.println("[reel] malformed");
    rtcVersion = r.version;
    Serial.printf("[reel] v%u done in %lums (%u partials since full)\n", rtcVersion,
                  millis() - t0, partialsSinceFull);
  } else if (r.code == 204) {
    rtcVersion = r.version ? r.version : rtcVersion;
  }
}

void setup() {
  bootCount++;
  Serial.begin(115200);
  delay(bootCount == 1 ? 800 : 50);
  Serial.printf("\n=== quad board boot #%u (%s) ===\n", bootCount, FW_BUILD);

  pinMode(LED_PIN, OUTPUT);
  led(true);
  for (int p : {KEY0_PIN, KEY1_PIN, KEY2_PIN}) {
    rtc_gpio_deinit((gpio_num_t)p);
    pinMode(p, INPUT_PULLUP);
  }

  // which button woke us (deep sleep), if any
  if (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_EXT1) {
    uint64_t st = esp_sleep_get_ext1_wakeup_status();
    if (st & (1ULL << KEY0_PIN)) pendingBtn = 0;
    else if (st & (1ULL << KEY1_PIN)) pendingBtn = 1;
    else if (st & (1ULL << KEY2_PIN)) pendingBtn = 2;
    Serial.printf("[wake] button %d\n", pendingBtn);
  }

  display::begin();
  reel = (uint8_t *)ps_malloc(MAX_REEL);

  // hold right + tap left => forget wifi
  if (digitalRead(KEY2_PIN) == LOW && keyHeld(KEY2_PIN, 600)) {
    Serial.println("[key2] held -- setup");
    wifisetup::forget();
    enterPortal("1. On your phone, join this Wi-Fi network:");
  }

  if (!wifisetup::load(nullptr, nullptr)) {
#ifdef DEV_WIFI_SSID
    if (DEV_WIFI_SSID[0]) {
      Serial.println("[wifi] seeding dev network");
      wifisetup::save(DEV_WIFI_SSID, DEV_WIFI_PASS);
    } else
#endif
    enterPortal("1. On your phone, join this Wi-Fi network:");
  }

  if (!wifisetup::connect(WIFI_TIMEOUT_MS)) {
    if (wifisetup::isFresh()) {
      const display::MsgLine l[] = {{"Wrong password, or a 5GHz-only network.", false},
                                    {"Starting setup again...", false}};
      ownScreen("Couldn't join Wi-Fi", l, 2, nullptr);
      wifisetup::forget();
      enterPortal("On your phone, join this Wi-Fi network again:");
    }
    failures++;
    if (failures == 6) enterPortal("1. On your phone, join this Wi-Fi network:");
    sleepNow(min(60u << min<uint8_t>(failures, 5), 3600u));
  }
  if (wifisetup::isFresh()) wifisetup::clearFresh();
  failures = 0;

  attachInterrupt(KEY0_PIN, isr0, FALLING);
  attachInterrupt(KEY1_PIN, isr1, FALLING);
  attachInterrupt(KEY2_PIN, isr2, FALLING);
  led(false);

  bool boot = esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_UNDEFINED;
  if (boot) rtcVersion = 0;
  int8_t b = takeBtn();
  if (b >= 0) blip();
  Resp r = poll(0, b, boot, false);
  handle(r);
  if (r.code == 0) {
    Serial.println("[http] first poll failed");
    if (!liveMode) sleepNow(120);
  }
}

void loop() {
  if (!liveMode) {
    // battery: one more quick look for queued work, then sleep
    sleepNow(nextSleepS);
  }

  int8_t b = takeBtn();
  if (b >= 0) blip();
  Resp r = poll(b >= 0 ? 0 : POLL_WAIT_S, b, false, b < 0);
  if (r.aborted) return;   // a button arrived mid-wait; loop re-polls with it
  if (r.code == 0) {
    failures++;
    delay(min(2000u * failures, 30000u));
    if (WiFi.status() != WL_CONNECTED) wifisetup::connect(WIFI_TIMEOUT_MS);
    return;
  }
  failures = 0;
  handle(r);

  // After an animation, e-paper partials leave faint ghosts. Once things have
  // been still for a while, one full refresh wipes them.
  if (cleanAfterS && partialsSinceFull >= 10 && pendingBtn < 0 &&
      millis() - lastPartialMs > cleanAfterS * 1000UL) {
    Serial.println("[epd] ghost-clean full refresh");
    display::full();
    partialsSinceFull = 0;
  }
}
