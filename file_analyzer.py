import json
import os
import re

def extract_sports_data(file_path, month="2026-03"):
    """
    Streaming extraction of sports data to handle large files.
    """
    results = []
    # Pattern to match the data structure in sportList.js
    # Assuming it contains objects with match date, name, and location
    
    # Example pattern for JSON-like content in JS
    date_pattern = re.compile(rf'"{month}-\d{{2}}".*?"eventName":"(.*?)".*?"location":"(.*?)"', re.DOTALL)
    
    print(f"🔍 Analyzing {file_path} for matches in {month}...")
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            # For 592K file, we can read chunks if it's not structured JSON
            content = f.read()
            
            # If it's a JS object assignments, we might need to clean it to get JSON
            # But let's try direct Regex first which is faster for "extraction"
            matches = re.finditer(r'\{[^{}]*?"matchDate"\s*:\s*"' + month + r'-\d{2}"[^{}]*?\}', content)
            
            for match in matches:
                try:
                    obj_str = match.group(0)
                    # Convert JS-like object to JSON safely (basic fix for keys)
                    json_str = re.sub(r'(\w+)\s*:', r'"\1":', obj_str)
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
