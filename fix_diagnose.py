import ast
import os as std_os
import sys

def check_file(filepath):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except Exception as e:
        # print(f"Failed to parse {filepath}: {e}")
        return

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            has_os_def = False
            for subnode in ast.walk(node):
                # Check for definition (assignment or import)
                if isinstance(subnode, ast.Name) and subnode.id == 'os':
                    if isinstance(subnode.ctx, ast.Store):
                        has_os_def = True
                if isinstance(subnode, ast.Import):
                    for alias in subnode.names:
                        if alias.name == 'os' or alias.asname == 'os':
                            has_os_def = True
                if isinstance(subnode, ast.ImportFrom):
                    for alias in subnode.names:
                        if alias.asname == 'os':
                            has_os_def = True
                if isinstance(subnode, ast.ExceptHandler):
                    if subnode.name == 'os':
                        has_os_def = True
            
            if has_os_def:
                print(f"找到: 在 {filepath} 的第 {node.lineno} 行中，函数 '{node.name}' 有对 'os' 的局部定义")

if __name__ == "__main__":
    cwd = std_os.getcwd()
    print(f"正在检查目录: {cwd}")
    for root, dirs, files in std_os.walk("."):
        if ".venv" in root or "node_modules" in root: continue
        for f in files:
            if f.endswith(".py"):
                check_file(std_os.path.join(root, f))
