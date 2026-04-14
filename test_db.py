import os
import pathlib

# .env を読み込む
_env_path = pathlib.Path(__file__).parent / ".env"
for _line in _env_path.read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

import psycopg2
import psycopg2.extras

conn = psycopg2.connect(
    host=os.environ["PG_HOST"],
    port=int(os.environ.get("PG_PORT", 5432)),
    dbname=os.environ.get("PG_DB", "postgres"),
    user=os.environ["PG_USER"],
    password=os.environ["PG_PASSWORD"],
    sslmode="require",
)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

cur.execute("SELECT COUNT(*) as cnt FROM users")
print("users:", cur.fetchone()["cnt"])

cur.execute("SELECT COUNT(*) as cnt FROM submissions")
print("submissions:", cur.fetchone()["cnt"])

cur.execute("SELECT id, name, email, is_active FROM users ORDER BY id")
for r in cur.fetchall():
    print(f" - {r['id']} {r['name']} is_active={r['is_active']}")

conn.close()
