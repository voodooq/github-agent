import asyncio
import os
import sqlite3
import json
from mcp_agent import McpAgent
from config import CLOUD_API_KEY, CLOUD_BASE_URL, CLOUD_MODEL, LOCAL_API_KEY, LOCAL_BASE_URL, LOCAL_MODEL, AGENT_MODE

async def test_scheduler():
    agent = McpAgent(
        cloud_config={"api_key": CLOUD_API_KEY, "base_url": CLOUD_BASE_URL, "model": CLOUD_MODEL},
        local_config={"api_key": LOCAL_API_KEY, "base_url": LOCAL_BASE_URL, "model": LOCAL_MODEL},
        systemPrompt="Test Prompt",
        mode=AGENT_MODE,
    )
    
    # 注册回调 (在 main.py 或 connect 时注册的)
    agent.scheduler.register_action("autonomous_task", agent._handle_scheduled_autonomous_task)
    agent.scheduler.start()
    
    # 添加一个任务
    payload = json.dumps({
        "task_id": "test_blitz_task_001",
        "instruction": "使用搜索或者直接输出一句话表示你被定时器成功唤醒执行了。"
    }, ensure_ascii=False)
    
    print("添加调度任务...")
    res = agent.scheduler.add_task(
        task_id="test_timer_001",
        description="测试定时器唤醒",
        cron_expr="* * * * * *", # 每秒执行？ scheduler.py 支持6段式吗？目前项目似乎支持简单的。
        action="autonomous_task",
        payload=payload
    )
    print("添加结果:", res)
    
    print("等待10秒触发...")
    await asyncio.sleep(10)
    
    agent.scheduler.cancel_task("test_timer_001")
    print("测试完毕，取消任务。")

if __name__ == "__main__":
    asyncio.run(test_scheduler())
