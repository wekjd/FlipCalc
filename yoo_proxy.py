"""Локальный сервер FlipCalc: прокси к Yoomarket + приложение + Telegram-боты.

Запуск: двойной клик по ЗАПУСК.bat (или: python -m uvicorn yoo_proxy:app --port 8000).
Сервер слушает только этот компьютер (127.0.0.1). Данные лежат в %APPDATA%\\FlipCalc у каждого пользователя свои
и не уходят никуда, кроме Yoomarket и Telegram."""
import asyncio
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

BASE = "https://api.yoo.market/integration/v1"
GET_OK = re.compile(r"^(check|balance|orders|orders/\d+|ads|ads/\d+|categories|categories/\d+/filters|values/labels)$")
POST_OK = re.compile(r"^(ads|media|ads/\d+/publish|ads/\d+/items)$")
HTML = sorted(Path(__file__).parent.glob("FlipCalc*.html"))[-1]  # самая свежая версия в папке
esc = html.escape
TITLES: dict = {}
ST = {"created": "🆕 Новый заказ (не оплачен)", "wait": "💰 Заказ оплачен — ждёт вас", "success": "🏁 Заказ завершён",
      "failed": "❌ Заказ не удался", "refunded": "↩️ Возврат по заказу"}
MENU = {"keyboard": [[{"text": "💰 Баланс"}, {"text": "📦 Заказы"}], [{"text": "📊 Сегодня"}, {"text": "📉 Остатки"}],
                     [{"text": "👤 Профиль"}, {"text": "💸 Вывод"}]], "resize_keyboard": True}
CMD = {"💰 Баланс": "/balance", "📦 Заказы": "/orders", "📊 Сегодня": "/today", "📉 Остатки": "/stock",
       "👤 Профиль": "/profile", "💸 Вывод": "/withdraw"}


def _dir():
    base = os.environ.get("APPDATA")
    p = (Path(base) / "FlipCalc") if base else Path(__file__).parent / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


DIR = _dir()
DATA, STATE = DIR / "flipcalc_data.json", DIR / "state.json"
_old = Path(__file__).with_name("flipcalc_data.json")
if _old.exists() and not DATA.exists():
    shutil.move(str(_old), str(DATA))  # токены не лежат рядом с программой
try:
    SD = json.loads(STATE.read_text("utf-8"))
except Exception:
    SD = {}


def _load():
    try:
        d = json.loads(DATA.read_text("utf-8"))
    except Exception:
        d = {}
    d.pop("license", None)  # лицензий больше нет
    for k, v in (("seen", {}), ("last_msg", {}), ("map", {}), ("bots", []), ("reminded", [])):
        d.setdefault(k, v)
    if d.get("bot") and not d["bots"]:  # переход со старого формата с одним ботом
        d["bots"] = [{"token": d.pop("bot"), "name": d.pop("bot_name", ""), "owner": d.pop("owner", None),
                      "code": d.pop("code", None), "offset": d.pop("offset", 0)}]
    return d


D = _load()


def save():
    tmp = DATA.with_suffix(".tmp")
    tmp.write_text(json.dumps(D, ensure_ascii=False), "utf-8")
    tmp.replace(DATA)


def active_bots():
    return D["bots"]


bid = lambda b: b["token"].split(":")[0]


# ---------------- Yoomarket и Telegram ----------------
def money(x):
    try:
        return f"{float(x):.2f}"
    except Exception:
        return "—"


async def ym(c, method, path, **kw):
    r = await c.request(method, f"{BASE}/{path}", headers={"Authorization": "Bearer " + D["ym"], "Accept": "application/json"}, **kw)
    r.raise_for_status()
    return r.json()


async def ym_all(c, path, params, pages=5, stop=None):
    out, cur = [], None
    for _ in range(pages):
        j = await ym(c, "GET", path, params=dict(params, **({"cursor": cur} if cur else {})))
        out += j.get("data") or []
        cur = (j.get("links") or {}).get("next_cursor")
        if not (j.get("meta") or {}).get("has_more") or not cur or (stop and stop(j.get("data") or [])):
            break
    return out


async def tg(c, b, method, **p):
    r = await c.post(f"https://api.telegram.org/bot{b['token']}/{method}", json=p)
    return r.json()


async def say(c, text, chat=None, markup=None):
    extra = {"reply_markup": markup} if markup else {}
    sent = 0
    for b in active_bots():
        if not b.get("owner"):
            continue
        r = await tg(c, b, "sendMessage", chat_id=b["owner"], text=text, parse_mode="HTML", disable_web_page_preview=True, **extra)
        if r.get("ok"):
            sent += 1
            if chat:
                D["map"][f"{bid(b)}:{r['result']['message_id']}"] = chat
                for k in list(D["map"])[:-500]:
                    D["map"].pop(k, None)
    if not sent:
        raise RuntimeError("telegram send failed")


def ready():
    return bool(D.get("ym") and any(b.get("owner") for b in active_bots()))


def work_kb(oid):
    return {"inline_keyboard": [[{"text": "▶️ Взять в работу", "callback_data": f"w:{oid}"}]]}


def _dt(x):
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def buy_of(ad):
    g = next((x for x in SD.get("goods", []) if x.get("adId") == ad), None)
    try:
        return float((g or {}).get("buy") or 0)
    except Exception:
        return 0.0


async def order_text(c, o):
    ad = o.get("ad_id")
    if ad not in TITLES:
        try:
            TITLES[ad] = (await ym(c, "GET", f"ads/{ad}")).get("title") or ""
        except Exception:
            TITLES[ad] = ""
    a = o.get("amount") or {}
    cur = a.get("currency", "")
    s = f"{ST[o['status']]}\n№{o['id']}" + (f" · {esc(TITLES[ad])}" if TITLES.get(ad) else "")
    s += f"\nСумма: {money(a.get('total'))} {cur}, вам: {money(a.get('net'))} {cur}"
    s += f"\nПокупатель: {esc(str((o.get('customer') or {}).get('name', '—')))}"
    if o["status"] in ("wait", "success") and buy_of(ad) > 0:
        try:
            s += f"\nПрибыль: {float(a.get('net')) - buy_of(ad):+.2f} {cur} (закупка {buy_of(ad):.2f})"
        except Exception:
            pass
    if o["status"] == "wait":
        s += "\n\nОтветьте (Reply) на это сообщение, чтобы написать покупателю."
    return s


async def tick(c):
    orders = (await ym(c, "GET", "orders")).get("data") or []
    seeding = not D["seen"]
    newly = set()
    for o in reversed(orders):  # от старых к новым
        oid, st = str(o["id"]), o.get("status")
        if D["seen"].get(oid) == st:
            continue
        if oid not in D["seen"]:
            newly.add(oid)
        if not seeding and st in ST:
            await say(c, await order_text(c, o), o.get("chat_id"), work_kb(o["id"]) if st == "wait" else None)
        D["seen"][oid] = st
    for k in list(D["seen"])[:-300]:
        D["seen"].pop(k, None)
    # напоминание о неотработанных заказах
    for o in orders:
        oid = str(o["id"])
        if o.get("status") != "wait" or oid in D["reminded"]:
            continue
        t = _dt(o.get("created_at"))
        if seeding:
            D["reminded"].append(oid)
        elif t and (datetime.now().astimezone() - t).total_seconds() >= 600:
            await say(c, f"⏰ Заказ №{o['id']} оплачен, но не взят в работу уже {int((datetime.now().astimezone() - t).total_seconds() // 60)} мин.",
                      o.get("chat_id"), work_kb(o["id"]))
            D["reminded"].append(oid)
    D["reminded"] = D["reminded"][-300:]
    # сообщения покупателей
    active = [o for o in orders if o.get("status") in ("wait", "work") and o.get("chat_id")][:8]
    for o in active:
        cid, cust = str(o["chat_id"]), (o.get("customer") or {}).get("id")
        ms = (await ym(c, "GET", f"chats/{cid}/messages")).get("data") or []
        if not ms:
            continue
        top, last = max(m["id"] for m in ms), D["last_msg"].get(cid)
        if last is None:
            last = 0 if str(o["id"]) in newly else top
        if seeding:
            D["last_msg"][cid] = top
            continue
        for m in sorted((m for m in ms if m["id"] > last), key=lambda m: m["id"]):
            if (m.get("sender") or {}).get("id") == cust and m.get("text"):
                name = esc(str((o.get("customer") or {}).get("name", "Покупатель")))
                await say(c, f"💬 {name}, заказ №{o['id']}:\n{esc(m['text'])}\n\nОтветьте (Reply) на это сообщение, чтобы ответить.", o["chat_id"])
        D["last_msg"][cid] = top
    save()


# ---------------- отчёты для команд бота ----------------
async def today_text(c):
    td = date.today()
    day = lambda o: (_dt(o.get("created_at")) or datetime.now().astimezone()).date()
    got = await ym_all(c, "orders", {"status": "success"}, 5, stop=lambda L: bool(L) and day(L[-1]) < td)
    rows = [o for o in got if day(o) == td]
    tot = net = prof = 0.0
    nob = 0
    for o in rows:
        a = o.get("amount") or {}
        t, n = float(a.get("total") or 0), float(a.get("net") or 0)
        tot, net = tot + t, net + n
        b = buy_of(o.get("ad_id"))
        if b > 0:
            prof += n - b
        else:
            nob += 1
    s = f"📊 Сегодня\nПродаж: {len(rows)}\nВыручка: {tot:.2f}\nВам на руки: {net:.2f}"
    if rows:
        s += f"\nЧистая прибыль: {prof:+.2f}" + (f" (у {nob} без закупки не учтена)" if nob else "")
    return s


async def stock_text(c):
    ads = await ym_all(c, "ads", {"status": "publish", "type": "auto-delivery,auto-value"}, 3)
    low = []
    for a in ads:
        try:
            if float(a.get("stock")) <= 3:
                low.append(f"• {esc(str(a.get('title', a.get('id'))))}: {a.get('stock')}")
        except (TypeError, ValueError):
            pass
    return "📉 Мало остатков:\n" + "\n".join(low) if low else "📉 Остатки в порядке (у всех объявлений больше 3)."


async def profile_text(c):
    chk = await ym(c, "GET", "check")
    sh = chk.get("shop") or {}
    return f"👤 Магазин: {esc(str(sh.get('title', '—')))} (№{sh.get('id', '—')})\nПодключено ботов: {len(active_bots())}"


# ---------------- Telegram: сообщения и кнопки ----------------
async def on_msg(c, b, m):
    uid, cid = (m.get("from") or {}).get("id"), (m.get("chat") or {}).get("id")
    txt = (m.get("text") or "").strip()
    if not uid or not txt:
        return
    reply = lambda t, **kw: tg(c, b, "sendMessage", chat_id=cid, text=t, parse_mode="HTML", **kw)
    if not b.get("owner"):
        if txt.startswith("/start") and b.get("code") and txt.split()[-1] == b["code"]:
            b["owner"], b["code"] = uid, None
            save()
            await set_menu(c)
            await reply("✅ Бот привязан к вам. Сюда будут приходить заказы и сообщения покупателей.\n"
                        "Чтобы ответить покупателю — сделайте Reply на сообщение о нём. Меню ниже.", reply_markup=MENU)
        return
    if uid != b["owner"] or not ready():
        return  # чужим не отвечаем
    cmd = CMD.get(txt, txt)
    try:
        if cmd.startswith("/menu") or cmd.startswith("/start"):
            await reply("Меню:", reply_markup=MENU)
        elif cmd.startswith("/balance"):
            r = await ym(c, "GET", "balance")
            await reply("\n".join(f"Счёт {x.get('id')}: {esc(str(x.get('amount')))} {esc(str(x.get('currency')))}" for x in r) or "Нет данных")
        elif cmd.startswith("/orders"):
            o = (await ym(c, "GET", "orders")).get("data") or []
            await reply("\n".join(f"№{x['id']} · {esc(str(x.get('status')))} · {money((x.get('amount') or {}).get('total'))}" for x in o[:5]) or "Заказов нет")
        elif cmd.startswith("/profile"):
            await reply(await profile_text(c))
        elif cmd.startswith("/withdraw"):
            bal = await ym(c, "GET", "balance")
            await reply("💸 Вывод через API Yoomarket недоступен: такого метода нет в их документации. Выведите средства в панели магазина.\n"
                        + "\n".join(f"Доступно: {esc(str(x.get('amount')))} {esc(str(x.get('currency')))}" for x in bal),
                        reply_markup={"inline_keyboard": [[{"text": "Открыть панель Yoomarket", "url": "https://panel.yoomarket.net"}]]})
        elif cmd.startswith(("/today", "/stock")):
            await reply(await (today_text(c) if cmd.startswith("/today") else stock_text(c)))
        elif m.get("reply_to_message"):
            chat = D["map"].get(f"{bid(b)}:{m['reply_to_message'].get('message_id')}")
            if not chat:
                await reply("Сделайте Reply на сообщение бота о заказе или о сообщении покупателя.")
                return
            await ym(c, "POST", f"chats/{chat}/sendMessage", json={"text": txt[:1500]})
            await reply("✓ Отправлено покупателю")
        else:
            await reply("Выберите действие в меню или сделайте Reply на сообщение о заказе.", reply_markup=MENU)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        await reply({409: "В этом чате нет активных заказов — отправить нельзя.", 429: "Лимит запросов, подождите минуту."}.get(code, f"Ошибка Yoomarket: {code}"))


async def on_cb(c, b, q):
    qid, uid = q.get("id"), (q.get("from") or {}).get("id")
    act, _, oid = (q.get("data") or "").partition(":")
    if uid != b.get("owner") or not ready() or act not in ("w", "c") or not oid.isdigit():
        await tg(c, b, "answerCallbackQuery", callback_query_id=qid)
        return
    msg = q.get("message") or {}
    try:
        await ym(c, "POST", f"orders/{oid}/" + ("work" if act == "w" else "confirm"))
        nxt = [[{"text": "✅ Выполнено", "callback_data": f"c:{oid}"}]] if act == "w" else []
        await tg(c, b, "answerCallbackQuery", callback_query_id=qid, text="Готово ✓")
        await tg(c, b, "editMessageReplyMarkup", chat_id=(msg.get("chat") or {}).get("id"), message_id=msg.get("message_id"),
                 reply_markup={"inline_keyboard": nxt})
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        await tg(c, b, "answerCallbackQuery", callback_query_id=qid, show_alert=True,
                 text="Статус заказа уже изменился" if code == 409 else f"Ошибка Yoomarket: {code}")


# ---------------- фоновые задачи ----------------
async def poll_orders():
    async with httpx.AsyncClient(timeout=30) as c:
        while True:
            delay = 5
            try:
                if ready():
                    await tick(c)
                    delay = 30
            except httpx.HTTPStatusError as e:
                delay = 90 if e.response.status_code == 429 else 30
            except Exception:
                delay = 30
            await asyncio.sleep(delay)


async def poll_bot(c, b):
    while b in active_bots():
        try:
            r = await tg(c, b, "getUpdates", offset=b.get("offset", 0), timeout=25, allowed_updates=["message", "callback_query"])
            if not r.get("ok"):
                await asyncio.sleep(5)
                continue
            for u in r.get("result", []):
                b["offset"] = u["update_id"] + 1
                if u.get("callback_query"):
                    await on_cb(c, b, u["callback_query"])
                else:
                    await on_msg(c, b, u.get("message") or {})
            save()
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(5)


async def poll_tg():
    tasks = {}
    async with httpx.AsyncClient(timeout=60) as c:
        while True:
            cur = {bid(b): b for b in active_bots()}
            for k in list(tasks):
                if k not in cur:
                    tasks.pop(k).cancel()
            for k, b in cur.items():
                if k not in tasks or tasks[k].done():
                    tasks[k] = asyncio.create_task(poll_bot(c, b))
            await asyncio.sleep(3)


# ---------------- Mini App: туннель и вход только для владельца бота ----------------
TUNNEL = {"url": ""}
SESSIONS: dict = {}
AUTH_FAILS: dict = {}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_public(request):
    """Запрос пришёл НЕ с этого компьютера напрямую (через туннель) или с чужим Host (защита от DNS rebinding)."""
    h = request.headers
    host = (urlsplit("//" + (h.get("host") or "")).hostname or "").lower()
    return bool(h.get("cf-connecting-ip") or h.get("x-forwarded-for") or h.get("x-forwarded-host")) or host not in LOCAL_HOSTS


def tg_verify(init_data):
    """Проверка подписи Telegram initData. Пускаем только владельца привязанного бота. -> id или None"""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    got = pairs.pop("hash", "")
    try:
        uid, age = int(json.loads(pairs["user"])["id"]), time.time() - int(pairs["auth_date"])
    except Exception:
        return None
    if not got or age > 86400 or age < -60:
        return None
    variants = [pairs, {k: v for k, v in pairs.items() if k != "signature"}]
    for b in D["bots"]:
        if not b.get("owner") or b["owner"] != uid:
            continue
        key = hmac.new(b"WebAppData", b["token"].encode(), hashlib.sha256).digest()
        for v in variants:
            check = "\n".join(f"{k}={x}" for k, x in sorted(v.items()))
            if hmac.compare_digest(hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), got):
                return uid
    return None


async def set_menu(c):
    """Кнопка «FlipCalc» в ваших ботах открывает Mini App по текущему адресу туннеля."""
    if not TUNNEL["url"]:
        return
    for b in D["bots"]:
        if b.get("owner"):
            try:
                await tg(c, b, "setChatMenuButton", chat_id=b["owner"],
                         menu_button={"type": "web_app", "text": "FlipCalc", "web_app": {"url": TUNNEL["url"]}})
            except Exception:
                pass


async def tunnel_task():
    """Туннель через SSH (localhost.run): работает там, где режут Cloudflare."""
    exe = shutil.which("ssh")
    if not exe or os.environ.get("FLIPCALC_NO_TUNNEL") == "1":
        return
    port = os.environ.get("FLIPCALC_PORT", "8000")
    async with httpx.AsyncClient(timeout=30) as c:
        while True:
            p = None
            try:
                p = await asyncio.create_subprocess_exec(
                    exe, "-T", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=NUL" if os.name == "nt" else "UserKnownHostsFile=/dev/null",
                    "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3", "-o", "ExitOnForwardFailure=yes",
                    "-R", f"80:127.0.0.1:{port}", "nokey@localhost.run",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                while True:
                    line = await p.stdout.readline()
                    if not line:
                        break
                    m = re.search(r"https://[a-z0-9.-]+\.lhr\.life", line.decode(errors="ignore"))
                    if m and m.group(0) != TUNNEL["url"]:
                        TUNNEL["url"] = m.group(0)
                        await set_menu(c)
                TUNNEL["url"] = ""
            except asyncio.CancelledError:
                if p and p.returncode is None:
                    p.kill()
                raise
            except Exception:
                TUNNEL["url"] = ""
            await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(app):
    tasks = [asyncio.create_task(poll_orders()), asyncio.create_task(poll_tg()), asyncio.create_task(tunnel_task())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)


from fastapi.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=1000)
@app.middleware("http")
async def guard(request: Request, call_next):
    """Через туннель доступны только страница приложения и вход; всё остальное — только по сессии владельца."""
    if is_public(request):
        p = request.url.path
        open_ = (request.method == "GET" and p in ("/", "/miniapp")) or (request.method == "POST" and p == "/api/telegram/webapp-auth")
        if not open_:
            s = SESSIONS.get(request.headers.get("authorization", "")[7:])
            if not (p.startswith("/api/") and s and s["exp"] > time.time()):
                return JSONResponse({"detail": "forbidden"}, status_code=403)
            request.state.session = True
    return await call_next(request)


@app.post("/api/telegram/webapp-auth")
async def webapp_auth(request: Request):
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    now = time.time()
    fails = [t for t in AUTH_FAILS.get(ip, []) if now - t < 600]
    if len(fails) >= 10:
        raise HTTPException(429, "Слишком много попыток. Подождите несколько минут.")
    uid = tg_verify(str((await request.json()).get("initData") or ""))
    if uid is None:
        AUTH_FAILS[ip] = fails + [now]
        raise HTTPException(403, "Откройте FlipCalc кнопкой в своём боте (бот должен быть привязан к вам)")
    for k in [k for k, v in SESSIONS.items() if v["exp"] < now]:
        SESSIONS.pop(k, None)
    tok = secrets.token_urlsafe(32)
    SESSIONS[tok] = {"uid": uid, "exp": now + 8 * 3600}
    return {"session": tok}


@app.get("/miniapp")
async def miniapp_page():
    return FileResponse(HTML)


@app.get("/")
async def index():
    return FileResponse(HTML)


def _local(request: Request):
    if getattr(request.state, "session", False):
        return  # вход владельца через Mini App (проверен в guard)
    if is_public(request):
        raise HTTPException(403, "forbidden")  # второй рубеж: через туннель без сессии нельзя
    host = request.client.host if request.client else ""
    if request.headers.get("x-flipcalc") != "1" or host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "forbidden")


def _status():
    return {"ym": bool(D.get("ym")), "miniapp": TUNNEL["url"], "bots": [
        {"name": b["name"], "owner": bool(b.get("owner")),
         "link": f"https://t.me/{b['name']}?start={b['code']}" if b.get("code") and not b.get("owner") else ""} for b in D["bots"]]}


@app.get("/api/local/settings")
async def settings_get(request: Request):
    _local(request)
    return _status()


@app.post("/api/local/settings")
async def settings_set(request: Request):
    _local(request)
    j = await request.json()
    if j.get("remove"):
        D["bots"] = [b for b in D["bots"] if b["name"] != j["remove"]]
    else:
        token, bot = str(j.get("ym") or "").strip(), str(j.get("bot") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._\-]{16,256}", token):
            raise HTTPException(400, "Токен магазина не подходит")
        if not re.fullmatch(r"\d+:[A-Za-z0-9_\-]{20,}", bot):
            raise HTTPException(400, "Токен бота выглядит неверно (пример: 123456:AAH...)")
        if any(b["token"] == bot for b in D["bots"]):
            raise HTTPException(400, "Этот бот уже добавлен")
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                me = (await c.get(f"https://api.telegram.org/bot{bot}/getMe")).json()
        except Exception:
            raise HTTPException(502, "Не удалось связаться с Telegram (возможно, нужен VPN)")
        if not me.get("ok"):
            raise HTTPException(400, "Telegram не принял токен бота")
        if not D["bots"]:
            D["seen"], D["last_msg"], D["map"], D["reminded"] = {}, {}, {}, []
        D["ym"] = token
        D["bots"].append({"token": bot, "name": me["result"]["username"], "owner": None, "code": secrets.token_hex(4), "offset": 0})
    save()
    return _status()


@app.get("/api/local/state")
async def state_get(request: Request):
    _local(request)
    return {"state": SD or None}


@app.post("/api/local/state")
async def state_set(request: Request):
    global SD
    _local(request)
    body = await request.body()
    if len(body) > 10_000_000:
        raise HTTPException(413, "too large")
    j = json.loads(body)
    if not isinstance(j, dict):
        raise HTTPException(400, "bad state")
    if j.get("_ts", 0) < SD.get("_ts", 0):
        return {"ok": False}  # более старое состояние не затирает новое
    SD = j
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(j, ensure_ascii=False), "utf-8")
    tmp.replace(STATE)
    return {"ok": True}


@app.api_route("/api/yoo/{path:path}", methods=["GET", "POST"])
async def yoo(path: str, request: Request):
    rule = GET_OK if request.method == "GET" else POST_OK
    if not rule.match(path):
        raise HTTPException(404, "method not allowed")
    token = request.headers.get("x-api-key", "")
    if not re.fullmatch(r"[A-Za-z0-9._\-]{16,256}", token):
        raise HTTPException(401, "bad token")
    url = f"{BASE}/{path}" + (f"?{request.url.query}" if request.url.query else "")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    body = None
    if request.method == "POST":
        body = await request.body()
        if request.headers.get("content-type"):
            headers["Content-Type"] = request.headers["content-type"]
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.request(request.method, url, headers=headers, content=body)
    return Response(r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))
