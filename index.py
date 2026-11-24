# =========================================================
#  LIVE SKY CLOCK (Tazaungdaing)  —  M5Stack Core (320x240) / UIFlow2
#
#  Festival of Lights theme: Thatbyinnyu Temple (Bagan) and the
#  golden Shwedagon Pagoda under blue mountains, toddy palms and
#  rain trees, a Nibbana night market with lantern strings and a
#  walking crowd, oil lamps, and fire balloons rising into the sky.
#
#  The scene follows the real sky over your location (set in secrets.py):
#   * DAWN / DAY / DUSK / NIGHT from today's real sunrise & sunset
#   * sun by day, moon (real phase) by night, stars on clear nights
#   * live weather: clear, cloudy, overcast, fog, drizzle, rain,
#     showers, snow, thunderstorm, and windy (lanterns drift sideways)
#   * weather + sun data from Open-Meteo (free, no API key)
#  Text and cards redraw only when their values change; the
#  landscape is animated on an off-screen canvas (no flicker).
# =========================================================
import time
import gc
import math
import random
import M5
from M5 import *
import network
import ntptime
import requests

# ---------------------------------------------------------
# Settings  (personal details live in secrets.py, which is not
#   committed — copy secrets_example.py to secrets.py and edit it)
#   WIFI_SSID = "..."
#   WIFI_PASSWORD = "..."
#   LAT, LON = "35.6812", "139.7671"
#   PLACE = "TOKYO"
# ---------------------------------------------------------
try:
    from secrets import WIFI_SSID, WIFI_PASSWORD
except ImportError:
    WIFI_SSID     = "YOUR_WIFI_SSID"
    WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"
try:
    from secrets import LAT, LON, PLACE
except ImportError:
    LAT, LON = "35.6812", "139.7671"   # default: Tokyo Station
    PLACE = "TOKYO"


WIFI_TIMEOUT = 12

WEATHER_EVERY_MS = 10 * 60 * 1000      # live weather: every 10 min
NTP_EVERY_MS = 6 * 60 * 60 * 1000
JST_OFFSET = 9 * 3600
FRAME_MS = 80                          # animation speed (~12 fps)
# Canvas colour depth. canvas_test.py showed 16-bit fails on this Core
# (no PSRAM) but 8-bit works, so use 8-bit only. Use (16, 8) on CoreS3/Core2.
CANVAS_BPP = (8,)
WINDY_MS = 6.0                         # wind speed that counts as "windy"

STRIP_Y, STRIP_H = 106, 54             # animated landscape area
GROUND = 154                           # ground line (screen y)
SKY_LOCAL = 26                         # sky part of the strip (local y)

DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

# ---------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------
def rgb(c):
    return (c >> 16) & 255, (c >> 8) & 255, c & 255


def blend(c1, c2, i, n):
    r1, g1, b1 = rgb(c1)
    r2, g2, b2 = rgb(c2)
    return ((r1 + (r2 - r1) * i // n) << 16) | ((g1 + (g2 - g1) * i // n) << 8) | (b1 + (b2 - b1) * i // n)


def shade(c, f, tint=0x000000):
    """Darken a colour by factor f (0..1) and mix in a little tint (night blue)."""
    r, g, b = rgb(c)
    tr, tg, tb = rgb(tint)
    return (min(255, int(r * f) + tr) << 16) | (min(255, int(g * f) + tg) << 8) | min(255, int(b * f) + tb)


CANVAS_DEPTH = 16          # set at boot (8 on Core Basic)


def q(c):
    """Round a colour to what an 8-bit (RGB332) canvas can show, so the parts
    drawn straight on the screen use exactly the same colour (no visible seam)."""
    if CANVAS_DEPTH != 8:
        return c
    r, g, b = rgb(c)
    return (((r >> 5) * 255 // 7) << 16) | (((g >> 5) * 255 // 7) << 8) | ((b >> 6) * 255 // 3)


# Base landscape colours (daylight)
BASE = {
    "mount_far": 0x8FB3E8, "mount_near": 0x5F86C9,         # soft blue hills
    "palm": 0x2E7D45, "palm_dk": 0x1E5530, "trunk": 0x6B4226,   # toddy palms
    "tree": 0x3F8F3A, "tree_lt": 0x7FC24A, "tree_dk": 0x285A2A, # rain trees
    "earth": 0xC9A26B, "earth_dk": 0xA07A45, "clay": 0x8B4513,
}
# Lit things: temples are floodlit and the market is lit at night
GOLD = {"gold": 0xE0B030, "gold_lt": 0xFFD54A, "gold_dk": 0xB8860B, "door": 0x5A2A10,
        "white": 0xF2EEE2, "white_sh": 0xC8C0B0, "trim": 0xD4AF37, "red": 0xB23A2A}
MARKET = {"awn_r": 0xC8372D, "awn_w": 0xF5F0E6, "stall": 0x8B5A2B, "pole": 0x5A3A20,
          "string": 0x6D4C2E, "skin": 0x8D5524,
          "pp0": 0xC0392B, "pp1": 0x2E86C1, "pp2": 0xF1C40F, "pp3": 0x8E44AD,
          "pp4": 0x27AE60, "pp5": 0xE67E22, "pp6": 0xF5F0E6}
LANTERN = (0xFF4A2A, 0xFF9A2E, 0xFFDD55)                    # paper lanterns / fire balloons
FLAME, FLAME_HOT = 0xFF9A2E, 0xFFFFA0

# Sky colours: (top, horizon) for each phase, clear and overcast
SKIES = {
    ("DAWN", 0): (0xB6B6FF, 0xFFB6AA), ("DAWN", 1): (0xB6B6AA, 0xDBB6AA),
    ("DAY", 0):  (0xB6DBFF, 0xDBFFFF), ("DAY", 1):  (0xB6B6AA, 0xDBDBAA),
    ("DUSK", 0): (0x926DAA, 0xFF9255), ("DUSK", 1): (0x6D6D55, 0xB69255),
    ("NIGHT", 0): (0x000055, 0x242455), ("NIGHT", 1): (0x242455, 0x494955),
}

# ---------------------------------------------------------
# State
# ---------------------------------------------------------
wlan = None
wifi_ok = False
weather = {"temp": None, "code": -1, "hum": None, "wind": None,
           "cloud": None, "is_day": None, "rise": None, "set": None}
shown = {}
T = {}                     # current theme colours
P = {}                     # current landscape colours
theme_key = None
phase = "DAY"
canvas = None
animate = True
particles = []             # fire balloons / rain / drizzle / snow
people = []                # festival crowd walking along the market
clouds = []
flash = 0                  # lightning frames left
brightness_levels = [255, 140, 50]
brightness_idx = 0
last_weather_ms = last_ntp_ms = last_frame_ms = 0


# ---------------------------------------------------------
# Small helpers
# ---------------------------------------------------------
def changed(key, value):
    if shown.get(key) == value:
        return False
    shown[key] = value
    return True


def text(s, x, y, size, color, bg=None):
    M5.Display.setTextSize(size)
    M5.Display.setTextColor(color, T["bg"] if bg is None else bg)
    M5.Display.drawString(s, x, y)


def text_w(s, size=1):
    """Pixel width of a string in the device font (falls back to 6 px/char)."""
    try:
        M5.Display.setTextSize(size)
        return M5.Display.textWidth(s)
    except Exception:
        return 6 * size * len(s)


def hhmm_to_min(s):
    """'2026-11-22T06:21' -> minutes after midnight"""
    try:
        return int(s[11:13]) * 60 + int(s[14:16])
    except Exception:
        return None


# ---------------------------------------------------------
# Weather meaning
# ---------------------------------------------------------
def condition():
    """Simplified live condition used for the scene."""
    c = weather["code"]
    if c in (95, 96, 99): return "STORM"
    if c in (71, 73, 75, 77, 85, 86): return "SNOW"
    if c in (80, 81, 82): return "SHOWERS"
    if c in (61, 63, 65, 66, 67): return "RAIN"
    if c in (51, 53, 55, 56, 57): return "DRIZZLE"
    if c in (45, 48): return "FOG"
    if c == 3: return "OVERCAST"
    if c == 2: return "CLOUDY"
    if c in (0, 1): return "CLEAR"
    return "UNKNOWN"


def sky_word():
    """Short word shown on the card (max 8 letters)."""
    cond = condition()
    if cond == "CLEAR":
        return "SUNNY" if phase in ("DAY", "DAWN") else "CLEAR"
    if cond == "CLOUDY":
        return "PARTLY"
    return {"UNKNOWN": "---"}.get(cond, cond)


def is_overcast():
    cl = weather["cloud"] or 0
    return condition() in ("OVERCAST", "FOG", "DRIZZLE", "RAIN", "SHOWERS", "SNOW", "STORM") or cl >= 80


def is_windy():
    return (weather["wind"] or 0) >= WINDY_MS


# ---------------------------------------------------------
# Day phase from real sunrise / sunset
# ---------------------------------------------------------
DEMOS = (                                   # (phase, weather) for demo mode
    ("DAWN",  {"code": 0,  "cloud": 10,  "wind": 1.5}),
    ("DAY",   {"code": 2,  "cloud": 45,  "wind": 3.0}),
    ("DAY",   {"code": 63, "cloud": 100, "wind": 3.5}),
    ("DAY",   {"code": 95, "cloud": 100, "wind": 7.0}),
    ("DAY",   {"code": 73, "cloud": 100, "wind": 2.0}),
    ("DAY",   {"code": 45, "cloud": 100, "wind": 0.5}),
    ("DUSK",  {"code": 1,  "cloud": 20,  "wind": 8.0}),
    ("NIGHT", {"code": 0,  "cloud": 5,   "wind": 1.0}),
    ("NIGHT", {"code": 61, "cloud": 100, "wind": 2.0}),
)
demo_idx = -1          # -1 = live
real_weather = None


def day_phase(t):
    if demo_idx >= 0:
        return DEMOS[demo_idx][0]
    now = t[3] * 60 + t[4]
    rise = hhmm_to_min(weather["rise"] or "")
    sset = hhmm_to_min(weather["set"] or "")
    if rise is None or sset is None:
        if weather["is_day"] is not None:
            return "DAY" if weather["is_day"] else "NIGHT"
        rise, sset = 6 * 60, 17 * 60
    if abs(now - rise) <= 30: return "DAWN"
    if abs(now - sset) <= 30: return "DUSK"
    return "DAY" if rise < now < sset else "NIGHT"


# ---------------------------------------------------------
# Moon phase (calculated on the device)
# ---------------------------------------------------------
def moon_age():
    y, m, d, h, mi = time.gmtime()[:5]            # UTC
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    jd = int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + b - 1524.5 + (h + mi / 60) / 24
    return (jd - 2451550.1) % 29.530588853        # days since new moon


def moon_name(age):
    names = ["NEW MOON", "WAX CRES", "1ST QTR", "WAX GIBB",
             "FULL MOON", "WAN GIBB", "LAST QTR", "WAN CRES"]
    return names[int((age / 29.530588853) * 8 + 0.5) % 8]


# ---------------------------------------------------------
# Myanmar calendar month (shown under the Wi-Fi / battery row)
#   Table made on a PC with tools/mm_months.py (Yan Naing Aye's
#   mmcal algorithm). Row = (first day yyyymmdd, month, length).
#   month: 0=1st Waso 1=Tagu 2=Kason 3=Nayon 4=Waso 5=Wagaung
#   6=Tawthalin 7=Thadingyut 8=Tazaungmon 9=Nadaw 10=Pyatho
#   11=Tabodwe 12=Tabaung.  Covers Dec 2025 - Dec 2032.
# ---------------------------------------------------------
MM_LABEL = "festival"      # "festival" (TAZAUNGDAING) or "month" (TAZAUNGMON)
MM_MONTHS = ("1ST WASO", "TAGU", "KASON", "NAYON", "WASO", "WAGAUNG", "TAWTHALIN",
             "THADINGYUT", "TAZAUNGMON", "NADAW", "PYATHO", "TABODWE", "TABAUNG")
MM_FESTIVALS = ("1ST WASO", "THINGYAN", "NYAUNGYE-THUN", "SARPYAN", "WASO-THINGAN",
                "HSUN-SAR-MAE", "HLAY-PYAING", "MEE-HTUN", "TAZAUNGDAING", "NAT-PWE",
                "MYIN-KHIN", "HTAMANE", "THEL-PONE-ZEDI")
MM_TABLE = (
    (20251219, 10, 30),
    (20260118, 11, 29),
    (20260216, 12, 30),
    (20260318, 1, 29),
    (20260416, 2, 30),
    (20260516, 3, 30),
    (20260615, 0, 30),
    (20260715, 4, 30),
    (20260814, 5, 29),
    (20260912, 6, 30),
    (20261012, 7, 29),
    (20261110, 8, 30),
    (20261210, 9, 29),
    (20270108, 10, 30),
    (20270207, 11, 29),
    (20270308, 12, 30),
    (20270407, 1, 29),
    (20270506, 2, 30),
    (20270605, 3, 29),
    (20270704, 4, 30),
    (20270803, 5, 29),
    (20270901, 6, 30),
    (20271001, 7, 29),
    (20271030, 8, 30),
    (20271129, 9, 29),
    (20271228, 10, 30),
    (20280127, 11, 29),
    (20280225, 12, 30),
    (20280326, 1, 29),
    (20280424, 2, 30),
    (20280524, 3, 29),
    (20280622, 4, 30),
    (20280722, 5, 29),
    (20280820, 6, 30),
    (20280919, 7, 29),
    (20281018, 8, 30),
    (20281117, 9, 29),
    (20281216, 10, 30),
    (20290115, 11, 29),
    (20290213, 12, 30),
    (20290315, 1, 29),
    (20290413, 2, 30),
    (20290513, 3, 29),
    (20290611, 0, 30),
    (20290711, 4, 30),
    (20290810, 5, 29),
    (20290908, 6, 30),
    (20291008, 7, 29),
    (20291106, 8, 30),
    (20291206, 9, 29),
    (20300104, 10, 30),
    (20300203, 11, 29),
    (20300304, 12, 30),
    (20300403, 1, 29),
    (20300502, 2, 30),
    (20300601, 3, 29),
    (20300630, 4, 30),
    (20300730, 5, 29),
    (20300828, 6, 30),
    (20300927, 7, 29),
    (20301026, 8, 30),
    (20301125, 9, 29),
    (20301224, 10, 30),
    (20310123, 11, 29),
    (20310221, 12, 30),
    (20310323, 1, 29),
    (20310421, 2, 30),
    (20310521, 3, 30),
    (20310620, 0, 30),
    (20310720, 4, 30),
    (20310819, 5, 29),
    (20310917, 6, 30),
    (20311017, 7, 29),
    (20311115, 8, 30),
    (20311215, 9, 29),
    (20320113, 10, 30),
    (20320212, 11, 29),
    (20320312, 12, 30),
    (20320411, 1, 29),
    (20320510, 2, 30),
    (20320609, 3, 29),
    (20320708, 4, 30),
    (20320807, 5, 29),
    (20320905, 6, 30),
    (20321005, 7, 29),
    (20321103, 8, 30),
    (20321203, 9, 29))


def day_number(y, m, d):
    """Days since a fixed date (integer maths, same on the device and PC)."""
    a = (14 - m) // 12
    y2, m2 = y + 4800 - a, m + 12 * a - 3
    return d + (153 * m2 + 2) // 5 + 365 * y2 + y2 // 4 - y2 // 100 + y2 // 400


def mm_date(t):
    """-> (label, moon-day text, is full moon) for the date t, or None."""
    key = t[0] * 10000 + t[1] * 100 + t[2]
    row = None
    for r in MM_TABLE:
        if r[0] > key:
            break
        row = r
    if row is None or key >= 20330101:
        return None
    s = row[0]
    md = day_number(t[0], t[1], t[2]) - day_number(s // 10000, s // 100 % 100, s % 100) + 1
    if md > row[2]:
        return None
    if md == 15:
        day = "FULL MOON"
    elif md == row[2]:
        day = "NEW MOON"
    elif md < 15:
        day = "WAX {}".format(md)
    else:
        day = "WAN {}".format(md - 15)
    names = MM_FESTIVALS if MM_LABEL == "festival" else MM_MONTHS
    return names[row[1]], day, md == 15


# ---------------------------------------------------------
# Theme
# ---------------------------------------------------------
def apply_theme():
    global T, P
    night = phase == "NIGHT"
    dim = phase in ("DAWN", "DUSK")
    top, hor = SKIES[(phase, 1 if is_overcast() else 0)]
    if night:
        T = {"bg": top, "haze": hor, "ink": 0xE8ECF5, "time": 0xF2C94C, "sec": 0xFFB347,
             "label": 0xD9C089, "card": 0x1C2240, "line": 0x5A4A7A, "top": 0xD4A017,
             "ok": 0x6FCF97, "warn": 0xFF6B6B, "amber": 0xF2C14E,
             "cloud": 0x3A4058, "rain": 0x7FA7D6, "snow": 0xE6EEF7, "fog": 0x4A5068}
        P = {k: shade(v, 0.42, 0x0A0F2A) for k, v in BASE.items()}
        P["mount_far"], P["mount_near"] = 0x3A4A8A, 0x24306A      # blue hills under moonlight
        P.update({k: shade(v, 0.85) for k, v in GOLD.items()})     # floodlit temples
        P["white"], P["white_sh"] = 0xE8D8B0, 0xB8A888             # warm floodlight on white
        P["door"] = 0xFFC94A                                       # glowing shrine doors
        P.update({k: shade(v, 0.75) for k, v in MARKET.items()})   # market under lamps
        T["skytext"] = T["label"]
    else:
        T = {"bg": top, "haze": hor, "ink": 0x2B2B3A, "time": 0x8B1E1E, "sec": 0xC8861E,
             "label": 0x9C6B1E, "card": 0xFFFBF5, "line": 0xE3C27A, "top": 0xD4A017,
             "ok": 0x2E8B57, "warn": 0xC0392B, "amber": 0xE0A100,
             "cloud": 0xFFFFFF if not is_overcast() else 0x9292AA,
             "rain": 0x4A90C2, "snow": 0xFFFFFF, "fog": 0xDBDBAA}
        f = 0.8 if dim else (0.88 if is_overcast() else 1.0)
        P = {k: shade(v, f) for k, v in BASE.items()}
        P.update({k: shade(v, f) for k, v in GOLD.items()})
        P.update({k: shade(v, f) for k, v in MARKET.items()})
        T["skytext"] = 0xFFEBD2 if phase == "DUSK" else T["label"]   # readable on purple dusk sky
    P["flame"], P["flame_hot"] = FLAME, FLAME_HOT
    for i, c in enumerate(LANTERN):                                # lanterns glow, never shaded
        P["lan%d" % i] = c
    for k in ("bg", "haze", "cloud", "rain", "snow", "fog"):
        T[k] = q(T[k])
    for k in P:
        P[k] = q(P[k])


def update_theme(t, force=False):
    """Redraw everything when day phase or cloudiness changes."""
    global phase, theme_key
    phase = day_phase(t)
    key = (phase, is_overcast())
    if force or key != theme_key:
        theme_key = key
        apply_theme()
        reset_effects()
        shown.clear()
        draw_scene()


# ---------------------------------------------------------
# Network
# ---------------------------------------------------------
def wifi_connect():
    global wlan, wifi_ok
    wlan = network.WLAN(network.STA_IF)
    try:
        wlan.active(True)
        if wlan.isconnected():
            wifi_ok = True
            return True
        try:
            wlan.disconnect()
        except OSError:
            pass
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        start = time.ticks_ms()
        while not wlan.isconnected():
            if time.ticks_diff(time.ticks_ms(), start) > WIFI_TIMEOUT * 1000:
                wifi_ok = False
                return False
            time.sleep(0.5)
        wifi_ok = True
        return True
    except Exception as e:
        print("Wi-Fi error:", e)
        wifi_ok = False
        return False


def sync_time():
    try:
        ntptime.host = "pool.ntp.org"
        ntptime.settime()
        return True
    except Exception as e:
        print("NTP error:", e)
        return False


def fetch_weather():
    if not (wlan and wlan.isconnected()) and not wifi_connect():
        return
    url = ("https://api.open-meteo.com/v1/forecast"
           "?latitude=" + LAT + "&longitude=" + LON +
           "&current=temperature_2m,relative_humidity_2m,weather_code,"
           "wind_speed_10m,cloud_cover,is_day"
           "&daily=sunrise,sunset&forecast_days=1"
           "&wind_speed_unit=ms&timezone=Asia%2FTokyo")
    r = None
    try:
        r = requests.get(url, headers={"User-Agent": "M5Stack"}, timeout=10)
        data = r.json()
        cur = data.get("current", {})
        daily = data.get("daily", {})
        weather["temp"] = cur.get("temperature_2m")
        weather["hum"] = cur.get("relative_humidity_2m")
        weather["wind"] = cur.get("wind_speed_10m")
        weather["cloud"] = cur.get("cloud_cover")
        weather["code"] = cur.get("weather_code", -1)
        weather["is_day"] = cur.get("is_day")
        if daily.get("sunrise"):
            weather["rise"] = daily["sunrise"][0]
            weather["set"] = daily["sunset"][0]
    except Exception as e:
        print("Weather error:", e)
    finally:
        if r:
            r.close()
        gc.collect()


# ---------------------------------------------------------
# Landscape drawing (g = screen or canvas, oy = y offset)
# ---------------------------------------------------------
def draw_thatbyinnyu(g, cx, oy):
    """Thatbyinnyu Temple (Bagan): white two-storey temple, gold trim,
    corner pinnacles with red tips and a tall corn-cob spire."""
    gy = GROUND - oy
    w, sh, tr, red, dr = P["white"], P["white_sh"], P["trim"], P["red"], P["door"]
    g.fillCircle(cx, gy - 32, 6, w)                                # spire (sikhara)
    g.fillTriangle(cx - 7, gy - 31, cx + 7, gy - 31, cx, gy - 42, w)
    g.fillTriangle(cx + 1, gy - 31, cx + 7, gy - 31, cx + 1, gy - 41, sh)
    g.fillRect(cx - 2, gy - 43, 5, 1, P["trim"])                    # hti: tiered umbrella
    g.fillRect(cx - 1, gy - 44, 3, 1, P["trim"])
    g.fillRect(cx, gy - 46, 1, 2, P["trim"])
    g.fillRect(cx - 12, gy - 31, 24, 2, tr)                         # upper terraces
    g.fillRect(cx - 14, gy - 29, 28, 2, w)
    g.fillRect(cx - 15, gy - 27, 30, 11, w)                         # upper storey
    g.fillRect(cx + 8, gy - 27, 7, 11, sh)
    g.fillRect(cx - 15, gy - 27, 30, 1, tr)
    for x in (cx - 10, cx + 5):
        g.fillRect(x, gy - 25, 1, 8, sh)
    g.fillRect(cx - 2, gy - 23, 4, 6, dr)                           # arched window
    g.fillCircle(cx, gy - 23, 2, dr)
    for sgn in (-1, 1):
        x = cx + sgn * 14
        g.fillTriangle(x - 2, gy - 27, x + 2, gy - 27, x, gy - 33, w)
        g.fillRect(x, gy - 35, 1, 2, red)
    g.fillRect(cx - 19, gy - 16, 38, 2, tr)                         # middle terraces
    g.fillRect(cx - 21, gy - 14, 42, 2, w)
    g.fillRect(cx - 22, gy - 12, 44, 12, w)                         # lower storey
    g.fillRect(cx + 12, gy - 12, 10, 12, sh)
    g.fillRect(cx - 22, gy - 12, 44, 1, tr)
    for x in (cx - 16, cx + 14):
        g.fillRect(x, gy - 9, 2, 4, dr)                             # small windows
    for sgn in (-1, 1):
        x = cx + sgn * 20
        g.fillTriangle(x - 2, gy - 14, x + 2, gy - 14, x, gy - 20, w)
        g.fillRect(x, gy - 22, 1, 2, red)
    g.fillTriangle(cx - 6, gy - 9, cx + 6, gy - 9, cx, gy - 16, w)  # porch gable
    g.fillRect(cx, gy - 18, 1, 2, red)
    g.fillRect(cx - 6, gy - 9, 12, 9, w)
    g.fillRect(cx - 3, gy - 7, 6, 7, dr)                            # main door
    g.fillCircle(cx, gy - 7, 3, dr)
    g.fillRect(cx - 24, gy - 2, 48, 2, sh)                          # plinth


def draw_shwedagon(g, cx, oy):
    """Shwedagon Pagoda: golden bell stupa on stepped terraces, bands,
    lotus petals, banana bud, hti and the diamond orb; small stupas around."""
    gy = GROUND - oy
    gold, lt, dk = P["gold"], P["gold_lt"], P["gold_dk"]
    g.fillCircle(cx, gy - 20, 10, gold)                             # bell
    g.fillTriangle(cx - 14, gy - 12, cx + 14, gy - 12, cx, gy - 25, gold)
    g.fillRect(cx - 6, gy - 26, 2, 10, lt)                          # sunlit side
    g.fillTriangle(cx + 4, gy - 12, cx + 14, gy - 12, cx + 6, gy - 24, dk)
    g.fillRect(cx - 7, gy - 31, 14, 2, dk)                          # turban band
    g.fillRect(cx - 6, gy - 33, 12, 2, gold)                        # inverted bowl
    g.fillRect(cx - 5, gy - 35, 10, 2, lt)                          # lotus petals
    g.fillTriangle(cx - 4, gy - 35, cx + 4, gy - 35, cx, gy - 44, gold)   # banana bud
    g.fillRect(cx - 1, gy - 42, 1, 7, lt)
    g.fillRect(cx - 3, gy - 45, 7, 1, dk)                           # hti: tiered umbrella
    g.fillRect(cx - 2, gy - 46, 5, 1, lt)                           # (rings get smaller
    g.fillRect(cx - 1, gy - 47, 3, 1, gold)                         #  towards the top)
    g.fillRect(cx, gy - 48, 1, 1, lt)                               # vane
    orb = 0xFFFFFF if (phase == "NIGHT" and (time.ticks_ms() // 400) % 2) else lt
    g.fillRect(cx, gy - 49, 1, 1, orb)                              # diamond orb
    g.fillRect(cx - 18, gy - 12, 36, 3, dk)                         # terraces
    g.fillRect(cx - 23, gy - 9, 46, 4, gold)
    g.fillRect(cx - 28, gy - 5, 56, 5, dk)
    g.fillRect(cx - 23, gy - 9, 46, 1, lt)
    g.fillRect(cx - 28, gy - 5, 56, 1, lt)
    for sgn in (-1, 1):                                             # surrounding small stupas
        for dx, base, h in ((20, gy - 9, 6), (26, gy - 5, 6), (32, gy, 8)):
            x = cx + sgn * dx
            g.fillTriangle(x - 2, base, x + 2, base, x, base - h, gold)
            g.fillRect(x - 1, base - 2, 1, 2, lt)
    if phase in ("NIGHT", "DUSK"):                                  # lamps on the terraces
        for x in range(cx - 26, cx + 27, 5):
            g.fillRect(x, gy - 4, 1, 1, P["flame_hot"])
        for x in range(cx - 21, cx + 22, 5):
            g.fillRect(x, gy - 8, 1, 1, P["flame_hot"])


PALM_FRONDS = (                                   # (bend x, bend y, tip x, tip y)
    (-6, -4, -13, 0), (-7, -1, -14, 5), (-5, 1, -10, 9), (-3, -5, -8, -7),
    (6, -4, 13, 0), (7, -1, 14, 5), (5, 1, 10, 9), (3, -5, 8, -7), (0, -6, 1, -10),
)


def draw_palm(g, x, h, lean, oy):
    """Toddy palm: slim leaning trunk and long, gently drooping fronds."""
    gy = GROUND - oy
    top = gy - h
    tx = x + lean
    g.drawLine(x, gy, tx, top, P["trunk"])
    g.drawLine(x + 1, gy, tx + 1, top, P["trunk"])
    for bx, by, ex, ey in PALM_FRONDS:
        g.drawLine(tx, top, tx + bx, top + by, P["palm"])          # frond rises...
        g.drawLine(tx + bx, top + by, tx + ex, top + ey, P["palm"])  # ...then droops
        g.drawLine(tx, top + 1, tx + bx, top + by + 1, P["palm_dk"])
        g.drawLine(tx + bx, top + by + 1, tx + ex, top + ey + 1, P["palm_dk"])
    g.fillRect(tx - 1, top - 1, 3, 2, P["palm_dk"])
    g.fillRect(tx - 1, top + 1, 3, 2, P["trunk"])                   # fruit cluster


def draw_raintree(g, x, h, r, oy):
    """Round shady tree with a darker underside and a sunlit crown."""
    gy = GROUND - oy
    top = gy - h
    g.fillRect(x - 1, top, 3, h, P["trunk"])
    g.drawLine(x, top + 3, x - r // 2, top - 1, P["trunk"])
    g.drawLine(x, top + 3, x + r // 2, top - 1, P["trunk"])
    g.fillCircle(x - r // 2, top + 1, (r * 2) // 3, P["tree_dk"])
    g.fillCircle(x + r // 2, top + 1, (r * 2) // 3, P["tree_dk"])
    g.fillCircle(x, top - 2, r, P["tree"])
    if phase != "NIGHT":
        g.fillCircle(x - r // 3, top - r // 2 - 1, r // 2, P["tree_lt"])


STALLS = (136, 157, 268, 289)                    # left edges of market stalls
STRINGS = ((120, 194), (262, 318))               # lantern strings over the market


def draw_market(g, oy):
    """Nibbana night market: striped stalls and strings of paper lanterns."""
    gy = GROUND - oy
    lit = phase in ("NIGHT", "DUSK")
    for a, b in STRINGS:
        for x in (a, b):
            g.fillRect(x, gy - 22, 1, 22, P["pole"])
        mid = (a + b) // 2
        g.drawLine(a, gy - 22, mid, gy - 17, P["string"])
        g.drawLine(mid, gy - 17, b, gy - 22, P["string"])
        for i, x in enumerate(range(a + 5, b - 2, 7)):
            sag = 5 - abs(x - mid) * 5 // (mid - a)
            y = gy - 22 + sag
            g.fillRect(x - 1, y + 1, 3, 3, P["lan%d" % (i % 3)])
            if lit:
                g.fillRect(x, y + 2, 1, 1, P["flame_hot"])
    for x in STALLS:
        for i in range(0, 18, 3):                                   # striped awning
            g.fillRect(x + i, gy - 11, 3, 3, P["awn_r"] if i % 6 == 0 else P["awn_w"])
        g.fillRect(x, gy - 8, 1, 8, P["pole"])
        g.fillRect(x + 17, gy - 8, 1, 8, P["pole"])
        g.fillRect(x + 2, gy - 5, 14, 5, P["stall"])                # counter
        for i, k in enumerate(("pp2", "pp0", "pp5", "pp4")):        # goods
            g.fillRect(x + 3 + i * 3, gy - 6, 2, 1, P[k])
        if lit:
            g.fillRect(x + 8, gy - 8, 2, 1, P["flame_hot"])         # stall lamp


def draw_lamps(g, oy):
    """Clay oil lamps along the front; flames flicker each frame."""
    gy = GROUND - oy
    tick = time.ticks_ms() // 160
    for i, x in enumerate(range(6, 320, 24)):
        g.fillRect(x - 2, gy + 3, 5, 2, P["clay"])
        tall = 3 + ((tick + i * 3) % 3 == 0)
        g.fillTriangle(x - 1, gy + 3, x + 1, gy + 3, x, gy + 3 - tall, P["flame"])
        g.fillRect(x, gy + 2, 1, 1, P["flame_hot"])


def draw_people(g, oy):
    """Festival crowd walking past the stalls (umbrellas when it rains)."""
    gy = GROUND - oy
    if not people:
        step_people()
    wet = condition() in ("RAIN", "SHOWERS", "STORM", "DRIZZLE")
    lit = phase in ("NIGHT", "DUSK")
    for p in people:
        x = int(p[0])
        top = gy - 5
        stride = (int(p[0]) // 2) % 2
        g.fillRect(x, top, 2, 2, P["skin"])                         # head
        g.fillRect(x - 1, top + 2, 4, 3, P[p[2]])                   # shirt
        g.fillRect(x - 1, top + 5, 4, 2, P[p[3]])                   # longyi
        g.fillRect(x + stride, top + 7, 1, 1, P["skin"])            # walking feet
        if wet:
            g.fillTriangle(x - 3, top - 1, x + 4, top - 1, x + 1, top - 4, P[p[2]])
            g.fillRect(x + 1, top - 1, 1, 2, P["pole"])
        elif lit and p[4]:
            g.fillRect(x + (3 if p[1] > 0 else -2), top + 3, 1, 2, P["lan%d" % (p[4] % 3)])


def step_people():
    if not people:
        shirts = ("pp0", "pp1", "pp2", "pp3", "pp4", "pp5", "pp6")
        for i in range(9):
            people.append([random.randint(0, 319), random.choice((-1, 1)),
                           shirts[random.randint(0, 6)], shirts[random.randint(0, 6)],
                           random.randint(0, 3) // 2 * (i + 1)])
    for p in people:
        p[0] += p[1] * 0.35
        if p[0] < -4:
            p[0] = 323
        elif p[0] > 323:
            p[0] = -4


STARS = ((14, 4), (46, 12), (88, 6), (132, 15), (171, 3), (214, 10), (251, 5), (299, 13), (318, 2))


def draw_landscape(g, oy):
    base = STRIP_Y - oy
    for i in range(8):                                             # sky fade
        g.fillRect(0, base + i * 6, 320, 6, blend(T["bg"], T["haze"], i, 7))
    if phase == "NIGHT" and not is_overcast():                     # stars
        for sx, sy in STARS:
            g.fillRect(sx, base + sy, 1, 1, 0xFFFFFF)
    for c in clouds:                                               # clouds
        draw_cloud(g, int(c[0]), base + c[1], c[2])
    for a, pk, b in ((-20, 112, 140), (90, 108, 290), (220, 116, 360)):
        g.fillTriangle(a, GROUND - oy, (a + b) // 2, pk - oy, b, GROUND - oy, P["mount_far"])
    for a, pk, b in ((20, 126, 200), (170, 128, 340)):
        g.fillTriangle(a, GROUND - oy, (a + b) // 2, pk - oy, b, GROUND - oy, P["mount_near"])
    draw_raintree(g, 40, 12, 9, oy)                                # trees behind
    draw_raintree(g, 182, 10, 7, oy)
    draw_raintree(g, 280, 11, 8, oy)
    draw_palm(g, 10, 34, 2, oy)
    draw_palm(g, 128, 31, -1, oy)
    draw_palm(g, 310, 33, -2, oy)
    draw_thatbyinnyu(g, 88, oy)                                    # Thatbyinnyu (left)
    draw_shwedagon(g, 230, oy)                                     # Shwedagon (right)
    draw_market(g, oy)                                             # Nibbana market
    g.fillRect(0, GROUND - oy, 320, 6, P["earth"])
    for x in range(14, 320, 21):
        g.fillRect(x, GROUND - oy + 5, 3, 1, P["earth_dk"])
    draw_people(g, oy)
    draw_lamps(g, oy)


def draw_cloud(g, x, y, s):
    col = T["cloud"]
    g.fillCircle(x, y, 6 * s // 2, col)
    g.fillCircle(x + 7 * s // 2, y - 2, 8 * s // 2, col)
    g.fillCircle(x + 14 * s // 2, y, 6 * s // 2, col)
    g.fillRect(x, y, 14 * s // 2, 6 * s // 2, col)


# ---------------------------------------------------------
# Live effects: particles, clouds, fog, wind, lightning
# ---------------------------------------------------------
def effect_kind():
    cond = condition()
    if cond in ("RAIN", "SHOWERS", "STORM"): return "rain"
    if cond == "DRIZZLE": return "drizzle"
    if cond == "SNOW": return "snow"
    return "lantern"


def new_particle(kind, top=False):
    y = random.randint(-6, 0) if top else random.randint(0, GROUND - STRIP_Y - 4)
    x = random.randint(-20, 319)
    if kind == "lantern":                       # [x, y, rise speed, sway phase, colour]
        y = GROUND - STRIP_Y - random.randint(4, 10) if top else random.randint(0, GROUND - STRIP_Y - 8)
        return [random.randint(-10, 315), y, random.randint(15, 40) / 100,
                random.randint(0, 62) / 10, P["lan%d" % random.randint(0, 2)]]
    if kind == "snow":
        return [x, y, random.randint(4, 9) / 10, random.randint(0, 62) / 10, T["snow"]]
    if kind == "drizzle":
        return [x, y, random.randint(25, 35) / 10, 0, T["rain"]]
    return [x, y, random.randint(50, 70) / 10, 0, T["rain"]]       # rain


def reset_effects():
    global particles, clouds
    kind = effect_kind()
    cond = condition()
    count = {"lantern": 7, "snow": 22, "drizzle": 18, "rain": 30}[kind]
    if cond in ("SHOWERS", "STORM"):
        count = 40
    if kind == "lantern" and is_windy():
        count = 9
    particles = [new_particle(kind) for _ in range(count)]
    cover = weather["cloud"] or 0
    n = min(4, cover // 25) if cond not in ("RAIN", "SHOWERS", "STORM", "DRIZZLE", "OVERCAST") else 4
    clouds = [[random.randint(0, 300), random.randint(5, 18), random.randint(2, 3)] for _ in range(n)]


def step_effects():
    global flash
    step_people()
    kind = effect_kind()
    wind = min(3.0, (weather["wind"] or 0) * 0.25)               # sideways push
    floor = GROUND - STRIP_Y - 1
    for p in particles:
        if kind == "lantern":                                     # fire balloons rise and sway
            p[3] += 0.08
            p[0] += math.sin(p[3]) * 0.3 + wind * 0.5
            p[1] -= p[2]
            if p[1] < -10 or p[0] > 325:
                p[:] = new_particle(kind, top=True)
            continue
        if kind == "snow":
            p[3] += 0.18
            p[0] += math.sin(p[3]) * 0.8 + wind
        else:
            p[0] += wind * 0.6
        p[1] += p[2]
        if p[1] > floor or p[0] > 325:
            p[:] = new_particle(kind, top=True)
    for c in clouds:
        c[0] += 0.15 + wind * 0.2
        if c[0] > 330:
            c[0] = -40
    if condition() == "STORM" and flash == 0 and random.randint(0, 70) == 0:
        flash = 2


def draw_effects(g):
    global flash
    kind = effect_kind()
    for p in particles:
        x, y = int(p[0]), int(p[1])
        if kind == "lantern":
            r = 4 if y > 32 else (3 if y > 14 else 2)            # smaller as they rise away
            g.fillCircle(x, y, r, p[4])
            g.fillTriangle(x - r, y, x + r, y, x, y + r + 2, p[4])
            if phase in ("NIGHT", "DUSK"):
                g.fillRect(x - r // 2, y, r, 2, P["flame_hot"])    # paper glowing from inside
            g.fillRect(x, y + r + 2, 1, 2 if r > 2 else 1, P["flame"])
        elif kind == "snow":
            g.fillCircle(x, y, 1, p[4])
        elif kind == "drizzle":
            g.drawLine(x, y, x + 1, y + 2, p[4])
        else:
            g.drawLine(x, y, x + 2, y + 6, p[4])
    if condition() == "FOG":                                     # fog bands
        for yy in range(6, 34, 4):                                 # light mist over the hills
            g.drawLine(0, yy, 319, yy, T["fog"])
    if is_windy():                                                # wind streaks
        off = (time.ticks_ms() // 12) % 360
        for i, yy in enumerate((6, 14, 22)):
            sx = (off + i * 120) % 360 - 40
            g.drawLine(sx, yy, sx + 22, yy, T["fog"])
    if flash:                                                     # lightning
        if flash == 2:
            g.fillRect(0, 0, 320, SKY_LOCAL, 0xDCE0F5)
        bx = 150
        g.drawLine(bx, 0, bx - 6, 10, 0xFFF3A0)
        g.drawLine(bx - 6, 10, bx + 2, 12, 0xFFF3A0)
        g.drawLine(bx + 2, 12, bx - 8, 30, 0xFFF3A0)
        flash -= 1


def make_canvas():
    """Off-screen buffer for the animated strip.
    M5Stack Core (Basic/Gray) has no PSRAM, so the buffer can fail
    silently when memory is short. Check that it really works,
    try 16-bit first, then 8-bit (half the memory), else None."""
    for bpp in CANVAS_BPP:
        gc.collect()
        try:
            c = M5.Display.newCanvas(320, STRIP_H, bpp, False)
        except Exception as e:
            print("canvas", bpp, "error:", e)
            continue
        ok = c is not None
        try:
            ok = ok and c.width() == 320 and c.height() == STRIP_H
        except Exception:
            pass
        if ok:
            print("canvas", bpp, "bit OK")
            return c, bpp
        print("canvas", bpp, "bit: no memory")
        try:
            c.delete()
        except Exception:
            pass
    return None, 0


def render_strip():
    global canvas
    if canvas is None:
        return
    try:
        draw_landscape(canvas, STRIP_Y)
        draw_effects(canvas)
        canvas.push(0, STRIP_Y)
    except Exception as e:
        print("Canvas off:", e)
        canvas = None
        draw_landscape(M5.Display, 0)          # static scene instead


# ---------------------------------------------------------
# Static frame (redrawn when the theme changes)
# ---------------------------------------------------------
def draw_sky_panel():
    """Sun or moon (top right) with sunrise/sunset or moon phase."""
    M5.Display.fillRect(250, 24, 70, 80, T["bg"])
    cx, cy, r = 290, 46, 11
    if phase == "NIGHT":
        age = moon_age()
        M5.Display.fillCircle(cx, cy, r, 0xF3EBD2)
        f = age / 29.530588853
        if f < 0.5:
            dx = -int(2 * r * (f / 0.5))                          # waxing: shadow on the left
        else:
            dx = int(2 * r * (1 - (f - 0.5) / 0.5))               # waning: shadow on the right
        if abs(dx) < 2 * r:
            M5.Display.fillCircle(cx + dx, cy, r, T["bg"])
        M5.Display.drawCircle(cx, cy, r, 0x8A8FA8)
        text(moon_name(age), 254, 66, 1, T["skytext"])
        text("{:>3}% LIT".format(int((1 - math.cos(2 * math.pi * f)) / 2 * 100)), 254, 78, 1, T["skytext"])
    else:
        glow = 0xFDDDA0 if phase == "DAY" else 0xF7B38A
        M5.Display.fillCircle(cx, cy, 16, glow if not is_overcast() else T["bg"])
        M5.Display.fillCircle(cx, cy, r, 0xF7B32B if not is_overcast() else 0xE8D9A8)
        if is_overcast():                                          # cloud over the sun
            M5.Display.fillCircle(cx - 6, cy + 6, 7, T["cloud"])
            M5.Display.fillCircle(cx + 4, cy + 4, 9, T["cloud"])
    rise = (weather["rise"] or "")[11:16] or "--:--"
    sset = (weather["set"] or "")[11:16] or "--:--"
    text("RISE " + rise, 254, 90 if phase == "NIGHT" else 66, 1, T["skytext"])
    if phase != "NIGHT":
        text("SET  " + sset, 254, 78, 1, T["skytext"])


def draw_scene():
    M5.Display.clear(T["bg"])
    M5.Display.fillRect(0, 0, 320, 4, T["top"])
    text(PLACE, 8, 9, 1, T["skytext"])
    draw_sky_panel()
    if canvas:
        render_strip()
    else:
        draw_landscape(M5.Display, 0)
    for cx in (6, 164):
        M5.Display.fillRoundRect(cx, 164, 150, 72, 8, T["card"])
        M5.Display.drawRoundRect(cx, 164, 150, 72, 8, T["line"])
    text("TEMP", 16, 170, 1, T["label"], T["card"])
    text("LIVE SKY", 174, 170, 1, T["label"], T["card"])


# ---------------------------------------------------------
# Dynamic text
# ---------------------------------------------------------
def draw_status(t):
    blink = t[5] % 2 == 0
    if changed("live", (phase, blink)):
        M5.Display.fillCircle(116, 12, 3, T["warn"] if blink else T["bg"])
        text(("DEMO " if demo_idx >= 0 else "LIVE ") + "{:<5}".format(phase), 123, 9, 1, T["ink"])
    mm = mm_date(t) if t[0] >= 2024 else None
    if changed("mm", mm):
        M5.Display.fillRect(150, 20, 170, 9, T["bg"])
        if mm:
            name, day, full = mm
            gap = text_w(" ")
            x = 318 - text_w(name) - gap - text_w(day)               # right-aligned
            text(name, x, 21, 1, T["time"] if phase != "DUSK" else 0xFFD54A)
            text(day, x + text_w(name) + gap, 21, 1, T["amber"] if full else T["skytext"])
    if changed("wifi", wifi_ok):
        text("WiFi", 222, 9, 1, T["ok"] if wifi_ok else T["warn"])
    try:
        level = M5.Power.getBatteryLevel()
    except Exception:
        level = -1
    if changed("batt", level):
        M5.Display.fillRect(252, 6, 68, 14, T["bg"])
        if level < 0:
            return
        col = T["ok"] if level > 50 else (T["amber"] if level > 20 else T["warn"])
        M5.Display.drawRect(254, 7, 22, 11, T["ink"])
        M5.Display.fillRect(276, 10, 2, 5, T["ink"])
        M5.Display.fillRect(256, 9, (18 * level) // 100, 7, col)
        text("{:>3}%".format(level), 282, 9, 1, T["ink"])


def draw_clock(t):
    year, month, day, h, m, s, wd = t[0], t[1], t[2], t[3], t[4], t[5], t[6]
    synced = year >= 2024
    date_s = "{}.{:02d}.{:02d}  {}".format(year, month, day, DAYS[wd]) if synced else "----.--.--  ---"
    if changed("date", date_s):
        text(date_s, 20, 30, 2, T["ink"])
    hm = "{:02d}:{:02d}".format(h, m) if synced else "--:--"
    if changed("hm", hm):
        text(hm, 20, 56, 6, T["time"])
    ss = "{:02d}".format(s) if synced else "--"
    if changed("ss", ss):
        text(ss, 206, 80, 3, T["sec"])


def draw_icon(x, y):
    card = T["card"]
    M5.Display.fillRect(x - 14, y - 14, 30, 30, card)
    cond = condition()
    if cond == "CLEAR":
        if phase == "NIGHT":
            M5.Display.fillCircle(x, y, 8, 0xF3EBD2)
            M5.Display.fillCircle(x + 4, y - 3, 7, card)
        else:
            M5.Display.fillCircle(x, y, 8, 0xF7B32B)
    elif cond in ("CLOUDY", "OVERCAST", "FOG"):
        if cond == "CLOUDY":
            M5.Display.fillCircle(x + 5, y - 5, 6, 0xF7B32B if phase != "NIGHT" else 0xF3EBD2)
        M5.Display.fillCircle(x - 5, y + 2, 6, 0xA9B4C2)
        M5.Display.fillCircle(x + 4, y, 8, 0xA9B4C2)
    elif cond in ("RAIN", "DRIZZLE", "SHOWERS", "STORM"):
        M5.Display.fillCircle(x, y - 4, 7, 0xA9B4C2)
        for dx in (-6, 0, 6):
            M5.Display.drawLine(x + dx, y + 5, x + dx - 3, y + 12, 0x4A90C2)
        if cond == "STORM":
            M5.Display.drawLine(x + 2, y + 2, x - 2, y + 10, 0xF2C14E)
    elif cond == "SNOW":
        for dx, dy in ((0, 9), (8, 5), (8, -5)):
            M5.Display.drawLine(x - dx, y - dy, x + dx, y + dy, 0x6FA8DC)


def draw_weather():
    t = weather["temp"]
    temp_s = "{:.1f}C".format(t) if t is not None else "--C"
    if changed("temp", temp_s):
        M5.Display.fillRect(14, 184, 136, 30, T["card"])
        text(temp_s, 16, 186, 4, T["ink"], T["card"])
    hm = weather["hum"]
    hum_s = "HUMIDITY {}%".format(hm) if hm is not None else "HUMIDITY --%"
    if changed("hum", hum_s):
        dry = hm is not None and hm < 40
        M5.Display.fillRect(14, 220, 136, 10, T["card"])
        text(hum_s, 16, 222, 1, T["warn"] if dry else T["label"], T["card"])
    word = sky_word()
    if changed("sky", (word, phase)):
        draw_icon(190, 198)
        M5.Display.fillRect(208, 188, 104, 18, T["card"])
        text(word, 210, 190, 2, T["ink"], T["card"])
    w = weather["wind"]
    wind_s = ("WIND {:.1f}m/s".format(w) + ("  WINDY" if is_windy() else "")) if w is not None else "WIND --m/s"
    if changed("wind", wind_s):
        M5.Display.fillRect(172, 220, 140, 10, T["card"])
        text(wind_s, 174, 222, 1, T["warn"] if is_windy() else T["label"], T["card"])


# ---------------------------------------------------------
# Buttons:  A = refresh weather   B = brightness   C = animation on/off
# ---------------------------------------------------------
def handle_buttons(t):
    """A = back to LIVE + refresh weather
       B = brightness: bright / medium / eco (dim + animation off)
       C = DEMO: show the next sky (dawn, rain, storm, snow, fog, dusk, night...)"""
    global last_weather_ms, brightness_idx, animate, demo_idx, real_weather
    try:
        if BtnA.wasPressed():
            if demo_idx >= 0:
                demo_idx = -1
                if real_weather:
                    weather.update(real_weather)
            fetch_weather()
            last_weather_ms = time.ticks_ms()
            update_theme(t, force=True)
        if BtnB.wasPressed():
            brightness_idx = (brightness_idx + 1) % len(brightness_levels)
            M5.Display.setBrightness(brightness_levels[brightness_idx])
            animate = brightness_idx != len(brightness_levels) - 1
        if BtnC.wasPressed():
            if demo_idx < 0:
                real_weather = dict(weather)
            demo_idx = (demo_idx + 1) % len(DEMOS)
            weather.update(DEMOS[demo_idx][1])
            update_theme(t, force=True)
    except Exception as e:
        print("button error:", e)


# ---------------------------------------------------------
# Boot
# ---------------------------------------------------------
def setup():
    global last_weather_ms, last_ntp_ms, canvas
    M5.begin()
    global T
    T = {"bg": 0x0E1630, "label": 0xC9B79C, "ink": 0xE8ECF5, "ok": 0x6FCF97, "warn": 0xFF6B6B}
    M5.Display.clear(T["bg"])
    text("LIVE SKY CLOCK", 12, 14, 2, T["ink"])
    text("> connecting Wi-Fi...", 12, 50, 1.5, T["label"])
    if wifi_connect():
        text("[ Wi-Fi connected ]", 12, 72, 1.5, T["ok"])
        ok = sync_time()
        text("[ time synced ]" if ok else "[ time sync failed ]", 12, 94, 1.5, T["ok"] if ok else T["warn"])
    else:
        text("[ offline - check Wi-Fi ]", 12, 72, 1.5, T["warn"])
    text("> reading the live sky over", 12, 116, 1.5, T["label"])
    text("  " + PLACE + "...", 12, 134, 1.5, T["label"])
    fetch_weather()
    last_weather_ms = last_ntp_ms = time.ticks_ms()
    global CANVAS_DEPTH
    canvas, bpp = make_canvas()
    CANVAS_DEPTH = bpp or 16
    mode = ("ANIMATED {}-BIT".format(bpp)) if canvas else "STATIC (low memory)"
    text("> scene: " + mode, 12, 156, 1.5, T["ok"] if canvas else T["warn"])
    print("scene:", mode)
    time.sleep(2)
    update_theme(time.gmtime(time.time() + JST_OFFSET), force=True)


def loop():
    global last_weather_ms, last_ntp_ms, last_frame_ms
    M5.update()
    t = time.gmtime(time.time() + JST_OFFSET)
    handle_buttons(t)

    if changed("theme_min", t[4]):                     # check phase once a minute
        update_theme(t)

    draw_clock(t)
    draw_weather()
    draw_status(t)

    now = time.ticks_ms()
    if animate and canvas and time.ticks_diff(now, last_frame_ms) > FRAME_MS:
        step_effects()
        render_strip()
        last_frame_ms = now
    if demo_idx < 0 and time.ticks_diff(now, last_weather_ms) > WEATHER_EVERY_MS:
        fetch_weather()
        last_weather_ms = time.ticks_ms()
        update_theme(t)
    if time.ticks_diff(now, last_ntp_ms) > NTP_EVERY_MS:
        if wifi_connect():
            sync_time()
        last_ntp_ms = time.ticks_ms()

    time.sleep(0.02)


if __name__ == "__main__":
    setup()
    while True:
        loop()
