import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, date
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, g

# .env file loading. Falls back to manual parsing if python-dotenv is unavailable.
import pathlib
_env_path = pathlib.Path(__file__).parent / ".env"
if _env_path.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_path, override=True)
    except ImportError:
        # Manual parse when dotenv is unavailable.
        for _line in _env_path.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me')
DATABASE_URL = os.environ.get('DATABASE_URL', '')

if not DATABASE_URL and not os.environ.get('PG_HOST'):
    raise RuntimeError("DATABASE_URL is not set. Set it to the Supabase PostgreSQL connection string.")

import psycopg2
import psycopg2.extras
import psycopg2.pool


# -------------------------------------------------------
# DB abstraction: Supabase PostgreSQL
# -------------------------------------------------------
class _PgCursorWrapper:
    """psycopg2 cursor wrapper that converts ? placeholders to %s."""
    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, params=()):
        self._cur.execute(sql.replace("?", "%s"), params)
        return self

    def executemany(self, sql, params_list):
        self._cur.executemany(sql.replace("?", "%s"), params_list)

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class _ConnWrapper:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql.replace("?", "%s"), params)
        return cur

    def cursor(self):
        return _PgCursorWrapper(
            self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        )

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


class _PooledConnWrapper(_ConnWrapper):
    """Return pooled connections to the pool on close()."""
    def __init__(self, conn, pool):
        super().__init__(conn)
        self._pool = pool

    def close(self):
        try:
            self._conn.reset()
        except Exception:
            pass
        self._pool.putconn(self._conn)

# -----------------------------
# Minimal fixed admin seed user.
# -----------------------------
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "admin123"

# JST helper.
def now_jst():
    return datetime.utcnow() + timedelta(hours=9)

def monday_of_week(d: date) -> date:
    return d - timedelta(days=d.weekday())

def next_monday(d: date) -> date:
    return monday_of_week(d) + timedelta(days=7)

def week_dates(week_start: date):
    return [week_start + timedelta(days=i) for i in range(7)]

def deadline_for_target_week(week_start: date) -> datetime:
    # Friday 23:59 immediately before the target week.
    friday = week_start - timedelta(days=3)
    return datetime(friday.year, friday.month, friday.day, 23, 59, 0)

def fmt_date(d: date):
    return d.strftime("%Y-%m-%d")

# -----------------------------
# DB
# -----------------------------
_pool = None

def _get_pool():
    global _pool
    if _pool is None:
        if DATABASE_URL:
            sslmode = os.environ.get('DB_SSLMODE')
            dsn = DATABASE_URL
            if sslmode and 'sslmode=' not in dsn:
                dsn += ('&' if '?' in dsn else '?') + f'sslmode={sslmode}'
            kwargs = dict(dsn=dsn)
        else:
            pg_host = os.environ.get('PG_HOST')
            kwargs = dict(
                host=pg_host,
                port=int(os.environ.get('PG_PORT', 5432)),
                dbname=os.environ.get('PG_DB', 'postgres'),
                user=os.environ.get('PG_USER', 'postgres'),
                password=os.environ.get('PG_PASSWORD', ''),
                sslmode=os.environ.get('DB_SSLMODE', 'require'),
                connect_timeout=10,
            )
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, **kwargs)
    return _pool

def db():
    pool = _get_pool()
    conn = pool.getconn()
    conn.autocommit = False
    return _PooledConnWrapper(conn, pool)

def _serial():
    return "SERIAL PRIMARY KEY"


def _send_smtp(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    """Send mail by SMTP. Return (False, reason) when SMTP is not configured."""
    host = os.environ.get("SMTP_HOST", "")
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    port = int(os.environ.get("SMTP_PORT", 587))
    from_addr = os.environ.get("SMTP_FROM", user)

    if not host or not user or not password:
        return False, "SMTP未設定"

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_email
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP(host, port, timeout=10) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(user, password)
            srv.sendmail(from_addr, [to_email], msg.as_string())
        return True, ""
    except Exception as e:
        return False, str(e)

def init_db():
    conn = db()
    cur = conn.cursor()
    PK = _serial()

    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS users (
      id {PK},
      name TEXT NOT NULL,
      email TEXT UNIQUE NOT NULL,
      password TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'staff',
      is_active INTEGER NOT NULL DEFAULT 1
    )
    """)

    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS time_ranges (
      id {PK},
      label TEXT NOT NULL,
      "start" TEXT NOT NULL,
      "end" TEXT NOT NULL,
      sort_order INTEGER NOT NULL DEFAULT 100,
      is_active INTEGER NOT NULL DEFAULT 1
    )
    """)

    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS submissions (
      id {PK},
      user_id INTEGER NOT NULL,
      week_start TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'draft',
      updated_at TEXT NOT NULL,
      UNIQUE(user_id, week_start)
    )
    """)

    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS slots (
      id {PK},
      submission_id INTEGER NOT NULL,
      day TEXT NOT NULL,
      slot_index INTEGER NOT NULL,
      time_range_id INTEGER,
      note TEXT,
      UNIQUE(submission_id, day, slot_index)
    )
    """)

    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS schedules (
      id {PK},
      title TEXT NOT NULL,
      date TEXT NOT NULL,
      time TEXT NOT NULL,
      user_id INTEGER NOT NULL,
      company_name TEXT,
      phone_number TEXT,
      contact_person TEXT,
      memo TEXT,
      created_at TEXT NOT NULL,
      project_id INTEGER,
      schedule_type TEXT
    )
    """)

    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS tasks (
      id {PK},
      title TEXT NOT NULL,
      user_id INTEGER,
      date TEXT,
      deadline TEXT,
      memo TEXT,
      is_completed INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL
    )
    """)
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS teams (
      id {PK},
      name TEXT NOT NULL,
      created_at TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS team_members (
      team_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      is_leader INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (team_id, user_id),
      FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE,
      FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    """)
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS projects (
      id {PK},
      name TEXT NOT NULL,
      email TEXT,
      description TEXT,
      mail_text TEXT,
      created_by INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      FOREIGN KEY(created_by) REFERENCES users(id)
    )
    """)
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS project_members (
      id {PK},
      project_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
      UNIQUE(project_id, user_id)
    )
    """)
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS mail_logs (
      id {PK},
      project_id INTEGER,
      user_id INTEGER NOT NULL,
      to_email TEXT NOT NULL,
      subject TEXT NOT NULL,
      body TEXT,
      sent_at TEXT NOT NULL,
      is_sent INTEGER NOT NULL DEFAULT 0,
      error_msg TEXT,
      FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL,
      FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)
    conn.commit()

    # Minimal admin seed.
    cur.execute("SELECT id FROM users WHERE email=?", (ADMIN_EMAIL,))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users(name,email,password,role,is_active) VALUES(?,?,?,?,1)",
            ("Admin", ADMIN_EMAIL, ADMIN_PASSWORD, "admin"),
        )
        conn.commit()

    # Minimal time range seed.
    cur.execute("SELECT COUNT(*) AS c FROM time_ranges")
    if cur.fetchone()["c"] == 0:
        seed = [
            ("朝", "09:00", "12:00", 10, 1),
            ("昼", "12:00", "15:00", 20, 1),
            ("夕", "15:00", "18:00", 30, 1),
        ]
        cur.executemany(
            'INSERT INTO time_ranges(label,"start","end",sort_order,is_active) VALUES(?,?,?,?,?)',
            seed
        )
        conn.commit()

    conn.close()

init_db()

# -----------------------------
# Auth (session)
# -----------------------------
def current_user():
    """Cache the current user in g for the duration of the request."""
    if "current_user" in g:
        return g.current_user
    uid = session.get("uid")
    if not uid:
        g.current_user = None
        return None
    conn = db()
    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    g.current_user = u
    return u

def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

def admin_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u:
            return redirect(url_for("login"))
        if u["role"] != "admin":
            abort(403)
        return fn(*args, **kwargs)
    return wrapper

# -----------------------------
# Target week
# -----------------------------
def target_week_start() -> date:
    return next_monday(now_jst().date())

def is_locked(week_start: date) -> bool:
    return now_jst() > deadline_for_target_week(week_start)

# -----------------------------
# Helpers
# -----------------------------
def hhmm_to_tuple(s: str):
    hh, mm = s.split(":")
    return int(hh), int(mm)

def validate_time_range(start: str, end: str):
    try:
        st = hhmm_to_tuple(start)
        et = hhmm_to_tuple(end)
    except Exception:
        return False, "時刻はHH:MMで"
    if st >= et:
        return False, "開始 < 終了 にして"
    return True, ""

# -----------------------------
# Routes
# -----------------------------
@app.get("/")
def root():
    return redirect(url_for("dashboard")) if current_user() else redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        conn = db()
        u = conn.execute(
            "SELECT * FROM users WHERE email=? AND is_active=1",
            (email,),
        ).fetchone()
        conn.close()
        if not u or u["password"] != password:
            flash("ログイン失敗（メール/パスワード）")
            return render_template("login.html")
        session["uid"] = u["id"]
        return redirect(url_for("dashboard"))
    return render_template("login.html")

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.get("/dashboard")
@login_required
def dashboard():
    u = current_user()
    ws = target_week_start()
    dates = week_dates(ws)
    locked = False

    conn = db()
    sub = conn.execute(
        "SELECT * FROM submissions WHERE user_id=? AND week_start=?",
        (u["id"], fmt_date(ws)),
    ).fetchone()
    
    # 1. Projects the current user belongs to.
    my_projects = conn.execute("""
        SELECT p.* FROM projects p
        JOIN project_members pm ON p.id = pm.project_id
        WHERE pm.user_id = ?
        ORDER BY p.created_at DESC
    """, (u["id"],)).fetchall()

    # 2. Incomplete tasks for the current user.
    my_tasks = conn.execute("""
        SELECT * FROM tasks 
        WHERE user_id = ? AND is_completed = 0 
        ORDER BY deadline ASC, id ASC
    """, (u["id"],)).fetchall()

    # 3. Teammates.
    my_teammates = conn.execute("""
        SELECT u.id, u.name, u.email, tm.is_leader 
        FROM users u 
        JOIN team_members tm ON u.id = tm.user_id 
        WHERE tm.team_id = (SELECT team_id FROM team_members WHERE user_id = ?) 
        AND u.id != ?
        ORDER BY tm.is_leader DESC, u.name ASC
    """, (u["id"], u["id"])).fetchall()
    
    my_team = conn.execute("""
        SELECT t.name FROM teams t
        JOIN team_members tm ON t.id = tm.team_id
        WHERE tm.user_id = ?
    """, (u["id"],)).fetchone()
    my_team_name = my_team["name"] if my_team else "未所属"

    # 4. Upcoming schedules from today onward.
    today_str = now_jst().strftime('%Y-%m-%d')
    my_schedules = conn.execute("""
        SELECT s.*, p.name as project_name 
        FROM schedules s
        LEFT JOIN projects p ON s.project_id = p.id
        WHERE s.user_id = ? AND s.date >= ?
        ORDER BY s.date ASC, s.time ASC
    """, (u["id"], today_str)).fetchall()

    conn.close()

    if not sub:
        status = "未作成"
    else:
        status = "提出済み" if sub["status"] == "submitted" else "下書き"

    return render_template(
        "dashboard.html",
        user=u,
        week_start=dates[0],
        week_end=dates[-1],
        locked=locked,
        my_status=status,
        my_projects=my_projects,
        my_tasks=my_tasks,
        my_teammates=my_teammates,
        my_schedules=my_schedules,
        my_team_name=my_team_name
    )

# -------- Admin: Users --------
@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    conn = db()

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        role = request.form.get("role") or "staff"
        if role not in ("staff", "admin"):
            role = "staff"

        if not name or not email or not password:
            flash("name/email/password は必須")
        else:
            try:
                conn.execute(
                    "INSERT INTO users(name,email,password,role,is_active) VALUES(?,?,?,?,1)",
                    (name, email, password, role),
                )
                conn.commit()
                flash("ユーザー作成OK")
            except psycopg2.IntegrityError:
                conn.rollback()
                flash("そのemailは既に存在")

    users = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    conn.close()
    return render_template("admin_users.html", user=current_user(), users=users)

@app.post("/admin/users/role")
@admin_required
def admin_users_role():
    uid = request.form.get("user_id")
    new_role = request.form.get("role")
    if not uid or new_role not in ("staff", "admin"):
        return redirect(url_for("admin_users"))
    conn = db()
    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if u and u["email"] != ADMIN_EMAIL:
        conn.execute("UPDATE users SET role=? WHERE id=?", (new_role, uid))
        conn.commit()
        flash(f"{u['name']} の権限を {new_role} に変更しました")
    conn.close()
    return redirect(url_for("admin_users"))

@app.post("/admin/users/toggle")
@admin_required
def admin_users_toggle():
    uid = request.form.get("user_id")
    if not uid:
        return redirect(url_for("admin_users"))
    conn = db()
    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if u and u["email"] != ADMIN_EMAIL:
        new_active = 0 if u["is_active"] == 1 else 1
        conn.execute("UPDATE users SET is_active=? WHERE id=?", (new_active, uid))
        conn.commit()
    conn.close()
    return redirect(url_for("admin_users"))

# -------- Admin: Time ranges --------
@app.route("/admin/timeranges", methods=["GET", "POST"])
@admin_required
def admin_timeranges():
    conn = db()
    if request.method == "POST":
        label = (request.form.get("label") or "").strip() or "枠"
        start = (request.form.get("start") or "").strip()
        end = (request.form.get("end") or "").strip()
        sort_order = request.form.get("sort_order") or "100"
        try:
            sort_order = int(sort_order)
        except Exception:
            sort_order = 100

        ok, msg = validate_time_range(start, end)
        if not ok:
            flash(msg)
        else:
            conn.execute(
                'INSERT INTO time_ranges(label,"start","end",sort_order,is_active) VALUES(?,?,?,?,1)',
                (label, start, end, sort_order),
            )
            conn.commit()
            flash("時間帯を追加した")

    ranges = conn.execute(
        'SELECT * FROM time_ranges ORDER BY sort_order, "start", id'
    ).fetchall()
    conn.close()
    return render_template("admin_timeranges.html", user=current_user(), ranges=ranges)

@app.post("/admin/timeranges/update")
@admin_required
def admin_timeranges_update():
    rid = request.form.get("id")
    label = (request.form.get("label") or "").strip() or "枠"
    start = (request.form.get("start") or "").strip()
    end = (request.form.get("end") or "").strip()
    sort_order = request.form.get("sort_order") or "100"
    is_active = 1 if request.form.get("is_active") == "1" else 0
    try:
        sort_order = int(sort_order)
    except Exception:
        sort_order = 100

    ok, msg = validate_time_range(start, end)
    if not ok:
        flash(msg)
        return redirect(url_for("admin_timeranges"))

    conn = db()
    conn.execute(
        'UPDATE time_ranges SET label=?, "start"=?, "end"=?, sort_order=?, is_active=? WHERE id=?',
        (label, start, end, sort_order, is_active, rid),
    )
    conn.commit()
    conn.close()
    flash("更新した")
    return redirect(url_for("admin_timeranges"))

@app.post("/admin/timeranges/delete")
@admin_required
def admin_timeranges_delete():
    rid = request.form.get("id")
    conn = db()
    # Use logical deletion so existing references remain valid.
    conn.execute("UPDATE time_ranges SET is_active=0 WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    flash("無効化しました")
    return redirect(url_for("admin_timeranges"))

# -------- Shifts --------
@app.route("/shift/submit", methods=["GET", "POST"])
@login_required
def shift_submit():
    u = current_user()
    # Allow editing any Monday-starting week. Default remains next week.
    qs = (request.args.get("week_start") or "").strip()
    if qs:
        try:
            ws = monday_of_week(datetime.strptime(qs, "%Y-%m-%d").date())
        except Exception:
            ws = target_week_start()
    else:
        ws = target_week_start()
    dates = week_dates(ws)
    prev_ws = ws - timedelta(days=7)
    next_ws = ws + timedelta(days=7)
    locked = False

    conn = db()

    # time ranges
    ranges = conn.execute(
        'SELECT * FROM time_ranges WHERE is_active=1 ORDER BY sort_order, "start", id'
    ).fetchall()
    valid_range_ids = {str(r["id"]) for r in ranges}

    # submission get-or-create
    sub = conn.execute(
        "SELECT * FROM submissions WHERE user_id=? AND week_start=?",
        (u["id"], fmt_date(ws)),
    ).fetchone()

    if not sub:
        conn.execute(
            "INSERT INTO submissions(user_id, week_start, status, updated_at) VALUES(?,?,?,?)",
            (u["id"], fmt_date(ws), "draft", now_jst().isoformat(timespec="seconds")),
        )
        conn.commit()
        sub = conn.execute(
            "SELECT * FROM submissions WHERE user_id=? AND week_start=?",
            (u["id"], fmt_date(ws)),
        ).fetchone()

    # existing slots
    existing = conn.execute(
        "SELECT * FROM slots WHERE submission_id=?",
        (sub["id"],),
    ).fetchall()
    selected_map = {fmt_date(d): set() for d in dates}
    for r in existing:
        if r["time_range_id"]:
            selected_map.setdefault(r["day"], set()).add(str(r["time_range_id"]))

    if request.method == "POST":
        # validate and upsert (multiple selections per日仁E
        for d in dates:
            day = fmt_date(d)
            tids = request.form.getlist(f"{day}__tid")

            filtered = []
            seen = set()
            for tid in tids:
                if tid not in valid_range_ids:
                    flash("不正な時間帯が選択されました")
                    conn.rollback()
                    conn.close()
                    return redirect(url_for("shift_submit", week_start=fmt_date(ws)))
                if tid in seen:
                    continue
                filtered.append(tid)
                seen.add(tid)

            conn.execute(
                "DELETE FROM slots WHERE submission_id=? AND day=?",
                (sub["id"], day),
            )

            for idx, tid in enumerate(filtered, start=1):
                conn.execute(
                    "INSERT INTO slots(submission_id, day, slot_index, time_range_id, note) VALUES(?,?,?,?,NULL)",
                    (sub["id"], day, idx, int(tid)),
                )

        status = "submitted" if "submit_final" in request.form else "draft"
        conn.execute(
            "UPDATE submissions SET status=?, updated_at=? WHERE id=?",
            (status, now_jst().isoformat(timespec="seconds"), sub["id"]),
        )
        conn.commit()
        conn.close()
        flash("提出確定！" if status == "submitted" else "下書き保存！")
        return redirect(url_for("shift_team", week_start=fmt_date(ws)))

    conn.close()

    return render_template(
        "shift_submit.html",
        user=u,
        week_dates=dates,
        week_start=dates[0],
        week_end=dates[-1],
        prev_week_start=prev_ws,
        next_week_start=next_ws,
        locked=locked,
        ranges=ranges,
        selected_map=selected_map,
    )

@app.get("/shift/team")
@login_required
def shift_team():
    u_current = current_user()
    team_only = request.args.get("team_only") == "1"
    
    # Default to the same target week used by shift submission.
    # week_start selects the Monday-starting week.
    qs = (request.args.get("week_start") or "").strip()
    if qs:
        try:
            ws = datetime.strptime(qs, "%Y-%m-%d").date()
            ws = monday_of_week(ws)
        except Exception:
            ws = target_week_start()
    else:
        ws = target_week_start()

    dates = week_dates(ws)
    prev_ws = ws - timedelta(days=7)
    next_ws = ws + timedelta(days=7)

    conn = db()
    
    if team_only:
        # Find the current user's team.
        my_team = conn.execute("SELECT team_id FROM team_members WHERE user_id=?", (u_current["id"],)).fetchone()
        if my_team:
            # Restrict to users in the same team.
            users = conn.execute("""
                SELECT u.* FROM users u
                JOIN team_members tm ON u.id = tm.user_id
                WHERE u.is_active=1 AND tm.team_id=?
                ORDER BY u.id
            """, (my_team["team_id"],)).fetchall()
        else:
            # If the user has no team, show only the current user.
            users = conn.execute("SELECT * FROM users WHERE id=? AND is_active=1", (u_current["id"],)).fetchall()
    else:
        users = conn.execute("SELECT * FROM users WHERE is_active=1 ORDER BY id").fetchall()
    subs = conn.execute("SELECT * FROM submissions WHERE week_start=?", (fmt_date(ws),)).fetchall()
    sub_by_uid = {s["user_id"]: s for s in subs}

    # time range map (include inactive for display safety)
    ranges = conn.execute("SELECT * FROM time_ranges").fetchall()
    range=[f'{r["start"]}-{r["end"]}' for r in ranges]
    range_map = {r["id"]: f'{r["start"]}-{r["end"]}' for r in ranges}
    
    # Fetch all slots in one query.
    sub_ids = [s["id"] for s in subs]
    all_slots = []
    if sub_ids:
        placeholders = ",".join(["?"] * len(sub_ids))
        all_slots = conn.execute(
            f"SELECT * FROM slots WHERE submission_id IN ({placeholders}) ORDER BY day, slot_index",
            sub_ids
        ).fetchall()

    # submission_id ↁE{(day, time_range_id): True} のマップを構篁E
    slot_set = {}
    slot_by_sub = {}
    for sl in all_slots:
        sid = sl["submission_id"]
        slot_set.setdefault(sid, set()).add((sl["day"], sl["time_range_id"]))
        slot_by_sub.setdefault(sid, []).append(sl)

    # Build users_data in Python without additional queries.
    users_data = {}
    user_shift_counts = {}
    for u in users:
        sub = sub_by_uid.get(u["id"])
        sub_id = sub["id"] if sub else None
        filled = slot_set.get(sub_id, set())
        user_date = []
        for date in dates:
            day_str = date.strftime("%Y-%m-%d")
            for r in ranges:
                user_date.append((day_str, r["id"]) in filled)
        users_data[u["name"]] = user_date
        user_shift_counts[u["name"]] = len(filled)

    rows = []
    for u in users:
        sub = sub_by_uid.get(u["id"])
        status = "未作成" if not sub else ("提出済み" if sub["status"] == "submitted" else "下書き")
        by_date = {fmt_date(d): [] for d in dates}
        if sub:
            for sl in slot_by_sub.get(sub["id"], []):
                if sl["time_range_id"]:
                    label = range_map.get(sl["time_range_id"], f"ID:{sl['time_range_id']}")
                    extra = f"（{sl['note']}）" if sl["note"] else ""
                    by_date[sl["day"]].append(label + extra)
        rows.append({"name": u["name"], "status": status, "by_date": by_date})

    conn.close()
    return render_template(
        "shift_team.html",
        week_dates=dates,
        week_start=dates[0],
        week_end=dates[-1],
        prev_week_start=prev_ws,
        next_week_start=next_ws,
        rows=rows,
        range=range,
        users=users_data,
        user_shift_counts=user_shift_counts,
        user=u_current,
        team_only=team_only,
    )


@app.route("/schedules", methods=["GET"])
@login_required
def schedules():
    conn = db()
    # Upcoming schedules ordered by date and time.
    schedules_data = conn.execute("""
        SELECT s.*, u.name as user_name, p.name as project_name
        FROM schedules s
        LEFT JOIN users u ON s.user_id = u.id
        LEFT JOIN projects p ON s.project_id = p.id
        ORDER BY s.date ASC, s.time ASC
    """).fetchall()

    # Recently added schedules ordered by creation time.
    recent_schedules_data = conn.execute("""
        SELECT s.*, u.name as user_name, p.name as project_name
        FROM schedules s 
        LEFT JOIN users u ON s.user_id = u.id
        LEFT JOIN projects p ON s.project_id = p.id
        ORDER BY s.created_at DESC
        LIMIT 5
    """).fetchall()

    conn.close()
    return render_template("schedules.html", user=current_user(), schedules=schedules_data, recent=recent_schedules_data)


@app.route("/schedules/add", methods=["GET", "POST"])
@login_required
def schedule_add():
    conn = db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        date_str = request.form.get("date", "").strip()
        time_str = request.form.get("time", "").strip()
        user_id = request.form.get("user_id")
        company_name = request.form.get("company_name", "").strip()
        phone_number = request.form.get("phone_number", "").strip()
        contact_person = request.form.get("contact_person", "").strip()
        memo = request.form.get("memo", "").strip()
        project_id = request.form.get("project_id")
        schedule_type = request.form.get("schedule_type", "").strip()
        created_at = now_jst().strftime("%Y-%m-%d %H:%M:%S")

        conn.execute("""
            INSERT INTO schedules (
                title, date, time, user_id, company_name, phone_number, contact_person, memo, created_at, project_id, schedule_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (title, date_str, time_str, user_id, company_name, phone_number, contact_person, memo, created_at, project_id if project_id else None, schedule_type))
        conn.commit()
        conn.close()
        flash("予定を追加しました。")
        return redirect(url_for("schedules"))

    users = conn.execute("SELECT id, name FROM users WHERE is_active=1 ORDER BY name").fetchall()
    projects = conn.execute("SELECT id, name FROM projects ORDER BY name").fetchall()
    conn.close()
    return render_template("schedule_add.html", user=current_user(), users=users, projects=projects)


@app.route("/tasks", methods=["GET", "POST"])
@login_required
def tasks():
    conn = db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        user_id = request.form.get("user_id") or None
        date_str = request.form.get("date", "").strip()
        deadline = request.form.get("deadline", "").strip()
        memo = request.form.get("memo", "").strip()
        
        if title:
            created_at = now_jst().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""
                INSERT INTO tasks (title, user_id, date, deadline, memo, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (title, user_id, date_str, deadline, memo, created_at))
            conn.commit()
            return redirect(url_for("tasks"))

    tasks_data = conn.execute("""
        SELECT t.*, u.name as user_name 
        FROM tasks t 
        LEFT JOIN users u ON t.user_id = u.id 
        ORDER BY t.is_completed ASC, t.id ASC
    """).fetchall()
    
    users = conn.execute("SELECT id, name FROM users WHERE is_active=1 ORDER BY name").fetchall()
    conn.close()
    
    return render_template("tasks.html", user=current_user(), tasks=tasks_data, users=users)

@app.route("/tasks/<int:id>/update", methods=["POST"])
@login_required
def task_update(id):
    conn = db()
    title = request.form.get("title", "").strip()
    user_id = request.form.get("user_id") or None
    date_str = request.form.get("date", "").strip()
    deadline = request.form.get("deadline", "").strip()
    memo = request.form.get("memo", "").strip()
    
    conn.execute("""
        UPDATE tasks 
        SET title=?, user_id=?, date=?, deadline=?, memo=? 
        WHERE id=?
    """, (title, user_id, date_str, deadline, memo, id))
    conn.commit()
    conn.close()
    flash("タスクを更新しました。")
    return redirect(url_for("tasks"))

@app.route("/tasks/<int:id>/toggle", methods=["POST"])
@login_required
def task_toggle(id):
    conn = db()
    t = conn.execute("SELECT is_completed FROM tasks WHERE id=?", (id,)).fetchone()
    if t:
        new_status = 0 if t["is_completed"] else 1
        conn.execute("UPDATE tasks SET is_completed=? WHERE id=?", (new_status, id))
        conn.commit()
    conn.close()
    return redirect(url_for("tasks"))

@app.route("/team", methods=["GET"])
@login_required
def team():
    conn = db()
    query = """
    SELECT u.id, u.name, u.role, u.email,
           t.name as team_name, tm.is_leader,
           (SELECT COUNT(*) FROM tasks WHERE user_id = u.id AND is_completed = 0) as task_count,
           (SELECT COUNT(*) FROM team_members WHERE team_id = t.id) as team_member_count
    FROM users u
    LEFT JOIN team_members tm ON u.id = tm.user_id
    LEFT JOIN teams t ON tm.team_id = t.id
    WHERE u.is_active = 1
    ORDER BY u.role ASC, u.id ASC
    """
    users_data = conn.execute(query).fetchall()
    conn.close()
    return render_template("team.html", user=current_user(), team_members=users_data)

@app.route("/admin/teams", methods=["GET", "POST"])
@login_required
def admin_teams():
    u = current_user()
    if u["role"] != "admin":
        abort(403)
        
    conn = db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            created_at = now_jst().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT INTO teams (name, created_at) VALUES (?, ?)", (name, created_at))
            conn.commit()
            flash("チームを作成しました。")
        return redirect(url_for("admin_teams"))
        
    teams = conn.execute("""
        SELECT t.id, t.name, t.created_at, 
               (SELECT COUNT(*) FROM team_members WHERE team_id = t.id) as member_count
        FROM teams t
        ORDER BY t.id ASC
    """).fetchall()
    conn.close()
    return render_template("admin_teams.html", user=u, teams=teams)

@app.route("/admin/teams/<int:team_id>", methods=["GET", "POST"])
@login_required
def admin_team_edit(team_id):
    u = current_user()
    if u["role"] != "admin":
        abort(403)
        
    conn = db()
    team = conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
    if not team:
        abort(404)
        
    if request.method == "POST":
        action = request.form.get("action")
        if action == "update_name":
            name = request.form.get("name", "").strip()
            if name:
                conn.execute("UPDATE teams SET name=? WHERE id=?", (name, team_id))
                conn.commit()
            flash("チーム名を更新しました。")
        elif action == "add_member":
            user_ids = request.form.getlist("user_ids")
            if user_ids:
                for uid in user_ids:
                    # A user can belong to only one team.
                    conn.execute("DELETE FROM team_members WHERE user_id=?", (uid,))
                    conn.execute("INSERT INTO team_members (team_id, user_id, is_leader) VALUES (?, ?, 0)", (team_id, uid))
                conn.commit()
                if len(user_ids) > 1:
                    flash(f"{len(user_ids)}人のメンバーを追加しました。")
                else:
                    flash("メンバーを追加しました。")
        elif action == "remove_member":
            user_id = request.form.get("user_id")
            if user_id:
                conn.execute("DELETE FROM team_members WHERE team_id=? AND user_id=?", (team_id, user_id))
                conn.commit()
            flash("メンバーを削除しました。")
        elif action == "set_leader":
            user_id = request.form.get("user_id")
            if user_id:
                conn.execute("UPDATE team_members SET is_leader=0 WHERE team_id=?", (team_id,))
                conn.execute("UPDATE team_members SET is_leader=1 WHERE team_id=? AND user_id=?", (team_id, user_id))
                conn.commit()
            flash("リーダーを設定しました。")
        elif action == "delete_team":
            conn.execute("DELETE FROM teams WHERE id=?", (team_id,))
            conn.commit()
            flash("チームを削除しました。")
            return redirect(url_for("admin_teams"))
            
        return redirect(url_for("admin_team_edit", team_id=team_id))
        
    members = conn.execute("""
        SELECT u.id, u.name, tm.is_leader 
        FROM users u 
        JOIN team_members tm ON u.id = tm.user_id 
        WHERE tm.team_id=?
        ORDER BY tm.is_leader DESC, u.name ASC
    """, (team_id,)).fetchall()
    
    # Exclude users already assigned to any team.
    available_users = conn.execute("""
        SELECT id, name FROM users 
        WHERE is_active=1 AND id NOT IN (SELECT user_id FROM team_members)
        ORDER BY name
    """).fetchall()
        
    conn.close()
    return render_template("admin_team_edit.html", user=u, team=team, members=members, available_users=available_users)


# -------- Projects --------
@app.route("/projects", methods=["GET"])
@login_required
def projects():
    conn = db()
    projects_data = conn.execute("""
        SELECT p.*, u.name as creator_name 
        FROM projects p 
        LEFT JOIN users u ON p.created_by = u.id 
        ORDER BY p.id DESC
    """).fetchall()
    conn.close()
    return render_template("projects.html", user=current_user(), projects=projects_data)

@app.route("/projects/add", methods=["GET", "POST"])
@login_required
def project_add():
    u = current_user()
    if u["role"] != "admin":
        abort(403)
        
    conn = db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        description = request.form.get("description", "").strip()
        user_ids = request.form.getlist("user_ids")
        
        if name:
            created_at = now_jst().strftime("%Y-%m-%d %H:%M:%S")
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO projects (name, email, description, created_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                RETURNING id
            """, (name, email, description, u["id"], created_at))
            project_id = cur.fetchone()["id"]
            
            for uid in user_ids:
                conn.execute("INSERT INTO project_members (project_id, user_id) VALUES (?, ?)", (project_id, uid))
            
            conn.commit()
            flash("案件を作成しました。")
            return redirect(url_for("projects"))
            
    users = conn.execute("SELECT id, name FROM users WHERE is_active=1 ORDER BY name").fetchall()
    conn.close()
    return render_template("project_add.html", user=u, users=users)

@app.route("/projects/<int:id>", methods=["GET", "POST"])
@login_required
def project_detail(id):
    u = current_user()
    conn = db()
    
    if request.method == "POST":
        # AdminのみがPOST可能
        if u["role"] != "admin":
            abort(403)
            
        action = request.form.get("action")
        if action == "update":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip()
            description = request.form.get("description", "").strip()
            mail_text = request.form.get("mail_text", "").strip()
            if name:
                conn.execute("UPDATE projects SET name=?, email=?, description=?, mail_text=? WHERE id=?", (name, email, description, mail_text, id))
                conn.commit()
                flash("案件を更新しました。")
        elif action == "delete":
            conn.execute("DELETE FROM projects WHERE id=?", (id,))
            conn.commit()
            flash("案件を削除しました。")
            return redirect(url_for("projects"))
        elif action == "add_member":
            user_id = request.form.get("user_id")
            if user_id:
                try:
                    conn.execute("INSERT INTO project_members (project_id, user_id) VALUES (?, ?)", (id, user_id))
                    conn.commit()
                    flash("メンバーを追加しました。")
                except psycopg2.IntegrityError:
                    conn.rollback()
                    flash("すでに参加しているメンバーです。")
        elif action == "remove_member":
            user_id = request.form.get("user_id")
            if user_id:
                conn.execute("DELETE FROM project_members WHERE project_id=? AND user_id=?", (id, user_id))
                conn.commit()
                flash("メンバーを削除しました。")
        return redirect(url_for("project_detail", id=id))
        
    project = conn.execute("""
        SELECT p.*, u.name as creator_name 
        FROM projects p 
        LEFT JOIN users u ON p.created_by = u.id 
        WHERE p.id=?
    """, (id,)).fetchone()
    
    if not project:
        abort(404)
        
    members = conn.execute("""
        SELECT u.id, u.name, u.email 
        FROM users u 
        JOIN project_members pm ON u.id = pm.user_id 
        WHERE pm.project_id=?
        ORDER BY u.name ASC
    """, (id,)).fetchall()
    
    # Users available to add as members.
    member_ids = [m["id"] for m in members]
    if member_ids:
        placeholders = ",".join("?" * len(member_ids))
        available_users = conn.execute(f"SELECT id, name FROM users WHERE is_active=1 AND id NOT IN ({placeholders}) ORDER BY name", tuple(member_ids)).fetchall()
    else:
        available_users = conn.execute("SELECT id, name FROM users WHERE is_active=1 ORDER BY name").fetchall()
    
    conn.close()
    return render_template("project_detail.html", user=u, project=project, members=members, available_users=available_users)


@app.errorhandler(403)
def forbidden(e):
    return ("Forbidden", 403)

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
