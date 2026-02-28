"""
AOS 2.4+ 进阶经验引擎 (Advanced Experience Engine)
核心功能：变量抽象、经验衰减、负面模式记录与财务风控对接。
"""

import json
import os
import logging
from datetime import datetime
import re

logger = logging.getLogger(__name__)

class ExperienceEngine:
    """
    认知级经验仓库：支持模板匹配、置信度评估与自愈。
    """

    def __init__(self, persist_path: str = "memories/experience.json"):
        self.persist_path = persist_path
        # 结构: { "pattern_id": { "template": "...", "plan": {...}, "success_rate": 1.0, "hit_count": 0, "is_negative": False } }
        self.experiences: dict[str, dict] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.persist_path):
            try:
                with open(self.persist_path, "r", encoding="utf-8") as f:
                    self.experiences = json.load(f)
                logger.info("🧠 [经验引擎] 已加载 %d 条历史经验 (AOS 2.4+)", len(self.experiences))
            except Exception as e:
                logger.error("经验库加载失败: %s", e)
                self.experiences = {}

    def _save(self):
        os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)
        with open(self.persist_path, "w", encoding="utf-8") as f:
            json.dump(self.experiences, f, ensure_ascii=False, indent=2)

    def match_plan(self, demand: str) -> dict | None:
        """
        匹配成功经验。支持置信度过滤与关键字匹配。
        """
        demand_low = demand.lower()
        
        # 1. 检查负面模式 (Anti-Patterns)
        for key, exp in self.experiences.items():
            if exp.get("is_negative", False):
                # 如果需求包含负面关键词，拒绝
                if key in demand_low:
                    logger.warning("🚫 [避坑指南] 检测到负面模式，该路径曾多次失败: %s", key)
                    return None

        # 2. 增强型匹配逻辑：关键词相交
        # 将需求拆分为关键词
        demand_keywords = set(re.findall(r'\w+', demand_low))
        
        best_match = None
        highest_score = 0
        
        for key, exp in self.experiences.items():
            if exp.get("is_negative", False): continue
            
            # 置信度检查
            if exp.get("success_rate", 1.0) < 0.4:
                continue

            # 计算匹配得分：重合关键词比例
            exp_keywords = set(re.findall(r'\w+', key))
            if not exp_keywords: continue
            
            intersection = demand_keywords.intersection(exp_keywords)
            score = len(intersection) / len(exp_keywords)
            
            # 只有当匹配度 > 70% 时才认为是同一类任务
            if score > 0.7 and score > highest_score:
                highest_score = score
                best_match = exp

        if best_match:
            logger.info("🎯 [进入快路径] 匹配度 %.2f: %s", highest_score, best_match.get("template", "未知"))
            best_match["hit_count"] = best_match.get("hit_count", 0) + 1
            best_match["last_used"] = datetime.now().isoformat()
            self._save()
            return best_match["plan"]
        
        return None

    def record_success(self, demand: str, plan: dict):
        """记录/更新成功方案"""
        key = demand.lower()
        if key in self.experiences:
            exp = self.experiences[key]
            # 强化置信度
            exp["success_rate"] = min(1.0, exp.get("success_rate", 1.0) + 0.1)
            exp["hit_count"] += 1
        else:
            self.experiences[key] = {
                "template": demand, # 暂时存原始需求，后期可做变量剥离
                "plan": plan,
                "created_at": datetime.now().isoformat(),
                "last_used": datetime.now().isoformat(),
                "hit_count": 1,
                "success_rate": 1.0,
                "is_negative": False
            }
        self._save()
        logger.info("🎓 [经验升级] 已记录/强化成功方案: %s", key)

    def record_failure(self, demand: str):
        """记录失败：实现“经验衰减”与“负面模式”"""
        key = demand.lower()
        if key in self.experiences:
            exp = self.experiences[key]
            # 经验衰减 (Decay)
            exp["success_rate"] = max(0.0, exp.get("success_rate", 1.0) - 0.3)
            if exp["success_rate"] < 0.2:
                exp["is_negative"] = True
                logger.error("💀 [经验坍缩] 方案连续失败，已标记为负面模式 (Blacklist)")
        else:
            # 新的失败记录
            self.experiences[key] = {
                "created_at": datetime.now().isoformat(),
                "success_rate": 0.5,
                "is_negative": (demand.count("failed") > 2), # 简单启发式
                "fail_log": "Initial failure recorded"
            }
        self._save()

    def get_negative_patterns(self) -> str:
        """获取所有已知的负面模式，用于冷启动避坑"""
        negatives = [k for k, v in self.experiences.items() if v.get("is_negative")]
        if not negatives: return ""
        return "\n【避坑避雷区】:\n- " + "\n- ".join(negatives)

    def list_experiences(self) -> list:
        return [
            {
                "id": key[:50] + "..." if len(key) > 50 else key,
                "status": "PASS" if not exp.get("is_negative") else "FAIL/BLACKLIST",
                "rate": f"{exp.get('success_rate', 1.0)*100:.0f}%",
                "matches": exp.get("hit_count", 0),
                "last": exp.get("last_used", "N/A")[:10]
            }
            for key, exp in self.experiences.items()
        ]
