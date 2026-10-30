#include "display.h"

#include <TFT_eSPI.h>

#ifndef QUAD_E1003
// ================================================================ E1001 (UC8179)

// Seeed_GFX has updataPartial(), but it sends only the new image, and parks
// the controller in deep sleep after every call -- which wipes the controller's
// record of the old image, so the next partial waveform is computed against
// garbage. For animation we want the controller awake across frames and the
// old image supplied explicitly, so this talks to the UC8179 directly using
// the same init sequence Seeed uses.
class Panel : public EPaper {
 public:
  bool inPartial = false;

  void enterPartial() {
    if (inPartial) return;
    digitalWrite(TFT_RST, LOW);
    delay(10);
    digitalWrite(TFT_RST, HIGH);
    delay(10);
    CHECK_BUSY();
    EPD_INIT_PARTIAL();
    inPartial = true;
  }

  void window(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1) {
    // x1/y1 inclusive; the controller forces the low 3 bits of x1 to 111.
    writecommand(0x50);
    writedata(0xA9);
    writedata(0x07);
    writecommand(0x91);   // partial in
    writecommand(0x90);
    writedata(x0 >> 8);
    writedata(x0 & 0xFF);
    writedata(x1 >> 8);
    writedata(x1 & 0xFF);
    writedata(y0 >> 8);
    writedata(y0 & 0xFF);
    writedata(y1 >> 8);
    writedata(y1 & 0xFF);
    writedata(0x01);
  }

  // One chip-select for the whole window. writedata() toggles CS per byte,
  // which made a full-screen push cost ~1s of pure bus overhead.
  void pushRect(uint8_t cmd, const uint8_t *buf, int bx0, int bx1, int y0,
                int y1) {
    static uint8_t *win = nullptr;
    if (!win) win = (uint8_t *)ps_malloc(display::BYTES);
    const int rb = bx1 - bx0;
    uint8_t *p = win;
    for (int y = y0; y <= y1; y++) {
      memcpy(p, buf + (size_t)y * display::STRIDE + bx0, rb);
      p += rb;
    }
    writecommanddata(cmd, win, (uint16_t)(p - win));
  }

  void parkController() {
    if (!inPartial) return;
    EPD_SLEEP();
    inPartial = false;
  }

  void refresh() { EPD_UPDATE(); }

  // 4-level grey full refresh straight from a 4bpp buffer (one nibble per
  // pixel, even x in the high nibble, value 0=black..3=white), bypassing the
  // sprite so our 1-bit framebuffer survives. Same sequence as EPaper::update()
  // in grey mode.
  void fullGray(const uint8_t *colors) {
    parkController();
    EPD_WAKEUP_GRAY();
    EPD_SET_WINDOW(0, 0, display::W - 1, display::H - 1);
    EPD_PUSH_NEW_GRAY_COLORS(display::W, display::H, colors);
    EPD_UPDATE_GRAY();
    EPD_SLEEP();
  }

  uint8_t *img() { return (uint8_t *)getPointer(); }
};

static Panel epaper;
static uint8_t *prevBuf = nullptr;   // what the glass shows

namespace display {

void begin() {
  epaper.begin();
  if (!prevBuf) {
    prevBuf = (uint8_t *)ps_malloc(BYTES);
    memset(prevBuf, 0xFF, BYTES);
  }
}

uint8_t *buffer() { return epaper.img(); }

void partial(int x0, int y0, int x1, int y1) {
  x0 = max(0, x0) & ~7;
  x1 = min(W, (x1 + 7) & ~7);
  y0 = max(0, y0);
  y1 = min(H, y1);
  if (x1 <= x0 || y1 <= y0) return;

  epaper.enterPartial();
  epaper.window(x0, y0, x1 - 1, y1 - 1);
  epaper.pushRect(0x10, prevBuf, x0 / 8, x1 / 8, y0, y1 - 1);
  epaper.pushRect(0x13, epaper.img(), x0 / 8, x1 / 8, y0, y1 - 1);
  epaper.refresh();
  // Keep prevBuf == glass, but only for the rows we touched.
  for (int y = y0; y < y1; y++) {
    size_t o = (size_t)y * STRIDE + x0 / 8;
    memcpy(prevBuf + o, epaper.img() + o, (x1 - x0) / 8);
  }
}

void full() {
  epaper.parkController();   // update() re-inits from sleep in full mode
  epaper.update();           // ends with the panel in deep sleep
  memcpy(prevBuf, epaper.img(), BYTES);
}

void fullGray(const uint8_t *g2) {
  static uint8_t *g4 = nullptr;
  if (!g4) g4 = (uint8_t *)ps_malloc(GRAY_BYTES * 2);
  // 2bpp on the wire (4 px per byte, MSB first) -> 4bpp nibbles for the driver
  for (size_t i = 0; i < GRAY_BYTES; i++) {
    uint8_t v = g2[i];
    g4[2 * i] = (uint8_t)(((v >> 6) & 3) << 4 | ((v >> 4) & 3));
    g4[2 * i + 1] = (uint8_t)(((v >> 2) & 3) << 4 | (v & 3));
  }
  epaper.fullGray(g4);
  memcpy(prevBuf, epaper.img(), BYTES);   // the glass ~= the 1-bit buffer now
}

void sleep() { epaper.parkController(); }

}  // namespace display

#else
// ================================================================ E1003 (ED103TC2 + TCON)
// The IT8951-class TCON keeps its own framebuffer and computes waveforms
// against it, so none of the UC8179 old/new juggling is needed: Seeed_GFX's
// updataPartial() (1bpp, fast mode) and update() (1bpp full / 4bpp grey) are
// used as-is.
static EPaper epaper;

namespace display {

static uint8_t *saved1 = nullptr;   // 1-bit buffer kept across grey refreshes

void begin() {
  epaper.begin();
  if (!saved1) saved1 = (uint8_t *)ps_malloc(BYTES);
}

uint8_t *buffer() { return (uint8_t *)epaper.getPointer(); }

void partial(int x0, int y0, int x1, int y1) {
  x0 = max(0, x0) & ~15;              // TCON wants 16-px aligned windows
  x1 = min(W, (x1 + 15) & ~15);
  y0 = max(0, y0);
  y1 = min(H, y1);
  if (x1 <= x0 || y1 <= y0) return;
  epaper.updataPartial(x0, y0, x1 - x0, y1 - y0);
}

void full() { epaper.update(); }

// g = 4bpp, 2 px per byte, even x high nibble, 0=black..15=white -- exactly the
// layout of Seeed's 4bpp grey sprite, so it is copied straight in.
void fullGray(const uint8_t *g) {
  memcpy(saved1, buffer(), BYTES);
  epaper.initGrayMode(GRAY_LEVEL16);          // reallocates the sprite at 4bpp
  memcpy(epaper.getPointer(), g, GRAY_BYTES);
  epaper.update();
  epaper.deinitGrayMode();                    // back to the 1bpp sprite
  memcpy(buffer(), saved1, BYTES);
}

void sleep() { epaper.sleep(); }

}  // namespace display
#endif

namespace display {

void drawMessage(const char *title, const MsgLine *lines, int n,
                 const char *footer) {
  epaper.fillSprite(TFT_WHITE);
  epaper.setTextColor(TFT_BLACK, TFT_WHITE, true);
  epaper.setTextDatum(TL_DATUM);
#ifdef QUAD_E1003
  const int K = 2;   // ~227 dpi vs ~125 dpi: double everything
#else
  const int K = 1;
#endif
  epaper.setTextSize(K);
  const int X = 48 * K;
  epaper.setFreeFont(&FreeSansBold18pt7b);
  epaper.drawString(title, X, 48 * K);
  epaper.fillRect(X, 104 * K, W - 2 * X, 2 * K, TFT_BLACK);
  int y = 126 * K;
  for (int i = 0; i < n; i++) {
    if (lines[i].big) {
      epaper.setFreeFont(&FreeSansBold24pt7b);
      epaper.drawString(lines[i].text, X, y);
      y += 62 * K;
    } else {
      epaper.setFreeFont(&FreeSans12pt7b);
      epaper.drawString(lines[i].text, X, y);
      y += 34 * K;
    }
  }
  if (footer) {
    epaper.setFreeFont(&FreeSans12pt7b);
    epaper.drawString(footer, X, H - 46 * K);
  }
  epaper.setTextSize(1);
  full();
}

}  // namespace display
