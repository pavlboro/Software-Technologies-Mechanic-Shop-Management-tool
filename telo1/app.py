# -*- coding: utf-8 -*-
"""Synergeio Auto - Διαχείριση γενικού συνεργείου αυτοκινήτων.

Python 3.9 - 3.13 | Flask + SQLite
Εκτέλεση:  python app.py   ->  http://127.0.0.1:5000
"""

import csv
import io
import os
import smtplib
import sqlite3
import uuid
from datetime import date
from email.message import EmailMessage
from functools import wraps

from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "synergeio.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
VAT_RATE = 0.24
MAX_CSV_MB = 20
MAX_PHOTO_MB = 10

ROLE_NAMES = {"owner": "Ιδιοκτήτης", "reception": "Υποδοχή", "mechanic": "Μηχανικός"}
STATUS_NAMES = {"pending": "Εκκρεμεί", "progress": "Σε εξέλιξη",
                "ready": "Έτοιμο προς παράδοση", "closed": "Κλεισμένο"}
FUEL_NAMES = ["Βενζίνη", "Πετρέλαιο", "Υγραέριο", "Ηλεκτρικό", "Υβριδικό"]

TICKET_QUERY = """
SELECT t.*, v.plate, v.vin, v.make, v.model, v.customer_id,
       c.name AS customer_name, c.phone AS customer_phone, c.email AS customer_email,
       u.full_name AS mechanic_name
FROM tickets t
JOIN vehicles v ON v.id = t.vehicle_id
JOIN customers c ON c.id = v.customer_id
LEFT JOIN users u ON u.id = t.mechanic_id
"""

VEHICLE_QUERY = """
SELECT v.*, c.name AS customer_name, c.phone AS customer_phone, c.email AS customer_email
FROM vehicles v
JOIN customers c ON c.id = v.customer_id
"""

app = Flask(__name__)
app.secret_key = os.environ.get("SYNERGEIO_SECRET", "synergeio-dev-secret")
app.config["MAX_CONTENT_LENGTH"] = MAX_CSV_MB * 1024 * 1024
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ---------------------------------------------------------------- Βάση δεδομένων

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def setting(conn, key, default=""):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def save_setting(key, value):
    conn = db()
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()
    conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner', 'reception', 'mechanic'))
);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    vat TEXT,
    phone TEXT,
    email TEXT,
    address TEXT
);
CREATE TABLE IF NOT EXISTS vehicles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    plate TEXT NOT NULL,
    vin TEXT,
    make TEXT,
    model TEXT,
    first_registration TEXT,
    fuel TEXT,
    engine_cc TEXT,
    engine_no TEXT
);
CREATE TABLE IF NOT EXISTS parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    description TEXT,
    manufacturer TEXT,
    compatible TEXT,
    price REAL DEFAULT 0,
    stock INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    mechanic_id INTEGER REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'pending',
    opened_date TEXT NOT NULL,
    km_in INTEGER NOT NULL DEFAULT 0,
    km_reason TEXT,
    description TEXT,
    diagnosis TEXT,
    work_done TEXT,
    notes TEXT,
    labor_hours REAL DEFAULT 0,
    labor_rate REAL DEFAULT 0,
    closed_date TEXT,
    invoice_type TEXT,
    invoice_no TEXT,
    emailed_at TEXT
);
CREATE TABLE IF NOT EXISTS ticket_parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    part_id INTEGER,
    code TEXT,
    description TEXT,
    qty REAL NOT NULL DEFAULT 1,
    price REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ticket_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'repair'
);
CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    items TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def seed(conn):
    if conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] > 0:
        return
    users = [("owner", "owner123", "Νίκος Παπαδόπουλος", "owner"),
             ("reception", "reception123", "Μαρία Ιωάννου", "reception"),
             ("mechanic", "mechanic123", "Γιώργος Δήμου", "mechanic")]
    for username, password, full_name, role in users:
        conn.execute("INSERT INTO users (username, password_hash, full_name, role) "
                     "VALUES (?, ?, ?, ?)",
                     (username, generate_password_hash(password), full_name, role))
    conn.execute("INSERT INTO settings (key, value) VALUES ('hourly_rate', '45')")
    for name, items in [
        ("Μικρό service", "Αλλαγή λαδιών και φίλτρου λαδιού, έλεγχος στάθμης υγρών, "
                          "έλεγχος πίεσης ελαστικών."),
        ("Μεγάλο service", "Αλλαγή λαδιών, φίλτρου λαδιού, φίλτρου αέρα, φίλτρου καμπίνας, "
                           "έλεγχος φρένων και αναρτήσεων."),
        ("Έλεγχος φρένων", "Έλεγχος δίσκων και τακακιών, μέτρηση πάχους, έλεγχος υγρού φρένων.")]:
        conn.execute("INSERT INTO templates (name, items) VALUES (?, ?)", (name, items))
    for code, description, manufacturer, compatible, price, stock in [
        ("FLT-OIL-01", "Φίλτρο λαδιού", "Bosch", "Όλα τα μοντέλα", 12.5, 20),
        ("FLT-AIR-02", "Φίλτρο αέρα", "Mann", "VW Golf, Seat Leon", 18.0, 15),
        ("BRK-PAD-03", "Τακάκια φρένων εμπρός", "Brembo", "Toyota Corolla", 65.0, 8),
        ("OIL-5W30", "Λάδι κινητήρα 5W-30 (1L)", "Castrol", "Βενζίνη / Πετρέλαιο", 9.5, 40)]:
        conn.execute("INSERT INTO parts (code, description, manufacturer, compatible, price, stock) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (code, description, manufacturer, compatible, price, stock))
    cur = conn.execute("INSERT INTO customers (name, vat, phone, email, address) "
                       "VALUES (?, ?, ?, ?, ?)",
                       ("Κώστας Αντωνίου", "123456789", "6970000000",
                        "kostas@example.com", "Δημοκρίτου 12, Κομοτηνή"))
    conn.execute("INSERT INTO vehicles (customer_id, plate, vin, make, model, "
                 "first_registration, fuel, engine_cc, engine_no) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (cur.lastrowid, "ΚΟΜ-1234", "WVWZZZ1KZAW123456", "Volkswagen", "Golf 1.6",
                  "2015-03-12", "Βενζίνη", "1598", "BSE123456"))


def init_db():
    conn = db()
    conn.executescript(SCHEMA)
    seed(conn)
    conn.commit()
    conn.close()


if not os.path.exists(DB_PATH):
    init_db()


# ---------------------------------------------------------------------- Βοηθητικά

def current_user():
    if "user_id" not in session:
        return None
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()
    return user


@app.context_processor
def inject_globals():
    return {"user": current_user(), "roles": ROLE_NAMES, "statuses": STATUS_NAMES,
            "vat_rate": VAT_RATE}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def roles_required(*allowed):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                return redirect(url_for("login", next=request.path))
            if user["role"] not in allowed:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def get_ticket(conn, tid):
    return conn.execute(TICKET_QUERY + " WHERE t.id = ?", (tid,)).fetchone()


def totals_for(conn, ticket):
    rows = conn.execute("SELECT * FROM ticket_parts WHERE ticket_id = ?", (ticket["id"],)).fetchall()
    labor = float(ticket["labor_hours"] or 0) * float(ticket["labor_rate"] or 0)
    parts = sum(float(row["qty"]) * float(row["price"]) for row in rows)
    net = labor + parts
    return {"labor": round(labor, 2), "parts": round(parts, 2), "net": round(net, 2),
            "vat": round(net * VAT_RATE, 2), "total": round(net * (1 + VAT_RATE), 2)}


def save_photos(conn, ticket_id, kind):
    for storage in request.files.getlist("photos"):
        if not storage or not storage.filename:
            continue
        name = secure_filename(storage.filename)
        ext = os.path.splitext(name)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png"):
            continue
        filename = "t" + str(ticket_id) + "_" + uuid.uuid4().hex[:10] + ext
        storage.save(os.path.join(UPLOAD_DIR, filename))
        conn.execute("INSERT INTO ticket_photos (ticket_id, filename, kind) VALUES (?, ?, ?)",
                     (ticket_id, filename, kind))


def smtp_config():
    conn = db()
    cfg = {key: setting(conn, key) for key in
           ("smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_from")}
    conn.close()
    return cfg


def send_mail(to_address, subject, body):
    cfg = smtp_config()
    if not cfg["smtp_host"] or not to_address:
        return False, "Δεν έχει ρυθμιστεί διακομιστής SMTP ή λείπει το e-mail του πελάτη."
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["smtp_from"] or cfg["smtp_user"]
    msg["To"] = to_address
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"] or 587), timeout=15) as server:
            server.starttls()
            if cfg["smtp_user"]:
                server.login(cfg["smtp_user"], cfg["smtp_pass"])
            server.send_message(msg)
        return True, "Το e-mail στάλθηκε."
    except Exception as exc:
        return False, "Αποτυχία αποστολής: " + str(exc)


# ------------------------------------------------------------------- Σύνδεση

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = db()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Λάθος όνομα χρήστη ή κωδικός πρόσβασης.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------- Πίνακας ελέγχου

@app.route("/")
@login_required
def dashboard():
    conn = db()
    rows = conn.execute(TICKET_QUERY + " WHERE t.status != 'closed' "
                                       "ORDER BY t.opened_date, t.id").fetchall()
    board = {"pending": [], "progress": [], "ready": []}
    for row in rows:
        if row["status"] in board:
            board[row["status"]].append(row)
    today = date.today().isoformat()
    stats = {
        "today": conn.execute("SELECT COUNT(*) AS c FROM tickets WHERE opened_date = ?",
                              (today,)).fetchone()["c"],
        "pending": len(board["pending"]),
        "ready": len(board["ready"]),
        "low_stock": conn.execute("SELECT COUNT(*) AS c FROM parts WHERE stock < 3").fetchone()["c"],
    }
    low_stock = conn.execute("SELECT * FROM parts WHERE stock < 3 ORDER BY stock").fetchall()
    conn.close()
    return render_template("dashboard.html", board=board, stats=stats, low_stock=low_stock)


# -------------------------------------------------------------------- Αναζήτηση

@app.route("/search")
@login_required
def search():
    filters = {}
    for key in ("plate", "vin", "vat", "name", "model", "work", "part",
                "mechanic", "date_from", "date_to"):
        filters[key] = request.args.get(key, "").strip()

    clauses, params = [], []
    if filters["plate"]:
        clauses.append("v.plate LIKE ?")
        params.append("%" + filters["plate"].upper() + "%")
    if filters["vin"]:
        clauses.append("v.vin LIKE ?")
        params.append("%" + filters["vin"].upper() + "%")
    if filters["vat"]:
        clauses.append("c.vat LIKE ?")
        params.append("%" + filters["vat"] + "%")
    if filters["name"]:
        clauses.append("c.name LIKE ?")
        params.append("%" + filters["name"] + "%")
    if filters["model"]:
        clauses.append("(v.make LIKE ? OR v.model LIKE ?)")
        params.extend(["%" + filters["model"] + "%"] * 2)
    if filters["work"]:
        clauses.append("(t.description LIKE ? OR t.work_done LIKE ? OR t.diagnosis LIKE ?)")
        params.extend(["%" + filters["work"] + "%"] * 3)
    if filters["part"]:
        clauses.append("t.id IN (SELECT ticket_id FROM ticket_parts "
                       "WHERE code LIKE ? OR description LIKE ?)")
        params.extend(["%" + filters["part"] + "%"] * 2)
    if filters["mechanic"]:
        clauses.append("u.full_name LIKE ?")
        params.append("%" + filters["mechanic"] + "%")
    if filters["date_from"]:
        clauses.append("t.opened_date >= ?")
        params.append(filters["date_from"])
    if filters["date_to"]:
        clauses.append("t.opened_date <= ?")
        params.append(filters["date_to"])

    sql = TICKET_QUERY
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY t.opened_date DESC, t.id DESC"
    conn = db()
    results = conn.execute(sql, params).fetchall()
    conn.close()
    return render_template("search.html", filters=filters, results=results)


# --------------------------------------------------------------------- Πελάτες

@app.route("/customers")
@roles_required("owner", "reception")
def customers():
    conn = db()
    rows = conn.execute("SELECT * FROM customers ORDER BY name").fetchall()
    edit_customer = None
    edit_id = request.args.get("edit", type=int)
    if edit_id:
        edit_customer = conn.execute("SELECT * FROM customers WHERE id = ?",
                                     (edit_id,)).fetchone()
    conn.close()
    return render_template("customers.html", customers=rows, edit_customer=edit_customer)


@app.route("/customers/save", methods=["POST"])
@roles_required("owner", "reception")
def customers_save():
    fields = [request.form.get(key, "").strip()
              for key in ("name", "vat", "phone", "email", "address")]
    if not fields[0]:
        flash("Το ονοματεπώνυμο είναι υποχρεωτικό.", "error")
        return redirect(url_for("customers"))
    conn = db()
    customer_id = request.form.get("customer_id", type=int)
    if customer_id:
        conn.execute("UPDATE customers SET name = ?, vat = ?, phone = ?, email = ?, address = ? "
                     "WHERE id = ?", (*fields, customer_id))
    else:
        cur = conn.execute("INSERT INTO customers (name, vat, phone, email, address) "
                           "VALUES (?, ?, ?, ?, ?)", fields)
        customer_id = cur.lastrowid
    conn.commit()
    conn.close()
    flash("Τα στοιχεία του πελάτη αποθηκεύτηκαν.", "ok")
    return redirect(url_for("customer_detail", cid=customer_id))


@app.route("/customers/<int:cid>")
@roles_required("owner", "reception")
def customer_detail(cid):
    conn = db()
    customer = conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    if customer is None:
        conn.close()
        abort(404)
    vehicles = conn.execute(VEHICLE_QUERY + " WHERE c.id = ? ORDER BY v.plate",
                            (cid,)).fetchall()
    conn.close()
    return render_template("customer_detail.html", customer=customer, vehicles=vehicles)


# --------------------------------------------------------------------- Οχήματα

@app.route("/vehicles/save", methods=["POST"])
@roles_required("owner", "reception")
def vehicles_save():
    fields = [request.form.get(key, "").strip() for key in
              ("plate", "vin", "make", "model", "first_registration",
               "fuel", "engine_cc", "engine_no")]
    customer_id = request.form.get("customer_id", type=int)
    vehicle_id = request.form.get("vehicle_id", type=int)
    if not fields[0] or not customer_id:
        flash("Η πινακίδα και ο πελάτης είναι υποχρεωτικά.", "error")
        return redirect(url_for("customers"))
    conn = db()
    if vehicle_id:
        conn.execute("UPDATE vehicles SET plate = ?, vin = ?, make = ?, model = ?, "
                     "first_registration = ?, fuel = ?, engine_cc = ?, engine_no = ? "
                     "WHERE id = ?", (*fields, vehicle_id))
    else:
        cur = conn.execute("INSERT INTO vehicles (plate, vin, make, model, first_registration, "
                           "fuel, engine_cc, engine_no, customer_id) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (*fields, customer_id))
        vehicle_id = cur.lastrowid
    conn.commit()
    conn.close()
    flash("Το όχημα αποθηκεύτηκε.", "ok")
    return redirect(url_for("vehicle_detail", vid=vehicle_id))


@app.route("/vehicles/<int:vid>")
@login_required
def vehicle_detail(vid):
    conn = db()
    vehicle = conn.execute(VEHICLE_QUERY + " WHERE v.id = ?", (vid,)).fetchone()
    if vehicle is None:
        conn.close()
        abort(404)
    history = conn.execute(TICKET_QUERY + " WHERE t.vehicle_id = ? "
                                          "ORDER BY t.opened_date DESC, t.id DESC", (vid,)).fetchall()
    mileage = conn.execute("SELECT opened_date, km_in FROM tickets WHERE vehicle_id = ? "
                           "ORDER BY opened_date, id", (vid,)).fetchall()
    last_km = mileage[-1]["km_in"] if mileage else 0
    conn.close()
    return render_template("vehicle_detail.html", vehicle=vehicle, history=history,
                           mileage=mileage, last_km=last_km, fuels=FUEL_NAMES)


# ---------------------------------------------------------------------- Δελτία

@app.route("/tickets/new", methods=["GET", "POST"])
@roles_required("owner", "reception")
def ticket_new():
    conn = db()
    vehicle_id = request.args.get("vehicle_id", type=int)
    if request.method == "POST":
        vehicle_id = request.form.get("vehicle_id", type=int)
        opened_date = request.form.get("opened_date") or date.today().isoformat()
        km_in = request.form.get("km_in", type=int) or 0
        reason = request.form.get("km_reason", "").strip()
        last = conn.execute("SELECT km_in FROM tickets WHERE vehicle_id = ? "
                            "ORDER BY opened_date DESC, id DESC LIMIT 1",
                            (vehicle_id,)).fetchone()
        if last and km_in < last["km_in"] and not reason:
            flash("Η ένδειξη χιλιομέτρων (" + str(km_in) + ") είναι μικρότερη από την "
                                                           "προηγούμενη (" + str(
                last["km_in"]) + "). Απαιτείται αιτιολόγηση.", "error")
        else:
            rate = float(setting(conn, "hourly_rate", "45"))
            cur = conn.execute("INSERT INTO tickets (vehicle_id, opened_date, km_in, km_reason, "
                               "description, labor_rate, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                               (vehicle_id, opened_date, km_in, reason,
                                request.form.get("description", ""), rate))
            ticket_id = cur.lastrowid
            save_photos(conn, ticket_id, "reception")
            conn.commit()
            conn.close()
            flash("Το δελτίο εργασίας άνοιξε.", "ok")
            return redirect(url_for("ticket_view", tid=ticket_id))
    vehicles = conn.execute(VEHICLE_QUERY + " ORDER BY v.plate").fetchall()
    conn.close()
    return render_template("ticket_new.html", vehicles=vehicles, vehicle_id=vehicle_id,
                           today=date.today().isoformat())


@app.route("/tickets/<int:tid>")
@login_required
def ticket_view(tid):
    conn = db()
    ticket = get_ticket(conn, tid)
    if ticket is None:
        conn.close()
        abort(404)
    parts_used = conn.execute("SELECT * FROM ticket_parts WHERE ticket_id = ?",
                              (tid,)).fetchall()
    photos = conn.execute("SELECT * FROM ticket_photos WHERE ticket_id = ?",
                          (tid,)).fetchall()
    totals = totals_for(conn, ticket)
    conn.close()
    return render_template("ticket_view.html", ticket=ticket, parts_used=parts_used,
                           photos=photos, totals=totals, today=date.today().isoformat())


@app.route("/tickets/<int:tid>/close", methods=["POST"])
@roles_required("owner", "reception")
def ticket_close(tid):
    conn = db()
    ticket = get_ticket(conn, tid)
    if ticket is None:
        conn.close()
        abort(404)
    hours = request.form.get("labor_hours", type=float) or 0
    rate = request.form.get("labor_rate", type=float) or float(setting(conn, "hourly_rate", "45"))
    conn.execute("UPDATE tickets SET status = 'closed', closed_date = ?, invoice_type = ?, "
                 "invoice_no = ?, labor_hours = ?, labor_rate = ? WHERE id = ?",
                 (request.form.get("closed_date") or date.today().isoformat(),
                  request.form.get("invoice_type", ""), request.form.get("invoice_no", ""),
                  hours, rate, tid))
    conn.commit()
    conn.close()
    flash("Το δελτίο κλείστηκε και τιμολογήθηκε.", "ok")
    return redirect(url_for("ticket_view", tid=tid))


@app.route("/tickets/<int:tid>/work", methods=["GET", "POST"])
@roles_required("owner", "mechanic")
def ticket_work(tid):
    conn = db()
    ticket = get_ticket(conn, tid)
    if ticket is None:
        conn.close()
        abort(404)

    if request.method == "POST":
        action = request.form.get("action")
        if action == "status":
            new_status = request.form.get("status")
            if new_status in STATUS_NAMES and new_status != "closed":
                conn.execute("UPDATE tickets SET status = ? WHERE id = ?", (new_status, tid))
        elif action == "template":
            tpl = conn.execute("SELECT * FROM templates WHERE id = ?",
                               (request.form.get("template_id", type=int),)).fetchone()
            if tpl:
                current = ticket["work_done"] or ""
                conn.execute("UPDATE tickets SET work_done = ? WHERE id = ?",
                             ((current + " " + (tpl["items"] or "")).strip(), tid))
        elif action == "part":
            part = conn.execute("SELECT * FROM parts WHERE id = ?",
                                (request.form.get("part_id", type=int),)).fetchone()
            if part:
                qty = request.form.get("qty", type=float) or 1
                conn.execute("INSERT INTO ticket_parts (ticket_id, part_id, code, description, "
                             "qty, price) VALUES (?, ?, ?, ?, ?, ?)",
                             (tid, part["id"], part["code"], part["description"],
                              qty, part["price"]))
                conn.execute("UPDATE parts SET stock = stock - 1 WHERE id = ?", (part["id"],))
        elif action == "remove_part":
            conn.execute("DELETE FROM ticket_parts WHERE id = ? AND ticket_id = ?",
                         (request.form.get("part_row", type=int), tid))
        elif action == "save":
            hours = request.form.get("labor_hours", type=float) or 0
            conn.execute("UPDATE tickets SET diagnosis = ?, work_done = ?, notes = ?, "
                         "labor_hours = ?, mechanic_id = ? WHERE id = ?",
                         (request.form.get("diagnosis", ""), request.form.get("work_done", ""),
                          request.form.get("notes", ""), hours, session["user_id"], tid))
            save_photos(conn, tid, "repair")
        conn.commit()
        conn.close()
        flash("Οι εργασίες ενημερώθηκαν.", "ok")
        return redirect(url_for("ticket_work", tid=tid))

    parts_used = conn.execute("SELECT * FROM ticket_parts WHERE ticket_id = ?",
                              (tid,)).fetchall()
    photos = conn.execute("SELECT * FROM ticket_photos WHERE ticket_id = ?",
                          (tid,)).fetchall()
    catalog = conn.execute("SELECT * FROM parts ORDER BY description").fetchall()
    templates_list = conn.execute("SELECT * FROM templates ORDER BY name").fetchall()
    conn.close()
    return render_template("ticket_work.html", ticket=ticket, parts_used=parts_used,
                           photos=photos, catalog=catalog, templates_list=templates_list)


@app.route("/tickets/<int:tid>/email", methods=["POST"])
@roles_required("owner", "reception")
def ticket_email(tid):
    conn = db()
    ticket = get_ticket(conn, tid)
    if ticket is None:
        conn.close()
        abort(404)
    totals = totals_for(conn, ticket)
    conn.close()
    body = (
        f"Αγαπητέ/ή {ticket['customer_name']},\n\n"
        f"Το όχημά σας {ticket['make'] or ''} {ticket['model'] or ''} "
        f"({ticket['plate'] or ''}) ολοκληρώθηκε.\n\n"
        f"Δελτίο εργασίας: #{ticket['id']}\n"
        f"Ημερομηνία εισόδου: {ticket['opened_date'] or ''}\n"
        f"Εργασίες: {ticket['work_done'] or '-'}\n"
        f"Καθαρό ποσό: {totals['net']} EUR\n"
        f"ΦΠΑ 24%: {totals['vat']} EUR\n"
        f"Σύνολο: {totals['total']} EUR\n\n"
        "Με εκτίμηση,\n"
        "Συνεργείο Auto"
    )
    ok, message = send_mail(ticket["customer_email"],
                            "Ολοκλήρωση εργασιών - δελτίο #" + str(ticket["id"]), body)
    if ok:
        conn = db()
        conn.execute("UPDATE tickets SET emailed_at = ? WHERE id = ?",
                     (date.today().isoformat(), tid))
        conn.commit()
        conn.close()
    flash(message, "ok" if ok else "error")
    return redirect(url_for("ticket_view", tid=tid))


# ---------------------------------------------------------------- Ανταλλακτικά

@app.route("/parts", methods=["GET"])
@roles_required("owner", "reception")
def parts():
    conn = db()
    rows = conn.execute("SELECT * FROM parts ORDER BY description").fetchall()
    edit_part = None
    edit_id = request.args.get("edit", type=int)
    if edit_id:
        edit_part = conn.execute("SELECT * FROM parts WHERE id = ?", (edit_id,)).fetchone()
    conn.close()
    return render_template("parts.html", parts=rows, edit_part=edit_part)


@app.route("/parts/save", methods=["POST"])
@roles_required("owner", "reception")
def parts_save():
    code = request.form.get("code", "").strip()
    if not code:
        flash("Ο κωδικός ανταλλακτικού είναι υποχρεωτικός.", "error")
        return redirect(url_for("parts"))
    values = (code, request.form.get("description", "").strip(),
              request.form.get("manufacturer", "").strip(),
              request.form.get("compatible", "").strip(),
              request.form.get("price", type=float) or 0,
              request.form.get("stock", type=int) or 0)
    conn = db()
    part_id = request.form.get("part_id", type=int)
    try:
        if part_id:
            conn.execute("UPDATE parts SET code = ?, description = ?, manufacturer = ?, "
                         "compatible = ?, price = ?, stock = ? WHERE id = ?",
                         (*values, part_id))
        else:
            conn.execute("INSERT INTO parts (code, description, manufacturer, compatible, "
                         "price, stock) VALUES (?, ?, ?, ?, ?, ?)", values)
        conn.commit()
        flash("Το ανταλλακτικό αποθηκεύτηκε.", "ok")
    except sqlite3.IntegrityError:
        flash("Υπάρχει ήδη ανταλλακτικό με αυτόν τον κωδικό.", "error")
    conn.close()
    return redirect(url_for("parts"))


@app.route("/parts/import", methods=["POST"])
@roles_required("owner", "reception")
def parts_import():
    storage = request.files.get("csv_file")
    if not storage or not storage.filename:
        flash("Επιλέξτε αρχείο CSV.", "error")
        return redirect(url_for("parts"))
    raw = storage.read()
    if len(raw) > MAX_CSV_MB * 1024 * 1024:
        flash("Το αρχείο ξεπερνά τα " + str(MAX_CSV_MB) + " MB.", "error")
        return redirect(url_for("parts"))
    text = raw.decode("utf-8-sig", errors="replace")
    sample = text[:2000]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    conn = db()
    added, updated = 0, 0
    for row in reader:
        code = (row.get("code") or row.get("ΚΩΔΙΚΟΣ") or "").strip()
        if not code:
            continue
        description = (row.get("description") or row.get("ΠΕΡΙΓΡΑΦΗ") or "").strip()
        manufacturer = (row.get("manufacturer") or row.get("ΚΑΤΑΣΚΕΥΑΣΤΗΣ") or "").strip()
        compatible = (row.get("compatible") or row.get("ΣΥΜΒΑΤΑ") or "").strip()
        try:
            price = float((row.get("price") or row.get("ΤΙΜΗ") or "0").replace(",", "."))
        except ValueError:
            price = 0.0
        try:
            stock = int(row.get("stock") or row.get("ΑΠΟΘΕΜΑ") or 0)
        except ValueError:
            stock = 0
        existing = conn.execute("SELECT id FROM parts WHERE code = ?", (code,)).fetchone()
        if existing:
            conn.execute("UPDATE parts SET description = ?, manufacturer = ?, compatible = ?, "
                         "price = ?, stock = ? WHERE id = ?",
                         (description, manufacturer, compatible, price, stock, existing["id"]))
            updated += 1
        else:
            conn.execute("INSERT INTO parts (code, description, manufacturer, compatible, "
                         "price, stock) VALUES (?, ?, ?, ?, ?, ?)",
                         (code, description, manufacturer, compatible, price, stock))
            added += 1
    conn.commit()
    conn.close()
    flash("Εισαγωγή CSV ολοκληρώθηκε: " + str(added) + " νέα, " + str(updated) +
          " ενημερώθηκαν.", "ok")
    return redirect(url_for("parts"))


# --------------------------------------------------------------------- Ρυθμίσεις

@app.route("/settings")
@roles_required("owner")
def settings():
    conn = db()
    users = conn.execute("SELECT * FROM users ORDER BY role, full_name").fetchall()
    tpls = conn.execute("SELECT * FROM templates ORDER BY name").fetchall()
    rate = setting(conn, "hourly_rate", "45")
    mail = {key: setting(conn, key) for key in
            ("smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_from")}
    conn.close()
    return render_template("settings.html", users=users, tpls=tpls, rate=rate, mail=mail)


@app.route("/settings/rate", methods=["POST"])
@roles_required("owner")
def settings_rate():
    save_setting("hourly_rate", request.form.get("hourly_rate", "45"))
    flash("Η ωριαία χρέωση ενημερώθηκε.", "ok")
    return redirect(url_for("settings"))


@app.route("/settings/mail", methods=["POST"])
@roles_required("owner")
def settings_mail():
    for key in ("smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_from"):
        save_setting(key, request.form.get(key, "").strip())
    flash("Οι ρυθμίσεις e-mail αποθηκεύτηκαν.", "ok")
    return redirect(url_for("settings"))


@app.route("/settings/test-mail", methods=["POST"])
@roles_required("owner")
def settings_test_mail():
    ok, message = send_mail(request.form.get("to", "").strip(),
                            "Δοκιμαστικό μήνυμα - Συνεργείο Auto",
                            "Η σύνδεση με τον διακομιστή αλληλογραφίας λειτουργεί.")
    flash(message, "ok" if ok else "error")
    return redirect(url_for("settings"))


@app.route("/settings/users", methods=["POST"])
@roles_required("owner")
def settings_users():
    action = request.form.get("action")
    conn = db()
    if action == "create":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role")
        full_name = request.form.get("full_name", "").strip()
        if not username or len(password) < 6 or role not in ROLE_NAMES:
            flash("Συμπληρώστε όλα τα πεδία (κωδικός τουλάχιστον 6 χαρακτήρες).", "error")
        else:
            try:
                conn.execute("INSERT INTO users (username, password_hash, full_name, role) "
                             "VALUES (?, ?, ?, ?)",
                             (username, generate_password_hash(password),
                              full_name or username, role))
                conn.commit()
                flash("Ο χρήστης δημιουργήθηκε.", "ok")
            except sqlite3.IntegrityError:
                flash("Το όνομα χρήστη υπάρχει ήδη.", "error")
    elif action == "password":
        user_id = request.form.get("user_id", type=int)
        password = request.form.get("password", "")
        if user_id and len(password) >= 6:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (generate_password_hash(password), user_id))
            conn.commit()
            flash("Ο κωδικός ενημερώθηκε.", "ok")
    elif action == "delete":
        user_id = request.form.get("user_id", type=int)
        if user_id and user_id != session["user_id"]:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
            flash("Ο χρήστης διαγράφηκε.", "ok")
    conn.close()
    return redirect(url_for("settings"))


@app.route("/settings/templates", methods=["POST"])
@roles_required("owner")
def settings_templates():
    action = request.form.get("action")
    conn = db()
    if action == "create":
        name = request.form.get("name", "").strip()
        if name:
            conn.execute("INSERT INTO templates (name, items) VALUES (?, ?)",
                         (name, request.form.get("items", "")))
            conn.commit()
            flash("Το πρότυπο δημιουργήθηκε.", "ok")
    elif action == "update":
        tpl_id = request.form.get("template_id", type=int)
        if tpl_id:
            conn.execute("UPDATE templates SET name = ?, items = ? WHERE id = ?",
                         (request.form.get("name", ""), request.form.get("items", ""), tpl_id))
            conn.commit()
            flash("Το πρότυπο ενημερώθηκε.", "ok")
    elif action == "delete":
        conn.execute("DELETE FROM templates WHERE id = ?",
                     (request.form.get("template_id", type=int),))
        conn.commit()
        flash("Το πρότυπο διαγράφηκε.", "ok")
    conn.close()
    return redirect(url_for("settings"))


# -------------------------------------------------------------------- Εκτύπωση

@app.route("/print/<kind>/<int:rid>")
@login_required
def print_view(kind, rid):
    conn = db()
    if kind == "ticket":
        ticket = get_ticket(conn, rid)
        if ticket is None:
            conn.close()
            abort(404)
        data = {"ticket": ticket, "totals": totals_for(conn, ticket),
                "parts_used": conn.execute("SELECT * FROM ticket_parts WHERE ticket_id = ?",
                                           (rid,)).fetchall()}
    elif kind == "customer":
        data = {"customer": conn.execute("SELECT * FROM customers WHERE id = ?",
                                         (rid,)).fetchone(),
                "vehicles": conn.execute(VEHICLE_QUERY + " WHERE c.id = ?", (rid,)).fetchall()}
    elif kind == "vehicle":
        data = {"vehicle": conn.execute(VEHICLE_QUERY + " WHERE v.id = ?",
                                        (rid,)).fetchone(),
                "history": conn.execute(TICKET_QUERY + " WHERE t.vehicle_id = ? "
                                                       "ORDER BY t.opened_date DESC", (rid,)).fetchall()}
    else:
        conn.close()
        abort(404)
    conn.close()
    return render_template("print.html", kind=kind, **data)


@app.errorhandler(403)
def forbidden(_error):
    return render_template("error.html", code=403,
                           message="Δεν έχετε δικαίωμα πρόσβασης σε αυτή τη σελίδα."), 403


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", code=404,
                           message="Η σελίδα ή η εγγραφή δεν βρέθηκε."), 404


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)