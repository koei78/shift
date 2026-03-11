import sqlite3

DB_PATH = "app.db"

users_to_add = [
    ("山田 太郎", "yamada@example.com", "password123", "staff"),
    ("佐藤 花子", "sato@example.com", "password123", "staff"),
    ("鈴木 一郎", "suzuki@example.com", "password123", "staff"),
    ("高橋 健太", "takahashi@example.com", "password123", "staff"),
    ("伊藤 美咲", "ito@example.com", "password123", "staff"),
    ("渡辺 翔太", "watanabe@example.com", "password123", "staff"),
    ("山本 さくら", "yamamoto@example.com", "password123", "staff"),
    ("中村 結衣", "nakamura@example.com", "password123", "staff"),
    ("小林 大輔", "kobayashi@example.com", "password123", "staff"),
    ("加藤 陽菜", "kato@example.com", "password123", "staff"),
]

def seed():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    count = 0
    for name, email, password, role in users_to_add:
        cur.execute("SELECT id FROM users WHERE email=?", (email,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO users(name,email,password,role,is_active) VALUES(?,?,?,?,1)",
                (name, email, password, role),
            )
            count += 1
            print(f"Added user: {name} ({email})")

    conn.commit()
    conn.close()
    print(f"Successfully added {count} users.")

if __name__ == "__main__":
    seed()