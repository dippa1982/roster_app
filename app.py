import os
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime, date, timedelta
from pathlib import Path
import calendar as pycalendar

import fitz  # PyMuPDF
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = BASE_DIR / "settings.json"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + str(BASE_DIR / "roster.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

db = SQLAlchemy(app)

ALLOWED_EXTENSIONS = {"pdf"}
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Coordinates in the supplied Leeds roster template. PyMuPDF keeps the
# page's underlying portrait coordinates even though the PDF is displayed rotated.
DAY_Y = {
    "Monday": 635,
    "Tuesday": 539,
    "Wednesday": 443,
    "Thursday": 347,
    "Friday": 251,
    "Saturday": 155,
    "Sunday": 59,
}
ICON_DAY_Y = {day: y + 46 for day, y in DAY_Y.items()}

class Roster(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    period_start = db.Column(db.Date, nullable=True)
    period_end = db.Column(db.Date, nullable=True)
    employee = db.Column(db.String(120), nullable=True)
    employee_number = db.Column(db.String(50), nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    shifts = db.relationship(
        "Shift", backref="roster", cascade="all, delete-orphan",
        lazy=True, order_by="Shift.shift_date"
    )

class Shift(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    roster_id = db.Column(db.Integer, db.ForeignKey("roster.id"), nullable=False)
    shift_date = db.Column(db.Date, nullable=False)
    day_name = db.Column(db.String(15), nullable=False)
    start_time = db.Column(db.String(5), nullable=True)
    hours = db.Column(db.Float, default=10.0, nullable=False)
    employer = db.Column(db.String(30), nullable=False)  # Ocado / Morrisons / Holiday
    week_number = db.Column(db.Integer, nullable=False)
    pay_type = db.Column(db.String(20), nullable=False, default="normal")  # normal / holiday / overtime

    @property
    def finish_time(self):
        if not self.start_time or self.employer == "Holiday":
            return None
        try:
            start = datetime.strptime(self.start_time, "%H:%M")
            finish = start + timedelta(hours=self.hours)
            return finish.strftime("%H:%M")
        except ValueError:
            return None

class HolidayRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    roster_id = db.Column(db.Integer, db.ForeignKey("roster.id"), nullable=False)
    request_date = db.Column(db.Date, nullable=False)
    requested_by = db.Column(db.String(120), nullable=False, default="Nicola")
    note = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="Requested")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)



def migrate_schema():
    """Safely add the small schema changes used by the newer features.

    This is deliberately non-destructive so an existing roster.db can be
    upgraded without deleting the user's imported rosters.
    """
    inspector = inspect(db.engine)
    tables = inspector.get_table_names()

    # Existing installations have a shift table without pay_type.
    if "shift" in tables:
        columns = {column["name"] for column in inspector.get_columns("shift")}
        if "pay_type" not in columns:
            db.session.execute(
                text("ALTER TABLE shift ADD COLUMN pay_type VARCHAR(20) NOT NULL DEFAULT 'normal'")
            )
            db.session.commit()

    # Existing installations will not have the holiday_request table.
    # create(checkfirst=True) leaves an existing table untouched.
    HolidayRequest.__table__.create(bind=db.engine, checkfirst=True)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def nearest_day(y):
    return min(DAY_Y.items(), key=lambda item: abs(item[1] - y))[0]

def parse_roster(pdf_path):
    """Parse the supplied Spoke Leeds roster template.

    The roster uses two embedded images for shift markers:
      xref 3 = Morrisons 'M'
      xref 5 = Ocado icon
    This avoids relying on OCR to recognise the icons.
    """
    doc = fitz.open(pdf_path)
    if not doc:
        raise ValueError("The PDF contains no pages.")

    page = doc[0]
    words = page.get_text("words")

    full_text = page.get_text()
    match = re.search(
        r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})\s*-\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        full_text,
    )
    if not match:
        raise ValueError("Could not find the roster date range.")

    period_start = datetime.strptime(match.group(1), "%d %B %Y").date()
    period_end = datetime.strptime(match.group(2), "%d %B %Y").date()

    employee = None
    employee_number = None
    emp_match = re.search(r"([A-Za-z][A-Za-z .'-]+?)\s+(\d{5,})", full_text)
    if emp_match:
        employee = emp_match.group(1).strip()
        employee_number = emp_match.group(2)

    # The first-column weekly dates are positioned at x=91, 120, 149...
    # Rather than depending on those exact numbers, collect the visible dd/mm words.
    date_words = []
    for w in words:
        text = w[4]
        if re.fullmatch(r"\d{2}/\d{2}", text) and 680 <= w[1] <= 750:
            date_words.append((w[0], text))

    date_words.sort(key=lambda item: item[0])
    if not date_words:
        raise ValueError("Could not find the weekly dates in the roster.")

    week_x = {round(x, 1): idx + 1 for idx, (x, _) in enumerate(date_words)}

    # Time words indexed by x and y so each icon can be paired with its time.
    time_words = []
    for w in words:
        if re.fullmatch(r"\d{1,2}:\d{2}", w[4]):
            time_words.append(w)

    def find_time(icon_rect):
        ix = icon_rect.x0
        iy = icon_rect.y0
        candidates = [
            w for w in time_words
            if abs(w[0] - ix) <= 1.5 and 30 <= abs(w[1] - iy) <= 70
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda w: abs((w[1] - iy) - 50))

    # Detect the two actual roster icon images by their source xref.
    morrisons_rects = page.get_image_rects(3)
    ocado_rects = page.get_image_rects(5)

    rows = []

    for company, rects in [("Morrisons", morrisons_rects), ("Ocado", ocado_rects)]:
        for rect in rects:
            # Ignore legend icons and other content below/outside the roster.
            matching_week = min(
                week_x.items(), key=lambda item: abs(item[0] - round(rect.x0, 1))
            )
            if abs(matching_week[0] - rect.x0) > 2.5:
                continue

            week_no = matching_week[1]
            day = min(ICON_DAY_Y.items(), key=lambda item: abs(item[1] - rect.y0))[0]

            time_word = find_time(rect)
            if not time_word:
                continue

            rows.append({
                "week": week_no,
                "day": day,
                "start_time": time_word[4],
                "employer": company,
            })

    # Holidays are text-only cells. Pair them to the weekly x position and day.
    for w in words:
        if w[4].lower() != "holiday":
            continue
        matching_week = min(
            week_x.items(), key=lambda item: abs(item[0] - round(w[0], 1))
        )
        if abs(matching_week[0] - w[0]) > 2.5:
            continue
        rows.append({
            "week": matching_week[1],
            "day": nearest_day(w[1]),
            "start_time": None,
            "employer": "Holiday",
        })

    # Deduplicate and calculate dates from the roster's Monday start date.
    unique = {}
    for row in rows:
        key = (row["week"], row["day"])
        unique[key] = row

    shifts = []
    for (week_no, day), row in sorted(unique.items()):
        shift_date = period_start + timedelta(days=(week_no - 1) * 7 + DAYS.index(day))
        shifts.append({
            **row,
            "shift_date": shift_date,
            "hours": 0.0 if row["employer"] == "Holiday" else 10.0,
        })

    doc.close()

    if not shifts:
        raise ValueError("No shifts were detected. This app currently expects the Spoke Leeds roster format.")

    return {
        "period_start": period_start,
        "period_end": period_end,
        "employee": employee,
        "employee_number": employee_number,
        "shifts": shifts,
    }

def load_settings():
    defaults = {
        "hourly_rate": None,
        "holiday_rate": 13.66,
        "overtime_rate": 19.125,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_username": os.environ.get("SMTP_USERNAME", ""),
        "smtp_password": os.environ.get("SMTP_PASSWORD", ""),
        "email_to": os.environ.get("EMAIL_TO", os.environ.get("SMTP_USERNAME", "")),
    }
    if not SETTINGS_FILE.exists():
        return defaults
    try:
        import json
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        rate = data.get("hourly_rate")
        holiday = data.get("holiday_rate", 13.66)
        overtime = data.get("overtime_rate", (float(rate) * 1.5 if rate not in (None, "") else 19.125))
        return {
            "hourly_rate": float(rate) if rate not in (None, "") else None,
            "holiday_rate": float(holiday),
            "overtime_rate": float(overtime),
            "smtp_host": data.get("smtp_host", "smtp.gmail.com"),
            "smtp_port": int(data.get("smtp_port", 587)),
            "smtp_username": os.environ.get("SMTP_USERNAME", data.get("smtp_username", "")),
            "smtp_password": os.environ.get("SMTP_PASSWORD", data.get("smtp_password", "")),
            "email_to": os.environ.get("EMAIL_TO", data.get("email_to", os.environ.get("SMTP_USERNAME", ""))),
        }
    except (ValueError, TypeError, OSError):
        return defaults

def send_holiday_request_email(holiday):
    """Email a newly submitted holiday request. Returns (True, message) or (False, error)."""
    settings = load_settings()
    host = settings.get("smtp_host") or "smtp.gmail.com"
    port = int(settings.get("smtp_port") or 587)
    username = (settings.get("smtp_username") or "").strip()
    password = settings.get("smtp_password") or ""
    recipient = (settings.get("email_to") or username).strip()

    if not username or not password or not recipient:
        return False, "Email is not configured yet. Add your SMTP details in Settings."

    msg = EmailMessage()
    msg["Subject"] = f"Holiday Request - {holiday.request_date.strftime('%A %d %B %Y')}"
    msg["From"] = username
    msg["To"] = recipient
    note = holiday.note.strip() if holiday.note else "None"
    msg.set_content(
        f"Holiday requested\n\n"
        f"Date: {holiday.request_date.strftime('%A %d %B %Y')}\n"
        f"Requested by: {holiday.requested_by}\n"
        f"Note: {note}\n\n"
        "Please add this holiday request to the MiOcado app.\n"
    )

    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(msg)
        return True, "Holiday request email sent."
    except Exception as exc:
        return False, f"Holiday request was saved, but the email could not be sent: {exc}"

def save_settings(data):
    import json
    SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

def money(value):
    return f"£{float(value):,.2f}"

def build_month_options(roster):
    options = []
    cursor = date(roster.period_start.year, roster.period_start.month, 1)
    end = date(roster.period_end.year, roster.period_end.month, 1)
    while cursor <= end:
        options.append({"year": cursor.year, "month": cursor.month, "label": cursor.strftime("%B %Y")})
        y, m = month_offset(cursor.year, cursor.month, 1)
        cursor = date(y, m, 1)
    return options

def serialise_shifts(shifts):
    import json
    return json.dumps({
        str(s.id): {
            "date_label": s.shift_date.strftime("%A %d %B %Y"),
            "day": s.day_name,
            "start_time": s.start_time,
            "finish_time": s.finish_time,
            "hours": s.hours,
            "employer": s.employer,
            "pay_type": s.pay_type,
            "week": s.week_number,
        } for s in shifts
    })

def build_calendar_data(shifts, year, month, holiday_requests=None):
    """Build a Monday-first calendar grid for the requested month."""
    cal = pycalendar.Calendar(firstweekday=0)  # Monday
    weeks = []
    today = date.today()
    by_date = {}
    for shift in shifts:
        by_date.setdefault(shift.shift_date, []).append(shift)
    requests_by_date = {}
    for req in (holiday_requests or []):
        requests_by_date.setdefault(req.request_date, []).append(req)

    for week in cal.monthdatescalendar(year, month):
        cells = []
        for day_value in week:
            cells.append({
                "date": day_value,
                "in_month": day_value.month == month,
                "is_today": day_value == today,
                "events": by_date.get(day_value, []),
                "holiday_requests": requests_by_date.get(day_value, []),
            })
        weeks.append(cells)
    return weeks


def month_offset(year, month, offset):
    value = year * 12 + (month - 1) + offset
    return value // 12, value % 12 + 1


def calendar_context(roster, shifts, year, month):
    requests = HolidayRequest.query.filter_by(roster_id=roster.id).all()
    month_requests = [r for r in requests if r.request_date.year == year and r.request_date.month == month]
    calendar_weeks = build_calendar_data(shifts, year, month, month_requests)
    month_events = [s for s in shifts if s.shift_date.year == year and s.shift_date.month == month]
    worked = [s for s in month_events if s.employer != "Holiday"]
    prev_year, prev_month = month_offset(year, month, -1)
    next_year, next_month = month_offset(year, month, 1)
    settings = load_settings()
    hourly_rate = settings.get("hourly_rate")
    holiday_rate = settings.get("holiday_rate", 13.66)
    overtime_rate = settings.get("overtime_rate", (hourly_rate * 1.5 if hourly_rate else 19.125))
    return {
        "roster": roster,
        "shifts": shifts,
        "calendar_weeks": calendar_weeks,
        "year": year,
        "month": month,
        "month_name": date(year, month, 1).strftime("%B"),
        "prev_year": prev_year,
        "prev_month": prev_month,
        "next_year": next_year,
        "next_month": next_month,
        "today": date.today(),
        "weekdays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "month_stats": {
            "shifts": len(worked),
            "hours": sum(s.hours for s in worked),
            "ocado": sum(s.employer == "Ocado" for s in worked),
            "morrisons": sum(s.employer == "Morrisons" for s in worked),
            "holidays": sum(s.employer == "Holiday" for s in month_events),
            "earnings": sum(
                s.hours * (
                    overtime_rate if s.pay_type == "overtime"
                    else holiday_rate if s.pay_type == "holiday"
                    else hourly_rate
                )
                for s in worked
            ) if hourly_rate else 0,
        },
        "hourly_rate": hourly_rate,
        "holiday_rate": holiday_rate,
        "overtime_rate": overtime_rate,
        "money": money,
        "month_events": month_events,
        "holiday_requests": month_requests,
        "month_options": build_month_options(roster),
        "shift_json": serialise_shifts(shifts),
    }


@app.route("/")
def dashboard():
    roster = Roster.query.filter_by(active=True).order_by(Roster.uploaded_at.desc()).first()
    if not roster:
        return render_template("dashboard.html", roster=None, shifts=[], stats={})

    shifts = Shift.query.filter_by(roster_id=roster.id).order_by(Shift.shift_date).all()
    worked = [s for s in shifts if s.employer != "Holiday"]
    today = date.today()

    stats = {
        "total_shifts": len(worked),
        "total_hours": sum(s.hours for s in worked),
        "ocado_shifts": sum(s.employer == "Ocado" for s in worked),
        "ocado_hours": sum(s.hours for s in worked if s.employer == "Ocado"),
        "morrisons_shifts": sum(s.employer == "Morrisons" for s in worked),
        "morrisons_hours": sum(s.hours for s in worked if s.employer == "Morrisons"),
        "holidays": sum(s.employer == "Holiday" for s in shifts),
    }

    # Dashboard preview uses the month containing today, unless today is
    # outside the roster period, in which case it uses the roster's start month.
    if roster.period_start <= today <= roster.period_end:
        preview_date = today
    else:
        preview_date = roster.period_start
    preview = build_calendar_data(shifts, preview_date.year, preview_date.month)

    upcoming = [s for s in shifts if s.shift_date >= today]
    upcoming_shifts = upcoming[:8]
    settings = load_settings()
    hourly_rate = settings.get("hourly_rate")
    holiday_rate = settings.get("holiday_rate", 13.66)
    overtime_rate = settings.get("overtime_rate", (hourly_rate * 1.5 if hourly_rate else 19.125))
    return render_template(
        "dashboard.html",
        roster=roster,
        shifts=shifts,
        stats=stats,
        upcoming_shifts=upcoming_shifts,
        preview_calendar_weeks=preview,
        preview_year=preview_date.year,
        preview_month=preview_date.month,
        preview_month_name=preview_date.strftime("%B"),
        weekdays=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        hourly_rate=hourly_rate,
        money=money,
    )


@app.route("/calendar")
def calendar():
    roster = Roster.query.filter_by(active=True).order_by(Roster.uploaded_at.desc()).first()
    if not roster:
        return render_template("calendar.html", roster=None)

    shifts = Shift.query.filter_by(roster_id=roster.id).order_by(Shift.shift_date).all()
    requested_year = request.args.get("year", type=int)
    requested_month = request.args.get("month", type=int)

    if requested_year and requested_month and 1 <= requested_month <= 12:
        year, month = requested_year, requested_month
    else:
        today = date.today()
        if roster.period_start <= today <= roster.period_end:
            year, month = today.year, today.month
        else:
            year, month = roster.period_start.year, roster.period_start.month

    return render_template("calendar.html", **calendar_context(roster, shifts, year, month))


@app.route("/upload", methods=["GET", "POST"])
def upload_roster():
    if request.method == "GET":
        history = Roster.query.order_by(Roster.uploaded_at.desc()).all()
        return render_template("upload.html", history=history)

    file = request.files.get("roster")
    if not file or not file.filename:
        flash("Choose a PDF roster first.", "error")
        return redirect(url_for("upload_roster"))

    if not allowed_file(file.filename):
        flash("Only PDF files are supported.", "error")
        return redirect(url_for("upload_roster"))

    filename = secure_filename(file.filename)
    saved_path = UPLOAD_DIR / f"{datetime.utcnow():%Y%m%d%H%M%S}_{filename}"
    file.save(saved_path)

    try:
        parsed = parse_roster(saved_path)
    except Exception as exc:
        saved_path.unlink(missing_ok=True)
        flash(f"Couldn't read that roster: {exc}", "error")
        return redirect(url_for("upload_roster"))

    # Keep old rosters for history, but only one is active.
    Roster.query.update({Roster.active: False})

    roster = Roster(
        filename=filename,
        period_start=parsed["period_start"],
        period_end=parsed["period_end"],
        employee=parsed["employee"],
        employee_number=parsed["employee_number"],
        active=True,
    )
    db.session.add(roster)
    db.session.flush()

    for item in parsed["shifts"]:
        db.session.add(Shift(
            roster_id=roster.id,
            shift_date=item["shift_date"],
            day_name=item["day"],
            start_time=item["start_time"],
            hours=item["hours"],
            employer=item["employer"],
            week_number=item["week"],
            pay_type="holiday" if item["employer"] == "Holiday" else "normal",
        ))

    db.session.commit()
    flash("New roster uploaded successfully.", "success")
    return redirect(url_for("dashboard"))


@app.route("/shift/add", methods=["POST"])
def add_shift():
    roster = Roster.query.filter_by(active=True).order_by(Roster.uploaded_at.desc()).first()
    if not roster:
        flash("Upload a roster before adding a shift.", "error")
        return redirect(url_for("upload_roster"))

    raw_date = request.form.get("shift_date", "").strip()
    employer = request.form.get("employer", "Ocado").strip()
    start_time = request.form.get("start_time", "").strip() or None
    raw_hours = request.form.get("hours", "10").strip()
    pay_type = request.form.get("pay_type", "normal").strip().lower()
    if pay_type not in {"normal", "overtime", "holiday"}:
        pay_type = "normal"

    try:
        shift_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        hours = float(raw_hours)
        if hours <= 0 or hours > 24:
            raise ValueError
    except ValueError:
        flash("Enter a valid date and shift length.", "error")
        return redirect(url_for("calendar"))

    if employer not in {"Ocado", "Morrisons"}:
        flash("Choose Ocado or Morrisons.", "error")
        return redirect(url_for("calendar", year=shift_date.year, month=shift_date.month))

    if start_time:
        try:
            datetime.strptime(start_time, "%H:%M")
        except ValueError:
            flash("Enter a valid start time.", "error")
            return redirect(url_for("calendar", year=shift_date.year, month=shift_date.month))

    shift = Shift(
        roster_id=roster.id,
        shift_date=shift_date,
        day_name=shift_date.strftime("%A"),
        start_time=start_time,
        hours=hours,
        employer=employer,
        week_number=((shift_date - roster.period_start).days // 7) + 1,
        pay_type=pay_type,
    )
    db.session.add(shift)
    db.session.commit()
    flash(f"Extra {employer} shift added for {shift_date.strftime('%d %b %Y')}.", "success")
    return redirect(url_for("calendar", year=shift_date.year, month=shift_date.month))

@app.route("/holiday-request", methods=["POST"])
def holiday_request():
    roster = Roster.query.filter_by(active=True).order_by(Roster.uploaded_at.desc()).first()
    if not roster:
        flash("Upload a roster before requesting holiday.", "error")
        return redirect(url_for("upload_roster"))
    raw_date = request.form.get("request_date", "").strip()
    try:
        request_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        flash("Choose a valid holiday date.", "error")
        return redirect(url_for("calendar"))
    req = HolidayRequest(
        roster_id=roster.id,
        request_date=request_date,
        requested_by=request.form.get("requested_by", "Nicola").strip() or "Nicola",
        note=request.form.get("note", "").strip(),
    )
    db.session.add(req)
    db.session.commit()

    sent, message = send_holiday_request_email(req)
    if sent:
        flash(f"Holiday request added and emailed for {request_date.strftime('%d %b %Y')}.", "success")
    else:
        flash(f"Holiday request added for {request_date.strftime('%d %b %Y')}. {message}", "error")
    return redirect(url_for("calendar", year=request_date.year, month=request_date.month))


@app.route("/test-email", methods=["POST"])
def test_email():
    settings = load_settings()
    host = settings.get("smtp_host") or "smtp.gmail.com"
    port = int(settings.get("smtp_port") or 587)
    username = (settings.get("smtp_username") or "").strip()
    password = settings.get("smtp_password") or ""
    recipient = (settings.get("email_to") or username).strip()

    if not username or not password or not recipient:
        flash("Enter your SMTP username, app password and recipient before testing.", "error")
        return redirect(url_for("settings"))

    msg = EmailMessage()
    msg["Subject"] = "My Roster - Test Email"
    msg["From"] = username
    msg["To"] = recipient
    msg.set_content(
        "This is a test email from My Roster.\n\n"
        "If you received this message, your SMTP email settings are working correctly."
    )

    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(msg)
        flash(f"Test email sent successfully to {recipient}.", "success")
    except Exception as exc:
        flash(f"Test email failed: {exc}", "error")
    return redirect(url_for("settings"))

@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        try:
            rate = float(request.form.get("hourly_rate", "").strip())
            holiday_rate = float(request.form.get("holiday_rate", "13.66").strip())
            overtime_rate = float(request.form.get("overtime_rate", "").strip())
            if rate < 0 or holiday_rate < 0 or overtime_rate < 0:
                raise ValueError
        except ValueError:
            flash("Enter valid pay rates.", "error")
            return redirect(url_for("settings"))
        current = load_settings()
        smtp_host = request.form.get("smtp_host", "smtp.gmail.com").strip() or "smtp.gmail.com"
        try:
            smtp_port = int(request.form.get("smtp_port", "587").strip())
            if smtp_port <= 0:
                raise ValueError
        except ValueError:
            flash("Enter a valid SMTP port.", "error")
            return redirect(url_for("settings"))
        smtp_username = request.form.get("smtp_username", "").strip()
        smtp_password = request.form.get("smtp_password", "")
        if smtp_password == "":
            smtp_password = current.get("smtp_password", "")
        email_to = request.form.get("email_to", "").strip()
        save_settings({
            "hourly_rate": rate,
            "holiday_rate": holiday_rate,
            "overtime_rate": overtime_rate,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "smtp_username": smtp_username,
            "smtp_password": smtp_password,
            "email_to": email_to,
        })
        flash("Settings saved.", "success")
        return redirect(url_for("settings"))
    return render_template("settings.html", settings=load_settings())

@app.route("/history")
def history():
    rosters = Roster.query.order_by(Roster.uploaded_at.desc()).all()
    return render_template("history.html", rosters=rosters)

@app.route("/roster/<int:roster_id>/activate")
def activate_roster(roster_id):
    roster = db.get_or_404(Roster, roster_id)
    Roster.query.update({Roster.active: False})
    roster.active = True
    db.session.commit()
    flash("Roster activated.", "success")
    return redirect(url_for("dashboard"))

@app.route("/roster/<int:roster_id>/download")
def download_roster(roster_id):
    roster = db.get_or_404(Roster, roster_id)
    return send_from_directory(UPLOAD_DIR, next(
        p.name for p in UPLOAD_DIR.iterdir() if p.name.endswith("_" + roster.filename)
    ), as_attachment=True, download_name=roster.filename)

@app.cli.command("init-db")
def init_db():
    db.create_all()
    print("Database ready.")

with app.app_context():
    db.create_all()
    migrate_schema()

if __name__ == "__main__":
    app.run(debug=True)
