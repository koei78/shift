# Shift App (Flask / app.py 1枚 / 管理者機能つき)

## できること
- ログイン（session）
- 次週の希望シフト提出（7日×3枠） ※締切：直前の金曜23:59（JST）
- みんなの希望一覧表示
- 管理者：ユーザー追加（権限 staff/admin 指定）
- 管理者：時間帯マスタ作成・編集（シフト提出はこのマスタから選択）

## フォルダ構成
- app.py
- templates/
- requirements.txt

## セットアップ
```bash
python -m venv .venv
# Windows:
.\.venv\Scripts\activate
# Mac/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
python app.py
```

ブラウザ:
- http://127.0.0.1:5000/login

## 初期ログイン（固定）
- Email: admin@example.com
- Pass:  admin123

SQLiteのDBは app.db が同じフォルダに生成されます。
