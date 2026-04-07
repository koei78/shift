import psycopg2

# テスト1: Session Pooler (port 5432)
print("テスト1: Session Pooler port 5432...")
try:
    conn = psycopg2.connect(
        host="aws-1-ap-northeast-1.pooler.supabase.com",
        port=5432,
        dbname="postgres",
        user="postgres.acpxaigmqtyejdrdgxdl",
        password="koei90811478",
        sslmode="require",
        connect_timeout=10,
    )
    print("→ 成功!")
    conn.close()
except Exception as e:
    print(f"→ 失敗: {e}")

# テスト2: Transaction Pooler (port 6543)
print("\nテスト2: Transaction Pooler port 6543...")
try:
    conn = psycopg2.connect(
        host="aws-1-ap-northeast-1.pooler.supabase.com",
        port=6543,
        dbname="postgres",
        user="postgres.acpxaigmqtyejdrdgxdl",
        password="koei90811478",
        sslmode="require",
        connect_timeout=10,
    )
    print("→ 成功!")
    conn.close()
except Exception as e:
    print(f"→ 失敗: {e}")

# テスト3: Direct接続 (IPv6)
print("\nテスト3: Direct接続...")
try:
    conn = psycopg2.connect(
        host="db.acpxaigmqtyejdrdgxdl.supabase.co",
        port=5432,
        dbname="postgres",
        user="postgres",
        password="koei90811478",
        sslmode="require",
        connect_timeout=10,
    )
    print("→ 成功!")
    conn.close()
except Exception as e:
    print(f"→ 失敗: {e}")
