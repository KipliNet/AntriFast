# app.py
import os
from datetime import datetime, date
import requests
import qrcode

from flask import (
    Flask, render_template, request, redirect,
    session, url_for, flash, abort
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask_socketio import SocketIO, join_room
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------
# CONFIG FLASK & DATABASE
# --------------------------------------------------

app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY", "supersecretkey_change_me")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:postgres@postgres/antridb"
)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
# folder upload untuk media display
app.config["UPLOAD_FOLDER"] = os.path.join("static", "upload")
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

ALLOWED_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
ALLOWED_VIDEO_EXT = {"mp4", "webm", "ogg"}

socketio = SocketIO(app)

# --------------------------------------------------
# JINJA CONTEXT (untuk now() di template)
# --------------------------------------------------

@app.context_processor
def inject_now():
    # di template, kita bisa pakai {{ now().year }}
    return {"now": datetime.now}


# --------------------------------------------------
# MODELS
# --------------------------------------------------

class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)

    umkm = db.relationship("UMKM", backref="owner", uselist=False)


class UMKM(db.Model):
    __tablename__ = "umkm"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    name = db.Column(db.String(150))
    owner_whatsapp = db.Column(db.String(30), nullable=True)
    slug = db.Column(db.String(150), unique=True)
    credit_balance = db.Column(db.Integer, default=0)
    qr_path = db.Column(db.String(255), nullable=True)
    display_ticker = db.Column(db.Text, nullable=True)
    display_images = db.Column(db.Text, nullable=True)
    display_videos = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    queues = db.relationship("Queue", backref="umkm", lazy=True)
    wa_logs = db.relationship("WALog", backref="umkm", lazy=True)
    credit_logs = db.relationship("CreditLog", backref="umkm", lazy=True)


class Queue(db.Model):
    __tablename__ = "queues"

    id = db.Column(db.Integer, primary_key=True)
    umkm_id = db.Column(db.Integer, db.ForeignKey("umkm.id"))
    queue_number = db.Column(db.Integer)  # nomor increment per UMKM per hari
    customer_name = db.Column(db.String(120))
    customer_phone = db.Column(db.String(30))
    status = db.Column(db.String(20), default="waiting")  # waiting/called/done/canceled
    created_at = db.Column(db.DateTime, default=datetime.now)
    called_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    canceled_at = db.Column(db.DateTime)

    wa_logs = db.relationship("WALog", backref="queue", lazy=True)


class WALog(db.Model):
    __tablename__ = "wa_logs"

    id = db.Column(db.Integer, primary_key=True)
    umkm_id = db.Column(db.Integer, db.ForeignKey("umkm.id"))
    queue_id = db.Column(db.Integer, db.ForeignKey("queues.id"))
    phone_number = db.Column(db.String(30))
    message = db.Column(db.Text)
    status = db.Column(db.String(20))  # 200/error/...
    response_raw = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now)


class CreditLog(db.Model):
    __tablename__ = "credit_logs"

    id = db.Column(db.Integer, primary_key=True)
    umkm_id = db.Column(db.Integer, db.ForeignKey("umkm.id"))
    change = db.Column(db.Integer)  # + / -
    description = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.now)

class TopupTransaction(db.Model):
    __tablename__ = "topup_transactions"

    id = db.Column(db.Integer, primary_key=True)
    umkm_id = db.Column(db.Integer, db.ForeignKey("umkm.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    package_name = db.Column(db.String(100), nullable=False)
    credits = db.Column(db.Integer, nullable=False)
    amount = db.Column(db.Integer, nullable=True)  # nominal rupiah (opsional)

    # gambar QRIS yang digunakan (misal: img/qris_kecil.png)
    qris_image = db.Column(db.String(255), nullable=True)
    # path bukti transfer (gambar) yang diupload user
    proof_image = db.Column(db.String(255), nullable=True)

    # "pending"        = baru pilih paket, belum konfirmasi
    # "waiting_admin"  = user sudah konfirmasi + upload bukti, menunggu ACC admin
    # "success"        = admin sudah ACC, kredit sudah masuk
    # "rejected"       = admin tolak (opsional)
    status = db.Column(db.String(20), default="pending")

    sender_name = db.Column(db.String(150), nullable=True)
    note = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.now)
    confirmed_at = db.Column(db.DateTime, nullable=True)

    umkm = db.relationship("UMKM", backref="topup_transactions", lazy=True)
    user = db.relationship("User", backref="topup_transactions", lazy=True)

# --------------------------------------------------
# UTIL: AUTH, QUEUE, WA, QR
# --------------------------------------------------

def allowed_file(filename, allowed_ext):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_ext

def broadcast_queue_update(umkm: UMKM):
    """
    Broadcast status antrian ke semua client display (mode TV) untuk UMKM ini.
    Data yang dikirim:
    - current_called: nomor & nama
    - waiting: list nomor & nama
    """
    today = date.today()

    current_called = (
        Queue.query
        .filter(
            Queue.umkm_id == umkm.id,
            Queue.status == "called",
            db.func.date(Queue.created_at) == today
        )
        .order_by(Queue.called_at.desc())
        .first()
    )

    waiting_list = (
        Queue.query
        .filter(
            Queue.umkm_id == umkm.id,
            Queue.status == "waiting",
            db.func.date(Queue.created_at) == today
        )
        .order_by(Queue.queue_number.asc())
        .all()
    )

    data = {
        "current_called": {
            "number": current_called.queue_number,
            "name": current_called.customer_name or "Tanpa nama"
        } if current_called else None,
        "waiting": [
            {
                "number": q.queue_number,
                "name": q.customer_name or "Tanpa nama"
            } for q in waiting_list
        ]
    }

    # gunakan slug sebagai room
    socketio.emit("queue_update", data, room=umkm.slug)

@socketio.on("join_display")
def handle_join_display(data):
    """
    Dipanggil dari halaman display untuk bergabung ke 'room' UMKM tertentu.
    Room yang dipakai: slug UMKM.
    """
    room = data.get("room")
    if room:
        join_room(room)

def get_current_user():
    """Return User object if logged in, else None."""
    user_id = session.get("user_id")
    if user_id:
        return User.query.get(user_id)
    return None


def generate_next_queue_number(umkm_id: int) -> int:
    """Ambil nomor antrian terakhir HARI INI untuk UMKM tersebut lalu +1."""
    today = date.today()
    last_queue = (
        Queue.query
        .filter(
            Queue.umkm_id == umkm_id,
            db.func.date(Queue.created_at) == today
        )
        .order_by(Queue.queue_number.desc())
        .first()
    )
    return 1 if not last_queue else last_queue.queue_number + 1


def send_whatsapp_notification(number: str, message: str):
    """
    Fungsi integrasi WhatsApp ke API SUKIPLI.
    Di sinilah request POST dikirim.
    """
    url = "https://wa.sukipli.work/send-message"
    payload = {
        "number": number,
        "message": message
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.status_code, r.text
    except Exception as e:
        # Jika error jaringan / timeout
        return "error", str(e)


def generate_umkm_qr(umkm: UMKM):
    """Generate QR code PNG file for the UMKM URL dan simpan di static/qr."""
    qr_folder = os.path.join("static", "qr")
    os.makedirs(qr_folder, exist_ok=True)

    url = f"{request.url_root}{umkm.slug}"  # misal: http://localhost:5000/barbershop-andi
    img = qrcode.make(url)

    filename = f"{umkm.slug}.png"
    path = os.path.join(qr_folder, filename)
    img.save(path)

    umkm.qr_path = filename
    db.session.commit()
    return filename


def send_auto_reminders(umkm: UMKM, base_url: str):
    """
    Kirim WA otomatis untuk antrian yang jaraknya <= 3 nomor lagi.
    - Hanya untuk HARI INI.
    - Hanya kirim sekali per antrian (cek di WALog dengan tag [AUTO_REMINDER]).
    - Mengurangi kredit per pesan.
    - base_url: request.url_root dari route pemanggil.
    """
    today = date.today()
    base_url = base_url.rstrip("/")

    current_called = (
        Queue.query
        .filter(
            Queue.umkm_id == umkm.id,
            Queue.status == "called",
            db.func.date(Queue.created_at) == today
        )
        .order_by(Queue.called_at.desc())
        .first()
    )

    if not current_called:
        return

    waiting_list = (
        Queue.query
        .filter(
            Queue.umkm_id == umkm.id,
            Queue.status == "waiting",
            db.func.date(Queue.created_at) == today
        )
        .order_by(Queue.queue_number.asc())
        .all()
    )

    for q in waiting_list:
        if not q.customer_phone:
            continue

        ahead = q.queue_number - current_called.queue_number - 1
        if ahead < 0 or ahead > 2:
            continue

        already = WALog.query.filter(
            WALog.queue_id == q.id,
            WALog.message.like("%[AUTO_REMINDER]%")
        ).first()

        if already:
            continue

        if umkm.credit_balance <= 0:
            break

        if ahead <= 0:
            status_text = "giliran Anda hampir tiba (berikutnya)."
        else:
            status_text = f"tinggal {ahead} orang di depan Anda."

        ticket_url = f"{base_url}/{umkm.slug}?ticket_id={q.id}"

        msg = (
            f"[AUTO_REMINDER] Halo {q.customer_name or 'Pelanggan'}, ini dari {umkm.name}. "
            f"Antrian Anda #{q.queue_number} {status_text} "
            f"Cek status antrian di sini: {ticket_url}"
        )

        status, raw = send_whatsapp_notification(q.customer_phone, msg)

        walog = WALog(
            umkm_id=umkm.id,
            queue_id=q.id,
            phone_number=q.customer_phone,
            message=msg,
            status=str(status),
            response_raw=str(raw)
        )
        db.session.add(walog)

        if status == 200:
            umkm.credit_balance -= 1
            db.session.add(CreditLog(
                umkm_id=umkm.id,
                change=-1,
                description=f"Auto reminder ke #{q.queue_number}"
            ))

        db.session.commit()


# --------------------------------------------------
# ROUTES: PUBLIC / LANDING / OFFLINE
# --------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/offline")
def offline():
    # digunakan oleh service worker sebagai offline fallback
    return render_template("offline.html")


# --------------------------------------------------
# ROUTES: AUTH
# --------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name")
        email = request.form.get("email")
        password = request.form.get("password")
        umkm_name = request.form.get("umkm_name")
        slug = request.form.get("slug")
        owner_whatsapp = request.form.get("owner_whatsapp", "").strip()

        # cek email sudah dipakai
        if User.query.filter_by(email=email).first():
            flash("Email sudah terdaftar.", "danger")
            return redirect(url_for("register"))

        # cek slug unik
        if UMKM.query.filter_by(slug=slug).first():
            flash("Slug sudah dipakai, gunakan yang lain.", "danger")
            return redirect(url_for("register"))

        hashed = generate_password_hash(password)

        user = User(name=name, email=email, password_hash=hashed)
        db.session.add(user)
        db.session.commit()

        umkm = UMKM(
            user_id=user.id,
            name=umkm_name,
            slug=slug,
            owner_whatsapp=owner_whatsapp,
            credit_balance=10  # bonus awal
        )
        db.session.add(umkm)
        db.session.commit()

        # auto generate QR pertama kali
        try:
            generate_umkm_qr(umkm)
        except Exception:
            # kalau gagal generate QR, tidak perlu blok registrasi
            pass

        flash("Registrasi berhasil, silakan login!", "success")
        return redirect(url_for("login"))

    return render_template("auth_register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")

        user = User.query.filter_by(email=email).first()

        if not user or not check_password_hash(user.password_hash, password):
            flash("Email atau password salah.", "danger")
            return redirect(url_for("login"))

        session["user_id"] = user.id
        return redirect(url_for("dashboard"))

    return render_template("auth_login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/contact")
def contact():
    return render_template("contact.html")


# --------------------------------------------------
# ROUTES: PUBLIC QUEUE (CUSTOMER)
# --------------------------------------------------

@app.route("/<slug_umkm>")
def queue_public(slug_umkm):
    umkm = UMKM.query.filter_by(slug=slug_umkm).first_or_404()
    today = date.today()

    current_called = (
        Queue.query
        .filter(
            Queue.umkm_id == umkm.id,
            Queue.status == "called",
            db.func.date(Queue.created_at) == today
        )
        .order_by(Queue.called_at.desc())
        .first()
    )

    waiting_list = (
        Queue.query
        .filter(
            Queue.umkm_id == umkm.id,
            Queue.status == "waiting",
            db.func.date(Queue.created_at) == today
        )
        .order_by(Queue.queue_number.asc())
        .all()
    )

    count_today = (
        Queue.query
        .filter(
            Queue.umkm_id == umkm.id,
            db.func.date(Queue.created_at) == today
        )
        .count()
    )

    ticket_id = request.args.get("ticket_id", type=int)
    new_ticket = None
    if ticket_id:
        new_ticket = Queue.query.filter_by(
            id=ticket_id,
            umkm_id=umkm.id
        ).first()

    return render_template(
        "queue_public.html",
        umkm=umkm,
        current_called=current_called,
        waiting=waiting_list,
        count_today=count_today,
        new_ticket=new_ticket
    )


@app.route("/<slug_umkm>/take", methods=["POST"])
def take_queue(slug_umkm):
    umkm = UMKM.query.filter_by(slug=slug_umkm).first_or_404()

    name = request.form.get("customer_name")
    phone = request.form.get("customer_phone")

    next_num = generate_next_queue_number(umkm.id)

    q = Queue(
        umkm_id=umkm.id,
        queue_number=next_num,
        customer_name=name,
        customer_phone=phone,
        status="waiting"
    )
    db.session.add(q)
    db.session.commit()

    # WA KONFIRMASI: kirim sekali saat ambil nomor (jika ada nomor WA & ada kredit)
    if phone and umkm.credit_balance > 0:
        base_url = request.url_root.rstrip("/")
        ticket_url = f"{base_url}/{umkm.slug}?ticket_id={q.id}"

        msg = (
            f"[NEW_TICKET] Halo {name or 'Pelanggan'}, ini dari {umkm.name}. \n"
            f"Terima kasih telah mengambil nomor antrian. \n"
            f"Nomor Anda adalah *#{next_num}*. \n"
            f"Cek status antrian Anda di sini: {ticket_url}\n"
        )

        status, raw = send_whatsapp_notification(phone, msg)

        walog = WALog(
            umkm_id=umkm.id,
            queue_id=q.id,
            phone_number=phone,
            message=msg,
            status=str(status),
            response_raw=str(raw)
        )
        db.session.add(walog)

        if status == 200:
            umkm.credit_balance -= 1
            db.session.add(CreditLog(
                umkm_id=umkm.id,
                change=-1,
                description=f"Konfirmasi tiket #{q.queue_number}"
            ))

        db.session.commit()

    broadcast_queue_update(umkm)

    flash(f"Nomor antrian Anda: {next_num}", "success")

    return redirect(
        url_for("queue_public", slug_umkm=slug_umkm, ticket_id=q.id)
    )

# --------------------------------------------------
# ROUTES: DASHBOARD OWNER
# --------------------------------------------------

@app.route("/dashboard")
def dashboard():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    umkm = user.umkm
    if not umkm:
        flash("UMKM belum terhubung dengan akun ini.", "danger")
        return redirect(url_for("index"))

    today = date.today()

    waiting = (
        Queue.query
        .filter(
            Queue.umkm_id == umkm.id,
            Queue.status == "waiting",
            db.func.date(Queue.created_at) == today
        )
        .order_by(Queue.queue_number.asc())
        .all()
    )

    current_called = (
        Queue.query
        .filter(
            Queue.umkm_id == umkm.id,
            Queue.status == "called",
            db.func.date(Queue.created_at) == today
        )
        .order_by(Queue.called_at.desc())
        .first()
    )

    count_today = (
        Queue.query
        .filter(
            Queue.umkm_id == umkm.id,
            db.func.date(Queue.created_at) == today
        )
        .count()
    )

    history = (
        Queue.query
        .filter(
            Queue.umkm_id == umkm.id,
            Queue.status.in_(["done", "canceled", "no_show"]),
            db.func.date(Queue.created_at) == today
        )
        .order_by(Queue.queue_number.asc())
        .all()
    )


    return render_template(
        "dashboard_owner.html",
        umkm=umkm,
        waiting=waiting,
        current_called=current_called,
        count_today=count_today,
        history=history
    )


@app.route("/dashboard/queue/next", methods=["POST"])
def queue_next():
    """
    Tombol utama:
    - Jika ada nomor yang sedang dipanggil → anggap SUDAH DILAYANI (done).
    - Lalu panggil nomor waiting berikutnya (kalau ada).
    - Setelah itu jalankan auto reminder (kurang 3 nomor lagi).
    """
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    umkm = user.umkm
    today = date.today()

    # 1. Tandai nomor yang sedang dipanggil sebagai selesai (done)
    active_called = (
        Queue.query
        .filter(
            Queue.umkm_id == umkm.id,
            Queue.status == "called",
            db.func.date(Queue.created_at) == today
        )
        .first()
    )

    if active_called:
        active_called.status = "done"
        active_called.finished_at = datetime.now()
        db.session.commit()

    # 2. Ambil antrian waiting paling awal
    waiting = (
        Queue.query
        .filter(
            Queue.umkm_id == umkm.id,
            Queue.status == "waiting",
            db.func.date(Queue.created_at) == today
        )
        .order_by(Queue.queue_number.asc())
        .first()
    )

    if waiting:
        waiting.status = "called"
        waiting.called_at = datetime.now()
        db.session.commit()

        flash(f"Memanggil nomor {waiting.queue_number}", "success")

        # 3. Auto reminder ke antrian yang sudah dekat
        send_auto_reminders(umkm, request.url_root)
    else:
        # Tidak ada waiting, hanya menyelesaikan yang aktif
        if active_called:
            flash("Nomor terakhir diselesaikan. Tidak ada antrian menunggu.", "info")
        else:
            flash("Tidak ada antrian aktif.", "info")

    broadcast_queue_update(umkm)

    return redirect(url_for("dashboard"))

@app.route("/dashboard/queue/skip", methods=["POST"])
def queue_skip():
    """
    Tombol untuk kasus pelanggan tidak hadir:
    - Nomor yang sedang dipanggil → status no_show.
    - Lalu panggil waiting berikutnya.
    - Auto reminder tetap dijalankan.
    """
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    umkm = user.umkm
    today = date.today()

    # 1. Tandai nomor aktif sebagai no_show
    active_called = (
        Queue.query
        .filter(
            Queue.umkm_id == umkm.id,
            Queue.status == "called",
            db.func.date(Queue.created_at) == today
        )
        .first()
    )

    if active_called:
        active_called.status = "no_show"
        active_called.finished_at = datetime.now()
        db.session.commit()
    else:
        flash("Tidak ada nomor yang sedang dipanggil.", "info")

    # 2. Ambil waiting berikutnya
    waiting = (
        Queue.query
        .filter(
            Queue.umkm_id == umkm.id,
            Queue.status == "waiting",
            db.func.date(Queue.created_at) == today
        )
        .order_by(Queue.queue_number.asc())
        .first()
    )

    if waiting:
        waiting.status = "called"
        waiting.called_at = datetime.now()
        db.session.commit()
        flash(f"Melewati nomor sebelumnya. Memanggil nomor {waiting.queue_number}.", "info")

        send_auto_reminders(umkm, request.url_root)
    else:
        flash("Tidak ada antrian menunggu.", "info")

    broadcast_queue_update(umkm)

    return redirect(url_for("dashboard"))


@app.route("/dashboard/queue/finish/<int:queue_id>", methods=["POST"])
def queue_finish(queue_id):
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    umkm = user.umkm
    q = Queue.query.get_or_404(queue_id)
    if q.umkm_id != umkm.id:
        abort(404)

    q.status = "done"
    q.finished_at = datetime.now()
    db.session.commit()

    flash(f"Nomor {q.queue_number} diselesaikan.", "success")
    return redirect(url_for("dashboard"))


@app.route("/dashboard/queue/cancel/<int:queue_id>", methods=["POST"])
def queue_cancel(queue_id):
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    umkm = user.umkm
    q = Queue.query.get_or_404(queue_id)
    if q.umkm_id != umkm.id:
        abort(404)

    q.status = "canceled"
    q.canceled_at = datetime.now()
    db.session.commit()

    flash(f"Nomor {q.queue_number} dibatalkan.", "success")
    broadcast_queue_update(umkm)
    return redirect(url_for("dashboard"))


# --------------------------------------------------
# ROUTES: WHATSAPP & KREDIT
# --------------------------------------------------

@app.route("/dashboard/wa/send/<int:queue_id>", methods=["POST"])
def send_wa(queue_id):
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    umkm = user.umkm
    q = Queue.query.get_or_404(queue_id)
    if q.umkm_id != umkm.id:
        abort(404)

    if umkm.credit_balance <= 0:
        flash("Kredit tidak cukup!", "danger")
        return redirect(url_for("dashboard"))

    if not q.customer_phone:
        flash("Nomor pelanggan tidak tersedia.", "danger")
        return redirect(url_for("dashboard"))

    base_url = request.url_root.rstrip("/")
    ticket_url = f"{base_url}/{umkm.slug}?ticket_id={q.id}"

    msg = (
        f"Halo {q.customer_name or 'Pelanggan'}, ini dari {umkm.name}. "
        f"Antrian Anda #{q.queue_number} akan segera dipanggil. Mohon bersiap ke lokasi. "
        f"Cek status antrian di sini: {ticket_url}"
    )

    status, raw = send_whatsapp_notification(q.customer_phone, msg)

    walog = WALog(
        umkm_id=umkm.id,
        queue_id=q.id,
        phone_number=q.customer_phone,
        message=msg,
        status=str(status),
        response_raw=str(raw)
    )
    db.session.add(walog)

    if status == 200:
        umkm.credit_balance -= 1
        db.session.add(CreditLog(
            umkm_id=umkm.id,
            change=-1,
            description=f"Kirim WA manual ke #{q.queue_number}"
        ))
        db.session.commit()
        flash("Pesan WA berhasil dikirim!", "success")
    else:
        db.session.commit()
        flash("Gagal mengirim WA. Kredit tidak dipotong.", "danger")

    return redirect(url_for("dashboard"))
 
@app.route("/dashboard/topup/start", methods=["POST"])
def start_topup():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    umkm = user.umkm
    if not umkm:
        flash("UMKM tidak ditemukan.", "danger")
        return redirect(url_for("dashboard_settings"))

    package_name = request.form.get("package_name", "").strip() or "Paket"
    credits_raw = request.form.get("credits", "0")
    amount_raw = request.form.get("amount", "").strip()

    try:
        credits = int(credits_raw)
    except ValueError:
        flash("Paket top-up tidak valid.", "danger")
        return redirect(url_for("dashboard_settings"))

    amount = None
    if amount_raw:
        try:
            amount = int(amount_raw)
        except ValueError:
            amount = None

    # mapping QRIS berdasarkan paket
    qris_map = {
        "Paket Kecil": "img/qris_kecil.jpg",
        "Paket Rame": "img/qris_rame.jpg",
        "Paket Sultan": "img/qris_sultan.jpg",
    }
    qris_image = qris_map.get(package_name, "img/qris_kecil.jpg")

    tx = TopupTransaction(
        umkm_id=umkm.id,
        user_id=user.id,
        package_name=package_name,
        credits=credits,
        amount=amount,
        status="pending",
        qris_image=qris_image,
    )
    db.session.add(tx)
    db.session.commit()

    # ❌ TIDAK ada send_whatsapp_notification di sini
    return redirect(url_for("topup_confirm", tx_id=tx.id))


@app.route("/dashboard/topup/<int:tx_id>/confirm", methods=["GET", "POST"])
def topup_confirm(tx_id):
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    umkm = user.umkm
    if not umkm:
        flash("UMKM tidak ditemukan.", "danger")
        return redirect(url_for("dashboard_settings"))

    tx = TopupTransaction.query.filter_by(id=tx_id, umkm_id=umkm.id).first_or_404()

    if request.method == "POST":
        sender_name = request.form.get("sender_name", "").strip()
        note = request.form.get("note", "").strip()

        if sender_name:
            tx.sender_name = sender_name
        if note:
            tx.note = note

        # Upload bukti transfer (gambar)
        file = request.files.get("proof_image")
        if file and file.filename:
            if allowed_file(file.filename, ALLOWED_IMAGE_EXT):
                upload_dir = os.path.join(app.config["UPLOAD_FOLDER"], umkm.slug, "topup")
                os.makedirs(upload_dir, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d%H%M%S")
                filename = secure_filename(f"topup_{tx.id}_{ts}_{file.filename}")
                filepath = os.path.join(upload_dir, filename)
                file.save(filepath)
                rel_path = os.path.relpath(filepath, "static").replace("\\", "/")
                tx.proof_image = rel_path
            else:
                flash("Format bukti transfer tidak didukung. Gunakan gambar (jpg/png/jpeg/webp).", "danger")
                return redirect(url_for("topup_confirm", tx_id=tx.id))

        # Set status menunggu admin, JANGAN langsung tambah kredit di sini
        if tx.status not in ("waiting_admin", "success"):
            tx.status = "waiting_admin"

        db.session.commit()

        # Kirim WA ke admin (nomor fix)
        try:
            admin_number = "628562603077"
            admin_message = (
                f"[TOPUP KONFIRMASI]\n"
                f"UMKM: {umkm.name}\n"
                f"Paket: {tx.package_name}\n"
                f"Kredit: {tx.credits}\n"
            )
            if tx.amount:
                admin_message += f"Perkiraan Rp: {tx.amount}\n"
            if sender_name:
                admin_message += f"Pengirim: {sender_name}\n"
            if note:
                admin_message += f"Catatan: {note}\n"

            # Link admin untuk ACC (sederhana)
            admin_link = request.url_root.rstrip("/") + url_for("admin_topup_list")
            admin_message += f"\nID Transaksi: {tx.id}\nPanel Admin: {admin_link}"

            send_whatsapp_notification(admin_number, admin_message)
        except Exception as e:
            # jangan gagalkan flow kalau WA ke admin gagal
            print("Error send WA to admin:", e)

        flash("Terima kasih! Konfirmasi top-up terkirim dan menunggu ACC admin.", "success")
        return redirect(url_for("dashboard_settings"))

    return render_template("topup_confirm.html", umkm=umkm, tx=tx)


@app.route("/dashboard/settings/topup", methods=["POST"])
def topup_credit():
    """
    MVP: top up kredit manual dari form.
    (Di production nanti bisa diganti integrasi pembayaran.)
    """
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    umkm = user.umkm
    amount_raw = request.form.get("amount", "0")

    try:
        amount = int(amount_raw)
    except ValueError:
        flash("Jumlah top-up tidak valid.", "danger")
        return redirect(url_for("dashboard_settings"))

    if amount <= 0:
        flash("Jumlah top-up harus lebih dari 0.", "danger")
        return redirect(url_for("dashboard_settings"))

    umkm.credit_balance += amount
    db.session.add(CreditLog(
        umkm_id=umkm.id,
        change=amount,
        description=f"Top-up manual +{amount}"
    ))
    db.session.commit()

    flash(f"Top-up {amount} kredit berhasil.", "success")
    return redirect(url_for("dashboard_settings"))

@app.route("/admin/topup")
def admin_topup_list():
    # TODO: tambahkan proteksi admin beneran di production
    txs = TopupTransaction.query.order_by(TopupTransaction.created_at.desc()).all()
    return render_template("admin_topup.html", txs=txs)


@app.route("/admin/topup/<int:tx_id>/approve", methods=["POST"])
def admin_topup_approve(tx_id):
    # TODO: proteksi admin
    tx = TopupTransaction.query.get_or_404(tx_id)
    umkm = tx.umkm

    if tx.status == "success":
        flash("Transaksi ini sudah ditandai berhasil sebelumnya.", "info")
        return redirect(url_for("admin_topup_list"))

    # Tandai sebagai berhasil
    tx.status = "success"
    tx.confirmed_at = datetime.now()

    # Tambah kredit ke UMKM
    umkm.credit_balance += tx.credits

    # Catat di CreditLog
    log = CreditLog(
        umkm_id=umkm.id,
        change=tx.credits,
        description=f"Top-up paket {tx.package_name} (ACC admin)."
    )
    db.session.add(log)
    db.session.commit()

    # ✅ Kirim WA ke pemilik UMKM (kalau ada nomor)
    try:
        owner_number = getattr(umkm, "owner_whatsapp", None)
        if owner_number:
            message = (
                f"[TOPUP BERHASIL]\n"
                f"UMKM: {umkm.name}\n"
                f"Paket: {tx.package_name}\n"
                f"Kredit: {tx.credits}\n"
                f"Saldo kredit saat ini: {umkm.credit_balance}.\n\n"
                f"Terima kasih, top-up Anda sudah kami proses. 🙏"
            )
            send_whatsapp_notification(owner_number, message)
    except Exception as e:
        print("Error send WA to UMKM owner:", e)

    flash("Transaksi disetujui dan kredit berhasil ditambahkan.", "success")
    return redirect(url_for("admin_topup_list"))


# --------------------------------------------------
# ROUTES: STATS & SETTINGS
# --------------------------------------------------

@app.route("/dashboard/stats")
def dashboard_stats():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    umkm = user.umkm

    # query parameter day=YYYY-MM-DD (optional)
    day_str = request.args.get("day")
    if day_str:
        try:
            target_day = datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            target_day = date.today()
    else:
        target_day = date.today()

    stats = (
        db.session.query(
            db.func.date_trunc("hour", Queue.created_at).label("hour"),
            db.func.count(Queue.id)
        )
        .filter(
            Queue.umkm_id == umkm.id,
            db.func.date(Queue.created_at) == target_day
        )
        .group_by("hour")
        .order_by("hour")
        .all()
    )

    return render_template(
        "stats.html",
        stats=stats,
        umkm=umkm,
        target_day=target_day
    )

# @app.route("/dashboard/settings")
# def dashboard_settings():
#     user = get_current_user()
#     if not user:
#         return redirect(url_for("login"))

#     umkm = user.umkm
#     return render_template("settings.html", umkm=umkm)

@app.route("/dashboard/settings", methods=["GET", "POST"])
def dashboard_settings():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    umkm = user.umkm
    if not umkm:
        flash("UMKM tidak ditemukan.", "danger")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        # contoh: update nama & ticker + wa owner
        umkm.name = request.form.get("name", umkm.name)
        umkm.owner_whatsapp = request.form.get("owner_whatsapp", "").strip() or None 
        db.session.commit()
        flash("Pengaturan berhasil disimpan.", "success")
        return redirect(url_for("dashboard_settings"))

    return render_template("settings.html", umkm=umkm)



@app.route("/dashboard/settings/generate-qr", methods=["POST"])
def generate_qr_route():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    umkm = user.umkm
    try:
        generate_umkm_qr(umkm)
        flash("QR Code berhasil diperbarui!", "success")
    except Exception as e:
        flash(f"Gagal membuat QR: {e}", "danger")

    return redirect(url_for("dashboard_settings"))

@app.route("/dashboard/settings/display", methods=["POST"])
def dashboard_settings_display():
    """
    Simpan:
    - teks berjalan (display_ticker)
    - upload image/video untuk display (disimpan sebagai path relatif dipisah koma)
    """
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    umkm = user.umkm
    if not umkm:
        flash("UMKM tidak ditemukan.", "danger")
        return redirect(url_for("dashboard_settings"))

    # teks berjalan
    ticker_text = request.form.get("display_ticker", "").strip()
    umkm.display_ticker = ticker_text if ticker_text else None

    # folder khusus UMKM
    umkm_folder = os.path.join(app.config["UPLOAD_FOLDER"], umkm.slug)
    os.makedirs(umkm_folder, exist_ok=True)

    # existing list
    current_images = [p for p in (umkm.display_images or "").split(",") if p.strip()]
    current_videos = [p for p in (umkm.display_videos or "").split(",") if p.strip()]

    # upload images
    image_files = request.files.getlist("image_files")
    for f in image_files:
        if not f or not f.filename:
            continue
        if not allowed_file(f.filename, ALLOWED_IMAGE_EXT):
            continue
        filename = secure_filename(f.filename)
        # untuk menghindari nama bentrok
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        final_name = f"{timestamp}_{filename}"
        rel_path = f"upload/{umkm.slug}/{final_name}"
        abs_path = os.path.join("static", rel_path.replace("upload/", "upload/"))
        # benerin path abs:
        abs_path = os.path.join("static", "upload", umkm.slug, final_name)

        f.save(abs_path)
        current_images.append(rel_path)

    # upload videos
    video_files = request.files.getlist("video_files")
    for f in video_files:
        if not f or not f.filename:
            continue
        if not allowed_file(f.filename, ALLOWED_VIDEO_EXT):
            continue
        filename = secure_filename(f.filename)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        final_name = f"{timestamp}_{filename}"
        rel_path = f"upload/{umkm.slug}/{final_name}"
        abs_path = os.path.join("static", "upload", umkm.slug, final_name)

        f.save(abs_path)
        current_videos.append(rel_path)

    umkm.display_images = ",".join(current_images) if current_images else None
    umkm.display_videos = ",".join(current_videos) if current_videos else None

    db.session.commit()

    flash("Pengaturan display / kiosk berhasil disimpan.", "success")
    return redirect(url_for("dashboard_settings"))

@app.route("/dashboard/settings/display/delete", methods=["POST"])
def dashboard_settings_display_delete():
    """
    Hapus 1 media (image/video) dari pengaturan display:
    - hapus file fisik dari static/upload/...
    - hapus path dari kolom display_images / display_videos
    """
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    umkm = user.umkm
    if not umkm:
        flash("UMKM tidak ditemukan.", "danger")
        return redirect(url_for("dashboard_settings"))

    media_type = request.form.get("type")
    path = request.form.get("path", "").strip()  # contoh: upload/slug/filename.png

    if not path:
        flash("Path media tidak valid.", "danger")
        return redirect(url_for("dashboard_settings"))

    if media_type == "image":
        field_name = "display_images"
    elif media_type == "video":
        field_name = "display_videos"
    else:
        flash("Tipe media tidak valid.", "danger")
        return redirect(url_for("dashboard_settings"))

    current_list = [p.strip() for p in (getattr(umkm, field_name) or "").split(",") if p.strip()]

    if path not in current_list:
        flash("Media tidak ditemukan dalam pengaturan.", "danger")
        return redirect(url_for("dashboard_settings"))

    # hapus path dari list
    current_list.remove(path)
    setattr(umkm, field_name, ",".join(current_list) if current_list else None)

    # hapus file fisik
    abs_path = os.path.join("static", path)
    try:
        if os.path.exists(abs_path):
            os.remove(abs_path)
    except Exception:
        # kalau gagal hapus file, kita abaikan, yang penting DB konsisten
        pass

    db.session.commit()

    flash("Media display berhasil dihapus.", "success")
    return redirect(url_for("dashboard_settings"))


@app.route("/display/<slug_umkm>")
def display_view(slug_umkm):
    """
    Mode display (kiosk) untuk UMKM tertentu.
    Gunakan pengaturan display_ticker, display_images, display_videos.
    """
    umkm = UMKM.query.filter_by(slug=slug_umkm).first_or_404()
    today = date.today()

    current_called = (
        Queue.query
        .filter(
            Queue.umkm_id == umkm.id,
            Queue.status == "called",
            db.func.date(Queue.created_at) == today
        )
        .order_by(Queue.called_at.desc())
        .first()
    )

    waiting_list = (
        Queue.query
        .filter(
            Queue.umkm_id == umkm.id,
            Queue.status == "waiting",
            db.func.date(Queue.created_at) == today
        )
        .order_by(Queue.queue_number.asc())
        .all()
    )

    images = [p.strip() for p in (umkm.display_images or "").split(",") if p.strip()]
    videos = [p.strip() for p in (umkm.display_videos or "").split(",") if p.strip()]
    ticker = umkm.display_ticker or "Selamat datang di " + (umkm.name or "UMKM Anda")

    return render_template(
        "display.html",
        umkm=umkm,
        current_called=current_called,
        waiting=waiting_list,
        images=images,
        videos=videos,
        ticker=ticker
    )


# --------------------------------------------------
# MAIN
# --------------------------------------------------

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    socketio.run(app, debug=True)
