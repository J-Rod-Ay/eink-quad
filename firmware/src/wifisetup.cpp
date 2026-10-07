#include "wifisetup.h"

#include <DNSServer.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>

namespace wifisetup {
namespace {

const char *NS = "einknotes";

Preferences prefs;
WebServer *server = nullptr;
DNSServer *dns = nullptr;

// Built once, before the AP comes up. The M5 version scans inside the request
// handler, which means every captive-portal probe the phone fires off starts
// another blocking 2-4s scan -- the single flakiest thing about it.
String networkOptions;

uint32_t lastHit = 0;
char apSsid[32] = "";

String esc(const String &s) {
  String out;
  out.reserve(s.length() + 8);
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    switch (c) {
      case '&': out += "&amp;"; break;
      case '<': out += "&lt;"; break;
      case '>': out += "&gt;"; break;
      case '"': out += "&quot;"; break;
      case '\'': out += "&#39;"; break;
      default: out += c;
    }
  }
  return out;
}

const char PAGE_HEAD[] PROGMEM =
    "<!doctype html><html><head><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>Note box setup</title><style>"
    "body{font:17px -apple-system,system-ui,sans-serif;margin:0;padding:24px;"
    "background:#fff;color:#111}"
    "h1{font-size:22px;margin:0 0 4px}p{color:#666;margin:0 0 20px;font-size:15px}"
    "select,input,button{width:100%;box-sizing:border-box;padding:13px;"
    "margin:6px 0 16px;border-radius:10px;border:1px solid #ccc;font-size:17px;"
    "background:#fff;color:#111}"
    "button{background:#111;color:#fff;border:0;font-weight:600}"
    "label{font-size:14px;color:#888}"
    "</style></head><body>"
    "<h1>Note box</h1><p>Choose the Wi-Fi network this display should use.</p>"
    "<form method=POST action=/save>";

const char PAGE_TAIL[] PROGMEM =
    "<label>Password</label>"
    "<input id=pw name=p type=password autocomplete=current-password "
    "autocapitalize=none autocorrect=off spellcheck=false>"
    "<label style='display:block;margin:-8px 0 16px'>"
    "<input type=checkbox style='width:auto;margin-right:8px'"
    " onchange=\"document.getElementById('pw').type="
    "this.checked?'text':'password'\"> Show password</label>"
    "<button type=submit>Connect</button></form>"
    "<p style='font-size:13px'>The display itself will tell you whether it "
    "worked &mdash; watch the screen after you tap Connect.</p>"
    "</body></html>";

void noStore() {
  server->sendHeader("Cache-Control", "no-store, no-cache, must-revalidate");
}

void handleRoot() {
  lastHit = millis();
  noStore();
  String page = FPSTR(PAGE_HEAD);
  page += "<label>Network</label><select name=s>";
  page += networkOptions;
  page += "<option value='__other__'>Other / hidden network&hellip;</option>";
  page += "</select>";
  page += "<label>If you picked Other, type its name</label>"
          "<input name=o autocapitalize=none autocorrect=off placeholder='Network name'>";
  page += FPSTR(PAGE_TAIL);
  server->send(200, "text/html", page);
}

void handleSave() {
  lastHit = millis();
  noStore();
  String ssid = server->arg("s");
  String other = server->arg("o");
  other.trim();
  if (ssid == "__other__" || ssid.isEmpty()) ssid = other;
  String pass = server->arg("p");

  if (ssid.isEmpty()) {
    server->send(200, "text/html",
                 "<!doctype html><meta name=viewport content='width=device-width'>"
                 "<body style='font:17px system-ui;padding:24px'>"
                 "<h1>No network chosen</h1>"
                 "<p><a href='/'>Go back</a> and pick one.</p>");
    return;
  }

  save(ssid, pass);
  server->send(200, "text/html",
               "<!doctype html><meta name=viewport content='width=device-width'>"
               "<body style='font:17px system-ui;padding:24px'>"
               "<h1>Saved</h1><p>Joining <b>" + esc(ssid) +
               "</b>. Watch the display &mdash; your notes will appear when it "
               "works, or it will tell you what went wrong.</p>");
  Serial.printf("[portal] saved ssid='%s' -- restarting\n", ssid.c_str());
  delay(900);           // let the response actually leave the radio
  ESP.restart();
}

void scanInto(String *out) {
  // The core defaults to persisting every WiFi.begin() into the IDF's own NVS
  // area. We keep credentials in our namespace deliberately, and a rejected
  // password left stashed where a later no-argument begin() could find it is
  // exactly the kind of ghost that makes a one-shot evening unexplainable.
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(100);
  int n = WiFi.scanNetworks();
  Serial.printf("[portal] scan found %d networks\n", n);
  for (int i = 0; i < n && i < 20; i++) {
    String s = WiFi.SSID(i);
    if (s.isEmpty()) continue;             // hidden; the Other box covers it
    *out += "<option value='" + esc(s) + "'>" + esc(s) + "  (" +
            String(WiFi.RSSI(i)) + " dBm)</option>";
  }
  WiFi.scanDelete();
}

}  // namespace

const char *apName() {
  if (apSsid[0] == '\0') {
    uint8_t mac[6];
    WiFi.macAddress(mac);
    snprintf(apSsid, sizeof(apSsid), "QuadBoard-%02X%02X", mac[4], mac[5]);
  }
  return apSsid;
}

bool load(String *ssid, String *pass) {
  // Read-write even to read: opening read-only logs a NOT_FOUND the first
  // time, before the namespace exists.
  prefs.begin(NS, false);
  String s = prefs.getString("ssid", "");
  String p = prefs.getString("pass", "");
  prefs.end();
  if (s.isEmpty()) return false;
  if (ssid) *ssid = s;
  if (pass) *pass = p;
  return true;
}

void save(const String &ssid, const String &pass) {
  prefs.begin(NS, false);
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  prefs.putBool("fresh", true);
  prefs.end();
}

void forget() {
  prefs.begin(NS, false);
  prefs.remove("ssid");
  prefs.remove("pass");
  prefs.remove("fresh");
  prefs.end();
}

bool isFresh() {
  prefs.begin(NS, false);
  bool f = prefs.getBool("fresh", false);
  prefs.end();
  return f;
}

void clearFresh() {
  prefs.begin(NS, false);
  prefs.putBool("fresh", false);
  prefs.end();
}

bool connect(uint32_t timeoutMs) {
  String ssid, pass;
  if (!load(&ssid, &pass)) {
    Serial.println("[wifi] no stored network");
    return false;
  }
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(true);
  WiFi.begin(ssid.c_str(), pass.c_str());
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - t0 > timeoutMs) {
      Serial.printf("[wifi] '%s' timeout after %lums\n", ssid.c_str(),
                    millis() - t0);
      return false;
    }
    delay(100);
  }
  Serial.printf("[wifi] %s  ip=%s  rssi=%d  in %lums\n", ssid.c_str(),
                WiFi.localIP().toString().c_str(), WiFi.RSSI(), millis() - t0);
  return true;
}

void runPortal(uint32_t idleTimeoutMs) {
  // Scan first, then switch to plain AP. Staying in AP_STA the way the M5
  // version does would let the softAP follow a station channel change and
  // knock the phone off mid-setup; we never need STA while the portal is up,
  // because the credentials are not tested here -- the panel reports the
  // verdict after the reboot.
  networkOptions = "";
  scanInto(&networkOptions);

  WiFi.mode(WIFI_AP);
  WiFi.softAP(apName());
  WiFi.setSleep(false);          // the phone is waiting on every request
  delay(400);

  dns = new DNSServer();
  dns->start(53, "*", WiFi.softAPIP());   // every lookup resolves to us, which
                                          // is what pops the page by itself

  server = new WebServer(80);
  server->on("/", handleRoot);
  server->on("/save", HTTP_POST, handleSave);
  server->onNotFound(handleRoot);         // catches the OS captive probes too
  server->begin();

  Serial.printf("[portal] AP '%s' up at http://%s/\n", apName(),
                WiFi.softAPIP().toString().c_str());

  lastHit = millis();
  while (millis() - lastHit < idleTimeoutMs) {
    dns->processNextRequest();
    server->handleClient();
    delay(2);
  }

  // Nobody showed up. Tear down rather than hold an AP open on battery.
  Serial.println("[portal] idle timeout");
  server->stop();
  dns->stop();
  delete server; server = nullptr;
  delete dns;    dns = nullptr;
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_OFF);
}

}  // namespace wifisetup
