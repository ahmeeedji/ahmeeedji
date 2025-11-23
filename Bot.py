from telethon import TelegramClient, events, Button, types
from telethon.tl.types import User, Channel, Chat
import sqlite3
import re
import time
import datetime
import asyncio
import json
import os
import requests
from bs4 import BeautifulSoup

# --- إعدادات البوت ---
api_id = '197279'
api_hash = '2bd9137b824c3e31da244e1d4a097be5'
bot_token = "7780871648:AAFsv-Lf0gjsnag_YX8JQ7U6ih3tEdTqgOo"
admin_username = '@cpuin' 

client = TelegramClient('p2p_v5_location_fix', api_id, api_hash).start(bot_token=bot_token)
BOT_ID = None
ADMIN_ID = None

# تخزين مؤقت
PENDING_TRADES = {}   # {user_id: trade_data} لانتظار موقع المعلن
PENDING_BOOKINGS = {} # {user_id: order_id} لانتظار موقع الحاجز

# --- قاعدة البيانات (SQLite) ---
DB_NAME = "p2p_v5.db"

def init_and_migrate_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # المستخدمين (تم إزالة الأعمدة الخاصة بالموقع من هنا لأنها ستكون في الطلب، 
    # لكن نبقيها لعدم كسر التوافق مع البيانات القديمة إذا وجدت)
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        phone TEXT,
        bank_account TEXT,
        is_verified INTEGER DEFAULT 0,
        is_trusted INTEGER DEFAULT 0,
        is_banned INTEGER DEFAULT 0,
        reg_step TEXT DEFAULT 'start',
        join_date REAL
    )''')
    
    # الطلبات (تخزين الموقع هنا)
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id INTEGER PRIMARY KEY AUTOINCREMENT,
        maker_id INTEGER,
        maker_username TEXT,
        type TEXT,
        amount REAL,
        currency TEXT,
        total_price REAL,
        price_unit REAL,
        status TEXT,
        
        -- موقع المعلن لهذا الطلب تحديداً
        maker_lat REAL,
        maker_long REAL,
        
        taker_id INTEGER,
        taker_username TEXT,
        
        -- موقع الحاجز لهذا الطلب تحديداً
        taker_lat REAL,
        taker_long REAL,
        
        booked_at REAL,
        msg_ids TEXT DEFAULT '{}',
        created_at REAL
    )''')

    # ترحيل البيانات (لضمان وجود الأعمدة في القواعد القديمة)
    try: c.execute("ALTER TABLE orders ADD COLUMN maker_lat REAL")
    except: pass
    try: c.execute("ALTER TABLE orders ADD COLUMN maker_long REAL")
    except: pass
    try: c.execute("ALTER TABLE orders ADD COLUMN taker_lat REAL")
    except: pass
    try: c.execute("ALTER TABLE orders ADD COLUMN taker_long REAL")
    except: pass

    conn.commit()
    conn.close()

# --- Database Functions ---

def get_user(user_id):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return c.fetchone()

def get_user_by_username(username):
    username = username.replace('@', '')
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username LIKE ? OR username LIKE ?", (username, f"@{username}"))
    return c.fetchone()

def update_user(user_id, **kwargs):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, join_date) VALUES (?, ?)", (user_id, time.time()))
    
    cols = []
    vals = []
    for k, v in kwargs.items():
        cols.append(f"{k} = ?")
        vals.append(v)
    vals.append(user_id)
    
    c.execute(f"UPDATE users SET {', '.join(cols)} WHERE user_id = ?", tuple(vals))
    conn.commit()
    conn.close()

def is_active_user(user_id):
    u = get_user(user_id)
    if not u: return False
    if u['is_banned']: return False
    return u['is_verified'] == 1

def ban_user(user_id):
    update_user(user_id, is_banned=1)

def unban_user(user_id):
    update_user(user_id, is_banned=0)

def create_order_db(maker, data, lat, long):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    m_name = f"@{maker.username}" if maker.username else "Unknown"
    
    # تخزين الموقع مع الطلب نفسه (maker_lat, maker_long)
    c.execute('''INSERT INTO orders (
        maker_id, maker_username, type, amount, currency, total_price, price_unit, 
        maker_lat, maker_long, status, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'available', ?)''', 
    (maker.id, m_name, data['type'], data['amount'], data['currency'], 
     data['total'], data['unit'], lat, long, time.time()))
    
    oid = c.lastrowid
    conn.commit()
    conn.close()
    return oid

def get_order(oid):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE order_id = ?", (oid,))
    return c.fetchone()

def update_order(oid, **kwargs):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    cols = []
    vals = []
    for k, v in kwargs.items():
        cols.append(f"{k} = ?")
        vals.append(v)
    vals.append(oid)
    c.execute(f"UPDATE orders SET {', '.join(cols)} WHERE order_id = ?", tuple(vals))
    conn.commit()
    conn.close()

def save_msg_ids(order_id, msg_dict):
    update_order(order_id, msg_ids=json.dumps(msg_dict))

def get_msg_ids(order_id):
    o = get_order(order_id)
    try: return json.loads(o['msg_ids'])
    except: return {}

def get_all_active_users():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE is_verified = 1 AND is_banned = 0")
    return [r[0] for r in c.fetchall()]

def is_trusted(user_id):
    u = get_user(user_id)
    return u and u['is_trusted'] == 1

def set_trusted_user(username, status=1):
    username = username.replace('@', '')
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET is_trusted = ? WHERE username LIKE ?", (status, f"%{username}%"))
    found = c.rowcount > 0
    conn.commit()
    conn.close()
    return found

# --- تقارير الأدمن ---

def get_users_locations_report():
    # تقرير يعرض آخر المواقع المسجلة في الطلبات (بما أننا لا نخزن الموقع في جدول المستخدمين)
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # جلب آخر طلب لكل مستخدم لاستخراج أحدث موقع له
    c.execute("""
        SELECT u.user_id, u.username, u.phone, o.maker_lat, o.maker_long 
        FROM users u 
        JOIN orders o ON u.user_id = o.maker_id 
        WHERE o.maker_lat IS NOT NULL 
        GROUP BY u.user_id 
        ORDER BY o.created_at DESC
    """)
    rows = c.fetchall()
    conn.close()
    
    report = "🗺️ **Last Known Locations (From Orders)**\n\n"
    for r in rows:
        maps_link = f"https://www.google.com/maps?q={r['maker_lat']},{r['maker_long']}"
        report += (
            f"👤 @{r['username']} (ID: {r['user_id']})\n"
            f"📞 {r['phone']}\n"
            f"📍 Last Order Loc: {maps_link}\n"
            f"--------------------------------\n"
        )
    return report

def get_users_report(filter_trusted=False):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    sql = "SELECT * FROM users"
    if filter_trusted: sql += " WHERE is_trusted = 1"
    c.execute(sql)
    rows = c.fetchall()
    conn.close()
    
    report = "📋 **Users Report**\n\n"
    for r in rows:
        status = "⛔ BANNED" if r['is_banned'] else ("✅ Active" if r['is_verified'] else "⏳ Pending")
        report += f"ID: `{r['user_id']}` | @{r['username']}\n📞 {r['phone']} | {status}\n----------------\n"
    return report

def get_orders_report_full():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM orders ORDER BY order_id DESC LIMIT 50")
    rows = c.fetchall()
    conn.close()
    
    report = "📋 **Orders Report (Last 50)**\n\n"
    for r in rows:
        dt = datetime.datetime.fromtimestamp(r['created_at']).strftime('%m/%d %H:%M')
        loc_info = "📍 Loc: Yes" if r['maker_lat'] else "📍 Loc: No"
        report += (f"#{r['order_id']} | {r['status']} | {dt}\n"
                   f"Maker: @{r['maker_username']} {loc_info}\n"
                   f"Taker: @{r['taker_username']}\n"
                   f"💰 {r['amount']} {r['currency']} = {r['total_price']} SDG\n"
                   f"----------------\n")
    return report

# --- Rate Functions ---

def fetch_binance_p2p_prices():
    try:
        r = requests.post("https://www.binance.info/bapi/c2c/v2/public/c2c/adv/quoted-price", 
                          json={"fromUserRole":"USER","assets":["USDT"],"payType":"","fiatCurrency":"SDG","tradeType":"buy"}, 
                          headers={"User-Agent":"Mozilla/5.0"}, timeout=3).json()
        p = float(r['data'][0]["referencePrice"])
        return f"Binance: {p:,.2f} SDG", {'USDT': {'sell': p}}
    except: return "Binance: N/A", {}

def fetch_okx_p2p_prices():
    try:
        r = requests.get("https://www.okx.com/v4/c2c/express/price", 
                         params={"crypto":"USDT","fiat":"SDG","side":"buy"}, 
                         headers={"User-Agent":"Mozilla/5.0"}, timeout=3).json()
        p = float(r['data']['price'])
        return f"OKX: {p:,.2f} SDG", {'USDT': {'sell': p}}
    except: return "OKX: N/A", {}

def fetch_alsoug_rate():
    try:
        return "Alsoug: ~2800 SDG", {'USD': {'sell': 2800.0}} 
    except: return "Alsoug: N/A", {}

def fetch_crypto_ticker(ticker):
    ticker = ticker.upper()
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr", params={"symbol": f"{ticker}USDT"}, timeout=3).json()
        return {'price': float(r['lastPrice']), 'change': float(r['priceChangePercent']), 'vol': float(r['volume']), 'name': 'Binance'}
    except: return None

# --- Helper: Formatter ---
def format_order_msg(order):
    s = order['status']
    st_map = {
        'available': "متاح ✅", 'booked': "محجوز 🔒",
        'waiting_proof': "بانتظار الدفع ⏳", 'paid': "تم الدفع 💸",
        'waiting_maker_proof': "بانتظار إشعار البائع 📤",
        'waiting_final_confirm': "تأكيد الاستلام 🏁",
        'completed': "مكتمل 🎉", 'cancelled': "ملغي ❌", 'dispute': "🚨 نزاع", 'admin_hold': "⛔ مجمد"
    }
    type_icon = "🔴 بيع" if order['type'] == 'sell' else "🟢 شراء"
    trusted = "\n✅ **تاجر موثوق**" if order['taker_id'] and is_trusted(order['taker_id']) else ""
    
    # إضافة رابط الموقع إذا توفر (للأدمن فقط أو للتوضيح أن الموقع مسجل)
    # هنا نكتفي بكتابة "مسجل" للمستخدمين العاديين
    loc_status = "📍 الموقع: مسجل" if order['maker_lat'] else "📍 الموقع: غير مسجل"

    return (
        f"🆔 **طلب #{order['order_id']}**\n"
        f"----------------------------\n"
        f"{type_icon} `{order['amount']} {order['currency']}`\n"
        f"💰 السعر: `{order['total_price']:,.0f} SDG`\n"
        f"👤 المعلن: {order['maker_username']}\n"
        f"{loc_status}\n"
        f"📊 الحالة: {st_map.get(s, s)}"
        f"{trusted}"
    )

async def broadcast_update(oid, status_key):
    msg_ids = get_msg_ids(oid)
    o = get_order(oid)
    if not o: return
    txt = format_order_msg(o)
    btns = [Button.inline("✅ حجز", data=f"prebook_{oid}")] if status_key == 'available' else None
    for cid, mid in msg_ids.items():
        try: await client.edit_message(int(cid), int(mid), txt, buttons=btns)
        except: pass

# --- Handlers: Registration (No Location Here) ---

@client.on(events.NewMessage(pattern=r'(?i)/start'))
async def start_handler(event):
    if event.sender_id == BOT_ID or not event.is_private: return
    uid = event.sender_id
    u = get_user(uid)
    
    if u and u['is_verified']:
        return await event.reply("✅ حسابك مفعل.\nاستخدم `/services` لخدمات الأدمن\nاستخدم `/rate` للأسعار.")
    
    update_user(uid, reg_step='req_phone', username=event.sender.username, first_name=event.sender.first_name)
    await event.reply("👋 **مرحباً! للتسجيل:**\n1. شارك رقم هاتفك السوداني.\n2. سجل حسابك البنكي.\n\n👇 اضغط الزر:", 
                      buttons=[[Button.request_phone("📱 مشاركة الرقم", resize=True)]])

@client.on(events.NewMessage(func=lambda e: e.is_private and e.contact))
async def contact_handler(event):
    uid = event.sender_id
    u = get_user(uid)
    if not u or u['reg_step'] != 'req_phone': return
    
    if event.contact.user_id != uid: return await event.reply("❌ شارك رقمك الخاص.")
    phone = event.contact.phone_number.replace('+', '').strip()
    if not phone.startswith('249'): return await event.reply("❌ أرقام سودانية فقط.")
    
    # الانتقال لطلب البنك مباشرة (بدون موقع)
    update_user(uid, reg_step='req_bank', phone=phone)
    await event.reply("✅ تم حفظ الرقم.\n📝 **أرسل الآن رقم حسابك البنكي واسم البنك:**", buttons=Button.clear())

@client.on(events.NewMessage(func=lambda e: e.is_private and not e.text.startswith('/') and get_user(e.sender_id) and get_user(e.sender_id)['reg_step'] == 'req_bank'))
async def bank_handler(event):
    uid = event.sender_id
    update_user(uid, bank_account=event.text.strip(), is_verified=1, reg_step='done')
    await event.reply("🎉 تم التسجيل بنجاح!\nيمكنك الآن التداول (`متوفر ...` / `مطلوب ...`)")
    if ADMIN_ID: await client.send_message(ADMIN_ID, f"👤 مستخدم جديد: @{event.sender.username}")

# --- Handlers: Trade & Location (Per Order) ---

@client.on(events.NewMessage(func=lambda e: e.is_private and ('متوفر' in e.raw_text or 'مطلوب' in e.raw_text)))
async def trade_init(event):
    if event.sender_id == BOT_ID: return
    uid = event.sender_id
    if not is_active_user(uid): 
        u = get_user(uid)
        if u and u['is_banned']: return await event.reply("⛔ أنت محظور.")
        return await event.reply("⛔ غير مسجل. أرسل /start")

    text = event.raw_text.lower()
    amt_m = re.search(r'(\d+(\.\d+)?)\s*([a-zA-Z]+)', text)
    prc_m = re.search(r'(?:سعر|ب|اجمالي|إجمالي)\s*(\d+(\.\d+)?)', text)
    
    if amt_m and prc_m:
        # حفظ بيانات الطلب مؤقتاً
        PENDING_TRADES[uid] = {
            'type': 'sell' if 'متوفر' in text else 'buy',
            'amount': float(amt_m.group(1)), 'currency': amt_m.group(3).upper(),
            'total': float(prc_m.group(1)), 'unit': float(prc_m.group(1))/float(amt_m.group(1))
        }
        # طلب الموقع الخاص بهذا الأوردر
        await event.reply("📍 **لإتمام نشر الطلب، يجب مشاركة موقعك الحالي:**", 
                          buttons=[[Button.request_location("📍 إرسال الموقع", resize=True)]])

@client.on(events.NewMessage(func=lambda e: e.is_private and e.geo))
async def location_receiver(event):
    uid = event.sender_id
    lat, long = event.geo.lat, event.geo.long
    
    # 1. المعلن يرسل موقعه (نشر الطلب)
    if uid in PENDING_TRADES:
        data = PENDING_TRADES[uid]
        # حفظ الموقع مع الطلب في قاعدة البيانات
        oid = create_order_db(await event.get_sender(), data, lat, long)
        del PENDING_TRADES[uid]
        
        msg = format_order_msg(get_order(oid))
        btns = [Button.inline("✅ حجز", data=f"prebook_{oid}")]
        
        users = get_all_active_users()
        sent = {}
        for u in users:
            if u != uid:
                try:
                    m = await client.send_message(u, msg, buttons=btns)
                    sent[str(u)] = m.id
                except: pass
        save_msg_ids(oid, sent)
        await event.reply(f"✅ تم النشر #{oid}", buttons=Button.clear())
        if ADMIN_ID: await client.send_message(ADMIN_ID, f"🔔 طلب #{oid} (Loc: {lat},{long})")
        return

    # 2. الحاجز يرسل موقعه (تأكيد الحجز)
    if uid in PENDING_BOOKINGS:
        oid = PENDING_BOOKINGS[uid]
        del PENDING_BOOKINGS[uid]
        
        o = get_order(oid)
        if not o or o['status'] != 'available':
            return await event.reply("❌ الطلب لم يعد متاحاً.", buttons=Button.clear())
        
        # تحديث الطلب بموقع الحاجز وبياناته
        update_order(oid, status='booked', taker_id=uid, taker_username=f"@{event.sender.username}", 
                     taker_lat=lat, taker_long=long, booked_at=time.time())
        
        await broadcast_update(oid, 'booked')
        
        # إخطار البائع مع رابط موقع المشتري
        maps_link = f"https://www.google.com/maps?q={lat},{long}"
        await client.send_message(o['maker_id'], 
            f"🔔 **تم الحجز #{oid}**\nبواسطة @{event.sender.username}\n"
            f"📍 موقع المشتري: [Map]({maps_link})\n"
            "👇 **أرسل تفاصيل حسابك الآن.**",
            link_preview=False,
            buttons=Button.inline("❌ إلغاء", data=f"cancel_{oid}"))
        
        await event.reply("✅ **تم تسجيل موقعك وتأكيد الحجز.**\nانتظر تفاصيل البائع.", buttons=Button.clear())
        return

# --- Callbacks ---
@client.on(events.CallbackQuery)
async def cb_handler(event):
    data = event.data.decode('utf-8')
    uid = event.sender_id
    try: oid = int(data.split('_')[-1])
    except: oid = 0

    if data.startswith('prebook_'):
        if not is_active_user(uid): return await event.answer("غير مسجل/محظور", alert=True)
        o = get_order(oid)
        if not o or o['status'] != 'available': return await event.answer("غير متاح", alert=True)
        if o['maker_id'] == uid: return await event.answer("هذا طلبك", alert=True)
        
        # حفظ نية الحجز وطلب الموقع
        PENDING_BOOKINGS[uid] = oid
        await client.send_message(uid, "📍 **لتأكيد الحجز، يجب مشاركة موقعك الحالي:**", 
                                  buttons=[[Button.request_location("📍 إرسال الموقع", resize=True)]])
        await event.answer("راجع الخاص لإرسال الموقع", alert=True)

    elif data.startswith('cancel_'):
        o = get_order(oid)
        if o['status'] not in ['booked', 'available']: return
        update_order(oid, status='available', taker_id=None, taker_lat=None, taker_long=None)
        await broadcast_update(oid, 'available')
        await client.send_message(o['maker_id'], "⚠️ تم الإلغاء.")
        if o['taker_id']: await client.send_message(o['taker_id'], "⚠️ تم الإلغاء.")
        await event.delete()

    elif data.startswith('confirm_pay_'):
        update_order(oid, status='paid')
        await broadcast_update(oid, 'paid')
        o = get_order(oid)
        await client.send_message(o['maker_id'], f"💸 تم الدفع #{oid}. راجع حسابك.", 
            buttons=[Button.inline("✅ استلام", data=f"req_proof_{oid}"), Button.inline("🚨 نزاع", data=f"report_{oid}")])
        await event.edit("✅ تم التنبيه.")

    elif data.startswith('req_proof_'):
        update_order(oid, status='waiting_maker_proof')
        await broadcast_update(oid, 'waiting_maker_proof')
        await event.edit("✅ أرسل إشعارك.")

    elif data.startswith('final_complete_'):
        o = get_order(oid)
        update_order(oid, status='completed')
        await broadcast_update(oid, 'completed')
        await client.send_message(o['maker_id'], "🎉 اكتملت.")
        await client.send_message(o['taker_id'], "🎉 اكتملت.")
        await event.edit("✅ شكراً.")

    elif data.startswith('report_'):
        o = get_order(oid)
        update_order(oid, status='dispute')
        if ADMIN_ID:
            # أزرار الحظر للأدمن
            btns = [
                [Button.inline(f"🚫 حظر Maker", data=f"ban_{o['maker_id']}_{oid}")],
                [Button.inline(f"🚫 حظر Taker", data=f"ban_{o['taker_id']}_{oid}")]
            ]
            await client.send_message(ADMIN_ID, 
                f"🚨 **نزاع #{oid}**\nMaker: {o['maker_username']}\nTaker: {o['taker_username']}\nالمبلغ: {o['total_price']}",
                buttons=btns)
        await event.edit("🚨 تم رفع البلاغ.")

    elif data.startswith('ban_'):
        target_id = int(data.split('_')[1])
        ban_user(target_id)
        await event.answer("🚫 تم حظر المستخدم.", alert=True)
        if ADMIN_ID: await client.send_message(ADMIN_ID, f"تم حظر المستخدم {target_id}")

    elif data.startswith('srv_'):
        srv = data.split('_')[1]
        await event.answer(f"طلب خدمة {srv}. تواصل مع @cpuin", alert=True)
        if ADMIN_ID: await client.send_message(ADMIN_ID, f"📩 طلب خدمة {srv} من @{event.sender.username}")

# --- Chat Flow ---
@client.on(events.NewMessage)
async def chat_flow(event):
    if event.sender_id == BOT_ID or event.is_group: return
    uid = event.sender_id
    if event.text.startswith('/') or 'متوفر' in event.raw_text or 'مطلوب' in event.raw_text: return
    if not is_active_user(uid): return

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    # Maker -> Taker
    c.execute("SELECT * FROM orders WHERE maker_id = ? AND status = 'booked'", (uid,))
    mb = c.fetchone()
    # Taker -> Maker
    c.execute("SELECT * FROM orders WHERE taker_id = ? AND status = 'waiting_proof'", (uid,))
    tp = c.fetchone()
    # Maker -> Taker (Proof)
    c.execute("SELECT * FROM orders WHERE maker_id = ? AND status = 'waiting_maker_proof'", (uid,))
    mp = c.fetchone()
    conn.close()

    if mb:
        dest = mb['taker_id']
        bank = get_user(uid)['bank_account']
        await client.send_message(dest, f"📩 **الحساب:**\n`{bank}`\n\n{event.text}")
        update_order(mb['order_id'], status='waiting_proof')
        await client.send_message(dest, "👇 أرسل الإشعار.")
        return await event.reply("✅ تم الإرسال.")
    
    if tp:
        dest = tp['maker_id']
        if event.photo:
            await client.send_message(dest, "📸 **إشعار المشتري:**")
            await client.send_file(dest, event.photo)
            await event.reply("✅", buttons=Button.inline("تأكيد التحويل", data=f"confirm_pay_{tp['order_id']}"))
        else: await event.reply("⚠️ صورة.")
        return

    if mp:
        dest = mp['taker_id']
        await client.send_message(dest, "📸 **إشعار البائع:**")
        if event.media: await client.send_file(dest, event.media)
        else: await client.send_message(dest, event.text)
        update_order(mp['order_id'], status='waiting_final_confirm')
        await broadcast_update(mp['order_id'], 'waiting_final_confirm')
        await client.send_message(dest, "استلمت؟", buttons=[Button.inline("✅ نعم", data=f"final_complete_{mp['order_id']}"), Button.inline("🚨 لا", data=f"report_{mp['order_id']}")])
        return await event.reply("✅ تم.")

# --- Old Features & Admin ---

@client.on(events.NewMessage(pattern=r'(?i)^\s*rate\s*$'))
async def rate_cmd(event):
    if not is_active_user(event.sender_id): return
    b, _ = fetch_binance_p2p_prices()
    o, _ = fetch_okx_p2p_prices()
    a, _ = fetch_alsoug_rate()
    await event.reply(f"📊 **الأسعار:**\n{b}\n{o}\n{a}")

@client.on(events.NewMessage(pattern=r'(?i).*?\b(\d+(?:\.\d*)?)\s*(\w+)\b.*'))
async def calc_cmd(event):
    if event.sender_id == BOT_ID or not is_active_user(event.sender_id): return
    txt = event.raw_text.lower()
    if any(x in txt for x in ['rate', 'promote', 'متوفر', 'مطلوب']): return
    try:
        amt = float(event.pattern_match.group(1))
        tick = event.pattern_match.group(2).upper()
        c = fetch_crypto_ticker(tick)
        if c:
            msg = f"📊 **{c['name']}**\nPrice: ${c['price']}\nVol: {c['vol']}\nChg: {c['change']}%"
            await event.reply(msg)
    except: pass

@client.on(events.NewMessage(pattern=r'(?i)/services'))
async def services_cmd(event):
    btns = [
        [Button.inline("💳 استلام PayPal", data="srv_paypal")],
        [Button.inline("🏦 استلام Coinbase", data="srv_coinbase")],
        [Button.inline("📞 تواصل مع الإدارة", data="srv_contact")]
    ]
    await event.reply("🛠 **خدمات الإدارة:**", buttons=btns)

# Admin: Unban & Reports
@client.on(events.NewMessage(from_users=admin_username, pattern=r'(?i)/unban (.+)'))
async def unban_cmd(event):
    arg = event.pattern_match.group(1).strip()
    uid = None
    if arg.isdigit(): uid = int(arg)
    elif arg.startswith('@'): 
        u = get_user_by_username(arg)
        if u: uid = u['user_id']
    if uid:
        unban_user(uid)
        await event.reply(f"✅ تم فك الحظر عن {uid}")
    else: await event.reply("❌ مستخدم غير موجود.")

@client.on(events.NewMessage(from_users=admin_username, pattern=r'/locations'))
async def loc_report(event):
    report = get_users_locations_report()
    with open("locations.txt", "w", encoding="utf-8") as f: f.write(report)
    await client.send_file(event.chat_id, "locations.txt", caption="🗺️ Locations")

@client.on(events.NewMessage(from_users=admin_username, pattern=r'/(users|trusted|orders)(?: file)?'))
async def admin_reports(event):
    rtype = event.pattern_match.group(1)
    as_file = bool(event.pattern_match.group(0).endswith('file'))
    header, rows = "", []
    
    if rtype == 'users': header, rows = "", [get_users_report()]
    elif rtype == 'orders': header, rows = "", [get_orders_report_full()]

    text = header + "".join(rows)
    if as_file or len(text) > 4000:
        with open(f"{rtype}.txt", "w", encoding="utf-8") as f: f.write(text)
        await client.send_file(event.chat_id, f"{rtype}.txt")
    else: await event.reply(text)

# --- Startup ---
async def main():
    global BOT_ID, ADMIN_ID
    init_and_migrate_db()
    me = await client.get_me()
    BOT_ID = me.id
    print(f"Bot: {me.username}")
    try:
        admin = await client.get_input_entity(admin_username)
        ADMIN_ID = await client.get_peer_id(admin)
    except: pass
    
    await client.run_until_disconnected()

if __name__ == '__main__':
    client.loop.run_until_complete(main())
