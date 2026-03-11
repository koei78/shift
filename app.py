import os
from datetime import datetime, timedelta, date
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me')
DB_PATH = os.environ.get('DB_PATH', 'app.db')

# -----------------------------
# 最小ユーザー（固定で1人だけ seed）
# -----------------------------
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "admin123"

# JST（ナイーブで扱う簡略版）
def now_jst():
    return datetime.utcnow() + timedelta(hours=9)

def monday_of_week(d: date) -> date:
    return d - timedelta(days=d.weekday())

def next_monday(d: date) -> date:
    return monday_of_week(d) + timedelta(days=7)

def week_dates(week_start: date):
    return [week_start + timedelta(days=i) for i in range(7)]

def deadline_for_target_week(week_start: date) -> datetime:
    # 次週の月曜(week_start)の「直前の金曜 23:59」
    friday = week_start - timedelta(days=3)
    return datetime(friday.year, friday.month, friday.day, 23, 59, 0)

def fmt_date(d: date):
    return d.strftime("%Y-%m-%d")

# -----------------------------
# DB
# -----------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      email TEXT UNIQUE NOT NULL,
      password TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'staff', -- staff/admin
      is_active INTEGER NOT NULL DEFAULT 1
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS time_ranges (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      label TEXT NOT NULL,            -- 例: 午前, 夕方など（任意）
      start TEXT NOT NULL,            -- HH:MM
      end TEXT NOT NULL,              -- HH:MM
      sort_order INTEGER NOT NULL DEFAULT 100,
      is_active INTEGER NOT NULL DEFAULT 1
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS submissions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      week_start TEXT NOT NULL,   -- YYYY-MM-DD (target week monday)
      status TEXT NOT NULL DEFAULT 'draft',  -- draft/submitted
      updated_at TEXT NOT NULL,
      UNIQUE(user_id, week_start)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS slots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      submission_id INTEGER NOT NULL,
      day TEXT NOT NULL,           -- YYYY-MM-DD
      slot_index INTEGER NOT NULL, -- 1..3
      time_range_id INTEGER,       -- time_ranges.id
      note TEXT,
      UNIQUE(submission_id, day, slot_index)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS schedules (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT NOT NULL,
      user_id INTEGER,
      date TEXT,
      deadline TEXT,
      memo TEXT,
      is_completed INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS teams (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    cur.execute("""
    CREATE TABLE IF NOT EXISTS projects (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      email TEXT,
      description TEXT,
      mail_text TEXT,
      created_by INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      FOREIGN KEY(created_by) REFERENCES users(id)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS project_members (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      project_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
      UNIQUE(project_id, user_id)
    )
    """)
    conn.commit()

    # admin seed（最低限）
    cur.execute("SELECT id FROM users WHERE email=?", (ADMIN_EMAIL,))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users(name,email,password,role,is_active) VALUES(?,?,?,?,1)",
            ("Admin", ADMIN_EMAIL, ADMIN_PASSWORD, "admin"),
        )
        conn.commit()

    # time range seed（0件だと選べないので最低限）
    cur.execute("SELECT COUNT(*) AS c FROM time_ranges")
    if cur.fetchone()["c"] == 0:
        seed = [
            ("朝", "09:00", "12:00", 10, 1),
            ("昼", "12:00", "15:00", 20, 1),
            ("夕", "15:00", "18:00", 30, 1),
        ]
        cur.executemany(
            "INSERT INTO time_ranges(label,start,end,sort_order,is_active) VALUES(?,?,?,?,?)",
            seed
        )
        conn.commit()

    conn.close()

init_db()

# -----------------------------
# Auth (session)
# -----------------------------
def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    conn = db()
    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
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
    deadline = deadline_for_target_week(ws)
    locked = is_locked(ws)

    conn = db()
    sub = conn.execute(
        "SELECT * FROM submissions WHERE user_id=? AND week_start=?",
        (u["id"], fmt_date(ws)),
    ).fetchone()
    
    # 1. 自分が関わっている案件
    my_projects = conn.execute("""
        SELECT p.* FROM projects p
        JOIN project_members pm ON p.id = pm.project_id
        WHERE pm.user_id = ?
        ORDER BY p.created_at DESC
    """, (u["id"],)).fetchall()

    # 2. 自分の未完了タスク
    my_tasks = conn.execute("""
        SELECT * FROM tasks 
        WHERE user_id = ? AND is_completed = 0 
        ORDER BY deadline ASC, id ASC
    """, (u["id"],)).fetchall()

    # 3. チームメイト
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

    # 4. 直近のスケジュール（今日以降のもの）
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
        deadline_at=deadline.strftime("%Y-%m-%d %H:%M"),
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
            except sqlite3.IntegrityError:
                flash("そのemailは既に存在")

    users = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    conn.close()
    return render_template("admin_users.html", user=current_user(), users=users)

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
                "INSERT INTO time_ranges(label,start,end,sort_order,is_active) VALUES(?,?,?,?,1)",
                (label, start, end, sort_order),
            )
            conn.commit()
            flash("時間帯を追加した")

    ranges = conn.execute(
        "SELECT * FROM time_ranges ORDER BY sort_order, start, id"
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
        "UPDATE time_ranges SET label=?, start=?, end=?, sort_order=?, is_active=? WHERE id=?",
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
    # 参照されていても壊れないように論理削除に寄せる（is_active=0）
    conn.execute("UPDATE time_ranges SET is_active=0 WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    flash("無効化した")
    return redirect(url_for("admin_timeranges"))

# -------- Shifts --------
@app.route("/shift/submit", methods=["GET", "POST"])
@login_required
def shift_submit():
    u = current_user()
    # シフト提出は「次週固定」（週移動はできない）
    ws = target_week_start()
    dates = week_dates(ws)
    deadline = deadline_for_target_week(ws)
    #locked = is_locked(ws)
    locked = False # 締切ロジックは一旦外す（管理者が締切後も編集できるようにするため）

    if locked and u["role"] != "admin" and request.method == "POST":
        flash("締切後なので編集できません。")
        return redirect(url_for("shift_submit"))

    conn = db()

    # time ranges
    ranges = conn.execute(
        "SELECT * FROM time_ranges WHERE is_active=1 ORDER BY sort_order, start, id"
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
        # validate and upsert (multiple selections per日付)
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
                    return redirect(url_for("shift_submit"))
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
        return redirect(url_for("dashboard"))

    conn.close()

    return render_template(
        "shift_submit.html",
        user=u,
        week_dates=dates,
        week_start=dates[0],
        week_end=dates[-1],
        deadline_at=deadline.strftime("%Y-%m-%d %H:%M"),
        locked=locked,
        ranges=ranges,
        selected_map=selected_map,
    )

@app.get("/shift/team")
@login_required
def shift_team():
    u_current = current_user()
    team_only = request.args.get("team_only") == "1"
    
    # デフォルトは「今週（今週の月曜〜日曜）」を表示。
    # ?week_start=YYYY-MM-DD が指定された場合は、その日付を含む週（月曜始まり）を表示。
    qs = (request.args.get("week_start") or "").strip()
    if qs:
        try:
            ws = datetime.strptime(qs, "%Y-%m-%d").date()
            ws = monday_of_week(ws)
        except Exception:
            ws = monday_of_week(now_jst().date())
    else:
        ws = monday_of_week(now_jst().date())

    dates = week_dates(ws)
    prev_ws = ws - timedelta(days=7)
    next_ws = ws + timedelta(days=7)

    conn = db()
    
    if team_only:
        # 自分が所属しているチームのIDを取得
        my_team = conn.execute("SELECT team_id FROM team_members WHERE user_id=?", (u_current["id"],)).fetchone()
        if my_team:
            # 同じチームのユーザーに絞り込む
            users = conn.execute("""
                SELECT u.* FROM users u
                JOIN team_members tm ON u.id = tm.user_id
                WHERE u.is_active=1 AND tm.team_id=?
                ORDER BY u.id
            """, (my_team["team_id"],)).fetchall()
        else:
            # チーム無所属なら自分のみ表示等にするか、または空にする
            # ここでは便宜上、自分のみとする
            users = conn.execute("SELECT * FROM users WHERE id=? AND is_active=1", (u_current["id"],)).fetchall()
    else:
        users = conn.execute("SELECT * FROM users WHERE is_active=1 ORDER BY id").fetchall()
    subs = conn.execute("SELECT * FROM submissions WHERE week_start=?", (fmt_date(ws),)).fetchall()
    sub_by_uid = {s["user_id"]: s for s in subs}

    # time range map (include inactive for display safety)
    ranges = conn.execute("SELECT * FROM time_ranges").fetchall()
    range=[f'{r["label"]} {r["start"]}-{r["end"]}' for r in ranges]
    range_map = {r["id"]: f'{r["label"]} {r["start"]}-{r["end"]}' for r in ranges}
    
    users_data={}
    for u in users:
        user_date=[]


        sub = sub_by_uid.get(u["id"])
        if sub is not None:
            sub_id = sub["id"]
        else:
            sub_id = None

        for date in dates:
            for r in ranges: 
                slots = conn.execute(
                    """
                    SELECT * FROM slots
                    WHERE submission_id=? AND day=? AND time_range_id=?
                    ORDER BY slot_index
                    """,
                (sub_id, date.strftime("%Y-%m-%d"), r["id"])).fetchall()
                if slots:
                    user_date.append("●")
                else:
                    user_date.append("×")
        users_data[u["name"]]=user_date

    rows = []
    for u in users:
        sub = sub_by_uid.get(u["id"])
        status = "未作成" if not sub else ("提出済み" if sub["status"] == "submitted" else "下書き")

        by_date = {fmt_date(d): [] for d in dates}
        if sub:
            slots = conn.execute(
                "SELECT * FROM slots WHERE submission_id=? ORDER BY day, slot_index",
                (sub["id"],),
            ).fetchall()
            for sl in slots:
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
        user=current_user(),
        team_only=team_only,
    )


@app.route("/schedules", methods=["GET"])
@login_required
def schedules():
    conn = db()
    # 予定が近い順（日付・時刻の昇順）
    schedules_data = conn.execute("""
        SELECT s.*, u.name as user_name, p.name as project_name
        FROM schedules s
        LEFT JOIN users u ON s.user_id = u.id
        LEFT JOIN projects p ON s.project_id = p.id
        ORDER BY s.date ASC, s.time ASC
    """).fetchall()

    # 最近追加されたスケジュール（作成日時の降順）
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
                    # 別のチームにいる場合はそこから削除（1人1チームとする）
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
    
    # いずれかのチームに所属しているユーザーは候補から除外（一人のユーザーは1つのチームにしか所属できない）
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
            """, (name, email, description, u["id"], created_at))
            project_id = cur.lastrowid
            
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
                except sqlite3.IntegrityError:
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
    
    # 追加可能なメンバーのリスト
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
