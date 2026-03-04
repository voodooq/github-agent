import re
import sys
import traceback

def main():
    try:
        file_path = 'e:\\work\\githubAgent\\main.py'
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        new_lines = []
        
        injected_contextlib = False
        skip_lines_start = 180
        skip_lines_end = 187
        
        for i, line in enumerate(lines):
            if line.startswith('import asyncio') and not injected_contextlib:
                new_lines.append(line)
                new_lines.append("from contextlib import asynccontextmanager\n")
                injected_contextlib = True
                continue
                
            if line.startswith('async def main():') and not any('hot_mcp_env' in l for l in new_lines):
                new_lines.append('''
@asynccontextmanager
async def hot_mcp_env(agent, serverParams):
    print("🔌 [AOS P3] 正在按需唤醒 MCP 运行时环境...")
    async with stdio_client(serverParams) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            toolNames = await agent.connect(session)
            print(f"✅ 热环境就绪: 加载了 {len(toolNames)} 个底层组件工具")
            yield session
''')
                new_lines.append(line)
                continue
                
            if skip_lines_start <= i <= skip_lines_end:
                continue
                
            if 188 <= i <= 626:
                if line.startswith('        '):
                    new_lines.append(line[8:])
                elif line.startswith('    ') and not line.strip():
                    new_lines.append('\\n')
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        content = "".join(new_lines)
        
        def replacer(match):
            indent = match.group(1)
            stmt = match.group(2)
            if 'no_tools=True' in stmt:
                return match.group(0)
            else:
                return f'{indent}async with hot_mcp_env(agent, serverParams):\\n{indent}    {stmt}'

        content = re.sub(r'^([ \\t]*)(async for chunk in agent\\..*?:(?!.*no_tools=True))', replacer, content, flags=re.MULTILINE)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
            
        print("Migration successful")
    except Exception as e:
        print("Error:")
        traceback.print_exc()

if __name__ == '__main__':
    main()
