import sqlite3

conn = sqlite3.connect("app.db")
cur = conn.cursor()

try:
    cur.execute("ALTER TABLE projects ADD COLUMN mail_text TEXT")
except sqlite3.OperationalError:
    pass

try:
    cur.execute("ALTER TABLE schedules ADD COLUMN project_id INTEGER")
except sqlite3.OperationalError:
    pass

try:
    cur.execute("ALTER TABLE schedules ADD COLUMN schedule_type TEXT")
except sqlite3.OperationalError:
    pass

conn.commit()
conn.close()
print("DB columns added!")
