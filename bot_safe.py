import asyncio
import json
import os
import re
import ssl
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
import aiohttp
from aiohttp_socks import ProxyConnector

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DATA_FILE = "encar_bot_data.json"
MAX_AD_AGE_HOURS = 48

ENCAR_QUERY = (
    "(And.Price.range(..3000)._.Hidden.N._.(Or.Separation.F._.Separation.B.)_."
    "SellType.일반._.FuelType.가솔린._.Mileage.range(..90000)._.Year.range(202301..202402)._."
    "(C.CarType.Y._.(C.Manufacturer.기아._.(C.ModelGroup.K5._."
    "(C.Model.더 뉴 K5 3세대._.BadgeGroup.가솔린 2000cc.)))))"
)
ENCAR_SORT = "ModifiedDate"
# =============================================

TG_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
KST = timezone(timedelta(hours=9))

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://car.encar.com/",
    "Origin": "https://car.encar.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

PROXY_LIST_URLS = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=2500&country=KR,JP,RU,KZ,SG,HK,TW",
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=2500&country=KR,JP,RU,KZ,SG,HK,TW",
]


def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("subscribers", [])
                data.setdefault("seen_ids", [])
                data.setdefault("last_update_id", 0)
                data.setdefault("working_proxy", "")
                return data
        except Exception:
            pass
    return {"subscribers": [], "seen_ids": [], "last_update_id": 0, "working_proxy": ""}


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def format_year(raw_year) -> str:
    s = str(raw_year)
    return f"{s[:4]}.{s[4:]}" if len(s) == 6 else s


def parse_encar_datetime_obj(raw_val) -> datetime | None:
    if not raw_val:
        return None
    if isinstance(raw_val, (int, float)):
        ts = raw_val / 1000.0 if raw_val > 1e11 else float(raw_val)
        try:
            return datetime.fromtimestamp(ts, tz=KST)
        except Exception:
            return None

    s = str(raw_val).strip()
    iso_match = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T\s](\d{2}):(\d{2})(?::(\d{2}))?", s)
    if iso_match:
        yyyy, mm, dd, hh, mins, sec = iso_match.groups()
        return datetime(int(yyyy), int(mm), int(dd), int(hh), int(mins), int(sec or 0), tzinfo=KST)

    digits = re.sub(r"\D", "", s)
    if len(digits) >= 12:
        return datetime(
            int(digits[:4]), int(digits[4:6]), int(digits[6:8]),
            int(digits[8:10]), int(digits[10:12]), tzinfo=KST
        )
    if len(digits) == 8:
        return datetime(int(digits[:4]), int(digits[4:6]), int(digits[6:8]), 0, 0, tzinfo=KST)
    return None


def format_dt(dt: datetime | None) -> str | None:
    if not dt:
        return None
    return dt.strftime("%d.%m.%Y") if (dt.hour == 0 and dt.minute == 0) else dt.strftime("%d.%m.%Y %H:%M")


def extract_photo_url(item: dict) -> str | None:
    photo_path = None
    photos = item.get("Photos")
    if isinstance(photos, list) and photos:
        main_photo = next((p for p in photos if p.get("type") == "001"), photos[0])
        photo_path = main_photo.get("location") or main_photo.get("path")

    if not photo_path:
        raw_photo = item.get("Photo")
        if isinstance(raw_photo, str) and raw_photo.strip():
            raw_photo = raw_photo.strip()
            photo_path = raw_photo if raw_photo.lower().endswith((".jpg", ".jpeg", ".png")) else f"{raw_photo}001.jpg"

    if not photo_path:
        return None
    if photo_path.startswith("http"):
        return photo_path
    if not photo_path.startswith("/"):
        photo_path = "/" + photo_path
    return f"https://ci.encar.com{photo_path}?impolicy=heightRate&rh=600&cw=800&cg=Center"


def parse_api_items(items: list) -> list[dict]:
    ads, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        car_id = str(item.get("Id", "")).strip()
        if not car_id or car_id in seen:
            continue
        seen.add(car_id)

        full_title = " ".join(filter(None, [
            item.get("Manufacturer", "Kia"),
            item.get("Model", "The New K5"),
            item.get("Badge", ""),
            item.get("BadgeDetail", "")
        ]))
        mod_dt = parse_encar_datetime_obj(item.get("ModifiedDate") or item.get("MobileModifiedDate"))

        ads.append({
            "id": car_id,
            "title": full_title,
            "year": format_year(item.get("Year", "2023")),
            "mileage": item.get("Mileage", "Н/Д"),
            "price": item.get("Price", "Н/Д"),
            "region": item.get("OfficeCityState", "Корея"),
            "first_dt": None,
            "mod_dt": mod_dt,
            "listed_date": format_dt(mod_dt) or "Не указана",
            "modified_date": format_dt(mod_dt),
            "photo": extract_photo_url(item),
            "mobile_link": f"https://car.encar.com/detail/car/{car_id}",
            "pc_link": f"https://fem.encar.com/cars/detail/{car_id}"
        })
    return ads


async def fetch_json_via_web_gateways(session: aiohttp.ClientSession, target_url: str) -> dict | None:
    """Способ 1: Безопасные веб-шлюзы (обходят гео-блок США без VPN)."""
    encoded = urllib.parse.quote(target_url, safe="")
    gateways = [
        target_url,
        f"https://api.codetabs.com/v1/proxy?quest={encoded}",
        f"https://api.allorigins.win/raw?url={encoded}",
    ]
    for gw in gateways:
        try:
            async with session.get(gw, headers=HEADERS, timeout=10) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    if "SearchResults" in text or "manage" in text or "registDateTime" in text:
                        return json.loads(text)
        except Exception:
            pass
    return None


async def try_proxy_request(proxy_url: str, target_url: str) -> dict | None:
    try:
        conn = ProxyConnector.from_url(proxy_url, ssl=SSL_CONTEXT, rdns=True)
        async with aiohttp.ClientSession(connector=conn, headers=HEADERS) as s:
            async with s.get(target_url, timeout=6) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    if "SearchResults" in text or "manage" in text:
                        return json.loads(text)
    except Exception:
        pass
    return None


async def fetch_encar_ads(session: aiohttp.ClientSession, data_store: dict, limit: int = 80) -> list[dict]:
    encoded_q = urllib.parse.quote(ENCAR_QUERY, safe="()._~")
    nocache = int(time.time() * 1000)
    sr = urllib.parse.quote(f"|{ENCAR_SORT}|0|{limit}", safe="")
    api_url = f"https://api.encar.com/search/car/list/general?count=true&q={encoded_q}&sr={sr}&_={nocache}"

    # 1. Сначала пробуем быстрые веб-шлюзы
    gw_data = await fetch_json_via_web_gateways(session, api_url)
    if gw_data and gw_data.get("SearchResults"):
        return parse_api_items(gw_data["SearchResults"])

    # 2. Если шлюзы заняты — пробуем сохраненный или новый азиатский прокси
    candidates = []
    if data_store.get("working_proxy"):
        candidates.append(data_store["working_proxy"])

    for src in PROXY_LIST_URLS:
        try:
            proto = "socks5" if "socks5" in src else "http"
            async with session.get(src, timeout=5) as r:
                if r.status == 200:
                    for line in (await r.text()).splitlines()[:25]:
                        if ":" in line:
                            candidates.append(f"{proto}://{line.strip()}")
        except Exception:
            pass

    for i in range(0, len(candidates), 8):
        batch = candidates[i:i + 8]
        results = await asyncio.gather(*(try_proxy_request(p, api_url) for p in batch))
        for p, res in zip(batch, results):
            if res and res.get("SearchResults"):
                data_store["working_proxy"] = p
                return parse_api_items(res["SearchResults"])

    return []


async def enrich_ad_dates(session: aiohttp.ClientSession, ad: dict, proxy_url: str):
    car_id = ad.get("id")
    if not car_id:
        return
    detail_url = f"https://api.encar.com/v1/readside/vehicle/{car_id}"
    data = await fetch_json_via_web_gateways(session, detail_url)
    if not data and proxy_url:
        data = await try_proxy_request(proxy_url, detail_url)

    if data:
        manage = data.get("manage") or {}
        raw_first = (
            manage.get("firstAdvertisedDateTime")
            or manage.get("registDateTime")
            or data.get("registDateTime")
        )
        raw_mod = manage.get("modifyDateTime") or data.get("modifyDateTime")
        first_dt = parse_encar_datetime_obj(raw_first)
        mod_dt = parse_encar_datetime_obj(raw_mod)
        if first_dt:
            ad["first_dt"] = first_dt
            ad["listed_date"] = format_dt(first_dt)
        if mod_dt:
            ad["mod_dt"] = mod_dt
            ad["modified_date"] = format_dt(mod_dt)


def is_truly_fresh_ad(ad: dict) -> bool:
    first_dt = ad.get("first_dt")
    if not first_dt:
        return True
    age_hours = (datetime.now(KST) - first_dt).total_seconds() / 3600.0
    return age_hours <= MAX_AD_AGE_HOURS


def format_ad_message(ad: dict, is_new: bool = True) -> str:
    header = "🔔 <b>Новое объявление на Encar!</b>" if is_new else "🚗 <b>Самое свежее объявление:</b>"
    price_manwon = ad["price"]
    price_krw = price_manwon * 10000 if isinstance(price_manwon, (int, float)) else 0
    price_str = f"{price_manwon:,} 만원 (~{price_krw:,} ₩)".replace(",", " ") if price_krw else str(price_manwon)
    mileage_str = f"{ad['mileage']:,} км".replace(",", " ") if isinstance(ad["mileage"], (int, float)) else str(ad["mileage"])

    listed_date = ad.get("listed_date", "Не указана")
    modified_date = ad.get("modified_date")
    date_block = f"🕒 Выставлен на сайт: <b>{listed_date}</b>"
    if modified_date and modified_date != listed_date:
        date_block += f"\n🔄 Обновлено дилером: {modified_date}"

    return (
        f"{header}\n\n"
        f"📌 <b>{ad['title']}</b>\n"
        f"📅 Год/месяц авто: <b>{ad['year']}</b>\n"
        f"🛣 Пробег: <b>{mileage_str}</b>\n"
        f"💰 Цена: <b>{price_str}</b>\n"
        f"📍 Регион: {ad['region']}\n"
        f"{date_block}\n\n"
        f"📱 <a href='{ad['mobile_link']}'>Мобильная версия</a>\n"
        f"💻 <a href='{ad['pc_link']}'>Компьютерная версия (история и кузов)</a>"
    )


async def tg_send_message(session: aiohttp.ClientSession, chat_id: int, text: str):
    await session.post(
        f"{TG_API_URL}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=15
    )


async def send_ad_card(session: aiohttp.ClientSession, chat_id: int, ad: dict, proxy_url: str, is_new: bool = True):
    if not ad.get("first_dt"):
        await enrich_ad_dates(session, ad, proxy_url)
    caption = format_ad_message(ad, is_new=is_new)

    if ad.get("photo"):
        try:
            proxy_photo = f"https://wsrv.nl/?url={urllib.parse.quote(ad['photo'], safe='')}"
            async with session.get(proxy_photo, timeout=10) as r:
                if r.status == 200:
                    img_bytes = await r.read()
                    form = aiohttp.FormData()
                    form.add_field("chat_id", str(chat_id))
                    form.add_field("caption", caption)
                    form.add_field("parse_mode", "HTML")
                    form.add_field("photo", img_bytes, filename=f"encar_{ad['id']}.jpg", content_type="image/jpeg")
                    async with session.post(f"{TG_API_URL}/sendPhoto", data=form, timeout=20) as resp:
                        if resp.status == 200:
                            return
        except Exception:
            pass

    await tg_send_message(session, chat_id, caption)


async def main():
    if not BOT_TOKEN:
        print("ОШИБКА: Секрет BOT_TOKEN не задан!")
        return

    data = load_data()
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=SSL_CONTEXT)) as session:
        await session.get(f"{TG_API_URL}/deleteWebhook")
        ads = await fetch_encar_ads(session, data, limit=80)
        active_proxy = data.get("working_proxy", "")

        # 1. Проверяем команды (/start, /check)
        offset = data.get("last_update_id", 0)
        try:
            async with session.get(f"{TG_API_URL}/getUpdates", params={"offset": offset, "timeout": 2}, timeout=10) as resp:
                if resp.status == 200:
                    updates = (await resp.json()).get("result", [])
                    for upd in updates:
                        data["last_update_id"] = upd["update_id"] + 1
                        msg = upd.get("message") or {}
                        text = (msg.get("text") or "").strip().lower()
                        chat_id = msg.get("chat", {}).get("id")
                        if not chat_id or not text:
                            continue

                        if chat_id not in data["subscribers"]:
                            data["subscribers"].append(chat_id)

                        if "start" in text:
                            await tg_send_message(
                                session,
                                chat_id,
                                "✅ <b>Безопасный облачный мониторинг Encar 24/7 включен!</b>\n\n"
                                "Бот проверяет сайт короткими циклами без риска блокировки.\n"
                                "Отправьте /check для получения 3 самых свежих авто."
                            )
                        elif "check" in text or "chek" in text:
                            if not ads:
                                await tg_send_message(
                                    session,
                                    chat_id,
                                    "⚠️ В этом цикле шлюз Encar был занят. Повторю при следующем плановом запуске."
                                )
                            else:
                                sample = ads[:10]
                                await asyncio.gather(*(enrich_ad_dates(session, a, active_proxy) for a in sample))
                                min_dt = datetime(2000, 1, 1, tzinfo=KST)
                                newest = sorted(
                                    sample,
                                    key=lambda a: a.get("first_dt") or a.get("mod_dt") or min_dt,
                                    reverse=True
                                )[:3]
                                for ad in newest:
                                    await send_ad_card(session, chat_id, ad, active_proxy, is_new=False)
        except Exception as e:
            print(f"Ошибка Telegram: {e}")

        # 2. Авторассылка новых авто
        if ads:
            if not data["seen_ids"]:
                data["seen_ids"] = [ad["id"] for ad in ads]
            elif data["subscribers"]:
                unseen = []
                for ad in ads:
                    if ad["id"] not in data["seen_ids"]:
                        unseen.append(ad)
                        data["seen_ids"].append(ad["id"])
                if unseen:
                    data["seen_ids"] = data["seen_ids"][-3000:]
                    for ad in reversed(unseen):
                        await enrich_ad_dates(session, ad, active_proxy)
                        if is_truly_fresh_ad(ad):
                            for sub_id in data["subscribers"]:
                                await send_ad_card(session, sub_id, ad, active_proxy, is_new=True)

        save_data(data)


if __name__ == "__main__":
    asyncio.run(main())
