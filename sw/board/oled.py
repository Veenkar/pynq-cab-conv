# SSD1306 128x32 I2C OLED over Linux i2c-dev, with a tiny 5x7 font and a canvas.
# No pynq / smbus / PIL needed: plain os.open + ioctl(I2C_SLAVE) + os.write.
#   python3 oled.py test [--bus N] [--addr 0x3C] [--flip] [--ascii]
# --ascii only renders the test frame to the terminal (no hardware needed).
import argparse, os, time

I2C_SLAVE = 0x0703
W, H = 128, 32

# 5x7 font, ASCII 0x20..0x7E; 5 column bytes per glyph, bit 0 = top row.
_FONT = bytes.fromhex(
    "0000000000" "00005f0000" "0007000700" "147f147f14" "242a7f2a12" "2313086462" "3649562050"
    "0005030000" "001c224100" "0041221c00" "14083e0814" "08083e0808" "0050300000" "0808080808"
    "0060600000" "2010080402" "3e5149453e" "00427f4000" "4261514946" "2141454b31" "1814127f10"
    "2745454539" "3c4a494930" "0171090503" "3649494936" "064949291e" "0036360000" "0056360000"
    "0814224100" "1414141414" "0041221408" "0201510906" "324979413e" "7e1111117e" "7f49494936"
    "3e41414122" "7f4141221c" "7f49494941" "7f09090901" "3e4149497a" "7f0808087f" "00417f4100"
    "2040413f01" "7f08142241" "7f40404040" "7f020c027f" "7f0408107f" "3e4141413e" "7f09090906"
    "3e4151215e" "7f09192946" "4649494931" "01017f0101" "3f4040403f" "1f2040201f" "3f4038403f"
    "6314081463" "0708700807" "6151494543" "007f414100" "0204081020" "0041417f00" "0402010204"
    "4040404040" "0001020400" "2054545478" "7f48444438" "3844444420" "384444487f" "3854545418"
    "087e090102" "0c5252523e" "7f08040478" "00447d4000" "2040443d00" "7f10284400" "00417f4000"
    "7c04180478" "7c08040478" "3844444438" "7c14141408" "081414187c" "7c08040408" "4854545420"
    "043f444020" "3c4040207c" "1c2040201c" "3c4030403c" "4428102844" "0c5050503c" "4464544c44"
    "0008364100" "00007f0000" "0041360800" "1008081008")
assert len(_FONT) == 95 * 5, len(_FONT)


class Canvas:
    """1-bit framebuffer in SSD1306 page layout: byte [page*w + x], bit (y % 8)."""

    def __init__(self, w=W, h=H):
        self.w, self.h = w, h
        self.buf = bytearray(w * h // 8)

    def clear(self, v=0):
        self.buf[:] = bytes([0xFF if v else 0]) * len(self.buf)

    def pixel(self, x, y, on=True):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y >> 3) * self.w + x
            if on:
                self.buf[i] |= 1 << (y & 7)
            else:
                self.buf[i] &= ~(1 << (y & 7)) & 0xFF

    def get(self, x, y):
        return bool(self.buf[(y >> 3) * self.w + x] >> (y & 7) & 1)

    def rect(self, x, y, w, h, on=True):
        """Filled rectangle."""
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.pixel(xx, yy, on)

    def invert(self, x, y, w, h):
        for yy in range(max(y, 0), min(y + h, self.h)):
            for xx in range(max(x, 0), min(x + w, self.w)):
                self.buf[(yy >> 3) * self.w + xx] ^= 1 << (yy & 7)

    def text(self, x, y, s, scale=1, on=True):
        """Draw s with its top-left at (x, y); 6*scale px per char. Returns the x after it."""
        for ch in s:
            o = ord(ch)
            if not 32 <= o <= 126:
                o = ord("?")
            g = _FONT[(o - 32) * 5:(o - 32) * 5 + 5]
            for cx in range(5):
                for cy in range(7):
                    if g[cx] >> cy & 1:
                        self.rect(x + cx * scale, y + cy * scale, scale, scale, on)
            x += 6 * scale
        return x

    def hbar(self, x, y, w, h, frac, ticks=()):
        """Meter bar: outline w x h, filled to frac (0..1); ticks are fractions marked inside."""
        frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac
        for xx in range(x, x + w):
            self.pixel(xx, y)
            self.pixel(xx, y + h - 1)
        for yy in range(y, y + h):
            self.pixel(x, yy)
            self.pixel(x + w - 1, yy)
        n = int(round((w - 4) * frac))
        self.rect(x + 2, y + 2, n, h - 4)
        for t in ticks:
            tx = x + 2 + int(round((w - 4) * t))
            self.pixel(tx, y + h // 2, tx >= x + 2 + n)     # dot on the empty part, hole in the fill

    def ascii(self):
        return "\n".join("".join("#" if self.get(x, y) else "." for x in range(self.w))
                         for y in range(self.h))


class SSD1306:
    """SSD1306 128x32 on /dev/i2c-N. Raises OSError on I2C errors."""

    def __init__(self, bus=0, addr=0x3C, flip=False, chunk=128):
        import fcntl                    # Linux only; keep module importable elsewhere
        self.chunk = chunk
        self.fd = os.open("/dev/i2c-%d" % bus, os.O_RDWR)
        try:
            fcntl.ioctl(self.fd, I2C_SLAVE, addr)
            self.init(flip)
        except OSError:
            self.close()
            raise

    def _cmd(self, *c):
        os.write(self.fd, bytes([0x00]) + bytes(c))     # 0x00 = command stream

    def init(self, flip=False):
        self._cmd(0xAE)                         # display off
        self._cmd(0xD5, 0x80)                   # clock divide / oscillator
        self._cmd(0xA8, 0x1F)                   # multiplex 32
        self._cmd(0xD3, 0x00)                   # display offset
        self._cmd(0x40)                         # start line 0
        self._cmd(0x8D, 0x95)                   # charge pump on, 9 V (0x14 = 7.5 V was too dim on the 0.91" module)
        self._cmd(0x20, 0x00)                   # horizontal addressing
        self._cmd(0xA0 if flip else 0xA1)       # segment remap
        self._cmd(0xC0 if flip else 0xC8)       # COM scan direction
        self._cmd(0xDA, 0x02)                   # COM pins: sequential, no remap (128x32)
        self._cmd(0x81, 0xFF)                   # contrast (max)
        self._cmd(0xD9, 0xF1)                   # pre-charge
        self._cmd(0xDB, 0x40)                   # VCOM deselect
        self._cmd(0xA4)                         # follow RAM
        self._cmd(0xA6)                         # normal (not inverted)
        self.clear()
        self._cmd(0xAF)                         # display on

    def contrast(self, v):
        self._cmd(0x81, v & 0xFF)

    def on(self, state=True):
        self._cmd(0xAF if state else 0xAE)

    def show(self, fb):
        """fb: a Canvas or 512 bytes in page layout."""
        data = bytes(fb.buf if hasattr(fb, "buf") else fb)
        self._cmd(0x21, 0, W - 1)               # column range
        self._cmd(0x22, 0, H // 8 - 1)          # page range
        for i in range(0, len(data), self.chunk):
            os.write(self.fd, b"\x40" + data[i:i + self.chunk])     # 0x40 = data stream

    def clear(self):
        self.show(bytes(W * H // 8))

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


def test_canvas():
    c = Canvas()
    c.text(0, 0, "OLED TEST 0123456789")
    c.text(0, 9, "ABCxyz !#%&()*+,-./:;<=>?")
    c.text(0, 18, "12:34", scale=2)
    c.hbar(64, 18, 62, 9, 0.6, ticks=(0.5, 0.8))
    return c


def main():
    ap = argparse.ArgumentParser(description="SSD1306 128x32 helper")
    ap.add_argument("cmd", choices=["test"])
    ap.add_argument("--bus", type=int, default=0)
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=0x3C)
    ap.add_argument("--flip", action="store_true", help="rotate the image by 180 degrees")
    ap.add_argument("--ascii", action="store_true", help="print the frame, do not touch the hardware")
    a = ap.parse_args()
    c = test_canvas()
    if a.ascii:
        print(c.ascii())
        return
    d = SSD1306(a.bus, a.addr, a.flip)
    try:
        d.show(c)
        print("test frame shown for 5 s", flush=True)
        time.sleep(5)
    finally:
        d.clear()
        d.close()


if __name__ == "__main__":
    main()
