#!/usr/bin/env python3
"""
Taco Weather Edge Scanner — METAR/TAF data vs Polymarket weather markets.
Fetches real-time aviation weather data and compares against market odds.
"""
import requests
import json
from datetime import datetime, timezone

# Aviation weather API (free, public, updated every 1-3 hours)
METAR_API = "https://aviationweather.gov/api/data/metar"
TAF_API = "https://aviationweather.gov/api/data/taf"

# Major city ICAO codes for weather markets
CITY_STATIONS = {
    "New York": ["KJFK", "KLGA", "KEWR"],
    "Los Angeles": ["KLAX"],
    "Chicago": ["KORD", "KMDW"],
    "Miami": ["KMIA"],
    "Houston": ["KIAH"],
    "Phoenix": ["KPHX"],
    "Philadelphia": ["KPHL"],
    "San Antonio": ["KSAT"],
    "San Diego": ["KSAN"],
    "Dallas": ["KDFW"],
    "Washington DC": ["KDCA", "KIAD"],
    "Denver": ["KDEN"],
    "Seattle": ["KSEA"],
    "Boston": ["KBOS"],
    "Atlanta": ["KATL"],
    "Minneapolis": ["KMSP"],
    "Detroit": ["KDTW"],
}

def get_metar(stations):
    """Fetch current METAR observations."""
    ids = ",".join(stations)
    try:
        r = requests.get(METAR_API, params={"ids": ids, "format": "json"}, timeout=10)
        return r.json() if r.status_code == 200 else []
    except:
        return []

def get_taf(stations):
    """Fetch TAF forecasts (6-30 hour outlook)."""
    ids = ",".join(stations)
    try:
        r = requests.get(TAF_API, params={"ids": ids, "format": "json"}, timeout=10)
        return r.json() if r.status_code == 200 else []
    except:
        return []

def parse_metar_temp(metar_data):
    """Extract temperature from METAR data."""
    results = {}
    for m in metar_data:
        station = m.get("icaoId", "")
        temp_c = m.get("temp")
        if temp_c is not None:
            temp_f = temp_c * 9/5 + 32
            results[station] = {
                "temp_c": temp_c,
                "temp_f": round(temp_f, 1),
                "obs_time": m.get("obsTime", ""),
                "raw": m.get("rawOb", ""),
                "wind_speed": m.get("wspd"),
                "wind_gust": m.get("wgst"),
                "visibility": m.get("visib"),
                "wx": m.get("wxString", ""),
                "clouds": m.get("clouds", []),
            }
    return results

def scan_polymarket_weather():
    """Search Polymarket for active weather markets."""
    weather_kw = ['temperature', 'fahrenheit', 'celsius', 'degrees', 'rain', 
                  'snow', 'precipitation', 'weather high', 'weather low',
                  'above degrees', 'below degrees']
    
    found = []
    for kw in weather_kw:
        try:
            r = requests.get("https://gamma-api.polymarket.com/markets", params={
                "limit": 20, "active": "true", "_q": kw
            }, timeout=10)
            for m in r.json():
                vol = float(m.get('volume24hr', 0) or 0)
                if vol > 100:
                    found.append(m)
        except:
            continue
    
    # Deduplicate
    seen = set()
    unique = []
    for m in found:
        mid = m.get('id', '')
        if mid not in seen:
            seen.add(mid)
            unique.append(m)
    
    return unique

def print_weather_report():
    """Print current weather data for all tracked cities."""
    all_stations = []
    station_city = {}
    for city, stations in CITY_STATIONS.items():
        all_stations.extend(stations)
        for s in stations:
            station_city[s] = city
    
    print(f"\n{'='*70}")
    print(f"🌤️  TACO WEATHER INTELLIGENCE — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*70}\n")
    
    # Get current conditions
    metar = get_metar(all_stations)
    temps = parse_metar_temp(metar)
    
    print("Current Conditions (METAR):\n")
    for station, data in sorted(temps.items(), key=lambda x: station_city.get(x[0], '')):
        city = station_city.get(station, "Unknown")
        wx = f" | {data['wx']}" if data['wx'] else ""
        wind = f" | Wind: {data['wind_speed']}kt" if data['wind_speed'] else ""
        print(f"  {city:15s} ({station}): {data['temp_f']:5.1f}°F / {data['temp_c']:5.1f}°C{wx}{wind}")
    
    # Get forecasts
    print(f"\n{'─'*70}")
    print("TAF Forecasts (next 6-30 hours):\n")
    taf = get_taf(list(CITY_STATIONS.values())[0])  # Just NYC for now
    for t in taf[:3]:
        print(f"  {t.get('icaoId', '')}: {t.get('rawTAF', '')[:120]}")
    
    # Check Polymarket
    print(f"\n{'─'*70}")
    print("Polymarket Weather Markets:\n")
    markets = scan_polymarket_weather()
    if markets:
        for m in markets:
            prices = json.loads(m.get('outcomePrices', '[]'))
            yes_p = float(prices[0]) if prices else None
            vol = float(m.get('volume24hr', 0) or 0)
            print(f"  YES:{yes_p:.2f} | Vol:${vol:,.0f} | {m['question'][:70]}")
    else:
        print("  No active weather markets found. Will keep scanning.")
    
    print(f"\n{'─'*70}")
    print("Strategy: When weather markets appear, compare METAR/TAF data")
    print("against market odds. If real weather data contradicts the odds, trade.")

if __name__ == "__main__":
    print_weather_report()
