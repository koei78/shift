with open('app.py', 'r', encoding='utf-8') as f:
    lines = f.read().splitlines()

for i, line in enumerate(lines):
    if '"""' in line:
        print(f"{i+1}: {line}")
