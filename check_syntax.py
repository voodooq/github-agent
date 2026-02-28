import traceback
import sys
import os

files = ['mcp_agent.py', 'orchestrator.py']
log_file = 'debug_syntax.log'

with open(log_file, 'w', encoding='utf-8') as log:
    for file_name in files:
        file_path = os.path.join(os.getcwd(), file_name)
        log.write(f"Checking {file_path}...\n")
        try:
            if not os.path.exists(file_path):
                log.write(f"File not found: {file_path}\n")
                continue
            with open(file_path, 'r', encoding='utf-8') as f:
                source = f.read()
            compile(source, file_path, 'exec')
            log.write(f"{file_name}: OK\n")
        except SyntaxError as e:
            log.write(f"SyntaxError in {file_name}:\n")
            log.write(f"  {e.msg}\n")
            log.write(f"  Line {e.lineno}, Col {e.offset}\n")
            if e.text:
                log.write(f"  Text: {e.text.strip()}\n")
        except Exception:
             log.write(traceback.format_exc())
        log.write("-" * 20 + "\n")
print(f"Check results written to {log_file}")
