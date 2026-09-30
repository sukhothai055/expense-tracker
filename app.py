from datetime import date
from flask import Flask, render_template, request, redirect, url_for
import requests
import urllib3
import threading
import time

# ปิดข้อความแจ้งเตือน SSL Warning
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# URL ของ Google Apps Script Web App
WEB_APP_URL = "https://script.google.com/macros/s/AKfycbylPAMK6fd01O8rEXnQOJ1WrVBPNbXpTQJg4EZ6Zpvd_rexZ9lbNAo33gifYakYaanU/exec"

# ---------- Memory Cache ----------
_DATA_CACHE = None
_LAST_FETCH_TIME = 0
CACHE_TTL = 300

def to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def to_int(value, default=0, minimum=0):
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default

def to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().upper() == "TRUE"

# ---------- Background Save Worker ----------
def _async_save_worker(payload):
    try:
        res = requests.post(WEB_APP_URL, json=payload, timeout=30, verify=False, allow_redirects=True)
        print("[Background Save] Status:", res.status_code)
    except Exception as e:
        print("[Background Save Error]:", e)

def save_all_data_async(data):
    global _DATA_CACHE, _LAST_FETCH_TIME
    _DATA_CACHE = data
    _LAST_FETCH_TIME = time.time()
    
    payload = {
        "action": "save_all",
        "expenses": data.get("expenses", []),
        "fuel_logs": data.get("fuel_logs", []),
        "billing_extra": data.get("billing_extra", []),
        "advances": data.get("advances", []),
        "fuel_percent": data.get("fuel_percent", 50.0)
    }
    
    threading.Thread(target=_async_save_worker, args=(payload,), daemon=True).start()

def get_all_data(force_refresh=False):
    global _DATA_CACHE, _LAST_FETCH_TIME
    now = time.time()
    
    if not force_refresh and _DATA_CACHE is not None:
        return _DATA_CACHE

    try:
        res = requests.get(f"{WEB_APP_URL}?action=get_all", timeout=15, verify=False, allow_redirects=True)
        if res.status_code == 200 and res.text.strip():
            _DATA_CACHE = res.json()
            _LAST_FETCH_TIME = now
            return _DATA_CACHE
    except Exception as e:
        print("Error fetching data:", e)
        
    if _DATA_CACHE:
        return _DATA_CACHE
    return {"expenses": [], "fuel_logs": [], "billing_extra": [], "advances": [], "fuel_percent": 50.0}

def parse_db_data(raw_data):
    expenses = []
    for r in raw_data.get("expenses", []):
        expenses.append({
            "name": str(r.get("name", "")),
            "price": to_float(r.get("price")),
            "paid": to_bool(r.get("paid")),
            "split": to_bool(r.get("split")),
            "installment": to_bool(r.get("installment")),
            "months_paid": to_int(r.get("months_paid"), 0),
            "months_total": to_int(r.get("months_total"), 12)
        })

    fuel_logs = []
    for r in raw_data.get("fuel_logs", []):
        fuel_logs.append({
            "date": str(r.get("date", "")),
            "note": str(r.get("note", "")),
            "amount": to_float(r.get("amount"))
        })

    billing_extra = []
    for r in raw_data.get("billing_extra", []):
        billing_extra.append({
            "name": str(r.get("name", "")),
            "price": to_float(r.get("price")),
            "installment": to_bool(r.get("installment")),
            "months_paid": to_int(r.get("months_paid"), 0),
            "months_total": to_int(r.get("months_total"), 12)
        })

    advances = []
    for r in raw_data.get("advances", []):
        advances.append({
            "name": str(r.get("name", "")),
            "price": to_float(r.get("price")),
            "divide": to_bool(r.get("divide"))
        })

    fuel_percent = to_float(raw_data.get("fuel_percent"), 50.0)
    
    return {
        "expenses": expenses,
        "fuel_logs": fuel_logs,
        "billing_extra": billing_extra,
        "advances": advances,
        "fuel_percent": fuel_percent
    }

def advance_amount(item):
    return item["price"] / 2 if item.get("divide") else item["price"]

def calc_fuel_total(fuel_logs):
    return sum(log["amount"] for log in fuel_logs)

def calc_fuel_bill(fuel_logs, fuel_percent):
    return calc_fuel_total(fuel_logs) * fuel_percent / 100

def apply_installment_fields(item, i):
    item["price"] = to_float(request.form.get(f"price_{i}"), item["price"])
    item["installment"] = request.form.get(f"installment_{i}") == "on"
    total = to_int(request.form.get(f"months_total_{i}"), item["months_total"], minimum=1)
    paid = to_int(request.form.get(f"months_paid_{i}"), item["months_paid"], minimum=0)
    item["months_total"] = total
    item["months_paid"] = min(paid, total)

# ---------- Routes ----------
@app.get("/")
def home():
    raw_data = get_all_data()
    db = parse_db_data(raw_data)
    
    fuel_total = calc_fuel_total(db["fuel_logs"])
    fuel_bill = calc_fuel_bill(db["fuel_logs"], db["fuel_percent"])
    
    return render_template(
        "index.html",
        expenses=db["expenses"],
        fuel_logs=db["fuel_logs"],
        fuel_total=fuel_total,
        fuel_percent=db["fuel_percent"],
        fuel_bill=fuel_bill,
        today=date.today().isoformat(),
    )

@app.post("/update")
def update():
    raw_data = get_all_data()
    db = parse_db_data(raw_data)
    for i, item in enumerate(db["expenses"]):
        item["paid"] = request.form.get(f"paid_{i}") == "on"
        item["split"] = request.form.get(f"split_{i}") == "on"
        apply_installment_fields(item, i)
    save_all_data_async(db)
    return redirect(url_for("home"))

@app.post("/next_month")
def next_month():
    raw_data = get_all_data()
    db = parse_db_data(raw_data)
    
    # 1. จัดการรายจ่ายหลัก (Expenses)
    updated_expenses = []
    for item in db["expenses"]:
        item["paid"] = False
        if item.get("installment"):
            current_paid = to_int(item.get("months_paid"), 0)
            total_months = to_int(item.get("months_total"), 12)
            if current_paid >= total_months:
                continue  # ผ่อนครบแล้ว ลบทิ้ง
            item["months_paid"] = current_paid + 1
        updated_expenses.append(item)
    db["expenses"] = updated_expenses
    
    # 2. จัดการรายการเรียกเก็บพิเศษ (Billing Extra)
    updated_extra = []
    for item in db.get("billing_extra", []):
        if item.get("installment"):
            current_paid = to_int(item.get("months_paid"), 0)
            total_months = to_int(item.get("months_total"), 12)
            if current_paid >= total_months:
                continue  # ผ่อนครบแล้ว ลบทิ้ง
            item["months_paid"] = current_paid + 1
        updated_extra.append(item)
    db["billing_extra"] = updated_extra
    
    # 3. ลบรายการสำรองจ่ายทั้งหมด (Advances) 💸
    db["advances"] = []
    
    # 4. ลบบันทึกค่าน้ำมันทั้งหมด (Fuel Logs) ⛽
    db["fuel_logs"] = []
    
    save_all_data_async(db)
    return redirect(url_for("home"))

@app.post("/add")
def add():
    name = request.form.get("name", "").strip()
    price = to_float(request.form.get("price"))
    if name:
        raw_data = get_all_data()
        db = parse_db_data(raw_data)
        db["expenses"].append({
            "name": name, "price": price, "paid": False, "split": False,
            "installment": False, "months_paid": 0, "months_total": 12
        })
        save_all_data_async(db)
    return redirect(url_for("home"))

@app.post("/delete/<int:index>")
def delete(index):
    raw_data = get_all_data()
    db = parse_db_data(raw_data)
    if 0 <= index < len(db["expenses"]):
        db["expenses"].pop(index)
        save_all_data_async(db)
    return redirect(url_for("home"))

@app.post("/fuel/add")
def fuel_add():
    amount = to_float(request.form.get("amount"))
    raw_data = get_all_data()
    db = parse_db_data(raw_data)
    db["fuel_logs"].append({
        "date": request.form.get("date") or date.today().isoformat(),
        "note": request.form.get("note", "").strip(),
        "amount": amount,
    })
    save_all_data_async(db)
    return redirect(url_for("home"))

@app.post("/fuel/update")
def fuel_update():
    raw_data = get_all_data()
    db = parse_db_data(raw_data)
    for i, log in enumerate(db["fuel_logs"]):
        log["date"] = request.form.get(f"date_{i}") or log["date"]
        log["note"] = request.form.get(f"note_{i}", "").strip()
        log["amount"] = to_float(request.form.get(f"amount_{i}"), log["amount"])
    save_all_data_async(db)
    return redirect(url_for("home"))

@app.post("/fuel/delete/<int:index>")
def fuel_delete(index):
    raw_data = get_all_data()
    db = parse_db_data(raw_data)
    if 0 <= index < len(db["fuel_logs"]):
        db["fuel_logs"].pop(index)
        save_all_data_async(db)
    return redirect(url_for("home"))

@app.post("/fuel/percent")
def fuel_percent():
    raw_data = get_all_data()
    db = parse_db_data(raw_data)
    percent = to_float(request.form.get("percent"), db["fuel_percent"])
    db["fuel_percent"] = min(max(percent, 0.0), 100.0)
    save_all_data_async(db)
    return redirect(url_for("home"))

@app.get("/billing")
def billing():
    raw_data = get_all_data()
    db = parse_db_data(raw_data)
    
    split_items = [item for item in db["expenses"] if item["split"]]
    f_total = calc_fuel_total(db["fuel_logs"])
    f_bill = calc_fuel_bill(db["fuel_logs"], db["fuel_percent"])
    
    total_billing = (sum(item["price"] / 2 for item in split_items)
                     + sum(item["price"] for item in db["billing_extra"])
                     + f_bill)
    total_advance = sum(advance_amount(item) for item in db["advances"])
    grand_total = total_billing + total_advance

    return render_template(
        "billing.html",
        split_items=split_items,
        extra=db["billing_extra"],
        advances=db["advances"],
        fuel_total=f_total,
        fuel_percent=db["fuel_percent"],
        fuel_bill=f_bill,
        total_billing=total_billing,
        total_advance=total_advance,
        grand_total=grand_total,
    )

@app.post("/billing/extra/add")
def extra_add():
    name = request.form.get("name", "").strip()
    price = to_float(request.form.get("price"))
    if name:
        raw_data = get_all_data()
        db = parse_db_data(raw_data)
        db["billing_extra"].append({
            "name": name, "price": price, "installment": False,
            "months_paid": 0, "months_total": 12
        })
        save_all_data_async(db)
    return redirect(url_for("billing"))

@app.post("/billing/extra/update")
def extra_update():
    raw_data = get_all_data()
    db = parse_db_data(raw_data)
    for i, item in enumerate(db["billing_extra"]):
        apply_installment_fields(item, i)
    save_all_data_async(db)
    return redirect(url_for("billing"))

@app.post("/billing/extra/delete/<int:index>")
def extra_delete(index):
    raw_data = get_all_data()
    db = parse_db_data(raw_data)
    if 0 <= index < len(db["billing_extra"]):
        db["billing_extra"].pop(index)
        save_all_data_async(db)
    return redirect(url_for("billing"))

@app.post("/billing/advance/add")
def advance_add():
    name = request.form.get("name", "").strip()
    price = to_float(request.form.get("price"))
    if name:
        raw_data = get_all_data()
        db = parse_db_data(raw_data)
        db["advances"].append({"name": name, "price": price, "divide": False})
        save_all_data_async(db)
    return redirect(url_for("billing"))

@app.post("/billing/advance/update")
def advance_update():
    raw_data = get_all_data()
    db = parse_db_data(raw_data)
    for i, item in enumerate(db["advances"]):
        item["price"] = to_float(request.form.get(f"price_{i}"), item["price"])
        item["divide"] = request.form.get(f"divide_{i}") == "on"
    save_all_data_async(db)
    return redirect(url_for("billing"))

@app.post("/billing/advance/delete/<int:index>")
def advance_delete(index):
    raw_data = get_all_data()
    db = parse_db_data(raw_data)
    if 0 <= index < len(db["advances"]):
        db["advances"].pop(index)
        save_all_data_async(db)
    return redirect(url_for("billing"))

if __name__ == "__main__":
    app.run(debug=True)