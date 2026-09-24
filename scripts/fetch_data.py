#!/usr/bin/env python3
"""
San Diego Bite Board -- data fetcher.

Runs on GitHub Actions, where outbound internet is open, and writes data.json
to the repo. Claude's cloud cannot reach NOAA or FishNotify directly, so this
robot does the fetching and hands the numbers back through GitHub.

Sources (all free, no API key):
  - NOAA CO-OPS  : exact tide predictions, station 9410170 (San Diego bay)
  - Open-Meteo   : wind, swell, water temp, sunrise/sunset
  - moon         : computed here (astronomy, deterministic)
"""
import json, math, datetime, urllib.request, urllib.parse

STATION = "9410170"
LAT, LON   = 32.7157, -117.1730     # San Diego bay
MLAT, MLON = 32.6700, -117.2700     # just offshore (Point Loma) for swell/SST
TZ = "America/Los_Angeles"

def get_json(url, tries=3):
    err = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "bite-board/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except Exception as e:
            err = e
    raise RuntimeError("fetch failed: %s (%s)" % (url, err))

# ---- today in Pacific time (offset good enough for picking the date) ----
now_utc = datetime.datetime.now(datetime.timezone.utc)
pt_off = -7 if 3 <= now_utc.month <= 11 else -8
now_pt = now_utc + datetime.timedelta(hours=pt_off)
d0 = now_pt.date()
begin = (d0 - datetime.timedelta(days=1)).strftime("%Y%m%d")
end   = (d0 + datetime.timedelta(days=8)).strftime("%Y%m%d")

# ---- 1) NOAA tides (the one source Claude cannot reach) ----
noaa = get_json(
    "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
    "?product=predictions&application=bite-board&datum=MLLW&station=%s"
    "&time_zone=lst_ldt&units=english&interval=hilo&format=json"
    "&begin_date=%s&end_date=%s" % (STATION, begin, end))
tides = [{"t": p["t"], "v": round(float(p["v"]), 2), "type": p["type"]}
         for p in noaa.get("predictions", [])]
if not tides:
    raise RuntimeError("NOAA returned no tide predictions")

# ---- 2) Open-Meteo land forecast (wind, sky, precip, sun) ----
q = urllib.parse.urlencode({
    "latitude": LAT, "longitude": LON,
    "daily": "weather_code,wind_speed_10m_max,precipitation_sum,sunrise,sunset",
    "wind_speed_unit": "mph", "precipitation_unit": "inch",
    "timezone": TZ, "forecast_days": 9})
land = get_json("https://api.open-meteo.com/v1/forecast?" + q)["daily"]

# ---- 3) Open-Meteo marine (swell, sea-surface temp) -- optional ----
swell_by, sst_by = {}, {}
try:
    mq = urllib.parse.urlencode({
        "latitude": MLAT, "longitude": MLON,
        "daily": "swell_wave_height_max", "hourly": "sea_surface_temperature",
        "length_unit": "imperial", "timezone": TZ, "forecast_days": 9})
    mar = get_json("https://marine-api.open-meteo.com/v1/marine?" + mq)
    md = mar.get("daily", {})
    swell_by = dict(zip(md.get("time", []), md.get("swell_wave_height_max", [])))
    hh = mar.get("hourly", {})
    tmp = {}
    for t, v in zip(hh.get("time", []), hh.get("sea_surface_temperature", [])):
        if v is None:
            continue
        tmp.setdefault(t[:10], []).append(v)
    for day, vals in tmp.items():
        c = sum(vals) / len(vals)
        sst_by[day] = round(c * 9 / 5 + 32, 1)   # marine SST is Celsius
except Exception as e:
    print("marine fetch skipped:", e)

# ---- 4) moon (computed) ----
def moon(dt):
    y, m, d = dt.year, dt.month, dt.day
    if m <= 2:
        y -= 1; m += 12
    a = y // 100; b = 2 - a + a // 4
    jd = int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + b - 1524.5
    age = (jd - 2451550.1) % 29.530588853
    illum = round((1 - math.cos(2 * math.pi * age / 29.530588853)) / 2 * 100)
    name = ("New" if age < 1.85 else "Waxing crescent" if age < 5.5 else
            "First quarter" if age < 9.2 else "Waxing gibbous" if age < 12.9 else
            "Full" if age < 16.6 else "Waning gibbous" if age < 20.3 else
            "Last quarter" if age < 24.0 else "Waning crescent")
    def hm(x):
        x %= 1440; h = x // 60; mm = x % 60
        ap = "AM" if h < 12 else "PM"; h12 = h % 12 or 12
        return "%d:%02d %s" % (h12, mm, ap)
    transit = (12 * 60 + 58 + int(round(age * 48.8))) % 1440
    return name, illum, hm(transit), hm(transit + 745)          # underfoot = transit + 12h25m

# ---- assemble per-day ----
days = []
for i in range(9):
    dt = d0 + datetime.timedelta(days=i)
    ds = dt.strftime("%Y-%m-%d")
    if ds not in land["time"]:
        continue
    j = land["time"].index(ds)
    nm, il, tr, uf = moon(dt)
    def g(key):
        return land[key][j]
    days.append({
        "date": ds,
        "dow": dt.strftime("%a"),
        "sunrise": (g("sunrise") or "")[11:16],
        "sunset": (g("sunset") or "")[11:16],
        "wind_mph": None if g("wind_speed_10m_max") is None else round(g("wind_speed_10m_max")),
        "swell_ft": None if swell_by.get(ds) is None else round(swell_by[ds], 1),
        "water_f": sst_by.get(ds),
        "weather_code": g("weather_code"),
        "precip_in": g("precipitation_sum"),
        "moon_phase": nm, "moon_illum": il,
        "moon_transit": tr, "moon_underfoot": uf,
    })

out = {
    "generated_at": now_utc.replace(microsecond=0).isoformat(),
    "station": STATION, "lat": LAT, "lon": LON,
    "note": "Fetched by GitHub Actions. Tides exact (NOAA), weather from Open-Meteo, moon computed.",
    "tides": tides, "days": days,
}
with open("data.json", "w") as f:
    json.dump(out, f, indent=2)
print("wrote data.json: %d tide extremes, %d days" % (len(tides), len(days)))
