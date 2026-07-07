"""
Petty Cash Request System (Postgres / Supabase version)
---------------------------------------------------------
A small Flask web app that replaces paper petty cash request forms.

- Anyone with the link can submit a request (multiple expense lines + drawn signature).
- Data is stored permanently in a Postgres database (Supabase free tier).
- Approver gets an email notification when a new request is submitted.
- A dashboard lists all requests and lets an approver mark them Approved / Rejected / Paid.
- Approver actions are protected by a single shared password (set via APPROVER_PASSWORD).

Requires the DATABASE_URL environment variable (your Supabase connection string).

Run locally:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000 in your browser.
"""

import os
import datetime
from zoneinfo import ZoneInfo
from functools import wraps

import psycopg
from psycopg.rows import dict_row
import requests
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, g
)

DATABASE_URL = os.environ.get("DATABASE_URL")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
APPROVER_PASSWORD = os.environ.get("APPROVER_PASSWORD", "changeme123")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
APPROVER_EMAIL = os.environ.get("APPROVER_EMAIL")


def send_approver_notification(ref_no, requester, gross_total):
    """Email the approver when a new request comes in. Fails silently if
    email isn't configured or the send fails, so it never blocks a submission."""
    if not RESEND_API_KEY or not APPROVER_EMAIL:
        print("EMAIL SKIPPED: RESEND_API_KEY or APPROVER_EMAIL not set")
        return
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": "GDX Petty Cash <onboarding@resend.dev>",
                "to": [e.strip() for e in APPROVER_EMAIL.split(",") if e.strip()],
                "subject": f"New petty cash request {ref_no}",
                "html": (
                    f"<p><strong>{requester}</strong> submitted a new petty cash request.</p>"
                    f"<p>Reference: <strong>{ref_no}</strong><br>"
                    f"Amount: <strong>₦{gross_total:,.2f}</strong></p>"
                    f"<p><a href=\"https://gdx-petty-cash.onrender.com/login\">Log in to review it</a></p>"
                ),
            },
            timeout=10,
        )
        print(f"EMAIL RESPONSE: status={resp.status_code} body={resp.text}")
    except Exception as ex:
        print(f"EMAIL FAILED: {ex}")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = psycopg.connect(
            DATABASE_URL,
            row_factory=dict_row
        )
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = psycopg.connect(DATABASE_URL)
    cur = db.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id SERIAL PRIMARY KEY,
            ref_no TEXT UNIQUE NOT NULL,
            request_date TEXT NOT NULL,
            requester TEXT NOT NULL,
            department TEXT,
            purpose TEXT,
            signature_name TEXT NOT NULL,
            signature_image TEXT,
            signed_on TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pending',
            approver_name TEXT,
            approved_on TEXT,
            gross_total REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS line_items (
            id SERIAL PRIMARY KEY,
            request_id INTEGER NOT NULL REFERENCES requests(id),
            line_date TEXT,
            description TEXT NOT NULL,
            amount REAL NOT NULL
        );
    """)
    db.commit()
    cur.close()
    db.close()


def next_ref_no(db):
    cur = db.cursor()
    cur.execute("""
        SELECT ref_no FROM requests
        WHERE ref_no LIKE 'PCR-%%'
        ORDER BY id DESC LIMIT 1
    """)
    row = cur.fetchone()
    cur.close()
    if row is None:
        return "PCR-0001"
    last_num = int(row["ref_no"].split("-")[1])
    return f"PCR-{last_num + 1:04d}"


# ---------------------------------------------------------------------------
# Auth helper for approver-only pages
# ---------------------------------------------------------------------------

def approver_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("is_approver"):
            flash("Please log in as an approver to do that.", "error")
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def new_request_form():
    db = get_db()
    ref_no = next_ref_no(db)
    today = datetime.date.today().isoformat()
    return render_template("request_form.html", ref_no=ref_no, today=today)


@app.route("/submit", methods=["POST"])
def submit_request():
    db = get_db()
    cur = db.cursor()

    requester = request.form.get("requester", "").strip()
    request_date = request.form.get("request_date", "").strip()
    signature_name = request.form.get("signature_name", "").strip()
    signature_image = request.form.get("signature_image", "").strip()

    line_dates = request.form.getlist("line_date[]")
    line_descs = request.form.getlist("line_desc[]")
    line_amounts = request.form.getlist("line_amount[]")

    errors = []
    if not requester:
        errors.append("Requester's name is required.")
    if not signature_name:
        errors.append("Your name is required to confirm the request.")
    if not signature_image:
        errors.append("Please sign the request before submitting.")

    line_items = []
    for d, desc, amt in zip(line_dates, line_descs, line_amounts):
        desc = desc.strip()
        if not desc and not amt.strip():
            continue
        try:
            amount = float(amt)
        except ValueError:
            amount = 0.0
        if desc and amount > 0:
            line_items.append({"date": d.strip(), "desc": desc, "amount": amount})

    if not line_items:
        errors.append("Please add at least one valid expense line (description + amount).")

    if errors:
        for e in errors:
            flash(e, "error")
        ref_no = next_ref_no(db)
        today = datetime.date.today().isoformat()
        return render_template(
            "request_form.html", ref_no=ref_no, today=today,
            form=request.form
        )

   gross_total = sum(i["amount"] for i in line_items)
ref_no = next_ref_no(db)

now = datetime.datetime.now(ZoneInfo("Africa/Lagos"))

signed_on = now.strftime("%d %b %Y %I:%M %p")
created_at = now.isoformat()

    cur.execute("""
        INSERT INTO requests
            (ref_no, request_date, requester, department, purpose,
             signature_name, signature_image, signed_on, status, gross_total, created_at)
        VALUES (%s, %s, %s, NULL, NULL, %s, %s, %s, 'Pending', %s, %s)
        RETURNING id
    """, (ref_no, request_date, requester,
          signature_name, signature_image, signed_on, gross_total, created_at))
    request_id = cur.fetchone()["id"]

    for item in line_items:
        cur.execute("""
            INSERT INTO line_items (request_id, line_date, description, amount)
            VALUES (%s, %s, %s, %s)
        """, (request_id, item["date"], item["desc"], item["amount"]))

    db.commit()
    cur.close()
    send_approver_notification(ref_no, requester, gross_total)
    return redirect(url_for("confirmation", ref_no=ref_no))


@app.route("/confirmation/<ref_no>")
def confirmation(ref_no):
    return render_template("confirmation.html", ref_no=ref_no)


# ---------------------------------------------------------------------------
# Approver login
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == APPROVER_PASSWORD:
            session["is_approver"] = True
            flash("Logged in as approver.", "success")
            next_url = request.args.get("next") or url_for("dashboard")
            return redirect(next_url)
        flash("Incorrect password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("is_approver", None)
    flash("Logged out.", "success")
    return redirect(url_for("new_request_form"))


# ---------------------------------------------------------------------------
# Dashboard (approver view)
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@approver_required
def dashboard():
    db = get_db()
    cur = db.cursor()
    status_filter = request.args.get("status", "All")

    if status_filter and status_filter != "All":
        cur.execute(
            "SELECT * FROM requests WHERE status = %s ORDER BY created_at DESC",
            (status_filter,)
        )
    else:
        cur.execute("SELECT * FROM requests ORDER BY created_at DESC")
    rows = cur.fetchall()

    cur.execute("""
        SELECT status, COUNT(*) as count, COALESCE(SUM(gross_total), 0) as total
        FROM requests GROUP BY status
    """)
    totals = cur.fetchall()
    cur.close()

    return render_template("dashboard.html", requests=rows, totals=totals,
                            status_filter=status_filter)


@app.route("/request/<int:request_id>")
@approver_required
def request_detail(request_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM requests WHERE id = %s", (request_id,))
    req = cur.fetchone()
    if req is None:
        flash("Request not found.", "error")
        cur.close()
        return redirect(url_for("dashboard"))
    cur.execute(
        "SELECT * FROM line_items WHERE request_id = %s ORDER BY id",
        (request_id,)
    )
    items = cur.fetchall()
    cur.close()
    return render_template("detail.html", req=req, items=items)


@app.route("/request/<int:request_id>/update_status", methods=["POST"])
@approver_required
def update_status(request_id):
    db = get_db()
    cur = db.cursor()
    new_status = request.form.get("status")
    approver_name = request.form.get("approver_name", "").strip()

    if new_status not in ("Pending", "Approved", "Rejected", "Paid"):
        flash("Invalid status.", "error")
        cur.close()
        return redirect(url_for("request_detail", request_id=request_id))

    approved_on = datetime.datetime.now().strftime("%Y-%m-%d %H:%M") \
        if new_status in ("Approved", "Rejected", "Paid") else None

    cur.execute("""
        UPDATE requests
        SET status = %s, approver_name = %s, approved_on = %s
        WHERE id = %s
    """, (new_status, approver_name, approved_on, request_id))
    db.commit()
    cur.close()

    flash(f"Request marked as {new_status}.", "success")
    return redirect(url_for("request_detail", request_id=request_id))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
