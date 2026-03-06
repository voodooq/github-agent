import os
import re

# 繁体中文字符集（部分常见）
TRADITIONAL_CHARS = set("個為這單實體劃變應統開關點啟動機樣現後復備輸處過進運遠連邏執")

def scan_file(filepath):
    found = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for i, line in enumerate(f, 1):
                for char in line:
                    if char in TRADITIONAL_CHARS:
                        found.append((i, char, line.strip()))
                        break
    except Exception as e:
        print(f"Error reading {filepath}: {e}")
    return found

if __name__ == "__main__":
    for root, dirs, files in os.walk("."):
        if any(d in root for d in [".git", "__pycache__", ".venv", "node_modules"]):
            continue
        for f in files:
            if f.endswith((".py", ".md")):
                path = os.path.join(root, f)
                results = scan_file(path)
                if results:
                    print(f"\nIn {path}:")
                    for line_no, char, content in results:
                        print(f"  Line {line_no}: Found '{char}' in -> {content}")
