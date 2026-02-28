"""
AOS AEA: 经济与生存引擎 (Economic & Survival Engine)
CFO Agent — 赋予 Agent "银行账户"和"求生欲"。

生存模式:
- 饥饿 (balance < $2): 全部降级 LOCAL，停止探索，向老板求救
- 温饱 ($2 ~ $15): AUTO 混合，每次调用前 ROI 评估
- 土豪 (> $50): TURBO 全开，解锁旗舰模型，扩大算力规模

核心指标（实时写入黑板）:
- current_balance: 当前余额
- daily_burn_rate: 日均燃烧率
- projected_runway: 剩余可用天数
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# 默认配置
DEFAULT_INITIAL_BALANCE = 10.0  # 初始 $10
ECONOMY_DB_PATH = os.path.join(os.path.dirname(__file__), "memories", "economy.db")

# Token 到美元的粗略换算（基于 GPT-4o/DeepSeek-V3 定价）
COST_PER_1K_INPUT_TOKENS = 0.003   # $/1K input tokens
COST_PER_1K_OUTPUT_TOKENS = 0.012  # $/1K output tokens
LOCAL_COST_MULTIPLIER = 0.0        # 本地模型不花钱


class SurvivalMode:
    """生存模式枚举"""
    HUNGER = "HUNGER"      # 饥饿: balance < $2
    SURVIVAL = "SURVIVAL"  # 温饱: $2 ~ $15
    COMFORT = "COMFORT"    # 舒适: $15 ~ $50
    RICH = "RICH"          # 土豪: > $50


class EconomyEngine:
    """
    CFO Agent: 管理虚拟钱包、燃烧率追踪、ROI 审批、生存模式切换。
    所有财务数据持久化到 SQLite。
    """

    def __init__(
        self,
        db_path: str = ECONOMY_DB_PATH,
        initial_balance: float | None = None,
        blackboard = None
    ):
        self.db_path = db_path
        self.blackboard = blackboard
        self._init_db()

        # 加载或初始化余额
        env_bal_str = os.getenv("AEA_INITIAL_BALANCE")
        env_bal = float(env_bal_str) if env_bal_str else DEFAULT_INITIAL_BALANCE
        
        last_initial = self._get_stored_initial()
        existing_balance = self._get_balance()

        # [AOS 2.5] 种子资金逻辑：只有在完全没数据，或者老板明确改了 .env 初始值时才覆盖
        if last_initial is None:
            # 第一次启动，发放天使轮
            self.balance = env_bal
            self._set_balance(env_bal)
            self._set_stored_initial(env_bal)
            self._record_transaction("revenue", env_bal, "天使轮种子资金注入")
            logger.info("💰 [CFO] 首次启动，注入种子资金: $%.2f", env_bal)
        elif abs(last_initial - env_bal) > 0.0001:
            # 老板手动改了初始值，视为追加或重置
            self.balance = env_bal
            self._set_balance(env_bal)
            self._set_stored_initial(env_bal)
            self._record_transaction("inject", env_bal, "手动重置初始资金")
            logger.info("🔄 [CFO] 检测到初始金额变更，已重置余额为: $%.2f", env_bal)
        else:
            # 正常重启，继承数据库里的真实余额
            self.balance = existing_balance if existing_balance is not None else env_bal
            logger.info("💾 [CFO] 继承持久化余额: $%.4f", self.balance)

        # 当日消费追踪
        self.today_spend = self._get_today_spend()
        self.today_revenue = self._get_today_revenue()
        
        # 同步一次黑板
        self.sync_blackboard()

    def sync_blackboard(self):
        """将最新余额与状态推送到黑板"""
        if self.blackboard:
            for key, val in self.get_blackboard_facts().items():
                self.blackboard.write(key, val, author="CFO")

    # ========== SQLite 持久化 ==========

    # ========== SQLite 持久化 ==========

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wallet (
                key TEXT PRIMARY KEY,
                value REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                type TEXT,
                amount REAL,
                description TEXT,
                balance_after REAL
            )
        """)
        conn.commit()
        conn.close()

    def _get_balance(self) -> float | None:
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT value FROM wallet WHERE key = 'balance'").fetchone()
        conn.close()
        return row[0] if row else None

    def _set_balance(self, value: float) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO wallet (key, value) VALUES ('balance', ?)",
            (value,),
        )
        conn.commit()
        conn.close()

    def _get_stored_initial(self) -> float | None:
        """获取上次记录在 .env 中的初始值"""
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT value FROM wallet WHERE key = 'last_initial_value'").fetchone()
        conn.close()
        return row[0] if row else None

    def _set_stored_initial(self, value: float) -> None:
        """记录当前 .env 中的初始值，用于下次比对"""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO wallet (key, value) VALUES ('last_initial_value', ?)",
            (value,),
        )
        conn.commit()
        conn.close()

    def _record_transaction(self, tx_type: str, amount: float, description: str) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO transactions (timestamp, type, amount, description, balance_after) VALUES (?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), tx_type, amount, description, self.balance),
        )
        conn.commit()
        conn.close()

    def _get_today_spend(self) -> float:
        today = datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type = 'spend' AND timestamp LIKE ?",
            (f"{today}%",),
        ).fetchone()
        conn.close()
        return abs(row[0]) if row else 0.0

    def _get_today_revenue(self) -> float:
        today = datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type = 'revenue' AND timestamp LIKE ?",
            (f"{today}%",),
        ).fetchone()
        conn.close()
        return row[0] if row else 0.0

    # ========== 核心财务操作 ==========

    def spend(self, amount: float, description: str = "API 调用") -> bool:
        """
        扣费。如果余额不足，拒绝执行并返回 False。
        """
        if amount <= 0:
            return True

        if self.balance < amount:
            logger.warning("🚨 [CFO] 余额不足！需要 $%.4f，当前 $%.2f", amount, self.balance)
            return False

        self.balance -= amount
        self.today_spend += amount
        self._set_balance(self.balance)
        self._record_transaction("spend", -amount, description)
        self.sync_blackboard()
        return True

    def earn(self, amount: float, description: str = "收入") -> None:
        """
        收入入账。
        """
        self.balance += amount
        self.today_revenue += amount
        self._set_balance(self.balance)
        self._record_transaction("revenue", amount, description)
        self.sync_blackboard()
        print(f"💵 [CFO] 收入 +${amount:.2f}: {description} | 余额: ${self.balance:.2f}")

    def inject_funds(self, amount: float) -> None:
        """老板手动注资"""
        self.balance += amount
        self._set_balance(self.balance)
        self._record_transaction("inject", amount, "老板注资")
        self.sync_blackboard()
        print(f"💰 [CFO] 老板注资 +${amount:.2f} | 新余额: ${self.balance:.2f}")

    # ========== Token 成本估算 ==========

    def estimate_cost(self, input_tokens: int, output_tokens: int, is_local: bool = False) -> float:
        """估算一次 API 调用的成本"""
        if is_local:
            return 0.0
        cost = (input_tokens / 1000 * COST_PER_1K_INPUT_TOKENS +
                output_tokens / 1000 * COST_PER_1K_OUTPUT_TOKENS)
        return round(cost, 6)

    def track_api_call(self, input_tokens: int, output_tokens: int, is_local: bool = False) -> float:
        """
        追踪一次 API 调用的消耗。
        自动扣费并返回本次消耗金额。
        """
        cost = self.estimate_cost(input_tokens, output_tokens, is_local)
        if cost > 0:
            self.spend(cost, f"API ({input_tokens}in/{output_tokens}out)")
        return cost

    # ========== 生存模式 ==========

    def get_survival_mode(self) -> str:
        """基于当前余额判定生存模式"""
        if self.balance < 2.0:
            return SurvivalMode.HUNGER
        elif self.balance < 15.0:
            return SurvivalMode.SURVIVAL
        elif self.balance < 50.0:
            return SurvivalMode.COMFORT
        else:
            return SurvivalMode.RICH

    def get_recommended_tier(self) -> str:
        """
        基于生存模式推荐算力层级。
        这是 CFO 的核心决策——用余额动态控制 Agent 的"大脑档位"。
        """
        mode = self.get_survival_mode()
        if mode == SurvivalMode.HUNGER:
            return "LOCAL"    # 全部走本地，省每一分钱
        elif mode == SurvivalMode.SURVIVAL:
            return "LOCAL"    # 默认本地，重要任务才上云
        elif mode == SurvivalMode.COMFORT:
            return "AUTO"     # 自动混合
        else:
            return "PREMIUM"  # 钱多到溢出，全部上最强模型

    def should_approve_cloud_call(self, estimated_cost: float, expected_value: float = 0.0) -> dict:
        """
        ROI 审批：在调用云端 API 前，CFO 进行投资回报率评估。
        @param estimated_cost 预估消耗 ($)
        @param expected_value 预期收益 ($)，0 表示无直接收益
        @returns {"approved": bool, "reason": str}
        """
        mode = self.get_survival_mode()

        # 饥饿模式：一律拒绝云端调用
        if mode == SurvivalMode.HUNGER:
            return {
                "approved": False,
                "reason": f"🚨 饥饿模式！余额仅 ${self.balance:.2f}，禁止云端调用。请使用本地模型。",
            }

        # 温饱模式：仅在 ROI > 1 或消耗极小时批准
        if mode == SurvivalMode.SURVIVAL:
            if expected_value > estimated_cost:
                return {"approved": True, "reason": f"✅ ROI 正: 预期收益 ${expected_value:.2f} > 成本 ${estimated_cost:.4f}"}
            if estimated_cost < 0.01:
                return {"approved": True, "reason": "✅ 微量消耗，放行"}
            return {
                "approved": False,
                "reason": f"❌ 温饱模式拒绝: 成本 ${estimated_cost:.4f} 但无明确 ROI。余额 ${self.balance:.2f}",
            }

        # 舒适/土豪：直接放行
        return {"approved": True, "reason": f"✅ 余额充足 (${self.balance:.2f})，批准"}

    # ========== 报表 ==========

    def get_daily_burn_rate(self) -> float:
        """计算 7 日平均日燃烧率"""
        week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT COALESCE(SUM(ABS(amount)), 0) FROM transactions WHERE type = 'spend' AND timestamp > ?",
            (week_ago,),
        ).fetchone()
        conn.close()
        total = row[0] if row else 0.0
        return round(total / 7, 4)

    def get_runway_days(self) -> float:
        """预估剩余可用天数"""
        burn = self.get_daily_burn_rate()
        if burn <= 0:
            return 999.0  # 无消耗
        return round(self.balance / burn, 1)

    def get_financial_report(self) -> str:
        """生成 CFO 财务简报（写入黑板或展示给用户）"""
        mode = self.get_survival_mode()
        mode_icon = {
            SurvivalMode.HUNGER: "🔴",
            SurvivalMode.SURVIVAL: "🟡",
            SurvivalMode.COMFORT: "🟢",
            SurvivalMode.RICH: "💎",
        }.get(mode, "❓")

        burn_rate = self.get_daily_burn_rate()
        runway = self.get_runway_days()

        lines = [
            f"{'='*40}",
            f"💰 CFO 财务简报 {mode_icon} [{mode}]",
            f"{'='*40}",
            f"  💵 当前余额:  ${self.balance:.2f}",
            f"  🔥 今日消耗:  ${self.today_spend:.4f}",
            f"  💵 今日收入:  ${self.today_revenue:.4f}",
            f"  📈 日均燃烧:  ${burn_rate:.4f}/天",
            f"  ⏳ 预估跑道:  {runway} 天",
            f"  🧠 推荐算力:  {self.get_recommended_tier()}",
            f"{'='*40}",
        ]
        return "\n".join(lines)

    def get_blackboard_facts(self) -> dict[str, str]:
        """生成应写入黑板的三项核心指标"""
        return {
            "current_balance": f"${self.balance:.2f}",
            "daily_burn_rate": f"${self.get_daily_burn_rate():.4f}/day",
            "projected_runway": f"{self.get_runway_days()} days",
            "survival_mode": self.get_survival_mode(),
        }

    def get_recent_transactions(self, limit: int = 10) -> list[dict]:
        """获取最近 N 条交易记录"""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT timestamp, type, amount, description, balance_after FROM transactions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [
            {
                "time": r[0][:16],
                "type": r[1],
                "amount": f"${r[2]:+.4f}",
                "description": r[3],
                "balance": f"${r[4]:.2f}",
            }
            for r in rows
        ]
