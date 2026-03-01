"""
AOS 2.0 黑板共享上下文 (Blackboard Architecture)
升级版：支持异步事件订阅（asyncio.Event）+ 任务进度追踪。
解决多 Agent 之间的"信息孤岛"与"异步等待"问题。
"""

import asyncio
import json
import os
import logging
import copy
from datetime import datetime

logger = logging.getLogger(__name__)


class Blackboard:
    """
    全局共享上下文黑板（异步事件驱动版）。
    防御性补丁：防死锁 + 上下文瘦身 + 超时降级。
    """

    # 防御补丁 #3: 黑板值最大字符数（超过自动截断 + 警告）
    # 只存状态指针、元数据或极简摘要，不存海量实体数据
    MAX_VALUE_SIZE = 2000

    def __init__(self, persist_path: str = "memories/blackboard.json"):
        self.persist_path = persist_path
        self.facts: dict[str, dict] = {}
        self.snapshots: list[dict] = []
        # 异步事件注册中心：key -> asyncio.Event
        self._events: dict[str, asyncio.Event] = {}
        # 任务进度追踪：agent_role -> {status, message, timestamp}
        self.task_progress: dict[str, dict] = {}
        # 错误事件队列：主 Agent 监控用
        self.error_queue: asyncio.Queue = asyncio.Queue()
        self._load()

    # ========== 核心读写 ==========

    def write(self, key: str, value: str, author: str = "system") -> None:
        """
        向黑板写入客观事实。
        防御补丁 #3: 超过 MAX_VALUE_SIZE 自动截断，防止 Token 爆仓。
        """
        if not isinstance(value, str):
            # [AOS 4.0.1] 自动序列化非字符串对象（如诊断报告 dict）
            try:
                value = json.dumps(value, ensure_ascii=False)
            except Exception:
                value = str(value)

        # 上下文瘦身：强制截断过大的值
        if len(value) > self.MAX_VALUE_SIZE:
            logger.warning(
                "⚠️ [黑板] %s 写入的值超过上限 (%d > %d)，已自动截断。"
                "请只存状态指针/元数据，不要存海量原始数据。",
                author, len(value), self.MAX_VALUE_SIZE,
            )
            value = value[:self.MAX_VALUE_SIZE] + "...[TRUNCATED]"

        self.facts[key] = {
            "value": value,
            "author": author,
            "timestamp": datetime.now().isoformat(),
        }
        # 🛡️ 防御式日志：确保 value 是字符串才切片
        display_log = value[:100] if isinstance(value, str) else str(value)[:100]
        logger.info("📋 [黑板] %s 写入: %s = %s", author, key, display_log)

        # 唤醒等待该 key 的所有订阅者
        if key in self._events:
            self._events[key].set()

        self._save()

    def read(self, key: str) -> str | None:
        """读取指定事实值"""
        fact = self.facts.get(key)
        return fact["value"] if fact else None

    def read_all(self) -> str:
        """
        格式化输出所有事实，供注入 Agent 上下文。
        防御补丁 #3: 每个值截断显示为 200 字，保持上下文精简。
        """
        if not self.facts:
            return "[黑板为空，尚无共享信息]"
        lines = ["=== 项目状态黑板 ==="]
        for key, fact in self.facts.items():
            val = fact.get('value', "")
            # 上下文瘦身：截断显示 (防御式编码)
            str_val = str(val)
            display_val = str_val[:200]
            if len(str_val) > 200:
                display_val += "..."
            lines.append(f"- {key}: {display_val} (by {fact['author']})")
        # 附加任务进度
        if self.task_progress:
            lines.append("\n=== 任务进度 ===")
            for role, info in self.task_progress.items():
                status_icon = {"COMPLETED": "✅", "RUNNING": "🔄", "WAITING": "⏳", "FAILED": "❌"}.get(info.get("status", ""), "❓")
                lines.append(f"- {status_icon} {role}: {info.get('status', '?')} — {info.get('message', '')}")
        return "\n".join(lines)

    # ========== 异步事件订阅 ==========

    async def wait_for(self, key: str, timeout: float = 120.0) -> str | None:
        """
        子 Agent 挂起等待特定前置条件完成。
        防御补丁 #1: 强制超时 + 错误升级到 error_queue，防止死锁。
        """
        # 如果已存在，直接返回
        if key in self.facts:
            return self.facts[key]["value"]

        # 创建事件并等待
        if key not in self._events:
            self._events[key] = asyncio.Event()

        try:
            await asyncio.wait_for(self._events[key].wait(), timeout=timeout)
            return self.facts.get(key, {}).get("value")
        except asyncio.TimeoutError:
            # 防御补丁 #1: 超时降级 — 向错误队列抛出详细诊断信息
            error_msg = f"等待 '{key}' 严重超时 ({timeout:.0f}s)，前置任务疑似失败或崩溃"
            logger.warning("🚨 [黑板] %s", error_msg)
            self.error_queue.put_nowait({
                "agent": "blackboard_watchdog",
                "type": "DEADLOCK_RISK",
                "waiting_key": key,
                "message": error_msg,
                "timestamp": datetime.now().isoformat(),
            })
            return None

    # ========== 任务进度追踪 ==========

    def update_task(self, agent_role: str, status: str, message: str) -> None:
        """
        子 Agent 汇报任务进度。
        status: WAITING | RUNNING | COMPLETED | FAILED
        """
        self.task_progress[agent_role] = {
            "status": status,
            "message": message,
            "timestamp": datetime.now().isoformat(),
        }
        icon = {"COMPLETED": "✅", "RUNNING": "🔄", "WAITING": "⏳", "FAILED": "❌"}.get(status, "❓")
        print(f"📢 [{icon} {agent_role}] {status}: {message}")

        # 失败事件推入错误队列，主 Agent 可监听并介入
        if status == "FAILED":
            self.error_queue.put_nowait({
                "agent": agent_role,
                "message": message,
                "timestamp": datetime.now().isoformat(),
            })

    def all_tasks_completed(self) -> bool:
        """检查是否所有已注册任务都已完成"""
        if not self.task_progress:
            return False
        return all(
            info.get("status") in ("COMPLETED", "FAILED")
            for info in self.task_progress.values()
        )

    def get_timeline(self) -> str:
        """生成任务时间轴摘要（供用户查看）"""
        if not self.task_progress:
            return "[暂无任务记录]"
        lines = ["📊 任务时间轴:"]
        for role, info in self.task_progress.items():
            icon = {"COMPLETED": "✅", "RUNNING": "🔄", "WAITING": "⏳", "FAILED": "❌"}.get(info.get("status", ""), "❓")
            ts = info.get("timestamp", "")[:19]
            lines.append(f"  {icon} [{ts}] {role}: {info.get('message', '')}")
        return "\n".join(lines)

    # ========== 快照与回滚 ==========

    def snapshot(self) -> int:
        """创建当前状态快照，返回快照 ID"""
        snap_id = len(self.snapshots)
        self.snapshots.append(copy.deepcopy(self.facts))
        logger.info("📸 [黑板] 已创建快照 #%d (共 %d 条事实)", snap_id, len(self.facts))
        return snap_id

    def rollback(self, snap_id: int) -> bool:
        """回滚到指定快照"""
        if 0 <= snap_id < len(self.snapshots):
            self.facts = copy.deepcopy(self.snapshots[snap_id])
            # 回滚后重置相关事件
            self._events.clear()
            logger.info("⏪ [黑板] 已回滚到快照 #%d", snap_id)
            self._save()
            return True
        logger.warning("⚠️ [黑板] 快照 #%d 不存在", snap_id)
        return False

    def delete(self, key: str) -> bool:
        """
        [AOS 4.1] 物理擦除黑板记录。
        用于免疫系统纠偏：当物理现实与黑板描述不符时，擦除虚假标志。
        """
        if key in self.facts:
            del self.facts[key]
            # 同时清除关联事件，防止残留触发
            if key in self._events:
                del self._events[key]
            logger.info("🗑️ [黑板] 物理纠偏：已删除虚假/过时记录: %s", key)
            self._save()
            return True
        return False

    # ========== 持久化 ==========

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)
        with open(self.persist_path, "w", encoding="utf-8") as f:
            json.dump(self.facts, f, ensure_ascii=False, indent=2)

    def _load(self) -> None:
        if os.path.exists(self.persist_path):
            try:
                with open(self.persist_path, "r", encoding="utf-8") as f:
                    self.facts = json.load(f)
                logger.info("📋 [黑板] 已加载 %d 条历史事实", len(self.facts))
            except (json.JSONDecodeError, IOError):
                self.facts = {}
