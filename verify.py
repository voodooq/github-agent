import py_compile
import traceback
import sys

with open('compile_err.log', 'w') as errlog:
    files = ['mcp_agent.py', 'main.py', 'blackboard.py', 'orchestrator.py', 'test_timer.py']
    for f in files:
        try:
            py_compile.compile(f, doraise=True)
            errlog.write(f"{f} compiled successfully.\n")
        except Exception as e:
            errlog.write(f"Error compiling {f}:\n")
            traceback.print_exc(file=errlog)
            errlog.write("\n")
