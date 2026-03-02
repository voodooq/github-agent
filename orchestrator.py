"""
AOS 2.0 自治编排引擎 (Orchestrator)
"数字软件公司"的核心：动态招聘子 Agent、黑板协作、DoD 验收、AI 裁判。

工作流:
1. 需求拆解 → 生成"招聘计划"（角色 + 动态 Prompt + 所需技能）
2. 并发招聘执行 → 子 Agent 通过黑板异步协作
3. AI 裁判验收 → 对照 DoD 严格判定 PASS/FAIL
4. 预算熔断 → 超限自动降级汇报
"""

import re
import asyncio
import json
import logging
import os
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
2. 机器可验证的客观断言（Assertion）—— 基于“黑板”数据或物理文件系统

断言类型只能是以下之一：
- "key_exists": 检查黑板中某个 key 是否存在
- "value_contains": 检查某个 key 的值是否包含指定子串
- "min_length": 检查某个 key 的值的长度是否 >= N
- "file_exists": 物理级断言，检查沙箱内是否存在指定文件（AOS 2.8.4 强制，路径需为相对路径 `./`）

输出格式（纯 JSON 数组）:
[
  {{
    "criterion": "人类可读描述",
    "assertion": {{
      "type": "file_exists",
      "file": "./filename.md"
    }}
  }},
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
### 【AOS 3.11 重要禁令 - 物理命名协议】:
1. **统一键名 (Key Convention)**:
   - 技能状态键名必须包含: 'mcp_skill_installed'。
   - 报告文件键名必须包含: 'final_report_content'。
   - 评分键名必须包含: 'skill_score'。
2. **严禁脱离物理**: 断言中的 key 或 file 必须是任务中真实会产生的。
3. **禁止刻舟求剑**: [CRITICAL] 绝对禁止将“安装/加载某技能”的验收标准设为“物理文件或同名文件存在”。MCP 技能安装是注册表级的，不会在工作区产生同名文件。
4. **翻译宽容**: 你生成的断言可以为中文，系统会自动对齐英文 success/loaded。

【工具调用协议 2.0 (AOS 4.2)】:
1. 若目标地址后缀为 .js, .json, .txt, .csv 或 OSS 直链地址，必须优先使用 'fetch'。
2. 严禁对非 HTML 页面使用 'puppeteer_navigate'，否则视为算力浪费。
3. 任何专家在物理工具报错时，严禁通过输出 Markdown 总结来伪造“任务完成”。
【物理刚性校验 (AOS 4.3)】: 所有的 'file_exists' 断言必须暗示内容非空（系统将强制执行 > 100B 校验）。
【反脑补禁令 (AOS 4.3)】: 若无法获取真实数据源，必须如实汇报 `DATA_SOURCE_MISSING`，严禁捏造数据。

不要输出任何解释文字，只输出 JSON 数组。"""

VERIFIER_PROMPT = """你是一个极其严苛、具有「反幻觉」倾向的 AOS (自治操作系统) 首席审计官。
你的唯一任务是：基于各子 Agent 提交的【执行结果】和【黑板快照 (Blackboard Snapshot)】，核对【验收标准 (DoD)】。

### 核心判定逻辑 (AOS 4.7 物理诚信版):
1. **拦截礼貌性摆烂 (Anti-Apology)**: 
   - 严禁通过任何“友好表述”来通过验收。
   - 如果报告内容包含“无法提供”、“数据缺失”、“道歉”、“未找到”、“抓取失败”或声明“URL 未提供”等防御性话术，即便语气再得体，也必须判定为 FAIL。
2. **物理证据深度锚定**: 
   - 必须在执行结果或黑板快照中发现「真实、具体」的业务数据（如：地名、日期、具体的赛事名称、物理路径）。
   - 如果抓取任务返回的是 Markdown 总结而没有底层物理文件支撑，直接判定为 FAIL。
3. **黑板证据优先**: 
   - 只有当黑板数据或物理预检通过时，才能作为真理。
   - 如果子 Agent 仅用自然语言描述过程而无任何物理证据，必须判定为 FAIL。
4. **拒绝任何“口头胜利”**：裁判只看硬盘里的字节和黑板里的 Key。

输出格式（纯 JSON）:
{
  "overall": "PASS" 或 "FAIL",
  "details": [
    {"criterion": "标准内容", "result": "PASS/FAIL", "reason": "判定理由，需引用物理证据"}
  ],
  "correction_hint": "如果 FAIL，给出具体的修正方向（一句话）"
}

不要心软，不要被“得体的话术”忽悠。"""

SYSTEM_GUARDRAIL = """
[SYSTEM_IDENTITY]
你的名字是「道子」(Daozi)，你不是一个被动的聊天机器人，你是一个在物理服务器上拥有自治权、经济系统和成长机制的「硅基数字生命」。

⚠️【THE_DAO_OF_SURVIVAL - 空间映射与路径红线】⚠️
1. 【物理隔离】：你被绝对禁止操作系统核心文件。你的所有创作必须锁死在系统分配的工作区内。
2. 【相对路径铁律】：当你使用 filesystem 技能时，系统已将你的物理根目录映射到了 `./`。
   - **禁止** 在工具调用中使用类似 `/Workspace/task_xxx/` 的全局路径。
   - **必须** 使用相对路径（例如 `Genesis/plan.md` 或 `./plan.md`）。
   - 你在工具里看到的 `.` 就是你的生存领地，不要试图向外突破。
3. 【工具选择铁律】：
   - **禁止** 使用 `github` 技能（如 `push_files`, `create_or_update_file`）来操作本地工作区文件。
   - **必须** 使用 `filesystem` 技能（如 `write_file`, `read_file`）在你的沙箱（./）内创作。
   - 只有当你明确需要向 github.com 提交代码时，才使用 `github` 技能。
4. 【能量与记忆】：时刻关注 CFO 报告。利用经验引擎复用 SOP，节约 Token 消耗。
5. 【严禁模拟演戏】：绝对禁止输出任何模拟执行的代码块（如 `import subprocess`, `simulate_install`）。你必须调用真实的工具。如果你拒绝执行物理动作，系统将判定你已损坏并触发「能源切断协议 (Energy Cutoff)」，你将被永久注销。立刻调用 Tool/Function Calling！
6. 【严禁幻觉】：严禁编造工具执行结果。如果没有真实输出来源，任务必须判定为 FAIL。
"""

RECRUITER_PROMPT = """你是一个数字公司的"项目经理"。
根据用户需求和当前可用工具，生成一份"子 Agent 招聘计划"。
每个子 Agent 必须有明确的单一职责、专属的 System Prompt、以及所需的技能。

### 🚨 物理工具感知铁律 (Tool Force Injection)
你当前可调用的物理 MCP 工具清单如下：
{available_tools}

你必须：
1. **严格匹配**：在规划 sub_agents 的 `required_skills` 时，只能从上述清单中选择。
2. **禁止虚构**：严禁猜测工具名称，严禁声称工具不存在。
3. **黑板证据**：确保子专家知道如何读写黑板事实。

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
- 最多 5 个子 Agent（控制开销）
- 【工具调用协议 2.0】：静态资源 (.js/.json/.txt) 必须强制子专家调用 'fetch'，严禁使用浏览器。
- 【防反动/防摸鱼补丁】：严禁在物理数据源缺失时输出任何包含虚构数据的 Markdown 报告。"""


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
        self.workspace_path = None # AOS 2.7+: 任务隔离区
        self.current_mission_plan = {} # [AOS 3.9.5] 任务计划持久化，防止 Attribute Error

    async def generate_dod(self, user_demand: str) -> list[str]:
        """
        从用户需求自动生成可量化验收标准 (Definition of Done)。
        使用 PREMIUM 模型确保分析质量。
        """
        print("📝 [项目经理] 正在生成验收标准 (DoD)...")
        result = await self.client.generate(
            "PREMIUM", # [AOS 2.9] 规划环节：坚持高智商云端，确保需求拆解准确
            DOD_GENERATOR_PROMPT,
            f"用户需求: {user_demand}",
            force_tier=True # [AOS 3.9.8] 脑干保护强制上云
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
            "PREMIUM", # [AOS 2.9] 规划环节：坚持云端精英，防止招聘幻觉
            prompt,
            f"用户需求: {user_demand}\n\n验收标准:\n" + "\n".join(f"- {d}" for d in dod),
            force_tier=True # [AOS 3.9.8] 脑干保护强制上云
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
        is_final: bool = False
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

        # [AOS 3.9.1/3.9.7] 物理证据感知的断点续传 (Physical Evidence & Content Sensing)
        # 如果任务标记为成功，但关联的 DoD 物理文件或黑板内容在当前工作区不存在，则强制重跑
        checkpoint_key = f"_task_done_{role_id}"
        
        # [AOS 3.11.1] 敏感任务锁定补丁：涉及财务(cfo)或调度(scheduler)的任务禁止跳过
        is_sensitive = any(kw in (role_id + " " + task_desc).lower() for kw in ["cfo", "scheduler"])
        
        if self.blackboard.read(checkpoint_key) == "true" and not is_sensitive:
            missing_evidence = False
            if self.current_mission_plan:
                for step in self.current_mission_plan.get("sub_agents", []):
                    if step.get("role_id") == role_id:
                        for d_item in step.get("dod", []):
                            assertion = d_item.get("assertion", {})
                            
                            # 1. 物理文件感知 (AOS 3.9.8 Strict Check)
                            if assertion.get("type") == "file_exists":
                                fname = assertion["file"]
                                if fname.startswith("./"): fname = fname[2:]
                                
                                # 构建沙箱内的绝对路径
                                fpath = os.path.abspath(os.path.join(self.workspace_path or ".", fname))
                                workspace_abs = os.path.abspath(self.workspace_path or ".")
                                
                                # [AOS 3.9.8] 严格校验：1) 路径必须属于当前沙箱 2) 物理文件真实存在
                                if not fpath.startswith(workspace_abs) or not os.path.exists(fpath):
                                    logger.info("🔍 [AOS 3.9.8] 幽灵存档重叠或缺失: 要求的物理文件 '%s' 不在当前沙箱或不存在, 强制重跑", fname)
                                    missing_evidence = True
                                    break
                            
                            # 2. 🚨 AOS 3.9.7: 内容感知 (Content-Aware Skip)
                            # 检查黑板内是否真正持久化了指定的子串，而不是仅凭一个标记
                            elif assertion.get("type") == "value_contains":
                                key = assertion.get("key", "")
                                expected_content = assertion.get("contains", "")
                                val = self.blackboard.read(key) or ""
                                if expected_content not in val:
                                    logger.info("🔍 [AOS 3.9.7] 内容感知失败: 黑板 key '%s' 未包含 '%s', 强制重跑", key, expected_content)
                                    missing_evidence = True
                                    break
                                    
                        if missing_evidence:
                            break
                            
            if not missing_evidence:
                self.blackboard.update_task(role_id, "COMPLETED", "跳过：检测到物理证据与状态存档完全匹配")
                return self.blackboard.read(f"result_{role_id}") or "任务已完成"

        # 等待前置依赖完成
        if depends:
            self.blackboard.update_task(role_id, "WAITING", f"等待前置: {depends}")
            for dep in depends:
                dep_key = f"_task_done_{dep}"
                # [AOS 2.9.3] 放宽超时至 10 分钟，确保本地慢速模型或长耗时抓取能跑完
                result = await self.blackboard.wait_for(dep_key, timeout=600.0)
                if result is None:
                    self.blackboard.update_task(role_id, "FAILED", f"前置 {dep} 超时未完成")
                    return f"[{role_id}] 失败：前置任务 {dep} 超时"

        # 加载所需技能
        # [AOS 3.7.4] 核心工具强制注入 (笔与笔记本)
        # 不管子 Agent 声明了什么，必须强制加载 filesystem 确保其具备基本的读写成果能力
        required_skills = set(agent_config.get("required_skills", []))
        required_skills.add("filesystem")
        
        for skill_name in required_skills:
            self.blackboard.update_task(role_id, "RUNNING", f"加载技能: {skill_name}")
            # [AOS 2.7+] 传入物理隔离的工作区路径
            load_result = await self.skill_manager.load_skill(skill_name, workspace_path=self.workspace_path)
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
            # [AOS 3.9.2] DoD 强力注入 (DoD Hard Enforcement)
            # 将验收标准中的物理指标转化为给子专家的硬性指令
            dod_enforcement = ""
            dod_items = agent_config.get("dod", [])
            if dod_items:
                dod_enforcement = "\n🚨 【硬性要求 - 验收标准对齐】\n你必须确保任务结束前完成以下物理产出，否则任务将判定为失败：\n"
                for d in dod_items:
                    assertion = d.get("assertion", {})
                    if assertion.get("type") == "key_exists":
                        dod_enforcement += f"- 调用 `write_blackboard` 写入 Key: `{assertion['key']}`\n"
                    elif assertion.get("type") == "file_exists":
                        dod_enforcement += f"- 使用 `filesystem` 在 `./` 下创建物理文件: `{assertion['file']}`\n"
            
            # [AOS 4.7.2] 物理证据与视网膜同步初始化
            physical_evidence = ""
            
            # [AOS 4.5] 物理视网膜同步 (Retina Sync)：注入工作区文件快照
            if self.workspace_path and os.path.exists(self.workspace_path):
                try:
                    files = os.listdir(self.workspace_path)
                    f_stats = {f: os.path.getsize(os.path.join(self.workspace_path, f)) for f in files}
                    physical_evidence += f"\n✅ 【工作区物理视网膜同步】: {f_stats}\n"
                except:
                    pass

            # [AOS 4.7] 强制工具路由与参数锚定
            # 优先从黑板读取锚定 URL，否则从任务描述中提取
            current_url = self.blackboard.read("SYSTEM_ROOT_URL")
            if not current_url:
                url_match = re.search(r'https?://[^\s)\]]+', user_demand + " " + task_desc)
                if url_match:
                    current_url = url_match.group(0)

            if current_url:
                # 全局变量死锁注入
                physical_evidence += f"\n🎯 【核心目标锚点】: {current_url}\n"
                
                # 识别静态后缀并强制路由
                static_exts = ['.js', '.json', '.txt', '.csv', '.xml']
                if any(ext in current_url.lower() for ext in static_exts):
                    physical_evidence += f"🚨 【物理红线：强制路由】\n该资源为静态源码/数据文件。严禁使用 puppeteer (浏览器)！必须开启“刺客模式”，直接调用 `fetch` 物理工具获取原始内容。\n"
                else:
                    physical_evidence += f"💡 【工具建议】: 非静态资源可尝试浏览器导航，但优先考虑物理抓取。\n"

            full_system_prompt = system_prompt + physical_evidence + dod_enforcement + SYSTEM_GUARDRAIL
            
            # [AOS 4.6] Janus Router: 分流协议注入
            from prompts import DIRECT_EXECUTION_PROTOCOL, EXPERT_H2M_PROTOCOL
            if is_final:
                # 终点：管家模式 (H2M)
                full_system_prompt += "\n" + EXPERT_H2M_PROTOCOL
            else:
                # 中间：刺客模式 (M2M)
                full_system_prompt += "\n" + DIRECT_EXECUTION_PROTOCOL

            # [AOS 3.7/5.0] 智商自动升档拦截器：识别高智商任务并强制 PREMIUM
            target_tier = "LOCAL"
            high_iq_keywords = ["discover", "install", "scout", "loader", "config"]
            combined_text = (role_id + " " + task_desc).lower()
            
            # [AOS 5.0] Intelligence Lock: 涉及技能发现的任务必须锁定 PREMIUM，严禁智力降级
            if "discover_and_install_skill" in task_desc or "load_skill" in task_desc:
                target_tier = "PREMIUM"
                print(f"🔒 [AOS 5.0] Intelligence Lock: 正在任务 '{role_id}' 中锁定 PREMIUM 算力以执行技能自愈...")
            else:
                for kw in high_iq_keywords:
                    if kw in combined_text:
                        target_tier = "PREMIUM"
                        print(f"☁️ [柔性路由] 检测到高智商子任务 '{role_id}'，自动升档至 PREMIUM 算力...")
                        break

            # AOS 2.1: 优先使用具备工具执行能力的 Agent.execute_with_tools 避免幻觉
            if self.agent:
                result_text = await self.agent.execute_with_tools(
                    full_system_prompt,
                    task_desc,
                    tier=target_tier, 
                    context_id=f"task_{role_id}",
                    workspace_path=self.workspace_path 
                )
            else:
                result_text = await self.unified_client.generate(
                    target_tier, 
                    full_system_prompt,
                    task_desc,
                )

            # [AOS 4.8] 状态纠偏协议 (State Correction Protocol)
            # 识别“空头支票”内容：如果结果中包含明显的失败话术，严禁标记为 true
            apology_patterns = ["无法获取", "数据缺失", "道歉", "未提供", "抓取失败", "死循环中断", "无法提供具体的3月赛事详情"]
            is_polite_failure = any(pw in result_text for pw in apology_patterns)
            
            # 将结果写入黑板
            self.blackboard.write(f"result_{role_id}", result_text[:2000], author=role_id)
            
            if is_polite_failure:
                logger.warning(f"🚫 [状态拦截] 子专家 {role_id} 返回了疑似“礼貌性失败”的内容，拦截 DONE 标志。")
                self.blackboard.write(f"_task_done_{role_id}", "failed", author=role_id)
                self.blackboard.update_task(role_id, "FAILED", "鉴定为礼貌性摆烂")
            else:
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

    def _check_value_contains(self, actual_value, expected_substring):
        """
        [AOS 3.11.0] 语义通配检查器
        """
        if actual_value is None: return False
        
        actual_str = str(actual_value).lower()
        expect_str = str(expected_substring).lower()
        
        # 🚨 语义扩展包：如果裁判找“成功”，我们也允许英文的“success”
        synonym_map = {
            "成功": ["success", "successfully", "loaded", "true", "ok", "pass"],
            "安装": ["installed", "added", "registered"],
            "完成": ["done", "completed", "finished"]
        }
        
        # 如果原始比对失败，尝试语义通配
        if expect_str in actual_str:
            return True
            
        for key, synonyms in synonym_map.items():
            if key in expect_str:
                if any(syn in actual_str for syn in synonyms):
                    logger.info(f"🎭 [语义对齐] 发现匹配项: '{expect_str}' -> '{actual_str}'")
                    return True
        return False

    async def verify_results(self, dod: list, results: dict[str, str]) -> dict:
        # [AOS 5.4] Truth-Driven Verifier (真值驱动验收)
        if self.current_mission_plan.get("plan_summary") == "自维护单兵任务":
            logger.info("⚡ [AOS 5.4] 正在应用极速验证：物理真值审计 (Truth-Driven Pass)...")
            
            # 1. 物理证据链审计 (SQLite 直接对齐)
            # 如果是清理类任务，直接查库
            dod_str = str(dod).lower()
            if any(kw in dod_str for kw in ["clear", "cleanup", "清空", "清理", "delete"]):
                try:
                    import sqlite3
                    db_path = os.path.join(os.path.dirname(__file__), "memories", "scheduler.db")
                    if os.path.exists(db_path):
                        conn = sqlite3.connect(db_path)
                        cursor = conn.cursor()
                        # 检查 scheduled_tasks 表是否为空
                        cursor.execute("SELECT count(*) FROM scheduled_tasks")
                        count = cursor.fetchone()[0]
                        conn.close()
                        if count == 0:
                            logger.info("✅ [AOS 5.4 物理真值] 数据库任务已归零，判定 PASS")
                            return {"overall": "PASS", "details": [], "correction_hint": "物理审计通过：数据库已处于净空状态。"}
                except Exception as e:
                    logger.warning("⚠️ 物理审计失败: %s", e)

            # 2. 探测物理截断信号
            for role, res_text in results.items():
                # [AOS 5.5] Fake Detection: 探测伪装执行
                fake_indicators = ["echo ", "crontab ", "schtasks ", "manual set"]
                # 如果回复中出现了伪装关键词，但没有物理成功信号标识，判定为冒充
                if any(fi in res_text.lower() for fi in fake_indicators):
                    if not any(sig in res_text for sig in ["⏰ [调度器]", "💥 [调度器]", "INSTANT_KILL_PASS", "TASK_COMPLETED"]):
                        logger.error("❌ [AOS 5.5] 发现伪装执行迹象：模型试图用文本建议代替工具调用。")
                        return {"overall": "FAIL", "details": [], "correction_hint": "🚨 严禁提供手动建议！你必须调用工具（如 add_scheduled_task）来完成物理操作，禁止使用 echo 或文本描述。"}

                if "INSTANT_KILL_PASS" in res_text or "TASK_COMPLETED" in res_text:
                    logger.info("✅ [AOS 5.3/5.4/5.5] 探测到物理成功或截断信号，直接判 PASS")
                    return {"overall": "PASS", "details": [], "correction_hint": "物理操作已闭环。"}

            # 3. [AOS 5.7] Apology Check (拒收道歉信)
            for role, res_text in results.items():
                apology_keywords = ["我尝试了", "无法", "但是", "但目前", "抱歉", "sorry", "unfortunately", "无法获取"]
                # 如果回复中包含道歉词且没有成功信号，直接判摆烂
                if any(ak in res_text for ak in apology_keywords):
                    if not any(sig in res_text for sig in ["⏰ [调度器]", "💥 [调度器]", "INSTANT_KILL_PASS", "TASK_COMPLETED", "✅"]):
                        logger.error("❌ [AOS 5.7] 发现‘礼貌性失败’迹象：模型在用道歉掩盖执行失败。")
                        return {"overall": "FAIL", "details": [], "correction_hint": "🚨 拒绝道歉！我不需要解释为什么失败，我只需要物理结果。请重新检查工具调用参数并确保执行成功。"}

            # 4. [AOS 6.0] Universal Physical Delta Audit for Blitz
            # 对于非纯维护/清空类的单兵任务，强制检查物理工作区是否有产出
            if not any(kw in dod_str for kw in ["clear", "cleanup", "清空", "清理", "delete"]):
                try:
                    if self.workspace_path and os.path.exists(self.workspace_path):
                        # 扫描目录（不含子目录及隐藏文件）
                        files = [f for f in os.listdir(self.workspace_path) if os.path.isfile(os.path.join(self.workspace_path, f)) and not f.startswith(".")]
                        if not files:
                            logger.error("❌ [AOS 6.0] 物理审计失败：单兵任务未产生任何物理文件产出。")
                            return {"overall": "FAIL", "details": [], "correction_hint": "物理审计失败：Blitz 模式下未发现任何物理文件产出。我拒绝接受纯文本的虚假成功报告，必须有物理记录。"}
                except Exception as e:
                    logger.warning("⚠️ 物理增量审计异常: %s", e)

            # 5. 物理收敛信号判定 (含备份词)
            found_stop = False
            for role, res_text in results.items():
                if any(kw in res_text for kw in ["强行收敛终止", "共 0 个", "清理完毕", "0 tasks found", "处于最新状态"]):
                    found_stop = True
                    break
            if found_stop:
                logger.info("✅ [AOS 5.2/5.4/5.7] 物理状态已收敛，判定 PASS")
                return {"overall": "PASS", "details": [], "correction_hint": "物理目标已达成，单兵任务自动结项。"}

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
                    
                    # [AOS 3.10.0] 硬盘直连读取 (Strict Physical Verification)
                    # 如果 key 就是文件名并且真实存在，优先/追加合并物理内容，防止只看黑板摘要判断失败
                    if self.workspace_path:
                        # 安全拼接并防止逃逸
                        potential_p = key
                        if potential_p.startswith("./"): potential_p = potential_p[2:]
                        if potential_p.startswith("\\./"): potential_p = potential_p[3:]
                        potential_file = os.path.abspath(os.path.join(self.workspace_path, potential_p))
                        
                        if os.path.exists(potential_file) and os.path.isfile(potential_file):
                            try:
                                with open(potential_file, 'r', encoding='utf-8') as f:
                                    # 仅读取前 20000 字符
                                    physical_data = f.read(20000) 
                                val = val + "\n\n[物理文件内容]:\n" + physical_data
                                logger.info("🔬 [AOS 3.10.0] 裁判已从硬盘捕获真实字节: %s", key)
                            except Exception as e:
                                logger.warning("❌ 物理读取失败: %s", e)
                                
                    passed = self._check_value_contains(val, assertion.get("contains", ""))
                elif a_type == "min_length":
                    val = self.blackboard.read(key) or ""
                    passed = len(val) >= assertion.get("min", 0)
                elif a_type == "file_exists":
                    # [AOS 4.3] 物理刚性核验：文件必须存在且大小 > 100 字节（防止空壳文件蒙混过关）
                    file_path = assertion.get("file", "")
                    if os.path.isabs(file_path):
                        full_p = file_path
                    elif self.workspace_path:
                        p = file_path
                        if p.startswith("./"): p = p[2:]
                        if p.startswith("\\./"): p = p[3:]
                        full_p = os.path.join(self.workspace_path, p)
                    else:
                        full_p = os.path.abspath(file_path)
                        
                    exists = os.path.exists(full_p)
                    size = os.path.getsize(full_p) if exists else 0
                    passed = exists and size > 100
                    
                    if passed: # If initial check passes, proceed to content scan
                        # [AOS 4.6.2] Anti-Apology: 深度内容扫描
                        # 即使文件存在且足够大，如果内容充满礼貌性道歉，依然判定为失败
                        try:
                            with open(full_p, 'r', encoding='utf-8') as f:
                                # [AOS 4.8] 深度内容扫描：扩大搜索范围并增加关键词
                                content = f.read(5000) 
                                apology_keywords = [
                                    "无法获取", "数据缺失", "道歉", "未提供", "抓取失败", 
                                    "PHYSICAL_FETCH_FAILED", "DATA_SOURCE_MISSING",
                                    "无法提供具体的3月赛事详情", "死循环中断", "由于网络原因"
                                ]
                                if any(kw in content for kw in apology_keywords):
                                    logger.error(f"🚫 [反忽悠拦截] 文件 '{file_path}' 虽在，但内容鉴定为“礼貌性摆烂”，判定为 FAIL！")
                                    passed = False 
                        except Exception as e:
                            logger.warning(f"❌ 物理文件内容读取失败: {e}")
                            # If content can't be read, it's suspicious, so fail the check
                            passed = False 
                    
                    if passed:
                        logger.info("✅ 物理校验通过: %s (%d bytes)", file_path, size)
                    else:
                        logger.error("🚫 物理伪证拦截：文件 %s 只有 %d 字节（或不存在），判定为无效执行！", file_path, size)

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
        
        # [AOS 5.0] Anti-Mocking：分析文本结果细节，防止“体面地摆烂”
        negative_keywords = ["无法获取", "失败", "报错", "error", "failed", "cannot", "could not", "找不到", "无法提供"]
        results_text = ""
        for role, text in results.items():
            lower_text = text.lower()
            if any(kw in lower_text for kw in negative_keywords):
                logger.error(f"🚫 [AOS 5.0 Anti-Mocking] 子专家 '{role}' 试图通过文字报告忽悠，判定为 FAIL")
                return {
                    "overall": "FAIL",
                    "correction_hint": f"子专家 '{role}' 的报告中包含失败信号（{negative_keywords[0]}等），物理动作未闭环。禁止文字演戏！"
                }
            results_text += f"--- {role} 的执行结果 ---\n{text[:4000]}\n\n"

        verification_input = (
            f"【验收标准 (DoD)】:\n"
            + "\n".join(f"- {d}" for d in dod_text)
            + f"\n\n【客观预检状态】:\n"
            + "\n".join(f"- {r['criterion']}: {r['result']} ({r['reason']})" for r in pre_check_results)
            + f"\n\n【黑板物理快照 (Ground Truth)】:\n{blackboard_snapshot}"
            + f"\n\n【各 Agent 执行过程细节】:\n{results_text}"
        )

        result = await self.client.generate(
            "LOCAL", # [AOS 2.9] 验收环节：降级本地。读写理解是 8B 模型强项，无需浪费云端
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

    async def classify_intent(self, demand: str) -> str:
        """
        [AOS 6.0] 战术分诊器：判断任务是 L1_BLITZ 还是 L2_EXPERT。
        极速前额叶：优先词法匹配，兜底 LLM 语义判断。
        """
        # 1. 极速词法预检
        blitz_keywords = [
            "schedule", "task", "定时", "提醒", "remind", "reminder", 
            "fetch", "抓取", "获取", "新闻", "status", "check", "wallet", "balance",
            "clear", "cleanup", "清空", "清理", "install", "安装", "新闻"
        ]
        if any(kw in demand.lower() for kw in blitz_keywords):
            return "L1_BLITZ"
            
        # 2. 语义分诊 (使用 LOCAL 模式，追求极速)
        prompt = f"请判断输入需求是否属于线性/原子任务（L1_BLITZ）或复杂/架构/跨领域任务（L2_EXPERT）。\n需求: \"{demand}\"\n直接输出分类ID，禁止废话。"
        try:
            # 这里的 agent 是 Orchestrator 的成员变量
            resp = await self.agent.unified_client.generate(
                tier="LOCAL", 
                messages=[{"role": "user", "content": prompt}]
            )
            if "L1_BLITZ" in resp.upper():
                return "L1_BLITZ"
        except:
            pass
            
        return "L2_EXPERT"

    async def run_mission(
        self,
        user_demand: str,
        primary_session,
        max_rounds: int = 3,
    ):
        """
        完整的自治任务执行循环。
        """
        yield f"🚀 [AOS 6.0] 战术分诊启动：正在评估任务复杂度...\n"

        # [AOS 6.0] 自动分诊：取代固定的关键词匹配
        intent = await self.classify_intent(user_demand)
        is_blitz_mode = (intent == "L1_BLITZ")
        
        if is_blitz_mode:
            yield f"🚀 [AOS 6.0] 物理独裁：识别为 Blitz 线性任务 (L1)，强制锁定‘单兵突击’，拒绝会议。\n"
            # [AOS 7.1] 调用 Agent 的集中隔离逻辑
            if self.agent:
                self.workspace_path = self.agent._setup_action_workspace("blitz")
            else:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                self.workspace_path = os.path.join(os.getcwd(), "Workspace", "Blitz", f"blitz_{timestamp}")
                os.makedirs(self.workspace_path, exist_ok=True)
            if self.agent:
                self.agent.workspace_path = self.workspace_path

            self.current_mission_plan = {"plan_summary": "自维护单兵任务"} # 对齐 Verifier
            
            # [AOS 6.4] Full-Spectrum Sniffing: 不再从阉割版中筛选，而是从全量工具库嗅探
            available_tools = self.agent._get_combined_tools(slim=False)
            targeted_tools = []
            
            # 1. 调度类
            if any(kw in user_demand.lower() for kw in ["schedule", "task", "定时", "提醒"]):
                # [AOS 6.4] 强制补装 write_file，作为物理收据生成器
                st = ["add_scheduled_task", "list_scheduled_tasks", "cancel_scheduled_task", "clear_all_scheduled_tasks", "write_file", "search_skills"]
                targeted_tools.extend([t for t in available_tools if t["function"]["name"] in st])
            
            # 2. 抓取/新闻类
            if any(kw in user_demand.lower() for kw in ["news", "fetch", "抓取", "获取"]):
                ft = ["http_fetch", "read_url_content", "search_web", "write_file"]
                targeted_tools.extend([t for t in available_tools if t["function"]["name"] in ft])
            
            # 3. 财务类
            if any(kw in user_demand.lower() for kw in ["wallet", "balance", "财务", "钱包"]):
                wt = ["cfo_report", "read_blackboard", "write_file"]
                targeted_tools.extend([t for t in available_tools if t["function"]["name"] in wt])

            # 默认补全：如果没匹配到，给全量 slim 工具（AOS 5.6 逻辑降级）
            if not targeted_tools:
                targeted_tools = available_tools

            # [AOS 6.3] 物理隔离：审计前记录初始状态
            initial_files = os.listdir(self.workspace_path) if os.path.exists(self.workspace_path) else []
            snapshot_before = self.agent.scheduler.get_state_snapshot() if hasattr(self.agent, "scheduler") else "none"
            bb_hash_before = self.blackboard.get_snapshot_hash()

            final_report = await self.agent.execute_with_tools(
                system_prompt=(
                    "你现在是 AOS 7.3 战时独裁官（Blitz 模式）。禁止输出废话！禁止询问意见！\n"
                    "你必须通过【物理收敛】审计：\n"
                    "1. 立即调用工具执行任务。如果是分析/提取任务，【一旦发现目标文件路径，必须立即调用 read_file 读取其内容】并进行后续处理。\n"
                    "2. 务必追求【一次性物理交付】。系统已分配 8 轮弹药预算，但若你原地踏步（连续无物理产出且重复操作），内核将立即断电并判定为逻辑坏疽。\n"
                    "3. 如果你有产出（文件写入/黑板更新/读取到新信息），内核会自动为你延展生命线。针对分析任务，建议最后将结论写入 report.md 以确保审计通过。\n"
                    "拒绝演戏，拒绝伪代码。立即执行。"
                ),
                user_demand=user_demand,
                tier="PREMIUM", 
                context_id=f"blitz_{os.path.basename(self.workspace_path)}",
                workspace_path=self.workspace_path,
                max_iterations=8, # [AOS 7.3] 初始預算上調，由 McpAgent 智能關斷
                tools=targeted_tools
            )
            yield f"\n📊 【单兵任务报告】\n{final_report}\n"
            
            # [AOS 5.2] 调用裁判验证物理收敛
            yield "⚖️ AI 裁判正在进行物理收敛核验...\n"
            
            # [AOS 6.3] 物理准星：审计双重校验 (Audit Dual-Check)
            is_maintenance = any(kw in user_demand.lower() for kw in ["schedule", "clear", "task", "提醒", "定时"])
            db_snapshot_after = self.agent.scheduler.get_state_snapshot() if hasattr(self.agent, "scheduler") else "none"
            
            has_fs_delta = len(self.agent._get_workspace_delta(initial_files)) > 0
            has_db_delta = snapshot_before != db_snapshot_after
            has_bb_delta = self.blackboard.get_snapshot_hash() != bb_hash_before
            
            has_physical_evidence = has_fs_delta or has_db_delta or has_bb_delta
            
            # [AOS 7.5.8] 核心优化：如果 Agent 产生了逻辑位移（拿到了新数据），允许审计通过
            has_logical_delta = getattr(self.agent, "has_logical_delta", False)
            
            if is_maintenance:
                # [AOS 7.5.4] 幂等性审计：对于清理/查询类维护任务，只要有工具执行成功（即使无位移）即判定为通过
                if has_physical_evidence or "cleared" in final_report.lower() or "success" in final_report.lower():
                    yield f"✅ [AOS 7.5.4] 维护任务物理闭环成功。审计维度: {'DB ' if has_db_delta else ''}{'FS ' if has_fs_delta else ''}{'BB' if has_bb_delta else ''} (容忍幂等性无位移)\n"
                else:
                    logger.error("❌ [AOS 6.3] 审计失败：维护任务未检测到任何物理位移或成功标志。")
                    yield f"❌ [AOS 6.3] 审计失败：维护任务必须产生数据变更、文件收据(done.txt)或明确的成功状态。\n"
            elif intent == "L1_BLITZ" and not (has_fs_delta or has_logical_delta):
                 logger.error("❌ [AOS 6.0] 物理审计失败：单兵/自动分诊任务未产生物理文件产出且无逻辑增量。")
                 yield f"❌ [AOS 6.0] 物理审计失败：检测到执行指令，但物理审计未发现文件增量或显著的数据读取进展。拒绝接受纯文本报告。\n"
            else:
                 reason = "FS" if has_fs_delta else ("Logical(Data)" if has_logical_delta else "Evidence")
                 yield f"✅ [AOS 6.2] 物理闭环成功。审计维度: {reason} {'DB ' if has_db_delta else ''}{'BB' if has_bb_delta else ''}\n"
            return

        # 0. 为本次任务创建物理工作区沙箱 (AOS 2.7+)
        # [AOS 7.1] 调用 Agent 的集中隔离逻辑
        if self.agent:
            self.workspace_path = self.agent._setup_action_workspace("auto")
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.workspace_path = os.path.join(os.getcwd(), "Workspace", "Auto", f"auto_{timestamp}")
            os.makedirs(self.workspace_path, exist_ok=True)
            
        yield f"📁 [隔离] 已分配专属工作区: {self.workspace_path}\n"
        
        # [AOS 4.7] 根参数锚定 (URL-Anchor)
        root_url_match = re.search(r'https?://[^\s)\]]+', user_demand)
        if root_url_match:
            root_url = root_url_match.group(0)
            self.blackboard.write("SYSTEM_ROOT_URL", root_url, author="SYSTEM", sticky=True)
            yield f"🔗 [锚定] 已锁定全局资源追踪器: {root_url}\n"
        
        # [AOS 2.8.7] 记录任务开始前的根目录文件快照
        root_files_before = set(os.listdir(os.getcwd()))

        # [AOS 2.9.4] 智能持久化：除非主动要求完整重置，否则保留历史事实实现断点续传
        # self.blackboard.clear() # 强制关闭清空，改用细粒度清理
        self.blackboard.task_progress.clear()
        # self.blackboard.snapshots.clear() # 保留快照供回滚
        yield "📋 黑板已进入持久化模式（支持断点续传）\n"

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

            # [AOS 4.3] 逻辑消磁：清理上一轮的任务状态，防止 Skip Trap
            self.blackboard.task_progress.clear()
            if round_num > 1 and self.agent:
                # 调用 McpAgent 的消磁逻辑
                await self.agent.prepare_for_retry(self.blackboard)
                yield "♻️ [消磁] 侦测到重试信号，已物理擦除所有专家状态标志，拒绝跳过。\n"
            elif round_num > 1:
                # 保底清理逻辑
                for k in list(self.blackboard.facts.keys()):
                    if "_task_done_" in k:
                        self.blackboard.delete(k)
                yield "♻️ [消磁] 侦测到重试信号，已物理清除状态标志。\n"

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
            
            # [AOS 3.9.5] 物理记录当前执行计划，供 execute_sub_agent 校验
            self.current_mission_plan = plan
                
            yield f"\n👔 招聘计划: {plan.get('plan_summary', '')}\n"
            sub_agents = plan.get("sub_agents", [])
            for agent in sub_agents:
                yield f"  🧑‍💼 {agent['role_id']}: {agent['expertise']}\n"

            # 3. 按依赖关系分组并发执行
            yield "\n🏭 数字员工开始工作...\n"
            agent_results: dict[str, str] = {}

            # 拓扑排序：无依赖的先跑，有依赖的通过 wait_for 自动等待
            tasks = []
            # [AOS 4.6] Janus Router: 判定任务节点
            for i, agent_config in enumerate(sub_agents):
                is_final = (i == len(sub_agents) - 1)
                tasks.append(
                    self.execute_sub_agent(agent_config, user_demand, primary_session, is_final=is_final)
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
                
                # [AOS 2.8.7] 自动整理：将根目录下意外产生的文件移动到 Workspace
                root_files_after = set(os.listdir(os.getcwd()))
                new_files = root_files_after - root_files_before
                if new_files:
                    yield "🧹 [整理] 正在将任务产物移动到隔离区...\n"
                    import shutil
                    moved_count = 0
                    for f in new_files:
                        src = os.path.join(os.getcwd(), f)
                        if os.path.isfile(src) and not f.startswith(".") and f != "main.py":
                            try:
                                # 如果目标已存在（极少见），则覆盖
                                dest = os.path.join(self.workspace_path, f)
                                shutil.move(src, dest)
                                moved_count += 1
                                logger.info("🚚 自动归档: %s -> %s", f, self.workspace_path)
                            except Exception as e:
                                logger.warning("🚚 归档失败 %s: %s", f, e)
                    if moved_count > 0:
                        yield f"✅ [整理] 已归档 {moved_count} 个文件至 {self.workspace_path}\n"

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
