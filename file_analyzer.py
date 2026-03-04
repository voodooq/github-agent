import json
import os
import re

def extract_sports_data(file_path, month="2026-03"):
    """
    Streaming extraction of sports data to handle large files.
    """
    results = []
    print(f"🔍 Analyzing {file_path} for matches in {month}...")
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            buffer = ""
            # Chunk by chunk processing or line by line
            # For JSON-like structure, let's accumulate enough context
            for line in f:
                buffer += line
                
                # Check for our target string matches
                matches = list(re.finditer(r'\{[^{}]*?"matchDate"\s*:\s*"' + month + r'-\d{2}"[^{}]*?\}', buffer))
                
                if matches:
                    for match in matches:
                        try:
                            obj_str = match.group(0)
                            # Convert JS-like object to JSON safely (basic fix for keys)
                            json_str = re.sub(r'([{,]\s*)(\w+)(\s*:)', r'\1"\2"\3', obj_str)
                            data = json.loads(json_str)
                            results.append({
                                "date": data.get("matchDate", ""),
                                "event": data.get("eventName", ""),
                                "location": data.get("location", "")
                            })
                        except Exception:
                            continue
                    
                    # Keep tail of buffer to avoid breaking objects across chunks
                    if len(buffer) > 2000:
                        buffer = buffer[-800:]
                    
            # Process remaining buffer just in case
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
        print(f"❌ Error during extraction: {e}")
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
            print(f"✅ Extracted {len(data)} records to {output_file}")
        else:
            print(f"❌ File not found: {target_file}")
            sys.exit(1)
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
