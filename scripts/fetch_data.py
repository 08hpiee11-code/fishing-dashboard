#!/usr/bin/env python3
"""Fetch water level / dam storage / weather data for the tracked fishing
spots and append the reading to data/<id>.json, keeping only the last 30
days of history.

Data sources:
  - mlit_dam    : MLIT river.go.jp real-time dam CGI (DspDamData.exe)
  - mlit_river  : MLIT river.go.jp real-time water level CGI (DspWaterData.exe)
  - yamaguchi_dam: Yamaguchi prefecture dam observation site
  - oita_dam    : Oita prefecture dam observation site
Weather comes from Open-Meteo (no API key required).
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
RETENTION_DAYS = 30

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOCATIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locations.json")


def http_get(url, encoding="utf-8", timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (fishing-dashboard-bot)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode(encoding, errors="replace")


def fnum(s):
    if s is None:
        return None
    s = s.strip()
    if s in ("", "-", "$", "#"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_dt(date_s, time_s):
    """Parse 'YYYY/MM/DD' + 'HH:MM', tolerating the '24:00' notation some
    Japanese government sites use for midnight of the next day."""
    if time_s.strip().startswith("24:"):
        base = datetime.strptime(date_s, "%Y/%m/%d").replace(tzinfo=JST)
        minute = int(time_s.strip().split(":")[1])
        return base + timedelta(days=1, minutes=minute)
    return datetime.strptime(f"{date_s} {time_s}", "%Y/%m/%d %H:%M").replace(tzinfo=JST)


def empty_reading():
    return {
        "time": None,
        "water_level_m": None,
        "storage_rate_pct": None,
        "storage_volume_1000m3": None,
        "inflow_m3s": None,
        "outflow_m3s": None,
        "rainfall_mm": None,
    }


def fetch_mlit_dat_url(page_url):
    html = http_get(page_url, encoding="euc-jp")
    m = re.search(r'href="(/dat/dload/download/[^"]+\.dat)"', html)
    if not m:
        raise RuntimeError("dat download link not found on MLIT page")
    return "http://www1.river.go.jp" + m.group(1)


def fetch_mlit_dam(source_id):
    page_url = f"http://www1.river.go.jp/cgi-bin/DspDamData.exe?ID={source_id}&KIND=3&PAGE=0"
    dat_url = fetch_mlit_dat_url(page_url)
    dat = http_get(dat_url, encoding="shift_jis")
    last = None
    for line in dat.splitlines():
        if re.match(r"\d{4}/\d{2}/\d{2}", line):
            parts = line.split(",")
            if len(parts) >= 11 and fnum(parts[4]) is not None:
                last = parts
    if last is None:
        raise RuntimeError("no data rows found in dam dat file")
    dt = parse_dt(last[0], last[1])
    r = empty_reading()
    r.update(
        time=dt.isoformat(),
        rainfall_mm=fnum(last[2]),
        storage_volume_1000m3=fnum(last[4]),
        inflow_m3s=fnum(last[6]),
        outflow_m3s=fnum(last[8]),
        storage_rate_pct=fnum(last[10]),
    )
    return r


def fetch_mlit_river(source_id):
    page_url = f"http://www1.river.go.jp/cgi-bin/DspWaterData.exe?KIND=9&ID={source_id}"
    dat_url = fetch_mlit_dat_url(page_url)
    dat = http_get(dat_url, encoding="shift_jis")
    last = None
    for line in dat.splitlines():
        if re.match(r"\d{4}/\d{2}/\d{2}", line):
            parts = line.split(",")
            # some rows are pre-generated placeholders for times that
            # haven't happened yet, e.g. "2026/07/13,23:50,-,-"
            if len(parts) >= 3 and fnum(parts[2]) is not None:
                last = parts
    if last is None:
        raise RuntimeError("no data rows found in river dat file")
    dt = parse_dt(last[0], last[1])
    r = empty_reading()
    r.update(time=dt.isoformat(), water_level_m=fnum(last[2]))
    return r


ROW_RE = re.compile(
    r'<tr[^>]*>\s*<td[^>]*>(\d{4}/\d{2}/\d{2})<br\s*/?>\s*(\d{2}:\d{2})</td>'
    r'\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>'
    r'\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>'
)


def fetch_yamaguchi_dam(source_id):
    # The site only has data for timestamps that are already "settled";
    # querying the current minute returns an empty table, so back off
    # ~70 minutes and round down to the nearest 10-minute mark.
    target = datetime.now(JST) - timedelta(minutes=70)
    target = target.replace(minute=(target.minute // 10) * 10, second=0, microsecond=0)
    obsdt = target.strftime("%Y%m%d%H%M")
    url = f"https://y-bousai.pref.yamaguchi.lg.jp/sp/dam/spdmObserve.aspx?stncd={source_id}&obsdt={obsdt}"
    html = http_get(url, encoding="utf-8")
    rows = ROW_RE.findall(html)
    if not rows:
        raise RuntimeError("no data rows found on Yamaguchi dam page")
    last = rows[-1]
    dt = parse_dt(last[0], last[1])
    r = empty_reading()
    r.update(
        time=dt.isoformat(),
        water_level_m=fnum(last[2]),
        storage_rate_pct=fnum(last[3]),
        inflow_m3s=fnum(last[4]),
        outflow_m3s=fnum(last[5]),
    )
    return r


OITA_MAP_URL = (
    "https://river.pref.oita.jp/bousai/servlet/bousaiweb.model.servletBousaiSelectMap?"
    "unq=1&mnflg=0&tmgo=&vo=0&mty=0&rk=1&og10=0&og9=0&og8=0&og7=0&og6=0&og5=0&og4=0&og3=0&og2=0&og1=0"
    "&ost=0&omp=0&gm=0&go=0&gc=0&gw=0&gl=0&gn=0&gk5=0&gk4=0&gk3=0&gk2=0&gk1=0&gk=0&ga=4&sb=0&tk=0&tsw=0"
    "&tsk=0&it=0&st=0&cn=0&fn=0&tvm=0&vm=0&pg=1&sn=0&nw=1&no=0&mp=0&dk=4&sv=1&lod=0&nwg=1&id={id}&model=dam_model"
)


def fetch_oita_dam(source_id):
    url = OITA_MAP_URL.format(id=source_id)
    html = http_get(url, encoding="shift_jis")
    m_time = re.search(r"(\d{4})年(\d{2})月(\d{2})日(\d{2})時(\d{2})分", html)
    if m_time:
        y, mo, d, h, mi = (int(x) for x in m_time.groups())
        dt = datetime(y, mo, d, h, mi, tzinfo=JST)
    else:
        dt = datetime.now(JST)
    m_level = re.search(r'貯水位EL\..*?class="dam-data">([\d.]+)\[m\]', html, re.S)
    r = empty_reading()
    r.update(time=dt.isoformat(), water_level_m=fnum(m_level.group(1)) if m_level else None)
    return r


FETCHERS = {
    "mlit_dam": fetch_mlit_dam,
    "mlit_river": fetch_mlit_river,
    "yamaguchi_dam": fetch_yamaguchi_dam,
    "oita_dam": fetch_oita_dam,
}


def fetch_weather(lat, lon):
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        "&current=temperature_2m,precipitation,weather_code,wind_speed_10m"
        "&timezone=Asia%2FTokyo"
    )
    raw = http_get(url, encoding="utf-8")
    data = json.loads(raw)
    cur = data.get("current", {})
    return {
        "temp_c": cur.get("temperature_2m"),
        "precip_mm": cur.get("precipitation"),
        "weather_code": cur.get("weather_code"),
        "wind_speed_ms": cur.get("wind_speed_10m"),
    }


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def prune(readings, days=RETENTION_DAYS):
    cutoff = datetime.now(JST) - timedelta(days=days)
    kept = []
    for r in readings:
        try:
            t = datetime.fromisoformat(r["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if t >= cutoff:
            kept.append(r)
    return kept


def main():
    locations = load_json(LOCATIONS_FILE, [])
    os.makedirs(DATA_DIR, exist_ok=True)
    any_success = False

    for loc in locations:
        loc_id = loc["id"]
        data_path = os.path.join(DATA_DIR, f"{loc_id}.json")
        record = load_json(data_path, {"id": loc_id, "name": loc["name"], "type": loc["type"],
                                        "lat": loc["lat"], "lon": loc["lon"], "readings": []})
        record["name"] = loc["name"]
        record["type"] = loc["type"]
        record["lat"] = loc["lat"]
        record["lon"] = loc["lon"]

        reading = None
        try:
            fetcher = FETCHERS[loc["source"]]
            reading = fetcher(loc["source_id"])
        except Exception as e:
            print(f"[warn] {loc_id}: failed to fetch site data: {e}", file=sys.stderr)

        weather = None
        try:
            weather = fetch_weather(loc["lat"], loc["lon"])
        except Exception as e:
            print(f"[warn] {loc_id}: failed to fetch weather: {e}", file=sys.stderr)

        if reading is None and weather is None:
            print(f"[warn] {loc_id}: skipped, no data at all this run", file=sys.stderr)
            continue

        if reading is None:
            reading = empty_reading()
            reading["time"] = datetime.now(JST).isoformat()
        reading["weather"] = weather

        record["readings"].append(reading)
        record["readings"] = prune(record["readings"])
        record["readings"].sort(key=lambda r: r["time"])
        save_json(data_path, record)
        any_success = True
        print(f"[ok] {loc_id}: {reading['time']} water_level_m={reading.get('water_level_m')} "
              f"storage_rate_pct={reading.get('storage_rate_pct')}")

    index = {
        "updated_at": datetime.now(JST).isoformat(),
        "locations": [{"id": l["id"], "name": l["name"], "type": l["type"]} for l in locations],
    }
    save_json(os.path.join(DATA_DIR, "index.json"), index)

    if not any_success:
        sys.exit(1)


if __name__ == "__main__":
    main()
