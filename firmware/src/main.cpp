// Quad board (forked from the note box firmware): long-poll the quad server
// and play whatever the server sends. The server renders every pixel, including animation, as a "reel" of
// changed rectangles; this firmware only copies bytes and picks refresh modes.
//
// Two ways of living, chosen by the server per request (X-Mode):
//   live  -- on USB: WiFi stays up, long-polls, buttons answer instantly,
//            Inky gets idle animations.
//   sleep -- on battery: deep sleep between checks, any button wakes it.
//
// Finding and trusting the server: it is looked up by its Bonjour name
// (SERVER_MDNS.local, falling back to SERVER_IP), and the TLS certificate it
// presents must have exactly the SHA-256 in SERVER_FP before the board's key is
// sent. All of these come from server/setup-mac.sh.
//
// Telling you what's wrong: e-paper keeps its last image with the power off, so
// a stuck board looks like a working one. On failure the board blinks its LED
// (2 = Wi-Fi, 3 = server unreachable, 4 = certificate or key), puts a small
// label in the bottom-right corner after a minute, and after a few minutes
// draws a full screen saying what failed and what to try. Press the left and
// right buttons together for a diagnostics page.

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <ESPmDNS.h>
#include <driver/rtc_io.h>

#include "board.h"
#include "display.h"
#include "secrets.h"
#include "sensors.h"
#include "wifisetup.h"

RTC_DATA_ATTR static uint32_t rtcVersion = 0;   // what the glass shows, per the server
RTC_DATA_ATTR static uint32_t bootCount = 0;
RTC_DATA_ATTR static uint8_t failures = 0;

// What went wrong on the last attempt, and what the glass currently says about it.
enum NetErr : uint8_t { NET_OK, NET_NO_WIFI, NET_NO_NAME, NET_NO_CONNECT, NET_BAD_CERT, NET_REJECTED, NET_HTTP };
RTC_DATA_ATTR static uint8_t lastErr = NET_OK;
RTC_DATA_ATTR static uint8_t badgeErr = NET_OK;    // a corner label is showing for this
RTC_DATA_ATTR static uint8_t screenErr = NET_OK;   // a full error screen is showing for this
RTC_DATA_ATTR static uint8_t failedWakes = 0;      // battery mode: consecutive wakes that failed
RTC_DATA_ATTR static uint32_t cachedIp = 0;        // last address the server answered on
static int lastHttp = 0;
static uint32_t failSinceMs = 0;                   // live mode: when the current failure began
static uint32_t lastOkMs = 0;
static IPAddress usedIp;
static IPAddress serverIp();

static const uint32_t POLL_WAIT_S = 25;
static const uint32_t WIFI_TIMEOUT_MS = 20000;
static const uint32_t PORTAL_IDLE_MS = 10UL * 60UL * 1000UL;
static const size_t MAX_REEL = 3 * 1024 * 1024;

static bool bufferValid = false;   // does display::buffer() match the glass?
static uint32_t partialsSinceFull = 0;
static uint32_t lastPartialMs = 0;
static uint32_t cleanAfterS = 90;
static bool unread = false;
// Kept across deep sleep: a wake that can't reach the server must still know it's on
// battery, count as a failed wake and go back to sleep, not stay awake retrying.
RTC_DATA_ATTR static bool liveMode = true;
RTC_DATA_ATTR static uint32_t nextSleepS = 300;

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
  if (WiFi.status() != WL_CONNECTED) {
    lastErr = NET_NO_WIFI;
    return r;
  }
  if (!tls.connected()) {
    tls.stop();
    // No CA chain to check against: instead the exact certificate is pinned,
    // checked below before anything (the key above all) is sent.
    tls.setInsecure();
    tls.setHandshakeTimeout(15);
    IPAddress ip = serverIp();
    if (ip == IPAddress()) {
      Serial.printf("[http] can't find %s.local\n", SERVER_MDNS);
      lastErr = NET_NO_NAME;
      return r;
    }
    usedIp = ip;
    uint32_t t0 = millis();
    if (!tls.connect(ip, SERVER_PORT)) {
      Serial.printf("[http] connect %s:%d failed\n", ip.toString().c_str(), SERVER_PORT);
      cachedIp = 0;   // look the name up again next time
      lastErr = NET_NO_CONNECT;
      return r;
    }
    if (!tls.verify(SERVER_FP, nullptr)) {
      Serial.println("[http] certificate does not match SERVER_FP -- not sending the key");
      tls.stop();
      lastErr = NET_BAD_CERT;
      return r;
    }
    cachedIp = (uint32_t)ip;
    Serial.printf("[http] connected to %s in %lums\n", ip.toString().c_str(), millis() - t0);
  }

  float t = 0, rh = 0;
  bool haveT = sensors::readSHT4x(&t, &rh);
  float vbat = sensors::readVBat();
  char path[256];
  int n = snprintf(path, sizeof(path),
                   "/api/device/poll?v=%u&wait=%u&vbat=%.3f&rssi=%d&g=1%s",
                   rtcVersion, waitS, vbat, WiFi.RSSI(), boot ? "&boot=1" : "");
  if (haveT) n += snprintf(path + n, sizeof(path) - n, "&t=%.1f&rh=%.0f", t, rh);
  bool usb;
  uint8_t chg;
  if (sensors::readCharger(&usb, &chg))
    n += snprintf(path + n, sizeof(path) - n, "&usb=%d&chg=%u", usb ? 1 : 0, (unsigned)chg);
  if (btn >= 0) n += snprintf(path + n, sizeof(path) - n, "&btn=%d", btn);
  if (!bufferValid) n += snprintf(path + n, sizeof(path) - n, "&fresh=1");

  tls.printf("GET %s HTTP/1.1\r\nHost: %s.local\r\nAuthorization: Bearer %s\r\n"
             "User-Agent: quadboard/" FW_BUILD "\r\nConnection: keep-alive\r\n\r\n",
             path, SERVER_MDNS, DEVICE_TOKEN);

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
  if (r.code == 200 || r.code == 204) lastErr = NET_OK;
  else if (r.code == 401) lastErr = NET_REJECTED;
  else if (r.code) { lastErr = NET_HTTP; lastHttp = r.code; }
  return r;
}

static bool ok(const Resp &r) { return r.code == 200 || r.code == 204; }

// ---------------------------------------------------------------- telling you what's wrong

static const char *signalWord(int rssi) {
  return rssi >= -60 ? "strong" : rssi >= -70 ? "good" : rssi >= -80 ? "weak" : "very weak";
}

static const char *errLabel(uint8_t e) {
  switch (e) {
    case NET_NO_WIFI: return "No Wi-Fi";
    case NET_NO_NAME: return "Server not found";
    case NET_NO_CONNECT: return "Server not answering";
    case NET_BAD_CERT: return "Wrong certificate";
    case NET_REJECTED: return "Key rejected";
    case NET_HTTP: return "Server error";
    default: return "OK";
  }
}

static void ledCode(uint8_t e) {
  int n = e == NET_NO_WIFI ? 2 : (e == NET_NO_NAME || e == NET_NO_CONNECT) ? 3 : 4;
  for (int i = 0; i < n; i++) { led(true); delay(120); led(false); delay(220); }
}

// Full-screen explanation, in words, with the next thing to try.
static void errorScreen(uint8_t e) {
  static char l1[120], l2[120], l3[120], l4[120];
  String ssid;
  wifisetup::load(&ssid, nullptr);
  const char *title = "Something's wrong";
  l3[0] = l4[0] = 0;
  switch (e) {
    case NET_NO_WIFI:
      title = "Can't join Wi-Fi";
      snprintf(l1, sizeof l1, "Network \"%s\" wasn't found, or refused the password.", ssid.c_str());
      snprintf(l2, sizeof l2, "This board only uses 2.4 GHz Wi-Fi.");
      snprintf(l3, sizeof l3, "To pick another network: hold the left button, tap the right.");
      break;
    case NET_NO_NAME:
      title = "Can't find the server";
      snprintf(l1, sizeof l1, "Nothing called %s.local answered on this network.", SERVER_MDNS);
      snprintf(l2, sizeof l2, "Is the Mac mini on, awake, and on the same network?");
      break;
    case NET_NO_CONNECT:
      title = "Server not answering";
      snprintf(l1, sizeof l1, "Found %s at %s, but port %d didn't answer.", SERVER_MDNS,
               usedIp.toString().c_str(), SERVER_PORT);
      snprintf(l2, sizeof l2, "Is the quad service running? On the Mac, see server/logs/server.log.");
      break;
    case NET_BAD_CERT:
      title = "Wrong server certificate";
      snprintf(l1, sizeof l1, "Something answered at %s, but not with this board's certificate.",
               usedIp.toString().c_str());
      snprintf(l2, sizeof l2, "If setup made a new certificate, flash the firmware it wrote.");
      break;
    case NET_REJECTED:
      title = "Server rejected this board";
      snprintf(l1, sizeof l1, "The board's key doesn't match the server's.");
      snprintf(l2, sizeof l2, "Run server/setup-mac.sh, then flash the firmware it writes.");
      break;
    default:
      title = "Server error";
      snprintf(l1, sizeof l1, "The server answered HTTP %d.", lastHttp);
      snprintf(l2, sizeof l2, "On the Mac, see server/logs/server.log.");
      break;
  }
  if (e != NET_NO_WIFI && WiFi.status() == WL_CONNECTED)
    snprintf(l4, sizeof l4, "Wi-Fi OK: %s, signal %s, address %s", WiFi.SSID().c_str(),
             signalWord(WiFi.RSSI()), WiFi.localIP().toString().c_str());
  const display::MsgLine lines[] = {{l1, false}, {l2, false}, {l3, false}, {l4, false}};
  ownScreen(title, lines, 4, "Still retrying. Left + right buttons together: details.   " FW_BUILD);
  screenErr = e;
  badgeErr = e;
}

// Called after every failed attempt. Escalates: LED, then a corner label, then a full screen.
static void reportFailure(bool battery) {
  uint8_t e = lastErr == NET_OK ? (uint8_t)NET_NO_CONNECT : lastErr;
  ledCode(e);
  uint32_t failingS = battery ? 0 : (millis() - failSinceMs) / 1000;
  bool label = battery ? failedWakes >= 1 : failingS >= 60;
  bool screen = battery ? failedWakes >= 3 : failingS >= 300;
  if (screen && screenErr != e) errorScreen(e);
  else if (label && !screen && badgeErr != e && screenErr == NET_OK) {
    display::badge(errLabel(e));
    badgeErr = e;
    rtcVersion = 0;   // the corner no longer matches the server's frame: ask for a whole one
  }
}

// Back to normal: forget the failure; the next poll (v=0) repaints the whole screen.
static void reportRecovered() {
  if (badgeErr != NET_OK || screenErr != NET_OK) Serial.println("[net] recovered");
  badgeErr = screenErr = NET_OK;
  failedWakes = 0;
  failSinceMs = 0;
  lastOkMs = millis();
}

// The server's address: Bonjour first, then the last address that worked, then SERVER_IP.
static IPAddress serverIp() {
  static bool mdnsUp = false;
  if (!mdnsUp) mdnsUp = MDNS.begin(wifisetup::apName());
  IPAddress ip;
  if (SERVER_MDNS[0]) ip = MDNS.queryHost(SERVER_MDNS, 2500);
  if (ip == IPAddress() && cachedIp) ip = IPAddress(cachedIp);
  if (ip == IPAddress() && SERVER_IP[0]) ip.fromString(SERVER_IP);
  return ip;
}

static bool comboHeld() {
  // left + right together; give the second finger a moment to land
  for (int i = 0; i < 30; i++) {
    if (digitalRead(KEY0_PIN) == LOW && digitalRead(KEY2_PIN) == LOW) return true;
    delay(10);
  }
  return false;
}

static void diagnostics() {
  static char l[6][120];
  float vbat = sensors::readVBat();
  snprintf(l[0], 120, "Firmware %s, boot #%u, %s mode", FW_BUILD, (unsigned)bootCount, liveMode ? "plugged-in" : "battery");
  if (WiFi.status() == WL_CONNECTED)
    snprintf(l[1], 120, "Wi-Fi: %s, %d dBm (%s), address %s", WiFi.SSID().c_str(), WiFi.RSSI(),
             signalWord(WiFi.RSSI()), WiFi.localIP().toString().c_str());
  else
    snprintf(l[1], 120, "Wi-Fi: not connected");
  snprintf(l[2], 120, "Server: %s.local, port %d, last at %s", SERVER_MDNS, SERVER_PORT,
           cachedIp ? IPAddress(cachedIp).toString().c_str() : "(never reached)");
  snprintf(l[3], 120, "Last attempt: %s%s", errLabel(lastErr),
           lastErr == NET_HTTP ? (String(" (HTTP ") + lastHttp + ")").c_str() : "");
  if (lastOkMs) snprintf(l[4], 120, "Last update: %lu s ago", (unsigned long)((millis() - lastOkMs) / 1000));
  else snprintf(l[4], 120, "Last update: none since this wake");
  snprintf(l[5], 120, "Battery: %.2f V   Screen version: v%u", vbat, (unsigned)rtcVersion);
  const display::MsgLine lines[] = {{l[0], false}, {l[1], false}, {l[2], false},
                                    {l[3], false}, {l[4], false}, {l[5], false}};
  ownScreen("Diagnostics", lines, 6, "Press any button to go back (or wait a minute).");
  uint32_t t0 = millis();
  takeBtn();
  while (millis() - t0 < 60000 && pendingBtn < 0) delay(50);
  takeBtn();
  // ownScreen set rtcVersion = 0, so the next poll brings the whole dashboard back
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

  // hold KEY2 (the left button, seen from the front) + tap another => forget wifi
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
    lastErr = NET_NO_WIFI;
    ledCode(NET_NO_WIFI);
    if (failures >= 2 && screenErr != NET_NO_WIFI) errorScreen(NET_NO_WIFI);
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
  if (boot) {
    rtcVersion = 0;
    // Power-on: show that Wi-Fi worked and which server it's trying, so a
    // failure after this point is clearly the server's side.
    static char w[120], sv[120];
    snprintf(w, sizeof w, "Wi-Fi: %s, signal %s, address %s", WiFi.SSID().c_str(),
             signalWord(WiFi.RSSI()), WiFi.localIP().toString().c_str());
    snprintf(sv, sizeof sv, "Server: connecting to %s.local ...", SERVER_MDNS);
    const display::MsgLine lines[] = {{w, false}, {sv, false}};
    ownScreen("Starting up", lines, 2, FW_BUILD);
  }
  if (digitalRead(KEY0_PIN) == LOW && digitalRead(KEY2_PIN) == LOW) diagnostics();
  int8_t b = takeBtn();
  if (b >= 0) blip();
  Resp r = poll(0, b, boot, false);
  if (ok(r)) {
    reportRecovered();
    handle(r);
  } else {
    Serial.printf("[http] first poll failed: %s\n", errLabel(lastErr));
    if (failedWakes < 255) failedWakes++;
    failSinceMs = millis() ? millis() : 1;
    reportFailure(!liveMode);
    if (!liveMode) sleepNow(120);
  }
}

void loop() {
  if (!liveMode) {
    // battery: one more quick look for queued work, then sleep
    sleepNow(nextSleepS);
  }

  int8_t b = takeBtn();
  if (b >= 0 && comboHeld()) { diagnostics(); return; }
  if (b >= 0) blip();
  Resp r = poll(b >= 0 ? 0 : POLL_WAIT_S, b, false, b < 0);
  if (r.aborted) return;   // a button arrived mid-wait; loop re-polls with it
  if (!ok(r)) {
    failures++;
    if (!failSinceMs) failSinceMs = millis() ? millis() : 1;
    reportFailure(false);
    delay(min(2000u * failures, 30000u));
    if (WiFi.status() != WL_CONNECTED) wifisetup::connect(WIFI_TIMEOUT_MS);
    return;
  }
  failures = 0;
  reportRecovered();
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
