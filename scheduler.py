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
    解析简化版 cron 表达式: "HH:MM" 每天定时 / "*/N" 每 N 分钟 / "0 * * * *" 每小时。
    """
    cron_expr = cron_expr.strip()

    # 每 N 分钟: "*/5" -> 每 5 分钟
    if cron_expr.startswith("*/"):
        interval_min = int(cron_expr[2:])
        if interval_min <= 0:
            raise ValueError("间隔分钟必须 > 0")
        return {"type": "interval", "minutes": interval_min}

    # 每天定时: "08:30" -> 每天 08:30
    if ":" in cron_expr and " " not in cron_expr:
        parts = cron_expr.split(":")
        hour = int(parts[0])
        minute = int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("daily 时间超出范围，应为 00:00~23:59")
        return {"type": "daily", "hour": hour, "minute": minute}

    # 标准 5 段 cron: "0 * * * *" -> 每小时 / "0 8 * * *" -> 每天 8 点
    parts = cron_expr.split()
    if len(parts) == 5:
        minute = parts[0]
        hour = parts[1]

        # 每小时: "0 * * * *"
        if minute != "*" and hour == "*":
            minute_int = int(minute)
            if not (0 <= minute_int <= 59):
                raise ValueError("hourly 分钟超出范围，应为 0~59")
            return {"type": "hourly", "minute": minute_int}

        # 每天: "0 8 * * *"
        if minute != "*" and hour != "*":
            minute_int = int(minute)
            hour_int = int(hour)
            if not (0 <= hour_int <= 23 and 0 <= minute_int <= 59):
                raise ValueError("daily 时间超出范围，应为 00:00~23:59")
            return {"type": "daily", "hour": hour_int, "minute": minute_int}
        
        # 通配每分钟: "* * * * *"
        if minute == "*" and hour == "*":
            return {"type": "interval", "minutes": 1}

    raise ValueError(f"无法解析 cron 表达式: {cron_expr}")


def _next_trigger_time(parsed_cron: dict) -> datetime:
    """计算下一次触发时间"""
    now = datetime.now()

    if parsed_cron["type"] == "interval":
        return now + timedelta(minutes=parsed_cron["minutes"])

    if parsed_cron["type"] == "hourly":
        target = now.replace(minute=parsed_cron["minute"], second=0, microsecond=0)
        if target <= now:
            target += timedelta(hours=1)
        return target

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
        # 正在执行中的任务，防止长任务被重复触发
        self._inflight_task_ids: set[str] = set()
        # 单任务执行超时（秒），避免心跳被慢任务拖垮
        self._task_timeout_sec = 20
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
        conn.execute("PRAGMA journal_mode=WAL")  # AOS 7.6: 启用 WAL 模式解决 Windows 并发锁定问题
        conn.commit()
        conn.close()
        logger.info("⏰ [调度器] SQLite 已初始化: %s", self.db_path)

    def _load_tasks(self) -> None:
        """从 SQLite 加载所有任务"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute("SELECT * FROM scheduled_tasks WHERE enabled = 1")
        new_tasks = {}
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
            new_tasks[task.task_id] = task
        conn.close()
        self.tasks = new_tasks
        if self.tasks:
            logger.info("⏰ [调度器] 已加载/同步 %d 个定时任务", len(self.tasks))

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
        if task_id in self.tasks:
            del self.tasks[task_id]
            
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM scheduled_tasks WHERE task_id = ?", (task_id,))
        conn.commit()
        conn.close()

        print(f"🗑️ [调度器] 已取消任务: {task_id}")
        return {"status": "cancelled", "task_id": task_id}

    def clear_all_tasks(self) -> dict:
        """清空所有定时任务"""
        count_mem = len(self.tasks)
        self.tasks.clear()
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute("DELETE FROM scheduled_tasks")
        count_db = conn.total_changes
        conn.commit()
        conn.close()
        
        print(f"💥 [调度器] 已清理所有任务 (内存: {count_mem}, 数据库: {count_db})")
        return {"status": "cleared", "count": count_db}

    def get_state_snapshot(self) -> str:
        """
        [AOS 6.2] 获取当前数据库的状态指纹。
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute("SELECT task_id, last_run, run_count FROM scheduled_tasks WHERE enabled = 1 ORDER BY task_id")
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            return "empty"
        
        fingerprint = "|".join([f"{r[0]}:{r[1]}:{r[2]}" for r in rows])
        return f"count:{len(rows)}|fp:{fingerprint}"

    def list_tasks(self) -> list[dict]:
        """列出所有定时任务 (AOS 7.5.5: 强制与 DB 同步)"""
        self._load_tasks()  # 确保返回的是最新物理状态
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
        """注册消息推送通道"""
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

    async def _execute_task_with_timeout(self, task: ScheduledTask) -> None:
        """带超时和并发保护的任务执行包装器"""
        self._inflight_task_ids.add(task.task_id)
        try:
            await asyncio.wait_for(self._execute_task(task), timeout=self._task_timeout_sec)
        except asyncio.TimeoutError:
            logger.error("⏰ [调度器] 任务 %s 执行超时(%ss)，已跳过本轮", task.task_id, self._task_timeout_sec)
        except Exception as e:
            logger.error("⏰ [调度器] 任务 %s 执行异常(包装器): %s", task.task_id, e)
        finally:
            self._inflight_task_ids.discard(task.task_id)

    async def _tick_loop(self) -> None:
        """后台心跳循环"""
        while self._running:
            now = datetime.now()
            # 每次心跳动态加载，防止外部进程更新了 DB
            self._load_tasks()
            
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

                    elif parsed["type"] == "hourly":
                        if now.minute == parsed["minute"]:
                            if task.last_run:
                                last = datetime.fromisoformat(task.last_run)
                                if last.hour == now.hour and last.day == now.day:
                                    continue
                            should_fire = True

                    elif parsed["type"] == "daily":
                        if now.hour == parsed["hour"] and now.minute == parsed["minute"]:
                            # 防止同一分钟重复触发
                            if task.last_run:
                                last = datetime.fromisoformat(task.last_run)
                                if last.date() == now.date() and last.hour == now.hour:
                                    continue
                            should_fire = True

                    if should_fire:
                        if task.task_id in self._inflight_task_ids:
                            logger.warning("⏰ [调度器] 任务 %s 仍在执行中，跳过重复触发", task.task_id)
                        else:
                            asyncio.create_task(
                                self._execute_task_with_timeout(task),
                                name=f"SchedulerTask-{task.task_id}"
                            )

                except Exception as e:
                    logger.error("⏰ [调度器] 任务 %s 调度异常: %s", task.task_id, e)

            await asyncio.sleep(30)  # 30 秒心跳

    def start(self) -> None:
        """启动后台调度循环"""
        if self._running:
            return
        self._running = True
        self._background_task = asyncio.create_task(self._tick_loop(), name="SchedulerTickLoop")
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
