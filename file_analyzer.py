import json
import os
import re

def extract_sports_data(file_path, month="2026-03"):
    """
    流式提取体育数据以处理大文件。
    """
    results = []
    print(f"🔍 正在从 {file_path} 中分析 {month} 的比赛...")
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            buffer = ""
            # 分块处理或逐行处理
            # 对于类似 JSON 的结构，我们需要积累足够的上下文
            for line in f:
                buffer += line
                
                # 检查目标字符串匹配
                matches = list(re.finditer(r'\{[^{}]*?"matchDate"\s*:\s*"' + month + r'-\d{2}"[^{}]*?\}', buffer))
                
                if matches:
                    for match in matches:
                        try:
                            obj_str = match.group(0)
                            # 将类似 JS 的对象安全地转换为 JSON（修复键名）
                            json_str = re.sub(r'([{,]\s*)(\w+)(\s*:)', r'\1"\2"\3', obj_str)
                            data = json.loads(json_str)
                            results.append({
                                "date": data.get("matchDate", ""),
                                "event": data.get("eventName", ""),
                                "location": data.get("location", "")
                            })
                        except Exception:
                            continue
                    
                    # 保持缓冲区末尾，避免跨块截断对象
                    if len(buffer) > 2000:
                        buffer = buffer[-800:]
                    
            # 处理剩余的缓冲区
            matches = list(re.finditer(r'\{[^{}]*?"matchDate"\s*:\s*"' + month + r'-\d{2}"[^{}]*?\}', buffer))
            for match in matches:
                try:
                    obj_str = match.group(0)
                    json_str = re.sub(r'([{,]\s*)(\w+)(\s*:)', r'\1"\2"\3', obj_str)
                    data = json.loads(json_str)
                    results.append({
                        "date": data.get("matchDate", ""),
                        "event": data.get("eventName", ""),
                        "location": data.get("location", "")
                    })
                except Exception:
                    continue

        return results
    except Exception as e:
        print(f"❌ 提取过程中出错: {e}")
        return []

if __name__ == "__main__":
    import sys
    try:
        target_file = r"e:\work\githubAgent\Workspace\Blitz\blitz_20260301_212808\sportList.js"
        output_file = r"e:\work\githubAgent\march_matches.json"
        
        if os.path.exists(target_file):
            data = extract_sports_data(target_file)
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"✅ 已提取 {len(data)} 条记录到 {output_file}")
        else:
            print(f"❌ 未找到文件: {target_file}")
            sys.exit(1)
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
