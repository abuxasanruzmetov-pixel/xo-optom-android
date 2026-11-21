"""
XASANBOY OPTOM — onlayn do'kon serveri
- Mijozlar uchun Telegram Mini App (katalog, savatcha, buyurtma)
- Telegram bot (webhook): ro'yxatdan o'tish (raqam + lokatsiya), xabarnomalar
- POS ilova uchun admin API: katalogni yuklash, buyurtmalarni olish va holatini o'zgartirish

Faqat Python standart kutubxonasi ishlatiladi (tashqi paket kerak emas).

Muhit o'zgaruvchilari (Railway → Variables):
  BOT_TOKEN   — @BotFather bergan token (majburiy)
  ADMIN_KEY   — POS ilova bilan ulanish uchun maxfiy kalit (majburiy, kamida 8 belgi)
  DATA_DIR    — ma'lumotlar papkasi (Railway Volume: /data)
  PUBLIC_URL  — https://... (bo'lmasa RAILWAY_PUBLIC_DOMAIN ishlatiladi)
  PORT        — Railway o'zi beradi
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("xo")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_KEY = os.environ.get("ADMIN_KEY", "").strip()
DATA_DIR = os.environ.get("DATA_DIR", "/data" if os.path.isdir("/data") else "./data")
PORT = int(os.environ.get("PORT", "8080"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
if not PUBLIC_URL and os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
    PUBLIC_URL = "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"].strip().rstrip("/")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")
DB_PATH = os.path.join(DATA_DIR, "shop.db")
WEBHOOK_SECRET = hashlib.sha256(("xo-webhook:" + BOT_TOKEN).encode()).hexdigest()[:32] if BOT_TOKEN else ""
INIT_DATA_MAX_AGE = 7 * 24 * 3600
MAX_EXTRA_IMAGES = 2
MAX_BANNERS = 8
DEFAULT_DELIVERY = {"enabled": False, "price": 0, "free_from": 0, "note": ""}
DEFAULT_MIN_ORDER = {"enabled": False, "amount": 0}

STATUS_LABELS = {
    "new": "🆕 Yangi",
    "accepted": "✅ Qabul qilindi",
    "delivering": "🚚 Yo'lda",
    "done": "📦 Yetkazildi",
    "cancelled": "❌ Bekor qilindi",
}

os.makedirs(DATA_DIR, exist_ok=True)
_db_lock = threading.Lock()


# ============================================================================
# MA'LUMOTLAR BAZASI
# ============================================================================
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT DEFAULT '',
                unit TEXT DEFAULT 'dona',
                price REAL DEFAULT 0,
                stock REAL DEFAULT 0,
                active INTEGER DEFAULT 1,
                img_hash TEXT DEFAULT '',
                sort INTEGER DEFAULT 0,
                updated_at INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS images (
                key TEXT PRIMARY KEY,
                mime TEXT,
                data BLOB,
                hash TEXT
            );
            CREATE TABLE IF NOT EXISTS customers (
                tg_id INTEGER PRIMARY KEY,
                first_name TEXT DEFAULT '',
                last_name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                lat REAL,
                lng REAL,
                loc_at INTEGER,
                address TEXT DEFAULT '',
                created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER,
                name TEXT,
                phone TEXT,
                lat REAL,
                lng REAL,
                address TEXT DEFAULT '',
                comment TEXT DEFAULT '',
                total REAL DEFAULT 0,
                status TEXT DEFAULT 'new',
                pos_sale_id TEXT DEFAULT '',
                created_at INTEGER,
                updated_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS order_items (
                order_id INTEGER,
                product_id TEXT,
                name TEXT,
                unit TEXT,
                price REAL,
                qty REAL,
                FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, id);
            CREATE INDEX IF NOT EXISTS idx_orders_tg ON orders(tg_id, id);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS promocodes (
                code TEXT PRIMARY KEY,
                kind TEXT DEFAULT 'percent',
                value REAL DEFAULT 0,
                min_sum REAL DEFAULT 0,
                usage_limit INTEGER DEFAULT 0,
                used_count INTEGER DEFAULT 0,
                active INTEGER DEFAULT 1,
                expires_at INTEGER DEFAULT 0,
                created_at INTEGER
            );
            """
        )
        # Eski bazalar uchun yangi ustunlar
        migrations = {
            "products": [("description", "TEXT DEFAULT ''"), ("old_price", "REAL DEFAULT 0"),
                         ("unlimited", "INTEGER DEFAULT 0"), ("extra_imgs", "TEXT DEFAULT '[]'")],
            "orders": [("subtotal", "REAL DEFAULT 0"), ("discount", "REAL DEFAULT 0"),
                       ("delivery_fee", "REAL DEFAULT 0"), ("promo_code", "TEXT DEFAULT ''")],
            "customers": [("lang", "TEXT DEFAULT ''")],
        }
        for table, cols in migrations.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def get_setting(key, default=""):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with db() as c:
        c.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def now():
    return int(time.time())


def get_json_setting(key, default):
    raw = get_setting(key, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def set_json_setting(key, value):
    set_setting(key, json.dumps(value, ensure_ascii=False))


def uuid_short():
    return hashlib.sha1(f"{time.time()}-{threading.get_ident()}".encode()).hexdigest()[:10]


def fmt_money(v):
    try:
        return f"{int(round(float(v))):,}".replace(",", " ") + " so'm"
    except Exception:
        return str(v)


def fmt_qty(v):
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(round(f, 3))
    except Exception:
        return str(v)


# ============================================================================
# TELEGRAM
# ============================================================================
def tg_call(method, payload=None, timeout=15):
    if not BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read().decode())
            if not res.get("ok"):
                log.warning("Telegram %s xato: %s", method, res)
            return res
    except Exception as e:
        log.warning("Telegram %s ulanmadi: %s", method, e)
        return None


def tg_send_photo(chat_id, data, mime, caption, markup=None, file_id=None):
    """Rasmni (bayt yoki file_id) yuborish."""
    if not BOT_TOKEN:
        return None
    if file_id:
        payload = {"chat_id": chat_id, "photo": file_id, "caption": caption}
        if markup:
            payload["reply_markup"] = markup
        return tg_call("sendPhoto", payload)
    boundary = "----xo" + uuid_short()
    parts = []
    fields = {"chat_id": str(chat_id), "caption": caption}
    if markup:
        fields["reply_markup"] = json.dumps(markup)
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    ext = "png" if "png" in (mime or "") else "jpg"
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"p.{ext}\"\r\nContent-Type: {mime or 'image/jpeg'}\r\n\r\n".encode())
    parts.append(data)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return None
    except Exception as e:
        log.warning("sendPhoto: %s", e)
        return None


def tg_async(method, payload):
    threading.Thread(target=tg_call, args=(method, payload), daemon=True).start()


def webapp_url():
    return (PUBLIC_URL + "/") if PUBLIC_URL else ""


def shop_name():
    return get_setting("shop_name", "XASANBOY OPTOM") or "XASANBOY OPTOM"


def kb_remove():
    return {"remove_keyboard": True}


def kb_contact():
    return {
        "keyboard": [[{"text": "📱 Telefon raqamni yuborish", "request_contact": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def kb_location():
    return {
        "keyboard": [
            [{"text": "📍 Joylashuvni yuborish", "request_location": True}],
            [{"text": "⏭ Keyinroq"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def inline_open_shop(text="🛒 Katalogni ochish"):
    url = webapp_url()
    if not url:
        return None
    return {"inline_keyboard": [[{"text": text, "web_app": {"url": url}}]]}


def send_shop_button(chat_id, text):
    markup = inline_open_shop()
    payload = {"chat_id": chat_id, "text": text}
    if markup:
        payload["reply_markup"] = markup
    tg_call("sendMessage", payload)


def upsert_customer_from_user(c, user):
    tg_id = int(user["id"])
    row = c.execute("SELECT tg_id FROM customers WHERE tg_id=?", (tg_id,)).fetchone()
    if row:
        c.execute(
            "UPDATE customers SET first_name=?, last_name=?, username=? WHERE tg_id=?",
            (user.get("first_name", ""), user.get("last_name", ""), user.get("username", ""), tg_id),
        )
    else:
        c.execute(
            "INSERT INTO customers(tg_id, first_name, last_name, username, created_at) VALUES(?,?,?,?,?)",
            (tg_id, user.get("first_name", ""), user.get("last_name", ""), user.get("username", ""), now()),
        )
    return tg_id


def normalize_phone(p):
    digits = re.sub(r"\D", "", str(p or ""))
    if len(digits) == 9:
        digits = "998" + digits
    return ("+" + digits) if digits else ""


def handle_update(update):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    if msg["chat"].get("type") != "private":
        return
    user = msg.get("from") or {}
    text = (msg.get("text") or "").strip()

    with db() as c:
        tg_id = upsert_customer_from_user(c, user)
        cust = dict(c.execute("SELECT * FROM customers WHERE tg_id=?", (tg_id,)).fetchone())

    # Do'kon egasi chatini ulash: /admin KALIT
    if text.startswith("/admin"):
        parts = text.split(maxsplit=1)
        if ADMIN_KEY and len(parts) == 2 and hmac.compare_digest(parts[1].strip(), ADMIN_KEY):
            set_setting("admin_chat_id", chat_id)
            tg_call("sendMessage", {"chat_id": chat_id, "text": "✅ Bu chat do'kon egasi sifatida ulandi. Yangi buyurtmalar shu yerga keladi."})
        else:
            tg_call("sendMessage", {"chat_id": chat_id, "text": "❌ Kalit noto'g'ri."})
        return

    if msg.get("contact"):
        contact = msg["contact"]
        if contact.get("user_id") and int(contact["user_id"]) != int(user.get("id", 0)):
            tg_call("sendMessage", {"chat_id": chat_id, "text": "Iltimos, o'zingizning raqamingizni yuboring 👇", "reply_markup": kb_contact()})
            return
        phone = normalize_phone(contact.get("phone_number"))
        with db() as c:
            c.execute("UPDATE customers SET phone=? WHERE tg_id=?", (phone, tg_id))
        tg_call("sendMessage", {
            "chat_id": chat_id,
            "text": f"✅ Raqamingiz saqlandi: {phone}\n\nEndi yetkazib berish manzilingizni yuboring 👇",
            "reply_markup": kb_location(),
        })
        return

    if msg.get("location"):
        loc = msg["location"]
        with db() as c:
            c.execute("UPDATE customers SET lat=?, lng=?, loc_at=? WHERE tg_id=?", (loc["latitude"], loc["longitude"], now(), tg_id))
        tg_call("sendMessage", {"chat_id": chat_id, "text": "✅ Manzilingiz saqlandi.", "reply_markup": kb_remove()})
        send_shop_button(chat_id, "Katalogni ochib, buyurtma bering 👇")
        return

    if text == "⏭ Keyinroq":
        tg_call("sendMessage", {"chat_id": chat_id, "text": "Mayli. Manzilni buyurtma berishda ham yuborishingiz mumkin.", "reply_markup": kb_remove()})
        send_shop_button(chat_id, "Katalogni ochish 👇")
        return

    if text.startswith("/manzil") or text == "📍 Manzilni yangilash":
        tg_call("sendMessage", {"chat_id": chat_id, "text": "Yangi manzilingizni yuboring 👇", "reply_markup": kb_location()})
        return

    # Kanaldagi "Buyurtma berish" tugmasidan kelgan: /start p_ID
    if text.startswith("/start p_") and cust.get("phone"):
        pid = text[len("/start p_"):].strip()
        with db() as c:
            p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if p:
            send_shop_button(chat_id, f"🛍 {p['name']} — {fmt_money(p['price'])}\n\nBuyurtma berish uchun katalogni oching 👇")
            return

    # /start va boshqa har qanday xabar
    if not cust.get("phone"):
        tg_call("sendMessage", {
            "chat_id": chat_id,
            "text": f"👋 {shop_name()} ga xush kelibsiz!\n\nRo'yxatdan o'tish uchun telefon raqamingizni yuboring 👇",
            "reply_markup": kb_contact(),
        })
        return
    if cust.get("lat") is None:
        tg_call("sendMessage", {"chat_id": chat_id, "text": "📍 Yetkazib berish manzilingizni yuboring 👇", "reply_markup": kb_location()})
        return
    send_shop_button(chat_id, f"🛒 {shop_name()} katalogi. Buyurtma berish uchun bosing 👇\n\nManzilni o'zgartirish: /manzil")


def setup_telegram():
    if not BOT_TOKEN:
        log.warning("BOT_TOKEN berilmagan — Telegram o'chiq")
        return
    me = tg_call("getMe")
    if me and me.get("ok"):
        set_setting("bot_username", me["result"].get("username", ""))
    if not PUBLIC_URL:
        log.warning("PUBLIC_URL yo'q — webhook o'rnatilmadi")
        return
    tg_call("setWebhook", {
        "url": f"{PUBLIC_URL}/tg/webhook",
        "secret_token": WEBHOOK_SECRET,
        "allowed_updates": ["message", "edited_message"],
        "drop_pending_updates": False,
    })
    tg_call("setChatMenuButton", {"menu_button": {"type": "web_app", "text": "🛒 Katalog", "web_app": {"url": webapp_url()}}})
    tg_call("setMyCommands", {"commands": [
        {"command": "start", "description": "Boshlash / Katalog"},
        {"command": "manzil", "description": "Manzilni yangilash"},
    ]})
    log.info("Webhook o'rnatildi: %s/tg/webhook", PUBLIC_URL)


def notify_admin_new_order(order_id):
    chat_id = get_setting("admin_chat_id", "")
    if not chat_id:
        return
    o = load_order(order_id)
    if not o:
        return
    lines = [f"🆕 Yangi buyurtma #{o['id']}", f"👤 {o['name']}", f"📞 {o['phone']}"]
    if o.get("address"):
        lines.append(f"🏠 {o['address']}")
    lines.append("")
    for it in o["items"]:
        lines.append(f"• {it['name']} — {fmt_qty(it['qty'])} {it['unit']} × {fmt_money(it['price'])}")
    lines.append("")
    if o.get("discount"):
        lines.append(f"🏷 Promokod {o.get('promo_code', '')}: −{fmt_money(o['discount'])}")
    if o.get("delivery_fee"):
        lines.append(f"🚚 Yetkazib berish: {fmt_money(o['delivery_fee'])}")
    lines.append(f"💰 Jami: {fmt_money(o['total'])}")
    if o.get("comment"):
        lines.append(f"💬 {o['comment']}")
    tg_call("sendMessage", {"chat_id": chat_id, "text": "\n".join(lines)})
    if o.get("lat") is not None and o.get("lng") is not None:
        tg_call("sendLocation", {"chat_id": chat_id, "latitude": o["lat"], "longitude": o["lng"]})


def notify_customer_status(order_id, status):
    o = load_order(order_id, with_items=False)
    if not o or not o.get("tg_id"):
        return
    label = STATUS_LABELS.get(status, status)
    text = f"Buyurtma #{o['id']}: {label}"
    if status == "accepted":
        text += "\nBuyurtmangiz tayyorlanmoqda."
    elif status == "delivering":
        text += "\nBuyurtmangiz yo'lga chiqdi."
    elif status == "done":
        text += f"\nRahmat! {shop_name()} dan xarid qilganingiz uchun minnatdormiz."
    tg_async("sendMessage", {"chat_id": o["tg_id"], "text": text})


# ============================================================================
# MINI APP AUTENTIFIKATSIYA (initData imzosini tekshirish)
# ============================================================================
def check_webapp_signature(raw):
    """Telegram WebApp initData (yoki requestContact javobi) imzosini tekshiradi."""
    if not raw or not BOT_TOKEN:
        return None
    try:
        pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    data = dict(pairs)
    recv_hash = data.pop("hash", "")  # 'signature' maydoni (bo'lsa) tekshiruv satriga kiradi
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv_hash):
        return None
    try:
        if now() - int(data.get("auth_date", "0")) > INIT_DATA_MAX_AGE:
            return None
    except ValueError:
        return None
    return data


def webapp_user(handler):
    raw = handler.headers.get("X-Tg-Init", "")
    data = check_webapp_signature(raw)
    if not data or "user" not in data:
        return None
    try:
        user = json.loads(data["user"])
        int(user["id"])
        return user
    except Exception:
        return None


# ============================================================================
# BUYURTMALAR
# ============================================================================
def load_order(order_id, with_items=True):
    with db() as c:
        row = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            return None
        o = dict(row)
        if with_items:
            o["items"] = [dict(r) for r in c.execute("SELECT product_id, name, unit, price, qty FROM order_items WHERE order_id=?", (order_id,))]
        return o


def order_public(o):
    o = dict(o)
    o["status_label"] = STATUS_LABELS.get(o.get("status"), o.get("status"))
    return o


# ============================================================================
# HTTP SERVER
# ============================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "XO/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    # ---- yordamchilar ----
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Admin-Key, X-Tg-Init")
        self.send_header("Access-Control-Max-Age", "86400")

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data, mime, cache="public, max-age=31536000, immutable", status=200):
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def error(self, status, message):
        self.send_json({"ok": False, "error": message}, status)

    def read_json(self, limit=25 * 1024 * 1024):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError("So'rov juda katta")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def is_admin(self):
        key = self.headers.get("X-Admin-Key", "")
        return bool(ADMIN_KEY) and hmac.compare_digest(key, ADMIN_KEY)

    def path_parts(self):
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, dict(urllib.parse.parse_qsl(parsed.query))

    # ---- HTTP metodlar ----
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        path, query = self.path_parts()
        try:
            route = ROUTES.get((method, path))
            if route:
                return route(self, query)
            for (m, pattern), fn in PATTERN_ROUTES:
                if m != method:
                    continue
                mt = pattern.fullmatch(path)
                if mt:
                    return fn(self, query, *mt.groups())
            if method == "GET":
                return serve_static(self, path)
            self.error(404, "Topilmadi")
        except json.JSONDecodeError:
            self.error(400, "JSON noto'g'ri")
        except ValueError as e:
            self.error(400, str(e))
        except BrokenPipeError:
            pass
        except Exception as e:
            log.exception("Xato: %s %s", method, path)
            try:
                self.error(500, "Server xatosi: " + str(e))
            except Exception:
                pass


# ---- statik fayllar (Mini App) ----
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript", ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}


def serve_static(h, path):
    if path in ("", "/"):
        path = "/index.html"
    safe = os.path.normpath(path.lstrip("/"))
    if safe.startswith(".."):
        return h.error(404, "Topilmadi")
    full = os.path.join(STATIC_DIR, safe)
    if not os.path.isfile(full):
        return h.error(404, "Topilmadi")
    with open(full, "rb") as f:
        data = f.read()
    ext = os.path.splitext(full)[1]
    h.send_bytes(data, MIME.get(ext, "application/octet-stream"), cache="no-cache")


# ---- umumiy ----
def r_health(h, q):
    h.send_json({"ok": True, "time": now(), "telegram": bool(BOT_TOKEN), "public_url": PUBLIC_URL, "version": 2})


# ---- umumiy ma'lumotlar (do'kon, katalog, rasmlar) ----
def shop_public():
    info = get_json_setting("shop_info", {})
    delivery = {**DEFAULT_DELIVERY, **get_json_setting("delivery", {})}
    min_order = {**DEFAULT_MIN_ORDER, **get_json_setting("min_order", {})}
    banners = get_json_setting("banners", [])
    logo_hash = get_setting("logo_hash", "")
    return {
        "name": shop_name(),
        "phone": get_setting("shop_phone", ""),
        "logo": ("/img/logo?h=" + logo_hash) if logo_hash else "",
        "bot": get_setting("bot_username", ""),
        "description": info.get("description", ""),
        "phones": [p for p in info.get("phones", []) if p][:3],
        "addresses": [a for a in info.get("addresses", []) if a and a.get("text")][:3],
        "banners": [{"id": b["id"], "image": f"/img/b/{b['id']}?h={b.get('hash', '')}", "link": b.get("link", "")} for b in banners if b.get("id")],
        "delivery": delivery,
        "min_order": float(min_order["amount"]) if min_order.get("enabled") else 0,
        "promo_enabled": promo_count_active() > 0,
    }


def r_shop(h, q):
    h.send_json(shop_public())


def product_public(d):
    imgs = []
    if d.get("img_hash"):
        imgs.append(f"/img/p/{urllib.parse.quote(d['id'])}?h={d['img_hash']}")
    try:
        extras = json.loads(d.get("extra_imgs") or "[]")
    except Exception:
        extras = []
    for n, hsh in enumerate(extras, start=1):
        if hsh:
            imgs.append(f"/img/p/{urllib.parse.quote(d['id'])}?n={n}&h={hsh}")
    return {
        "id": d["id"],
        "name": d["name"],
        "category": d.get("category") or "",
        "unit": d.get("unit") or "dona",
        "price": d.get("price") or 0,
        "old_price": d.get("old_price") or 0,
        "stock": d.get("stock") or 0,
        "unlimited": bool(d.get("unlimited")),
        "description": d.get("description") or "",
        "image": imgs[0] if imgs else "",
        "images": imgs,
    }


def r_catalog(h, q):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM products WHERE active=1 ORDER BY sort, name"
        ).fetchall()
    items = [product_public(dict(r)) for r in rows]
    h.send_json({"products": items, "updated": get_setting("catalog_updated", "0")})


def r_image(h, q, kind, key=None):
    if kind == "logo":
        db_key = "logo"
    elif kind == "b":
        db_key = "b_" + urllib.parse.unquote(key or "")
    else:
        n = str(q.get("n", "") or "")
        db_key = "p_" + urllib.parse.unquote(key or "") + (f"__{int(n)}" if n.isdigit() and int(n) > 0 else "")
    with db() as c:
        row = c.execute("SELECT mime, data FROM images WHERE key=?", (db_key,)).fetchone()
    if not row:
        return h.error(404, "Rasm yo'q")
    h.send_bytes(bytes(row["data"]), row["mime"] or "image/jpeg")


# ---- mijoz (Mini App) ----
def r_me(h, q):
    user = webapp_user(h)
    if not user:
        return h.error(401, "Telegram orqali oching")
    with db() as c:
        upsert_customer_from_user(c, user)
        cust = dict(c.execute("SELECT * FROM customers WHERE tg_id=?", (int(user["id"]),)).fetchone())
    h.send_json({
        "tg_id": cust["tg_id"],
        "name": " ".join(x for x in [cust.get("first_name"), cust.get("last_name")] if x).strip(),
        "phone": cust.get("phone") or "",
        "lat": cust.get("lat"),
        "lng": cust.get("lng"),
        "address": cust.get("address") or "",
        "registered": bool(cust.get("phone")),
        "lang": cust.get("lang") or "",
    })


def r_me_lang(h, q):
    user = webapp_user(h)
    if not user:
        return h.error(401, "Telegram orqali oching")
    lang = "ru" if str(h.read_json().get("lang")) == "ru" else "uz"
    with db() as c:
        upsert_customer_from_user(c, user)
        c.execute("UPDATE customers SET lang=? WHERE tg_id=?", (lang, int(user["id"])))
    h.send_json({"ok": True})


def r_me_contact(h, q):
    """Mini App'dagi requestContact javobini (imzolangan) qabul qiladi."""
    user = webapp_user(h)
    if not user:
        return h.error(401, "Telegram orqali oching")
    body = h.read_json()
    data = check_webapp_signature(body.get("response", ""))
    if not data or "contact" not in data:
        return h.error(400, "Raqam tasdiqlanmadi")
    contact = json.loads(data["contact"])
    if int(contact.get("user_id", 0)) != int(user["id"]):
        return h.error(400, "Bu sizning raqamingiz emas")
    phone = normalize_phone(contact.get("phone_number"))
    with db() as c:
        upsert_customer_from_user(c, user)
        c.execute("UPDATE customers SET phone=? WHERE tg_id=?", (phone, int(user["id"])))
    h.send_json({"ok": True, "phone": phone})


def r_me_location(h, q):
    user = webapp_user(h)
    if not user:
        return h.error(401, "Telegram orqali oching")
    body = h.read_json()
    try:
        lat = float(body.get("lat"))
        lng = float(body.get("lng"))
    except (TypeError, ValueError):
        raise ValueError("Joylashuv noto'g'ri")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError("Joylashuv noto'g'ri")
    address = str(body.get("address") or "")[:300]
    with db() as c:
        upsert_customer_from_user(c, user)
        c.execute("UPDATE customers SET lat=?, lng=?, loc_at=?, address=COALESCE(NULLIF(?, ''), address) WHERE tg_id=?",
                  (lat, lng, now(), address, int(user["id"])))
    h.send_json({"ok": True})


# ---- promokodlar ----
def promo_count_active():
    with db() as c:
        return c.execute("SELECT COUNT(*) AS n FROM promocodes WHERE active=1").fetchone()["n"]


def promo_evaluate(c, code, subtotal):
    """(chegirma_summasi, xato_matni, promo_qatori)"""
    code = str(code or "").strip().upper()
    if not code:
        return 0.0, "", None
    p = c.execute("SELECT * FROM promocodes WHERE code=?", (code,)).fetchone()
    if not p or not p["active"]:
        return 0.0, "Promokod topilmadi", None
    if p["expires_at"] and now() > int(p["expires_at"]):
        return 0.0, "Promokod muddati tugagan", None
    if p["usage_limit"] and int(p["used_count"]) >= int(p["usage_limit"]):
        return 0.0, "Promokod limiti tugagan", None
    if float(p["min_sum"] or 0) > subtotal:
        return 0.0, f"Promokod {fmt_money(p['min_sum'])} dan katta buyurtmaga amal qiladi", None
    if p["kind"] == "percent":
        disc = subtotal * min(100.0, float(p["value"])) / 100.0
    else:
        disc = float(p["value"])
    disc = max(0.0, min(subtotal, round(disc)))
    return disc, "", p


def r_promo_check(h, q):
    user = webapp_user(h)
    if not user:
        return h.error(401, "Telegram orqali oching")
    body = h.read_json()
    subtotal = float(body.get("subtotal") or 0)
    with db() as c:
        disc, err, p = promo_evaluate(c, body.get("code"), subtotal)
    if err:
        return h.error(400, err)
    h.send_json({"ok": True, "discount": disc, "code": p["code"], "kind": p["kind"], "value": p["value"]})


def delivery_fee_for(subtotal_after_discount):
    d = {**DEFAULT_DELIVERY, **get_json_setting("delivery", {})}
    if not d.get("enabled"):
        return 0.0
    free_from = float(d.get("free_from") or 0)
    if free_from > 0 and subtotal_after_discount >= free_from:
        return 0.0
    return float(d.get("price") or 0)


def r_create_order(h, q):
    user = webapp_user(h)
    if not user:
        return h.error(401, "Telegram orqali oching")
    tg_id = int(user["id"])
    body = h.read_json(limit=200 * 1024)
    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        raise ValueError("Savatcha bo'sh")
    if len(items) > 300:
        raise ValueError("Juda ko'p mahsulot")
    comment = str(body.get("comment") or "")[:500]
    address = str(body.get("address") or "")[:300]
    min_cfg = {**DEFAULT_MIN_ORDER, **get_json_setting("min_order", {})}

    with _db_lock, db() as c:
        cust = c.execute("SELECT * FROM customers WHERE tg_id=?", (tg_id,)).fetchone()
        if not cust or not cust["phone"]:
            return h.error(403, "Avval ro'yxatdan o'ting (telefon raqam)")
        if cust["lat"] is None and not address and not cust["address"]:
            return h.error(400, "Yetkazib berish manzilini yuboring")
        recent = c.execute("SELECT COUNT(*) AS n FROM orders WHERE tg_id=? AND created_at>?", (tg_id, now() - 3600)).fetchone()["n"]
        if recent >= 20:
            return h.error(429, "Juda ko'p buyurtma. Keyinroq urinib ko'ring")

        lines = []
        subtotal = 0.0
        for it in items:
            pid = str(it.get("id") or it.get("product_id") or "")
            try:
                qty = float(it.get("qty"))
            except (TypeError, ValueError):
                continue
            if not pid or qty <= 0 or qty > 100000:
                continue
            p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
            if not p:
                continue
            if not p["unlimited"] and float(p["stock"] or 0) <= 0:
                continue
            if not p["unlimited"] and qty > float(p["stock"] or 0):
                qty = float(p["stock"] or 0)
            lines.append((pid, p["name"], p["unit"] or "dona", float(p["price"] or 0), qty))
            subtotal += float(p["price"] or 0) * qty
        if not lines:
            raise ValueError("Mahsulotlar topilmadi yoki tugagan. Katalogni yangilang")
        if min_cfg.get("enabled") and subtotal < float(min_cfg.get("amount") or 0):
            return h.error(400, f"Minimal buyurtma — {fmt_money(min_cfg['amount'])}")

        discount, perr, promo = promo_evaluate(c, body.get("promo_code"), subtotal)
        if perr:
            return h.error(400, perr)
        fee = delivery_fee_for(subtotal - discount)
        total = round(subtotal - discount + fee, 2)

        name = " ".join(x for x in [cust["first_name"], cust["last_name"]] if x).strip() or "Mijoz"
        t = now()
        cur = c.execute(
            """INSERT INTO orders(tg_id, name, phone, lat, lng, address, comment, total, status, created_at, updated_at,
                                  subtotal, discount, delivery_fee, promo_code)
               VALUES(?,?,?,?,?,?,?,?, 'new', ?, ?, ?, ?, ?, ?)""",
            (tg_id, name, cust["phone"], cust["lat"], cust["lng"], address or cust["address"] or "", comment, total, t, t,
             round(subtotal, 2), discount, fee, promo["code"] if promo else ""),
        )
        order_id = cur.lastrowid
        c.executemany("INSERT INTO order_items(order_id, product_id, name, unit, price, qty) VALUES(?,?,?,?,?,?)",
                      [(order_id, *ln) for ln in lines])
        if promo:
            c.execute("UPDATE promocodes SET used_count=used_count+1 WHERE code=?", (promo["code"],))

    threading.Thread(target=notify_admin_new_order, args=(order_id,), daemon=True).start()
    extra = f"\nChegirma: −{fmt_money(discount)}" if discount else ""
    extra += f"\nYetkazib berish: {fmt_money(fee)}" if fee else ""
    tg_async("sendMessage", {"chat_id": tg_id, "text": f"✅ Buyurtma #{order_id} qabul qilindi!{extra}\nJami: {fmt_money(total)}\n\nTez orada siz bilan bog'lanamiz."})
    h.send_json({"ok": True, "order_id": order_id, "total": total, "discount": discount, "delivery_fee": fee})


def r_my_orders(h, q):
    user = webapp_user(h)
    if not user:
        return h.error(401, "Telegram orqali oching")
    with db() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM orders WHERE tg_id=? ORDER BY id DESC LIMIT 50", (int(user["id"]),))]
    h.send_json({"orders": [order_public(load_order(i)) for i in ids]})


def r_my_order_cancel(h, q, order_id):
    user = webapp_user(h)
    if not user:
        return h.error(401, "Telegram orqali oching")
    with db() as c:
        row = c.execute("SELECT * FROM orders WHERE id=? AND tg_id=?", (int(order_id), int(user["id"]))).fetchone()
        if not row:
            return h.error(404, "Buyurtma topilmadi")
        if row["status"] != "new":
            return h.error(400, "Bu buyurtma allaqachon qabul qilingan. Bekor qilish uchun do'kon bilan bog'laning")
        c.execute("UPDATE orders SET status='cancelled', updated_at=? WHERE id=?", (now(), int(order_id)))
        if row["promo_code"]:
            c.execute("UPDATE promocodes SET used_count=MAX(0, used_count-1) WHERE code=?", (row["promo_code"],))
    chat = get_setting("admin_chat_id", "")
    if chat:
        tg_async("sendMessage", {"chat_id": chat, "text": f"❌ Mijoz #{order_id} buyurtmani bekor qildi ({row['name']}, {row['phone']})"})
    h.send_json({"ok": True})


# ---- admin (POS ilova) ----
def admin_only(fn):
    def wrapper(h, q, *args):
        if not ADMIN_KEY:
            return h.error(503, "Serverda ADMIN_KEY sozlanmagan")
        if not h.is_admin():
            return h.error(401, "Admin kaliti noto'g'ri")
        return fn(h, q, *args)
    return wrapper


@admin_only
def r_admin_ping(h, q):
    with db() as c:
        n_new = c.execute("SELECT COUNT(*) AS n FROM orders WHERE status='new'").fetchone()["n"]
        n_prod = c.execute("SELECT COUNT(*) AS n FROM products WHERE active=1").fetchone()["n"]
        n_cust = c.execute("SELECT COUNT(*) AS n FROM customers WHERE phone<>''").fetchone()["n"]
    h.send_json({
        "ok": True,
        "version": 2,
        "new_orders": n_new,
        "products": n_prod,
        "customers": n_cust,
        "bot": get_setting("bot_username", ""),
        "admin_chat": bool(get_setting("admin_chat_id", "")),
        "webapp_url": webapp_url(),
        "telegram": bool(BOT_TOKEN),
        "persistent": os.path.isdir("/data") and DATA_DIR.startswith("/data"),
    })


@admin_only
def r_admin_sync(h, q):
    body = h.read_json()
    products = body.get("products")
    if not isinstance(products, list):
        raise ValueError("products ro'yxati kerak")
    shop = body.get("shop") or {}
    t = now()
    need_images = []
    with _db_lock, db() as c:
        existing = {r["id"] for r in c.execute("SELECT id FROM products")}
        have_img = {r["key"]: r["hash"] for r in c.execute("SELECT key, hash FROM images")}
        seen = set()
        for i, p in enumerate(products[:5000]):
            pid = str(p.get("id") or "").strip()
            name = str(p.get("name") or "").strip()
            if not pid or not name:
                continue
            seen.add(pid)
            img_hash = str(p.get("img_hash") or "")
            extras = [str(x or "") for x in (p.get("extra_hashes") or [])][:MAX_EXTRA_IMAGES]
            c.execute(
                """INSERT INTO products(id, name, category, unit, price, stock, active, img_hash, sort, updated_at,
                                        description, old_price, unlimited, extra_imgs)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, category=excluded.category, unit=excluded.unit,
                     price=excluded.price, stock=excluded.stock, active=excluded.active, img_hash=excluded.img_hash,
                     sort=excluded.sort, updated_at=excluded.updated_at, description=excluded.description,
                     old_price=excluded.old_price, unlimited=excluded.unlimited, extra_imgs=excluded.extra_imgs""",
                (pid, name[:200], str(p.get("category") or "")[:100], str(p.get("unit") or "dona")[:20],
                 float(p.get("price") or 0), float(p.get("stock") or 0), 1 if p.get("active", True) else 0,
                 img_hash, i, t, str(p.get("description") or "")[:2000], float(p.get("old_price") or 0),
                 1 if p.get("unlimited") else 0, json.dumps(extras)),
            )
            if img_hash and have_img.get("p_" + pid) != img_hash:
                need_images.append({"id": pid, "n": 0})
            if not img_hash and ("p_" + pid) in have_img:
                c.execute("DELETE FROM images WHERE key=?", ("p_" + pid,))
            for n in range(1, MAX_EXTRA_IMAGES + 1):
                key = f"p_{pid}__{n}"
                hsh = extras[n - 1] if n - 1 < len(extras) else ""
                if hsh and have_img.get(key) != hsh:
                    need_images.append({"id": pid, "n": n})
                if not hsh and key in have_img:
                    c.execute("DELETE FROM images WHERE key=?", (key,))
        gone = [pid for pid in existing if pid not in seen]
        for pid in gone:
            c.execute("UPDATE products SET active=0 WHERE id=?", (pid,))
            c.execute("DELETE FROM images WHERE key=? OR key LIKE ?", ("p_" + pid, f"p_{pid}__%"))
    if shop.get("name"):
        set_setting("shop_name", str(shop["name"])[:100])
    if "phone" in shop:
        set_setting("shop_phone", str(shop.get("phone") or "")[:40])
    need_logo = False
    logo_hash = str(shop.get("logo_hash") or "")
    if logo_hash and logo_hash != get_setting("logo_hash", ""):
        need_logo = True
    if not logo_hash and get_setting("logo_hash", ""):
        set_setting("logo_hash", "")
        with db() as c:
            c.execute("DELETE FROM images WHERE key='logo'")
    set_setting("catalog_updated", t)
    h.send_json({
        "ok": True, "count": len(seen), "removed": len(gone),
        "need_images": [x["id"] for x in need_images if x["n"] == 0],
        "need_images_v2": need_images,
        "need_logo": need_logo,
    })


def parse_data_url(s, max_bytes=2 * 1024 * 1024):
    m = re.match(r"^data:(image/[a-z0-9.+-]+);base64,(.+)$", s or "", re.S)
    if not m:
        raise ValueError("Rasm formati noto'g'ri")
    data = base64.b64decode(m.group(2))
    if len(data) > max_bytes:
        raise ValueError("Rasm juda katta")
    return m.group(1), data


def save_image(key, mime, data, img_hash):
    with db() as c:
        c.execute("INSERT INTO images(key, mime, data, hash) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET mime=excluded.mime, data=excluded.data, hash=excluded.hash",
                  (key, mime, sqlite3.Binary(data), img_hash))


@admin_only
def r_admin_image(h, q, pid):
    pid = urllib.parse.unquote(pid)
    body = h.read_json(limit=4 * 1024 * 1024)
    mime, data = parse_data_url(body.get("data"))
    img_hash = str(body.get("hash") or hashlib.sha1(data).hexdigest()[:16])
    n = int(q.get("n", "0") or 0)
    if n < 0 or n > MAX_EXTRA_IMAGES:
        raise ValueError("Rasm raqami noto'g'ri")
    key = "p_" + pid + (f"__{n}" if n else "")
    save_image(key, mime, data, img_hash)
    if not n:
        with db() as c:
            c.execute("UPDATE products SET img_hash=? WHERE id=?", (img_hash, pid))
    h.send_json({"ok": True})


@admin_only
def r_admin_logo(h, q):
    body = h.read_json(limit=4 * 1024 * 1024)
    mime, data = parse_data_url(body.get("data"))
    img_hash = str(body.get("hash") or hashlib.sha1(data).hexdigest()[:16])
    save_image("logo", mime, data, img_hash)
    set_setting("logo_hash", img_hash)
    h.send_json({"ok": True})


# ---- do'kon sozlamalari (POS 🔑 paneli) ----
@admin_only
def r_admin_settings_get(h, q):
    with db() as c:
        promos = [dict(r) for r in c.execute("SELECT * FROM promocodes ORDER BY created_at DESC")]
    h.send_json({
        "shop": shop_public(),
        "shop_info": get_json_setting("shop_info", {}),
        "delivery": {**DEFAULT_DELIVERY, **get_json_setting("delivery", {})},
        "min_order": {**DEFAULT_MIN_ORDER, **get_json_setting("min_order", {})},
        "channels": get_json_setting("channels", []),
        "banners": get_json_setting("banners", []),
        "promos": promos,
        "broadcast": get_json_setting("broadcast_status", {}),
        "bot": get_setting("bot_username", ""),
        "admin_chat": bool(get_setting("admin_chat_id", "")),
    })


@admin_only
def r_admin_settings_put(h, q):
    body = h.read_json()
    if "shop_info" in body:
        si = body["shop_info"] or {}
        phones = [str(p).strip()[:30] for p in (si.get("phones") or []) if str(p).strip()][:3]
        addrs = []
        for a in (si.get("addresses") or [])[:3]:
            if not isinstance(a, dict) or not str(a.get("text") or "").strip():
                continue
            item = {"text": str(a["text"]).strip()[:200]}
            try:
                if a.get("lat") not in (None, "") and a.get("lng") not in (None, ""):
                    item["lat"] = float(a["lat"])
                    item["lng"] = float(a["lng"])
            except (TypeError, ValueError):
                pass
            addrs.append(item)
        set_json_setting("shop_info", {"description": str(si.get("description") or "")[:1000], "phones": phones, "addresses": addrs})
    if "delivery" in body:
        d = body["delivery"] or {}
        set_json_setting("delivery", {
            "enabled": bool(d.get("enabled")),
            "price": max(0.0, float(d.get("price") or 0)),
            "free_from": max(0.0, float(d.get("free_from") or 0)),
            "note": str(d.get("note") or "")[:200],
        })
    if "min_order" in body:
        m = body["min_order"] or {}
        set_json_setting("min_order", {"enabled": bool(m.get("enabled")), "amount": max(0.0, float(m.get("amount") or 0))})
    if "channels" in body:
        chans = []
        for ch in (body["channels"] or [])[:5]:
            cid = str((ch or {}).get("chat") or "").strip()
            if cid:
                chans.append({"chat": cid[:100], "title": str(ch.get("title") or cid)[:100]})
        set_json_setting("channels", chans)
    h.send_json({"ok": True, "shop": shop_public()})


@admin_only
def r_admin_banner_put(h, q, bid):
    body = h.read_json(limit=4 * 1024 * 1024)
    mime, data = parse_data_url(body.get("data"), max_bytes=3 * 1024 * 1024)
    img_hash = hashlib.sha1(data).hexdigest()[:16]
    banners = get_json_setting("banners", [])
    bid = re.sub(r"[^a-zA-Z0-9_-]", "", bid)[:40] or uuid_short()
    save_image("b_" + bid, mime, data, img_hash)
    found = False
    for b in banners:
        if b.get("id") == bid:
            b["hash"] = img_hash
            b["link"] = str(body.get("link") or b.get("link") or "")[:300]
            found = True
    if not found:
        if len(banners) >= MAX_BANNERS:
            raise ValueError(f"Ko'pi bilan {MAX_BANNERS} ta banner")
        banners.append({"id": bid, "hash": img_hash, "link": str(body.get("link") or "")[:300]})
    set_json_setting("banners", banners)
    h.send_json({"ok": True, "banners": banners})


@admin_only
def r_admin_banner_delete(h, q, bid):
    banners = [b for b in get_json_setting("banners", []) if b.get("id") != bid]
    set_json_setting("banners", banners)
    with db() as c:
        c.execute("DELETE FROM images WHERE key=?", ("b_" + bid,))
    h.send_json({"ok": True, "banners": banners})


@admin_only
def r_admin_banner_order(h, q):
    order = [str(x) for x in (h.read_json().get("order") or [])]
    banners = get_json_setting("banners", [])
    banners.sort(key=lambda b: order.index(b["id"]) if b["id"] in order else 999)
    set_json_setting("banners", banners)
    h.send_json({"ok": True, "banners": banners})


@admin_only
def r_admin_promo_put(h, q):
    p = h.read_json()
    code = re.sub(r"[^A-Z0-9_-]", "", str(p.get("code") or "").upper())[:30]
    if len(code) < 3:
        raise ValueError("Promokod kamida 3 belgi (lotin harf/raqam)")
    kind = "percent" if p.get("kind") == "percent" else "fixed"
    value = float(p.get("value") or 0)
    if value <= 0 or (kind == "percent" and value > 100):
        raise ValueError("Chegirma qiymati noto'g'ri")
    expires = 0
    if p.get("expires_days"):
        expires = now() + int(float(p["expires_days"]) * 86400)
    elif p.get("expires_at"):
        expires = int(p["expires_at"])
    with db() as c:
        c.execute(
            """INSERT INTO promocodes(code, kind, value, min_sum, usage_limit, used_count, active, expires_at, created_at)
               VALUES(?,?,?,?,?,0,?,?,?)
               ON CONFLICT(code) DO UPDATE SET kind=excluded.kind, value=excluded.value, min_sum=excluded.min_sum,
                 usage_limit=excluded.usage_limit, active=excluded.active, expires_at=excluded.expires_at""",
            (code, kind, value, max(0.0, float(p.get("min_sum") or 0)), max(0, int(p.get("usage_limit") or 0)),
             1 if p.get("active", True) else 0, expires, now()),
        )
        promos = [dict(r) for r in c.execute("SELECT * FROM promocodes ORDER BY created_at DESC")]
    h.send_json({"ok": True, "promos": promos})


@admin_only
def r_admin_promo_delete(h, q, code):
    with db() as c:
        c.execute("DELETE FROM promocodes WHERE code=?", (urllib.parse.unquote(code).upper(),))
        promos = [dict(r) for r in c.execute("SELECT * FROM promocodes ORDER BY created_at DESC")]
    h.send_json({"ok": True, "promos": promos})


# ---- xabar yuborish (rassilka) ----
_broadcast_lock = threading.Lock()


def run_broadcast(text, image_bytes, image_mime, button):
    with db() as c:
        ids = [r["tg_id"] for r in c.execute("SELECT tg_id FROM customers WHERE phone<>''")]
    status = {"running": True, "total": len(ids), "sent": 0, "failed": 0, "started_at": now(), "text": text[:80]}
    set_json_setting("broadcast_status", status)
    file_id = None
    for i, chat in enumerate(ids):
        markup = inline_open_shop(button) if button else None
        ok = False
        try:
            if image_bytes:
                res = tg_send_photo(chat, image_bytes if not file_id else None, image_mime, text, markup, file_id=file_id)
                if res and res.get("ok"):
                    ok = True
                    if not file_id:
                        photos = res["result"].get("photo") or []
                        if photos:
                            file_id = photos[-1]["file_id"]
            else:
                payload = {"chat_id": chat, "text": text}
                if markup:
                    payload["reply_markup"] = markup
                res = tg_call("sendMessage", payload)
                ok = bool(res and res.get("ok"))
        except Exception:
            ok = False
        status["sent" if ok else "failed"] += 1
        if i % 10 == 0:
            set_json_setting("broadcast_status", status)
        time.sleep(0.05)
    status["running"] = False
    status["finished_at"] = now()
    set_json_setting("broadcast_status", status)
    _broadcast_lock.release()


@admin_only
def r_admin_broadcast(h, q):
    body = h.read_json(limit=5 * 1024 * 1024)
    text = str(body.get("text") or "").strip()[:1000]
    if not text:
        raise ValueError("Xabar matnini yozing")
    image_bytes, mime = None, None
    if body.get("image"):
        mime, image_bytes = parse_data_url(body["image"], max_bytes=4 * 1024 * 1024)
    if not BOT_TOKEN:
        return h.error(503, "BOT_TOKEN yo'q")
    if not _broadcast_lock.acquire(blocking=False):
        return h.error(409, "Oldingi xabar hali yuborilmoqda")
    button = str(body.get("button") or "").strip()[:40]
    threading.Thread(target=run_broadcast, args=(text, image_bytes, mime, button), daemon=True).start()
    with db() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM customers WHERE phone<>''").fetchone()["n"]
    h.send_json({"ok": True, "total": n})


@admin_only
def r_admin_broadcast_status(h, q):
    h.send_json(get_json_setting("broadcast_status", {}))


# ---- kanal / guruhga mahsulot joylash ----
def product_caption(p):
    lines = [f"🛍 {p['name']}"]
    if p.get("description"):
        lines.append("")
        lines.append(p["description"][:700])
    lines.append("")
    price = f"💰 {fmt_money(p['price'])}"
    if float(p.get("old_price") or 0) > float(p.get("price") or 0):
        pct = round((1 - float(p["price"]) / float(p["old_price"])) * 100)
        price += f"  ̶{fmt_money(p['old_price'])}  (−{pct}%)"
    lines.append(price)
    if not p.get("unlimited"):
        lines.append(f"📦 Mavjud: {fmt_qty(p['stock'])} {p.get('unit') or 'dona'}")
    return "\n".join(lines)[:1024]


@admin_only
def r_admin_post_product(h, q):
    body = h.read_json()
    pid = str(body.get("product_id") or "")
    chat = str(body.get("chat") or "").strip()
    if not chat:
        raise ValueError("Kanal yoki guruh tanlanmagan")
    with db() as c:
        row = c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        img = c.execute("SELECT mime, data FROM images WHERE key=?", ("p_" + pid,)).fetchone() if row else None
    if not row:
        return h.error(404, "Mahsulot serverda topilmadi. Avval katalogni yuboring")
    p = dict(row)
    bot = get_setting("bot_username", "")
    markup = {"inline_keyboard": [[{"text": "🛒 Buyurtma berish", "url": f"https://t.me/{bot}?start=p_{pid}"}]]} if bot else None
    caption = product_caption(p)
    if img:
        res = tg_send_photo(chat, bytes(img["data"]), img["mime"], caption, markup)
    else:
        payload = {"chat_id": chat, "text": caption}
        if markup:
            payload["reply_markup"] = markup
        res = tg_call("sendMessage", payload)
    if not res or not res.get("ok"):
        desc = (res or {}).get("description", "Telegram javob bermadi")
        return h.error(400, "Yuborilmadi: " + desc + ". Bot kanal/guruhda admin ekanini tekshiring")
    h.send_json({"ok": True})


# ---- statistika ----
@admin_only
def r_admin_stats(h, q):
    days = max(1, min(365, int(q.get("days", "30") or 30)))
    since = now() - days * 86400
    with db() as c:
        tot = c.execute("""SELECT COUNT(*) AS n, COALESCE(SUM(total),0) AS s FROM orders
                           WHERE created_at>? AND status<>'cancelled'""", (since,)).fetchone()
        canc = c.execute("SELECT COUNT(*) AS n FROM orders WHERE created_at>? AND status='cancelled'", (since,)).fetchone()["n"]
        cust_new = c.execute("SELECT COUNT(*) AS n FROM customers WHERE created_at>? AND phone<>''", (since,)).fetchone()["n"]
        cust_all = c.execute("SELECT COUNT(*) AS n FROM customers WHERE phone<>''").fetchone()["n"]
        buyers = c.execute("SELECT COUNT(DISTINCT tg_id) AS n FROM orders WHERE created_at>? AND status<>'cancelled'", (since,)).fetchone()["n"]
        top = [dict(r) for r in c.execute("""SELECT oi.name, SUM(oi.qty) AS qty, SUM(oi.qty*oi.price) AS sum
                    FROM order_items oi JOIN orders o ON o.id=oi.order_id
                    WHERE o.created_at>? AND o.status<>'cancelled'
                    GROUP BY oi.product_id ORDER BY sum DESC LIMIT 10""", (since,))]
        top_customers = [dict(r) for r in c.execute("""SELECT name, phone, COUNT(*) AS orders, SUM(total) AS sum
                    FROM orders WHERE created_at>? AND status<>'cancelled'
                    GROUP BY tg_id ORDER BY sum DESC LIMIT 10""", (since,))]
        daily = [dict(r) for r in c.execute("""SELECT date(created_at, 'unixepoch', '+5 hours') AS day, COUNT(*) AS n, SUM(total) AS sum
                    FROM orders WHERE created_at>? AND status<>'cancelled' GROUP BY day ORDER BY day""", (since,))]
    h.send_json({
        "days": days, "orders": tot["n"], "revenue": tot["s"], "cancelled": canc,
        "avg": (tot["s"] / tot["n"]) if tot["n"] else 0,
        "customers_new": cust_new, "customers_all": cust_all, "buyers": buyers,
        "top_products": top, "top_customers": top_customers, "daily": daily,
    })


@admin_only
def r_admin_orders(h, q):
    status = q.get("status", "")
    limit = max(1, min(200, int(q.get("limit", "100") or 100)))
    with db() as c:
        if status and status != "all":
            ids = [r["id"] for r in c.execute("SELECT id FROM orders WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))]
        else:
            ids = [r["id"] for r in c.execute("SELECT id FROM orders ORDER BY id DESC LIMIT ?", (limit,))]
        counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) AS n FROM orders GROUP BY status")}
    h.send_json({"orders": [order_public(load_order(i)) for i in ids], "counts": counts, "server_time": now()})


@admin_only
def r_admin_order_status(h, q, order_id):
    body = h.read_json()
    status = str(body.get("status") or "")
    if status not in STATUS_LABELS:
        raise ValueError("Holat noto'g'ri")
    with db() as c:
        row = c.execute("SELECT status FROM orders WHERE id=?", (int(order_id),)).fetchone()
        if not row:
            return h.error(404, "Buyurtma topilmadi")
        c.execute("UPDATE orders SET status=?, updated_at=?, pos_sale_id=COALESCE(NULLIF(?, ''), pos_sale_id) WHERE id=?",
                  (status, now(), str(body.get("pos_sale_id") or ""), int(order_id)))
    if row["status"] != status and body.get("notify", True):
        notify_customer_status(int(order_id), status)
    h.send_json({"ok": True, "order": order_public(load_order(int(order_id)))})


@admin_only
def r_admin_customers(h, q):
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT tg_id, first_name, last_name, username, phone, lat, lng, address, created_at FROM customers WHERE phone<>'' ORDER BY created_at DESC LIMIT 1000")]
    h.send_json({"customers": rows})


def r_webhook(h, q):
    if not WEBHOOK_SECRET or not hmac.compare_digest(h.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), WEBHOOK_SECRET):
        return h.error(403, "forbidden")
    update = h.read_json(limit=1024 * 1024)
    h.send_json({"ok": True})
    try:
        handle_update(update)
    except Exception:
        log.exception("Update xatosi")


ROUTES = {
    ("GET", "/health"): r_health,
    ("GET", "/api/shop"): r_shop,
    ("GET", "/api/catalog"): r_catalog,
    ("GET", "/api/me"): r_me,
    ("POST", "/api/me/lang"): r_me_lang,
    ("POST", "/api/me/contact"): r_me_contact,
    ("POST", "/api/me/location"): r_me_location,
    ("POST", "/api/promo/check"): r_promo_check,
    ("POST", "/api/orders"): r_create_order,
    ("GET", "/api/my/orders"): r_my_orders,
    ("GET", "/api/admin/ping"): r_admin_ping,
    ("POST", "/api/admin/sync"): r_admin_sync,
    ("PUT", "/api/admin/logo"): r_admin_logo,
    ("GET", "/api/admin/orders"): r_admin_orders,
    ("GET", "/api/admin/customers"): r_admin_customers,
    ("GET", "/api/admin/settings"): r_admin_settings_get,
    ("PUT", "/api/admin/settings"): r_admin_settings_put,
    ("POST", "/api/admin/banners/order"): r_admin_banner_order,
    ("PUT", "/api/admin/promo"): r_admin_promo_put,
    ("POST", "/api/admin/broadcast"): r_admin_broadcast,
    ("GET", "/api/admin/broadcast"): r_admin_broadcast_status,
    ("POST", "/api/admin/post"): r_admin_post_product,
    ("GET", "/api/admin/stats"): r_admin_stats,
    ("POST", "/tg/webhook"): r_webhook,
    ("GET", "/img/logo"): lambda h, q: r_image(h, q, "logo"),
}

PATTERN_ROUTES = [
    (("GET", re.compile(r"/img/p/([^/]+)")), lambda h, q, key: r_image(h, q, "p", key)),
    (("GET", re.compile(r"/img/b/([^/]+)")), lambda h, q, key: r_image(h, q, "b", key)),
    (("PUT", re.compile(r"/api/admin/image/([^/]+)")), r_admin_image),
    (("PUT", re.compile(r"/api/admin/banner/([^/]+)")), r_admin_banner_put),
    (("DELETE", re.compile(r"/api/admin/banner/([^/]+)")), r_admin_banner_delete),
    (("DELETE", re.compile(r"/api/admin/promo/([^/]+)")), r_admin_promo_delete),
    (("POST", re.compile(r"/api/admin/orders/(\d+)/status")), r_admin_order_status),
    (("POST", re.compile(r"/api/my/orders/(\d+)/cancel")), r_my_order_cancel),
]


def main():
    init_db()
    if not ADMIN_KEY or len(ADMIN_KEY) < 8:
        log.warning("ADMIN_KEY yo'q yoki juda qisqa — POS ulanishi ishlamaydi")
    threading.Thread(target=setup_telegram, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    log.info("Server ishga tushdi: port %s, baza %s", PORT, DB_PATH)
    srv.serve_forever()


if __name__ == "__main__":
    main()
