"""
AOS Phase 3: Cron 守护进程调度器 (Scheduler)
系统的"心脏"——让 Agent 具备时间观念，支持定时任务、后台巡检、周期性推送。

设计原则:
- 触发器准时唤醒，不再需要唤醒昂贵的大模型（极度节省算力）
- SQLite 持久化任务列表（重启不丢失）
- 支持 cron 表达式 + 一次性延迟任务
- 可挂载消息推送通道（微信/Webhook/本地通知）
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

# 定时任务持久化路径
SCHEDULER_DB_PATH = os.path.join(os.path.dirname(__file__), "memories", "scheduler.db")


def _parse_simple_cron(cron_expr: str) -> dict:
    """
    解析简化版 cron 表达式: "HH:MM" 每天定时 / "*/N" 每 N 分钟。
    完整 cron 解析可后续接入 croniter 库。
    """
    cron_expr = cron_expr.strip()

    # 每 N 分钟: "*/5" -> 每 5 分钟
    if cron_expr.startswith("*/"):
        interval_min = int(cron_expr[2:])
        return {"type": "interval", "minutes": interval_min}

    # 每天定时: "08:30" -> 每天 08:30
    if ":" in cron_expr:
        parts = cron_expr.split(":")
        return {"type": "daily", "hour": int(parts[0]), "minute": int(parts[1])}

    # 标准 5 段 cron: "0 8 * * *" -> 简化解析
    parts = cron_expr.split()
    if len(parts) == 5:
        return {
            "type": "daily",
            "minute": int(parts[0]) if parts[0] != "*" else 0,
            "hour": int(parts[1]) if parts[1] != "*" else 0,
        }

    raise ValueError(f"无法解析 cron 表达式: {cron_expr}")


def _next_trigger_time(parsed_cron: dict) -> datetime:
    """计算下一次触发时间"""
    now = datetime.now()

    if parsed_cron["type"] == "interval":
        return now + timedelta(minutes=parsed_cron["minutes"])

    if parsed_cron["type"] == "daily":
        target = now.replace(
            hour=parsed_cron["hour"],
            minute=parsed_cron["minute"],
            second=0,
            microsecond=0,
        )
        if target <= now:
            target += timedelta(days=1)
        return target

    return now + timedelta(hours=1)  # 安全回退


class ScheduledTask:
    """单个定时任务"""

    def __init__(
        self,
        task_id: str,
        description: str,
        cron_expr: str,
        action: str,
        payload: str = "",
        enabled: bool = True,
    ):
        self.task_id = task_id
        self.description = description
        self.cron_expr = cron_expr
        self.action = action  # 动作类型: "print", "webhook", "wechat", "agent_chat"
        self.payload = payload
        self.enabled = enabled
        self.last_run: str | None = None
        self.run_count: int = 0


class Scheduler:
    """
    Cron 守护进程调度器。
    在 Agent 主进程中以后台 asyncio.Task 运行，定时触发任务。
    """

    def __init__(self, db_path: str = SCHEDULER_DB_PATH):
        self.db_path = db_path
        self.tasks: dict[str, ScheduledTask] = {}
        self._running = False
        self._background_task: asyncio.Task | None = None
        # 可注册的动作执行器（消息推送通道）
        self._action_handlers: dict[str, Callable[[str], Awaitable[None]]] = {}
        self._init_db()
        self._load_tasks()

    def _init_db(self) -> None:
        """初始化 SQLite 持久化"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                task_id TEXT PRIMARY KEY,
                description TEXT,
                cron_expr TEXT,
                action TEXT,
                payload TEXT,
                enabled INTEGER DEFAULT 1,
                last_run TEXT,
                run_count INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        conn.commit()
        conn.close()
        logger.info("⏰ [调度器] SQLite 已初始化: %s", self.db_path)

    def _load_tasks(self) -> None:
        """从 SQLite 加载所有任务"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute("SELECT * FROM scheduled_tasks WHERE enabled = 1")
        for row in cursor.fetchall():
            task = ScheduledTask(
                task_id=row[0],
                description=row[1],
                cron_expr=row[2],
                action=row[3],
                payload=row[4],
                enabled=bool(row[5]),
            )
            task.last_run = row[6]
            task.run_count = row[7] or 0
            self.tasks[task.task_id] = task
        conn.close()
        if self.tasks:
            logger.info("⏰ [调度器] 已加载 %d 个定时任务", len(self.tasks))

    def add_task(
        self,
        task_id: str,
        description: str,
        cron_expr: str,
        action: str = "print",
        payload: str = "",
    ) -> dict:
        """
        添加定时任务。
        @param task_id 唯一标识
        @param description 任务描述
        @param cron_expr cron 表达式（支持 "08:30" 或 "*/5" 或 "0 8 * * *"）
        @param action 动作类型: print / webhook / wechat / agent_chat
        @param payload 动作参数（消息内容 / URL 等）
        """
        # 验证 cron 表达式
        try:
            parsed = _parse_simple_cron(cron_expr)
            next_time = _next_trigger_time(parsed)
        except Exception as e:
            return {"status": "error", "message": f"cron 表达式无效: {e}"}

        task = ScheduledTask(
            task_id=task_id,
            description=description,
            cron_expr=cron_expr,
            action=action,
            payload=payload,
        )
        self.tasks[task_id] = task

        # 持久化
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT OR REPLACE INTO scheduled_tasks
               (task_id, description, cron_expr, action, payload, enabled, created_at)
               VALUES (?, ?, ?, ?, ?, 1, ?)""",
            (task_id, description, cron_expr, action, payload, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

        print(f"⏰ [调度器] 已添加任务 '{task_id}': {description} (下次触发: {next_time.strftime('%Y-%m-%d %H:%M')})")
        return {
            "status": "created",
            "task_id": task_id,
            "next_trigger": next_time.isoformat(),
            "cron_parsed": parsed,
        }

    def cancel_task(self, task_id: str) -> dict:
        """取消定时任务"""
        if task_id not in self.tasks:
            return {"status": "not_found", "message": f"任务 '{task_id}' 不存在"}

        del self.tasks[task_id]
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM scheduled_tasks WHERE task_id = ?", (task_id,))
        conn.commit()
        conn.close()

        print(f"🗑️ [调度器] 已取消任务: {task_id}")
        return {"status": "cancelled", "task_id": task_id}

    def clear_all_tasks(self) -> dict:
        """清空所有定时任务"""
        count = len(self.tasks)
        self.tasks.clear()
        
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM scheduled_tasks")
        conn.commit()
        conn.close()
        
        print(f"💥 [调度器] 已清理所有任务 (共 {count} 个)")
        return {"status": "cleared", "count": count}

    def get_state_snapshot(self) -> str:
        """
        [AOS 6.2] 获取当前数据库的状态指纹（用于物理审计）。
        返回所有启用任务的任务 ID 拼接后的哈希或简单描述。
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute("SELECT task_id, last_run, run_count FROM scheduled_tasks WHERE enabled = 1 ORDER BY task_id")
        rows = cursor.fetchall()
        conn.close()
        
        # 简单指纹：(任务数, 任务ID列表的Hash-like字符串)
        if not rows:
            return "empty"
        
        fingerprint = "|".join([f"{r[0]}:{r[1]}:{r[2]}" for r in rows])
        return f"count:{len(rows)}|fp:{fingerprint}"

    def list_tasks(self) -> list[dict]:
        """列出所有定时任务"""
        result = []
        for task in self.tasks.values():
            try:
                parsed = _parse_simple_cron(task.cron_expr)
                next_time = _next_trigger_time(parsed).strftime("%m-%d %H:%M")
            except Exception:
                next_time = "?"
            result.append({
                "task_id": task.task_id,
                "description": task.description,
                "cron": task.cron_expr,
                "action": task.action,
                "next_trigger": next_time,
                "run_count": task.run_count,
                "last_run": task.last_run,
            })
        return result

    def register_action(self, action_name: str, handler: Callable[[str], Awaitable[None]]) -> None:
        """注册消息推送通道（微信/Webhook/自定义）"""
        self._action_handlers[action_name] = handler
        logger.info("⏰ [调度器] 已注册动作通道: %s", action_name)

    async def _execute_task(self, task: ScheduledTask) -> None:
        """执行单个任务"""
        logger.info("⏰ [调度器] 触发任务: %s (%s)", task.task_id, task.description)

        try:
            if task.action == "print":
                print(f"\n⏰ [定时提醒] {task.payload or task.description}")
            elif task.action in self._action_handlers:
                await self._action_handlers[task.action](task.payload)
            else:
                logger.warning("⏰ [调度器] 未知动作类型: %s", task.action)
                print(f"\n⏰ [定时提醒] {task.payload or task.description}")

            # 更新运行记录
            task.last_run = datetime.now().isoformat()
            task.run_count += 1
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE scheduled_tasks SET last_run = ?, run_count = ? WHERE task_id = ?",
                (task.last_run, task.run_count, task.task_id),
            )
            conn.commit()
            conn.close()

        except Exception as e:
            logger.error("⏰ [调度器] 任务 %s 执行失败: %s", task.task_id, e)

    async def _tick_loop(self) -> None:
        """后台心跳循环：每 30 秒检查一次是否有任务需要触发"""
        while self._running:
            now = datetime.now()
            for task in list(self.tasks.values()):
                if not task.enabled:
                    continue
                try:
                    parsed = _parse_simple_cron(task.cron_expr)

                    # 判断是否应该触发
                    should_fire = False
                    if parsed["type"] == "interval":
                        if task.last_run is None:
                            should_fire = True
                        else:
                            last = datetime.fromisoformat(task.last_run)
                            should_fire = (now - last).total_seconds() >= parsed["minutes"] * 60

                    elif parsed["type"] == "daily":
                        if now.hour == parsed["hour"] and now.minute == parsed["minute"]:
                            # 防止同一分钟重复触发
                            if task.last_run:
                                last = datetime.fromisoformat(task.last_run)
                                if last.date() == now.date() and last.hour == now.hour:
                                    continue
                            should_fire = True

                    if should_fire:
                        await self._execute_task(task)

                except Exception as e:
                    logger.error("⏰ [调度器] 任务 %s 调度异常: %s", task.task_id, e)

            await asyncio.sleep(30)  # 30 秒心跳

    def start(self) -> None:
        """启动后台调度循环"""
        if self._running:
            return
        self._running = True
        self._background_task = asyncio.ensure_future(self._tick_loop())
        task_count = len(self.tasks)
        logger.info("💓 [调度器] 后台心跳已启动 (%d 个任务)", task_count)
        if task_count > 0:
            print(f"💓 [调度器] 后台守护进程已启动，{task_count} 个定时任务待触发")

    async def stop(self) -> None:
        """停止后台调度"""
        self._running = False
        if self._background_task:
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                pass
        logger.info("💓 [调度器] 后台心跳已停止")
