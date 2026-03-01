
import asyncio
import json

class MockAgent:
    def _normalize_tool_name(self, func_name: str) -> str:
        if len(func_name) % 2 == 0:
            half = len(func_name) // 2
            if func_name[:half] == func_name[half:]:
                print(f"🧠 [AOS 5.2] Adhesion Fixed: {func_name} -> {func_name[:half]}")
                func_name = func_name[:half]
        for sep in ["_", "."]:
            if sep in func_name:
                parts = func_name.split(sep)
                if len(parts) == 2 and parts[0] == parts[1]:
                    print(f"🧠 [AOS 5.2] Separator Adhesion Fixed: {func_name} -> {parts[0]}")
                    func_name = parts[0]
                    break
        return func_name

    async def simulate_execute_with_tools(self, resultText):
        instant_kill_signals = ["⏰ [调度器]", "💥 [调度器]"]
        if any(sig in resultText for sig in instant_kill_signals):
            print(f"⚡ [AOS 5.3] INSTANT_KILL Triggered: {resultText}")
            return f"INSTANT_KILL_PASS: {resultText}"
        return "CONTINUE"

async def run_tests():
    agent = MockAgent()
    print("--- Test 1: Adhesion Correction (AOS 5.2) ---")
    n1 = agent._normalize_tool_name("add_scheduled_taskadd_scheduled_task")
    print(f"Result: {n1}")
    
    print("\n--- Test 2: INSTANT_KILL (AOS 5.3) ---")
    r1 = await agent.simulate_execute_with_tools("⏰ [调度器] 任务已添加成功 (ID: drink_water)")
    print(f"Result: {r1}")

if __name__ == "__main__":
    asyncio.run(run_tests())
