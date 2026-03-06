import py_compile
import traceback
import sys

with open('compile_err.log', 'w') as errlog:
    files = [
        'mcp_agent.py', 'main.py', 'blackboard.py', 'orchestrator.py', 
        'prompts.py', 'economy.py', 'runtime_engine.py', 'scheduler.py', 
        'skill_manager.py', 'config.py', 'docker_sandbox.py', 'tool_converter.py',
        'experience_engine.py', 'file_analyzer.py', 'fix_diagnose.py', 
        'migrate_p3.py', 'verify.py'
    ]
    for f in files:
        try:
            py_compile.compile(f, doraise=True)
            errlog.write(f"{f} 编译成功。\n")
        except Exception as e:
            errlog.write(f"编译 {f} 出错：\n")
            traceback.print_exc(file=errlog)
            errlog.write("\n")
