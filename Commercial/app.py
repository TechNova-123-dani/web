import os, json, base64, sqlite3, datetime, time
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import requests
from twilio.rest import Client as TwilioClient

load_dotenv()

app = Flask(__name__)
CORS(app)
app.secret_key = os.getenv("SECRET_KEY", "dev_secret_key")
DB_PATH = "store.db"

# --------- Config ---------
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:5000")

DARAJA_ENV = os.getenv("DARAJA_ENV", "sandbox")
CONSUMER_KEY = os.getenv("DARAJA_CONSUMER_KEY")
CONSUMER_SECRET = os.getenv("DARAJA_CONSUMER_SECRET")
SHORTCODE = os.getenv("DARAJA_SHORTCODE")
PASSKEY = os.getenv("DARAJA_PASSKEY")
PARTYB = os.getenv("DARAJA_PARTYB", SHORTCODE)
ACCOUNT_REF = os.getenv("DARAJA_ACCOUNT_REF", "Technova")
TXN_DESC = os.getenv("DARAJA_TRANSACTION_DESC", "Order Payment")

if DARAJA_ENV.lower() == "production":
    OAUTH_URL = "https://api.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
    STK_URL = "https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest"
else:
    OAUTH_URL = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
    STK_URL = "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest"

# Twilio (optional)
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_WHATSAPP_FROM")
WHATSAPP_OWNER = os.getenv("WHATSAPP_OWNER")
twilio_client = None
if TWILIO_SID and TWILIO_TOKEN:
    try:
        twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
    except Exception as e:
        print("Twilio init error:", e)

# --------- Demo catalog (products + services) ---------
PRODUCTS = [
    {"id": 1, "type": "product", "name": "Starter Web Design", "price": 14900, "img": "https://images.unsplash.com/photo-1498050108023-c5249f4df085?q=80&w=1200&auto=format&fit=crop", "short": "Single-page site (basic)", "desc": "Responsive landing page, contact form, basic SEO."},
    {"id": 2, "type": "product", "name": "Business Website", "price": 49900, "img": "https://images.unsplash.com/photo-1529336953121-adb1182b1d5a?q=80&w=1200&auto=format&fit=crop", "short": "5-page business site", "desc": "Home, About, Services, Blog, Contact + analytics."},
    {"id": 3, "type": "product", "name": "Logo & Brand Kit", "price": 17900, "img": "https://images.unsplash.com/photo-1520975916090-3105956dac38?q=80&w=1200&auto=format&fit=crop", "short": "Logo + colors + fonts", "desc": "Logo, palette, fonts, social media kit."},
]

SERVICES = [
    {"id": 101, "type": "service", "name": "KRA Registration", "price": 3500, "img": "https://images.unsplash.com/photo-1521791136064-7986c2920216?q=80&w=1200&auto=format&fit=crop", "short": "Guided KRA setup", "desc": "KRA PIN assistance, guidance & verification."},
    {"id": 102, "type": "service", "name": "eCitizen Account Setup", "price": 2500, "img": "https://images.unsplash.com/photo-1454165804606-c3d57bc86b40?q=80&w=1200&auto=format&fit=crop", "short": "Account + verification", "desc": "Assisted account creation and setup."},
    {"id": 103, "type": "service", "name": "SEO Starter", "price": 12900, "img": "https://images.unsplash.com/photo-1454165804606-c3d57bc86b40?q=80&w=1200&auto=format&fit=crop", "short": "On-page basics", "desc": "Meta tags, sitemap/robots, analytics hookup."},
]

# --------- DB Setup ---------
def db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT NOT NULL,
            address TEXT,
            items_json TEXT NOT NULL,
            total INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            mpesa_merchant_request_id TEXT,
            mpesa_checkout_request_id TEXT,
            mpesa_result_code TEXT,
            mpesa_result_desc TEXT,
            created_at TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()

init_db()

# --------- Helpers ---------
def all_catalog():
    return PRODUCTS + SERVICES

def find_item(item_id):
    for it in all_catalog():
        if it["id"] == item_id:
            return it
    return None

def shilling(x):
    return f"KSh {x:,.0f}"

def now_iso():
    return datetime.datetime.now().isoformat()

def ensure_cart():
    session.setdefault("cart", {})  # { id_str: qty }

def cart_items_and_total():
    ensure_cart()
    items = []
    total = 0
    for id_str, qty in session["cart"].items():
        it = find_item(int(id_str))
        if it:
            line = it["price"] * qty
            total += line
            items.append({"id": it["id"], "type": it["type"], "name": it["name"], "price": it["price"], "qty": qty, "img": it["img"], "line": line})
    return items, total

def send_whatsapp(text):
    if not twilio_client:
        return
    try:
        twilio_client.messages.create(from_=TWILIO_FROM, to=WHATSAPP_OWNER, body=text)
    except Exception as e:
        print("WhatsApp send error:", e)

# --------- Daraja (M-Pesa) ---------
def daraja_token():
    resp = requests.get(OAUTH_URL, auth=(CONSUMER_KEY, CONSUMER_SECRET), timeout=15)
    resp.raise_for_status()
    return resp.json()["access_token"]

def lipa_password():
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    data = f"{SHORTCODE}{PASSKEY}{timestamp}".encode("utf-8")
    return base64.b64encode(data).decode("utf-8"), timestamp

def initiate_stk_push(phone, amount, order_id):
    token = daraja_token()
    password, timestamp = lipa_password()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    payload = {
        "BusinessShortCode": SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount,
        "PartyA": phone,         # Customer phone e.g. 2547xxxxxxxx
        "PartyB": PARTYB,
        "PhoneNumber": phone,
        "CallBackURL": f"{PUBLIC_BASE_URL}/mpesa/callback",
        "AccountReference": ACCOUNT_REF,
        "TransactionDesc": TXN_DESC
    }
    r = requests.post(STK_URL, headers=headers, json=payload, timeout=30)
    data = r.json()
    return data

# --------- Routes: Pages ---------
@app.context_processor
def inject_helpers():
    return {"money": shilling, "year": datetime.datetime.now().year}

@app.route("/")
def home():
    return render_template("index.html", products=PRODUCTS, services=SERVICES, active="home")

@app.route("/shop")
def shop():
    return render_template("shop.html", products=PRODUCTS, active="shop")

@app.route("/services")
def services():
    return render_template("services.html", services=SERVICES, active="services")

@app.route("/product/<int:item_id>")
def product_detail(item_id):
    p = find_item(item_id)
    if not p:
        flash("Item not found.")
        return redirect(url_for("shop"))
    return render_template("product.html", p=p, active="shop" if p["type"]=="product" else "services")

# --------- Routes: Cart ---------
@app.route("/cart")
def cart():
    items, total = cart_items_and_total()
    return render_template("cart.html", items=items, total=total, active="cart")

@app.route("/cart/add/<int:item_id>")
def cart_add(item_id):
    ensure_cart()
    session["cart"][str(item_id)] = session["cart"].get(str(item_id), 0) + 1
    session.modified = True
    flash("Added to cart.")
    return redirect(request.referrer or url_for("cart"))

@app.route("/cart/remove/<int:item_id>")
def cart_remove(item_id):
    ensure_cart()
    session["cart"].pop(str(item_id), None)
    session.modified = True
    flash("Removed from cart.")
    return redirect(url_for("cart"))

@app.route("/cart/update/<int:item_id>", methods=["POST"])
def cart_update(item_id):
    ensure_cart()
    qty = max(1, int(request.form.get("qty", 1)))
    session["cart"][str(item_id)] = qty
    session.modified = True
    flash("Quantity updated.")
    return redirect(url_for("cart"))

# --------- Checkout / Order ---------
@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    items, total = cart_items_and_total()
    if request.method == "GET":
        return render_template("checkout.html", items=items, total=total, active="")

    # POST: create order & trigger M-Pesa STK
    name = request.form.get("name","").strip()
    email = request.form.get("email","").strip()
    phone = request.form.get("phone","").strip()
    address = request.form.get("address","").strip()

    if not (name and email and phone and items):
        flash("Missing details or empty cart.")
        return redirect(url_for("checkout"))

    # normalize phone to 2547XXXXXXXX
    phone_norm = phone.replace("+", "").replace(" ", "")
    if phone_norm.startswith("0"):
        phone_norm = "254" + phone_norm[1:]
    elif phone_norm.startswith("7"):
        phone_norm = "254" + phone_norm

    con = db()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO orders (customer_name,email,phone,address,items_json,total,status,created_at)
        VALUES (?,?,?,?,?,?,?,?)
    """, (name, email, phone_norm, address, json.dumps(items), total, "PENDING", now_iso()))
    order_id = cur.lastrowid
    con.commit()

    # initiate M-Pesa STK
    resp = initiate_stk_push(phone_norm, total, order_id)
    merchant_id = resp.get("MerchantRequestID")
    checkout_id = resp.get("CheckoutRequestID")
    error_msg = resp.get("errorMessage")

    cur.execute("UPDATE orders SET mpesa_merchant_request_id=?, mpesa_checkout_request_id=?, mpesa_result_desc=? WHERE id=?",
                (merchant_id, checkout_id, error_msg, order_id))
    con.commit()
    con.close()

    # Notify owner (optional)
    send_whatsapp(f"🛒 New order #{order_id}\nCustomer: {name}\nTotal: {shilling(total)}\nStatus: PENDING")

    # clear cart to avoid double orders (we’ll show pending page)
    session["cart"] = {}
    session.modified = True

    return redirect(url_for("order_pending", order_id=order_id))

@app.route("/order/<int:order_id>/pending")
def order_pending(order_id):
    return render_template("order_pending.html", order_id=order_id, active="")

@app.route("/order/<int:order_id>/status")
def order_status(order_id):
    con = db()
    cur = con.cursor()
    cur.execute("SELECT status, mpesa_result_code, mpesa_result_desc FROM orders WHERE id=?", (order_id,))
    row = cur.fetchone()
    con.close()
    if not row:
        return jsonify({"status": "NOT_FOUND"})
    return jsonify({"status": row[0], "code": row[1], "desc": row[2]})

@app.route("/success/<int:order_id>")
def success(order_id):
    con = db()
    cur = con.cursor()
    cur.execute("SELECT customer_name,total,status FROM orders WHERE id=?", (order_id,))
    row = cur.fetchone()
    con.close()
    if not row:
        flash("Order not found.")
        return redirect(url_for("home"))
    name, total, status = row
    return render_template("success.html", name=name, total=total, status=status, active="")

# --------- Daraja Callback ---------
@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    data = request.get_json(force=True, silent=True) or {}
    try:
        body = data.get("Body", {})
        stk = body.get("stkCallback", {})
        result_code = str(stk.get("ResultCode", ""))
        result_desc = stk.get("ResultDesc", "")
        checkout_id = stk.get("CheckoutRequestID", "")

        con = db()
        cur = con.cursor()
        cur.execute("SELECT id FROM orders WHERE mpesa_checkout_request_id=?", (checkout_id,))
        row = cur.fetchone()

        if row:
            order_id = row[0]
            new_status = "PAID" if result_code == "0" else "FAILED"
            cur.execute("UPDATE orders SET status=?, mpesa_result_code=?, mpesa_result_desc=? WHERE id=?",
                        (new_status, result_code, result_desc, order_id))
            con.commit()
            con.close()

            # Notify owner
            send_whatsapp(f"✅ Order #{order_id} update: {new_status}\n{result_desc}")

        return jsonify({"ResultCode": 0, "ResultDesc": "OK"})
    except Exception as e:
        print("Callback error:", e)
        return jsonify({"ResultCode": 1, "ResultDesc": "Error"}), 200

# --------- Admin (simple read-only) ---------
@app.route("/admin/orders")
def admin_orders():
    con = db()
    cur = con.cursor()
    cur.execute("SELECT id, customer_name, phone, total, status, created_at FROM orders ORDER BY id DESC")
    rows = cur.fetchall()
    con.close()
    return render_template("success.html", name="Admin", total=0, status=f"Orders: {len(rows)}", active="")  # reuse simple template

if __name__ == "__main__":
    app.run(debug=True, port=5000)
