"""Open-Meteo 天气 MCP Server（stdio）。

给销量预测 Agent 提供两类数据：
- 未来天气预报（forecast API，最多 16 天）→ 作为 Chronos-2 的 future_covariates
- 历史实况（archive API）→ 演示时"未来"日期已过去，也能拿到当天真实天气

Open-Meteo 免费、无需 API Key：https://open-meteo.com/
天气现象用 WMO 代码映射为中文类别，与客户销售数据里的"天气"列口径一致（晴 / 多云 / 阴 / 小雨 / 中雨 / 大雨 …）。
"""
import datetime as dt
import logging

import requests
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s weather-mcp %(message)s")
logger = logging.getLogger(__name__)

mcp = FastMCP("open-meteo-weather")
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
DAILY_VARS = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum"
TZ = "Asia/Shanghai"

# WMO 4677 天气代码 → 中文类别（与销售数据"天气"列对齐）
WMO_CN = {0: "晴", 1: "晴", 2: "多云", 3: "阴", 45: "雾", 48: "雾",
          51: "小雨", 53: "小雨", 55: "小雨", 56: "小雨", 57: "小雨",
          61: "小雨", 63: "中雨", 65: "大雨", 66: "中雨", 67: "大雨",
          71: "小雪", 73: "中雪", 75: "大雪", 77: "小雪",
          80: "小雨", 81: "中雨", 82: "大雨", 85: "小雪", 86: "大雪",
          95: "雷阵雨", 96: "雷阵雨", 99: "雷阵雨"}
_geo_cache: dict[str, dict] = {}


def _geocode(city: str) -> dict:
    if city not in _geo_cache:
        r = requests.get(GEOCODE_URL, params={"name": city, "count": 1, "language": "zh"}, timeout=20)
        r.raise_for_status()
        results = r.json().get("results") or []
        if not results:
            raise ValueError(f"找不到城市：{city}")
        g = results[0]
        _geo_cache[city] = {"name": g["name"], "admin1": g.get("admin1", ""), "country": g.get("country", ""),
                            "latitude": g["latitude"], "longitude": g["longitude"]}
    return _geo_cache[city]


def _fetch(url: str, lat: float, lon: float, start: dt.date, end: dt.date) -> list[dict]:
    r = requests.get(url, params={"latitude": lat, "longitude": lon, "start_date": start.isoformat(),
                                  "end_date": end.isoformat(), "daily": DAILY_VARS, "timezone": TZ}, timeout=30)
    r.raise_for_status()
    d = r.json()["daily"]
    rows = []
    for i, day in enumerate(d["time"]):
        code = d["weather_code"][i]
        rows.append({"date": day, "weather": WMO_CN.get(code, "多云") if code is not None else None, "weather_code": code,
                     "temp_low": d["temperature_2m_min"][i], "temp_high": d["temperature_2m_max"][i],
                     "precip_mm": d["precipitation_sum"][i]})
    return rows


@mcp.tool()
def geocode(city: str) -> dict:
    """查询城市的经纬度（Open-Meteo 地理编码）。

    Args:
        city: 城市名，中文或英文，如 "西安"
    """
    return _geocode(city)


@mcp.tool()
def daily_weather(city: str, start_date: str, end_date: str) -> dict:
    """查询某城市一段日期的逐日天气：天气类别（晴/多云/阴/小雨…）、最低最高气温、降水量。
    日期在今天之后用预报（最多未来 16 天），在今天之前用历史实况，可以混合。

    Args:
        city: 城市名，如 "西安"
        start_date: 开始日期 YYYY-MM-DD
        end_date: 结束日期 YYYY-MM-DD（含）
    """
    g = _geocode(city)
    start, end = dt.date.fromisoformat(start_date), dt.date.fromisoformat(end_date)
    today = dt.date.today()
    archive_end = today - dt.timedelta(days=3)   # archive 通常滞后 2 天
    rows, sources = [], []
    if start <= archive_end:
        rows += _fetch(ARCHIVE_URL, g["latitude"], g["longitude"], start, min(end, archive_end))
        sources.append("archive")
    if end > archive_end:
        fc_start = max(start, archive_end + dt.timedelta(days=1))
        fc_end = min(end, today + dt.timedelta(days=15))
        if fc_start <= fc_end:
            # forecast API 用 past_days/forecast_days 表达范围，这里直接用 start_date/end_date（同样支持）
            rows += _fetch(FORECAST_URL, g["latitude"], g["longitude"], fc_start, fc_end)
            sources.append("forecast")
    logger.info(f"daily_weather city={g['name']} {start_date}~{end_date} rows={len(rows)} source={'+'.join(sources)}")
    return {"city": g["name"], "latitude": g["latitude"], "longitude": g["longitude"],
            "source": "+".join(sources) or "none", "days": rows}


if __name__ == "__main__":
    mcp.run(transport="stdio")
