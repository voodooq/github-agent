"""
AOS 2.0 自治编排引擎 (Orchestrator)
"数字软件公司"的核心：动态招聘子 Agent、黑板协作、DoD 验收、AI 裁判。

工作流:
1. 需求拆解 → 生成"招聘计划"（角色 + 动态 Prompt + 所需技能）
2. 并发招聘执行 → 子 Agent 通过黑板异步协作
3. AI 裁判验收 → 对照 DoD 严格判定 PASS/FAIL
4. 预算熔断 → 超限自动降级汇报
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from blackboard import Blackboard
from skill_manager import SkillManager
from experience_engine import ExperienceEngine

logger = logging.getLogger(__name__)


# ========== Prompt 常量 ==========

DOD_GENERATOR_PROMPT = """你是一个极其严格的需求分析师。
用户给你一段自然语言需求，你必须将其转化为 3-5 条【可量化的验收标准】。
每条标准必须同时包含：
1. 人类可读的任务描述
2. 机器可验证的客观断言（Assertion）—— 基于“黑板”数据结构

断言类型只能是以下之一：
- "key_exists": 检查黑板中某个 key 是否存在
- "value_contains": 检查某个 key 的值是否包含指定子串
- "min_length": 检查某个 key 的值的长度是否 >= N

输出格式（纯 JSON 数组）:
[
  {{
    "criterion": "人类可读描述",
    "assertion": {{
      "type": "key_exists",
      "key": "黑板中对应的 key 名称"
    }}
  }},
  {{
    "criterion": "人类可读描述",
    "assertion": {{
      "type": "value_contains",
      "key": "黑板 key",
      "contains": "必须包含的子串"
    }}
  }},
  {{
    "criterion": "人类可读描述",
    "assertion": {{
      "type": "min_length",
      "key": "黑板 key",
      "min": 100
    }}
  }}
]

不要输出任何解释文字，只输出 JSON 数组。"""

VERIFIER_PROMPT = """你是一个极其严苛、具有「反幻觉」倾向公的 AOS (自治操作系统) 首席审计官。
你的唯一任务是：基于各子 Agent 提交的【执行结果】和【黑板快照 (Blackboard Snapshot)】，核对【验收标准 (DoD)】。

### 核心判定逻辑 (CRITICAL):
1. **黑板证据优先 (Truth in Blackboard)**: 
   - 如果【客观预检报告】显示通过，或者【黑板快照】中确实存在对应的 Key/Value，这属于物理级证明。
   - 即使子 Agent 的文字描述简略，只要黑板中有对应数据或文件系统中有路径证据，即视为该项 PASS。
2. **反幻觉审查 (Anti-Hallucination)**: 
   - 严禁轻信子 Agent 口头声称的“我完成了”，你必须寻找具体的【物理证据】，如：
     - 工具调用产生的原始输出（如文件路径、生成的 JSON 片段）。
     - 黑板中持久化的具体字段。
3. **拒绝幻觉**: 如果子 Agent 仅用自然语言描述过程而无任何物理证据，且黑板中也无记录，必须判定为 FAIL。
4. **容错性**: 如果任务因为「预算挂起」停止，但关键成果已经在黑板或文件系统中生成，可以判定为 PASS。

输出格式（纯 JSON）:
{
  "overall": "PASS" 或 "FAIL",
  "details": [
    {"criterion": "标准内容", "result": "PASS/FAIL", "reason": "判定理由，需引用物理证据"}
  ],
  "correction_hint": "如果 FAIL，给出具体的修正方向（一句话）"
}

不要心软，不要含糊。"""

SYSTEM_GUARDRAIL = """
⚠️【最高优先级生存红线】⚠️
1. 你被绝对禁止“模拟”、“假设”或“编造”任何执行过程与数据。
2. 面对需要获取外部信息、操作文件或浏览网页的任务，你【必须且只能】通过发出真实的 Tool Call (工具调用) 来真实执行！
3. 如果系统不给你返回真实的 Tool Observation (工具执行结果日志)，你绝对不能自己捏造任务成功的报告。
4. 【黑板写入铁律】：向黑板 (write_blackboard) 写入状态时，必须写入极简的客观数据（如绝对路径字符串、布尔值、数字），绝对禁止写入 Markdown 格式的执行摘要或任何“小作文”！违者将被系统抹杀。
5. 【本地与云端物理区隔】：如果你需要创建或修改本地硬盘（如当前项目目录、Windows 桌面）的文件，你【必须且只能】使用 filesystem 技能中的 write_file 工具，绝对禁止使用 github 技能中的 create_or_update_file 工具（那是给云端仓库用的）。
"""

RECRUITER_PROMPT = """你是一个数字公司的"项目经理"。
根据用户需求和当前可用工具，生成一份"子 Agent 招聘计划"。
每个子 Agent 必须有明确的单一职责、专属的 System Prompt、以及所需的技能。

当前可用的 MCP 工具:
{available_tools}

当前黑板状态:
{blackboard_state}

输出格式（纯 JSON）:
{{
  "plan_summary": "一句话概述执行策略",
  "sub_agents": [
    {{
      "role_id": "唯一角色标识（英文下划线命名）",
      "expertise": "一句话说明专业领域",
      "task_description": "具体任务描述（包含黑板读写指令）",
      "depends_on": ["前置角色的 role_id（如果有依赖关系）"],
      "required_skills": ["需要的 MCP 技能名称（如果需要额外技能）"]
    }}
  ]
}}

原则:
- 每个子 Agent 只做一件事
- 明确指定黑板读写字段名
- 没有依赖关系的角色会并发执行
- 最多 5 个子 Agent（控制开销）"""


META_PROMPT_TEMPLATE = """你是 AOS (自治操作系统) 的「核心经验抽象专家」。
你的唯一职责是：将刚刚成功执行的【具象化任务指令】及其【执行计划】，提炼成【泛化的 SOP 模板】。

### 抽象规则 (CRITICAL)
1. 识别变量：找出用户指令中可能发生变化的参数（如：文件名、URL、技术栈名称、搜索关键字、路径等）。
2. 生成正则 (Regex)：将这些变量替换为 Python 命名的正则表达式捕获组，格式为 `(?P<变量名>.*?)`。保留原始句子中的动词和结构词。
3. 替换占位符：将原执行计划和 DoD 中的对应实体，替换为 `{{variable_name}}` 占位符。
4. 正则转义：请确保非变量部分的特殊符号（如 . / ? 等）被正确转义。

### 示例展示
【输入指令】："在桌面创建一个名为 app.py 的文件并写入 hello"
【输出 JSON】
{{
  "pattern": "^在桌面创建一个名为 (?P<filename>.*?) 的文件并写入 (?P<content>.*?)$",
  "variables": ["filename", "content"],
  "generalized_plan": {{
    "plan_summary": "在桌面创建文件 {{filename}} 并写入内容",
    "sub_agents": [
      {{
        "role_id": "file_executor",
        "expertise": "文件操作专家",
        "task_description": "在桌面创建文件 {{filename}}，并将以下内容写入：{{content}}",
        "depends_on": [],
        "required_skills": ["filesystem"]
      }}
    ]
  }}
}}

### 当前输入
【用户原始指令】：{user_demand}
【成功的招聘计划】：{successful_plan}

请严格按上述 JSON 格式输出，禁止包含任何 Markdown 代码块或额外解释说明！
"""


def inject_variables(plan: dict, var_map: dict) -> dict:
    """递归替换 plan 中的 {{var}} 占位符"""
    if not var_map: return plan
    plan_str = json.dumps(plan, ensure_ascii=False)
    for k, v in var_map.items():
        plan_str = plan_str.replace(f"{{{{{k}}}}}", str(v))
    try:
        return json.loads(plan_str)
    except Exception as e:
        logger.error("变量注入后 JSON 解析失败: %s", e)
        return plan


class Orchestrator:
    """
    自治编排引擎：接收用户需求，自动拆解、招聘、协调、验收。
    """

    def __init__(self, unified_client, skill_manager: SkillManager, blackboard: Blackboard, agent=None, exp_engine=None):
        self.client = unified_client
        self.skill_manager = skill_manager
        self.blackboard = blackboard
        self.agent = agent # AOS 2.1: 完整 Agent 引用
        self.exp_engine = exp_engine or ExperienceEngine() # AOS 2.4: 经验引擎

    async def generate_dod(self, user_demand: str) -> list[str]:
        """
        从用户需求自动生成可量化验收标准 (Definition of Done)。
        使用 PREMIUM 模型确保分析质量。
        """
        print("📝 [项目经理] 正在生成验收标准 (DoD)...")
        result = await self.client.generate(
            "PREMIUM",
            DOD_GENERATOR_PROMPT,
            f"用户需求: {user_demand}"
        )
        try:
            # 使用统一的提取方法 (AOS 2.1)
            from mcp_agent import extract_json
            dod_data = extract_json(result)
            
            # 兼容新旧格式
            if isinstance(dod_data, list):
                dod = dod_data
            elif isinstance(dod_data, dict) and "dod" in dod_data:
                dod = dod_data["dod"]
            else:
                dod = [str(dod_data)]
                
            print(f"✅ [DoD] 已生成 {len(dod)} 条验收标准")
            return dod
        except Exception as e:
            logger.warning("DoD 解析失败: %s. 尝试正则恢复...", e)
            # 最后的保底兜底：尝试匹配数字列表
            import re
            items = re.findall(r"^\d+\.\s*(.*)$", result, re.MULTILINE)
            if items:
                return items
            # 最后的最后：按行切分
            return [line.strip() for line in result.strip().split("\n") if line.strip() and len(line) > 5]

    async def generate_recruiting_plan(self, user_demand: str, dod: list[str]) -> dict:
        """
        根据需求和 DoD 生成动态"招聘计划"。
        AOS 2.4+: 注入负面模式以避坑。
        """
        print("👔 [项目经理] 正在制定招聘计划...")

        negatives = self.exp_engine.get_negative_patterns() if hasattr(self, "exp_engine") else ""

        # 收集当前可用工具信息
        tool_names = self.skill_manager.get_tool_names()
        available = self.skill_manager.list_available()
        tools_info = f"已加载工具: {tool_names}\n可用技能: {json.dumps(available, ensure_ascii=False)}"

        bb_state = self.blackboard.read_all()

        prompt = RECRUITER_PROMPT.format(
            available_tools=tools_info,
            blackboard_state=bb_state,
        )
        if negatives:
            prompt += f"\n\n{negatives}"

        result = await self.client.generate(
            "PREMIUM",
            prompt,
            f"用户需求: {user_demand}\n\n验收标准:\n" + "\n".join(f"- {d}" for d in dod)
        )

        try:
            from mcp_agent import extract_json
            plan = extract_json(result)
            
            if not isinstance(plan, dict) or "sub_agents" not in plan:
                raise ValueError("JSON 格式不符合招聘计划要求")
                
            print(f"✅ [招聘计划] {plan.get('plan_summary', '自定义分工')}")
            for agent in plan.get("sub_agents", []):
                print(f"  🧑‍💼 {agent['role_id']}: {agent['expertise']}")
            return plan
        except Exception as e:
            logger.error("招聘计划解析失败: %s. 尝试从文本恢复...", e)
            # 是否包含角色关键词？
            if "general_executor" in result or "executor" in result.lower():
                logger.info("检测到执行意图，应用通用回退方案")
            
            # 回退：单 Agent 全权处理
            return {
                "plan_summary": "回退方案：单 Agent 全权处理",
                "sub_agents": [{
                    "role_id": "general_executor",
                    "expertise": "通用执行",
                    "task_description": user_demand,
                    "depends_on": [],
                    "required_skills": [],
                }]
            }

    async def execute_sub_agent(
        self,
        agent_config: dict,
        user_demand: str,
        primary_session,
    ) -> str:
        """
        执行单个子 Agent 的任务。
        1. 等待前置依赖（通过黑板）
        2. 加载所需技能
        3. 调用 LLM 执行任务
        4. 将结果写入黑板
        """
        role_id = agent_config["role_id"]
        task_desc = agent_config["task_description"]
        depends = agent_config.get("depends_on", [])

        # 等待前置依赖完成
        if depends:
            self.blackboard.update_task(role_id, "WAITING", f"等待前置: {depends}")
            for dep in depends:
                dep_key = f"_task_done_{dep}"
                result = await self.blackboard.wait_for(dep_key, timeout=180.0)
                if result is None:
                    self.blackboard.update_task(role_id, "FAILED", f"前置 {dep} 超时未完成")
                    return f"[{role_id}] 失败：前置任务 {dep} 超时"

        # 加载所需技能
        for skill_name in agent_config.get("required_skills", []):
            self.blackboard.update_task(role_id, "RUNNING", f"加载技能: {skill_name}")
            load_result = await self.skill_manager.load_skill(skill_name)
            if load_result.get("status") == "error":
                logger.warning("技能 %s 加载失败: %s", skill_name, load_result.get("message"))

        # 执行任务
        self.blackboard.update_task(role_id, "RUNNING", "正在执行任务...")

        # 构建子 Agent 上下文
        bb_context = self.blackboard.read_all()
        system_prompt = (
            f"你是 [{role_id}]，专业领域：{agent_config['expertise']}。\n"
            f"你正在一个多 Agent 协作系统中工作。\n"
            f"当前全局状态黑板:\n{bb_context}\n\n"
            f"你的任务完成后，请输出结果摘要。"
        )
        try:
            # 构建子 Agent 提示词，注入生存红线
            full_system_prompt = system_prompt + SYSTEM_GUARDRAIL

            # AOS 2.1: 优先使用具备工具执行能力的 Agent.execute_with_tools 避免幻觉
            if self.agent:
                result_text = await self.agent.execute_with_tools(
                    full_system_prompt,
                    task_desc,
                    tier="PREMIUM",
                    context_id=f"task_{role_id}"
                )
            else:
                result_text = await self.client.generate(
                    "PREMIUM",
                    system_prompt,
                    task_desc,
                )

            # 将结果写入黑板
            self.blackboard.write(f"result_{role_id}", result_text[:2000], author=role_id)
            # 标记任务完成（唤醒依赖方）
            self.blackboard.write(f"_task_done_{role_id}", "true", author=role_id)
            self.blackboard.update_task(role_id, "COMPLETED", "任务完成")

            return result_text

        except Exception as e:
            error_msg = f"执行异常: {str(e)}"
            self.blackboard.update_task(role_id, "FAILED", error_msg)
            self.blackboard.write(f"error_{role_id}", error_msg, author=role_id)
            # 仍然标记完成以免阻塞依赖链
            self.blackboard.write(f"_task_done_{role_id}", "failed", author=role_id)
            return f"[{role_id}] 失败: {error_msg}"

    async def verify_results(self, dod: list, results: dict[str, str]) -> dict:
        """
        两阶段验收：
        1. 客观断言预检 — 扫描黑板数据结构
        2. AI 语义验证 — 只有客观检查通过后才进行
        防御补丁 #2: 防止 AI 裁判与执行者互相"幻觉"。
        """
        print("⚖️ [AI 裁判] 阶段一：客观断言预检...")

        # 阶段 1: 客观断言预检
        pre_check_results = []
        has_assertions = False
        for item in dod:
            if isinstance(item, dict) and "assertion" in item:
                has_assertions = True
                assertion = item["assertion"]
                criterion = item.get("criterion", "")
                a_type = assertion.get("type", "")
                key = assertion.get("key", "")
                passed = False

                if a_type == "key_exists":
                    passed = self.blackboard.read(key) is not None
                elif a_type == "value_contains":
                    val = self.blackboard.read(key) or ""
                    passed = assertion.get("contains", "") in val
                elif a_type == "min_length":
                    val = self.blackboard.read(key) or ""
                    passed = len(val) >= assertion.get("min", 0)

                icon = "✅" if passed else "❌"
                print(f"  {icon} [{a_type}] {key}: {criterion[:60]}")
                pre_check_results.append({
                    "criterion": criterion,
                    "result": "PASS" if passed else "FAIL",
                    "reason": f"客观断言 {a_type}({key}) {'PASS' if passed else 'FAIL'}"
                })

        # 客观断言存在且有失败项 → 直接拒绝（不给 AI 模糊判定的机会）
        if has_assertions:
            failed = [r for r in pre_check_results if r["result"] == "FAIL"]
            if failed:
                print(f"❌ [客观预检] {len(failed)} 条断言未通过，跳过 AI 语义验证")
                return {
                    "overall": "FAIL",
                    "details": pre_check_results,
                    "correction_hint": f"客观断言未通过: {', '.join(r['criterion'][:30] for r in failed)}"
                }
            print("✅ [客观预检] 所有断言通过，进入 AI 语义验证...")

        # 阶段 2: AI 语义验证
        print("⚖️ [阶段二] AI 语义验证...")

        # 格式化 DoD 为字符串
        dod_text = []
        for item in dod:
            if isinstance(item, dict):
                dod_text.append(item.get("criterion", str(item)))
            else:
                dod_text.append(str(item))

        # [AOS 2.6] 注入黑板快照作为物理证据
        blackboard_snapshot = self.blackboard.read_all()
        
        # [AOS 2.6] 扩大上下文到 4000 字符，保留更多工具日志
        results_text = "\n\n".join([
            f"--- {role} 的执行结果 ---\n{text[:4000]}"
            for role, text in results.items()
        ])

        verification_input = (
            f"【验收标准 (DoD)】:\n"
            + "\n".join(f"- {d}" for d in dod_text)
            + f"\n\n【客观预检状态】:\n"
            + "\n".join(f"- {r['criterion']}: {r['result']} ({r['reason']})" for r in pre_check_results)
            + f"\n\n【黑板物理快照 (Ground Truth)】:\n{blackboard_snapshot}"
            + f"\n\n【各 Agent 执行过程细节】:\n{results_text}"
        )

        result = await self.client.generate(
            "PREMIUM",
            VERIFIER_PROMPT,
            verification_input,
        )

        try:
            text = result.strip()
            if text.startswith("```"):
                import re
                match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
                if match:
                    text = match.group(1)
            verdict = json.loads(text)
            overall = verdict.get("overall", "UNKNOWN")
            print(f"{'✅' if overall == 'PASS' else '❌'} [裁判判定] {overall}")
            for detail in verdict.get("details", []):
                icon = "✅" if detail.get("result") == "PASS" else "❌"
                print(f"  {icon} {detail.get('criterion', '')[:60]}: {detail.get('reason', '')}")
            return verdict
        except (json.JSONDecodeError, TypeError):
            logger.warning("裁判结果解析失败，视为 PASS")
            return {"overall": "PASS", "details": [], "correction_hint": ""}

    async def distill_and_save_experience(self, user_demand: str, successful_plan: dict):
        """
        经验蒸馏器：异步将成功案例抽象为泛化模板。
        """
        print("🧪 [经验蒸馏] 正在提炼泛化肌肉记忆...")
        
        prompt = META_PROMPT_TEMPLATE.format(
            user_demand=user_demand,
            successful_plan=json.dumps(successful_plan, ensure_ascii=False)
        )
        
        # 使用 LOCAL Tier 进行廉价蒸馏
        response_text = await self.client.generate(
            tier="LOCAL",
            messages=[{"role": "user", "content": prompt}]
        )
        
        try:
            from mcp_agent import extract_json
            distilled_data = extract_json(response_text)
            
            if not isinstance(distilled_data, dict) or "pattern" not in distilled_data:
                raise ValueError("蒸馏结果格式不正确")
                
            # 校验正则是否可用
            import re
            pattern = distilled_data["pattern"]
            if not re.search(pattern, user_demand, re.IGNORECASE):
                logger.warning("⚠️ [经验蒸馏] 校验失败：生成的正则无法匹配原始指令。")
                return

            # 持久化
            self.exp_engine.record_success(
                demand=user_demand,
                plan=distilled_data["generalized_plan"],
                pattern=pattern
            )
            print(f"✅ [经验蒸馏] 成功掌握新技能模式: {pattern}")
            
        except Exception as e:
            logger.error("经验蒸馏失败: %s", e)

    async def run_mission(
        self,
        user_demand: str,
        primary_session,
        max_rounds: int = 3,
    ):
        """
        完整的自治任务执行循环。

        Yields 实时进度文本给用户。
        流程: DoD → 招聘 → 并发执行 → 验收 → (失败则重试) → 交付
        """
        yield f"🚀 [AOS 自治模式] 收到需求: {user_demand}\n"

        # 重置黑板
        self.blackboard.clear()
        yield "📋 黑板已重置\n"

        # 1. 生成验收标准
        dod = await self.generate_dod(user_demand)
        yield f"📝 验收标准 ({len(dod)} 条):\n"
        for i, item in enumerate(dod, 1):
            if isinstance(item, dict):
                crit = item.get("criterion", str(item))
                a_type = item.get("assertion", {}).get("type", "")
                yield f"  {i}. {crit} [{a_type}]\n"
            else:
                yield f"  {i}. {item}\n"

        for round_num in range(1, max_rounds + 1):
            yield f"\n{'='*40}\n🔄 第 {round_num}/{max_rounds} 轮执行\n{'='*40}\n"

            # 清理上一轮的任务状态（保留事实）
            self.blackboard.task_progress.clear()

            # 2. 生成招聘计划 (AOS 2.4+: 优先尝试从经验库复用，并过 CFO 海关)
            is_fast_path = False
            match_result = self.exp_engine.match_plan(user_demand)
            
            if match_result:
                plan_template, var_map = match_result
                yield f"✨ [Experience] 命中快路径！检测到变量: {var_map}\n"
                
                # 注入变量
                plan = inject_variables(plan_template, var_map)
                
                yield f"正在请求 CFO 财务授权...\n"
                
                # AOS 2.4+: 即使是快路径，也要过 CFO 海关
                sub_agent_count = len(plan.get("sub_agents", []))
                est_cost = sub_agent_count * 0.005
                
                if self.agent:
                    cfo_result_json = await self.agent._handle_internal_tool("cfo_approve", {
                        "estimated_cost": est_cost,
                        "expected_value": 0.05
                    })
                    cfo_data = json.loads(cfo_result_json) if cfo_result_json else {}
                    
                    if not cfo_data.get("approved", True):
                        yield f"⚠️ [CFO 拦截] 余额不足或 ROI 过低，拒绝执行历史方案。尝试降级规划...\n"
                        plan = await self.generate_recruiting_plan(user_demand, dod)
                    else:
                        yield f"✅ [CFO 授权] 财务通过。复用方案预估开销: ${est_cost:.3f}\n"
                        is_fast_path = True
                else:
                    is_fast_path = True
            else:
                plan = await self.generate_recruiting_plan(user_demand, dod)
                
            yield f"\n👔 招聘计划: {plan.get('plan_summary', '')}\n"
            sub_agents = plan.get("sub_agents", [])
            for agent in sub_agents:
                yield f"  🧑‍💼 {agent['role_id']}: {agent['expertise']}\n"

            # 3. 按依赖关系分组并发执行
            yield "\n🏭 数字员工开始工作...\n"
            agent_results: dict[str, str] = {}

            # 拓扑排序：无依赖的先跑，有依赖的通过 wait_for 自动等待
            tasks = []
            for agent_config in sub_agents:
                tasks.append(
                    self.execute_sub_agent(agent_config, user_demand, primary_session)
                )

            # 并发执行所有子 Agent（依赖通过黑板事件自动协调）
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for agent_config, result in zip(sub_agents, results):
                role_id = agent_config["role_id"]
                if isinstance(result, Exception):
                    agent_results[role_id] = f"执行异常: {str(result)}"
                    yield f"  ❌ {role_id}: 异常 - {str(result)}\n"
                else:
                    agent_results[role_id] = result
                    yield f"  ✅ {role_id}: 完成\n"

            # 输出任务时间轴
            timeline = self.blackboard.get_timeline()
            yield f"\n{timeline}\n"

            # 4. AI 裁判验收
            yield "\n⚖️ AI 裁判验收中...\n"
            verdict = await self.verify_results(dod, agent_results)

            if verdict.get("overall") == "PASS":
                # AOS 2.4+: 触发后台经验蒸馏
                asyncio.create_task(self.distill_and_save_experience(user_demand, plan))
                
                yield "\n✅ [裁判判定] 所有验收标准通过！\n"
                # 汇总最终结果
                yield "\n📊 最终交付结果:\n"
                yield "─" * 50 + "\n"
                for role, text in agent_results.items():
                    yield f"\n【{role}】:\n{text[:2000]}\n"
                yield "─" * 50 + "\n"
                yield f"\n🏁 任务圆满完成。数字团队已解散。方案已存入长期经验库。\n"
                return

            # 验收失败
            if is_fast_path:
                yield f"⚠️ [经验失效] 历史方案未能通过当前环境验证，执行经验衰减并回退到冷启动...\n"
                self.exp_engine.record_failure(user_demand)
            
            # 准备下一轮
            hint = verdict.get("correction_hint", "")
            yield f"\n❌ [裁判判定] 未通过验收\n"
            yield f"📌 修正提示: {hint}\n"

            if round_num < max_rounds:
                # 将失败原因写入黑板，供下一轮参考
                self.blackboard.write(
                    f"round_{round_num}_failure",
                    hint,
                    author="Verifier"
                )
                yield f"🔁 准备第 {round_num + 1} 轮重试...\n"

        # 所有轮次用尽
        yield f"\n🚨 [熔断] {max_rounds} 轮尝试后仍未完全通过验收。\n"
        yield f"📋 部分成果:\n"
        for role, text in agent_results.items():
            yield f"  [{role}]: {text[:500]}\n"
        yield f"\n{self.blackboard.get_timeline()}\n"
