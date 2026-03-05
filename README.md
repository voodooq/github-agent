# 🚀GitHub Agent OS(AOS 8.0 - Blitz Edition)

基于 **AOS (Agentic OS)** 架构的极速、高可用 GitHub 协同平台。AOS 8.0 引入了全新的 **Blitz 性能引擎**，通过极致的架构瘦身与并行预热，实现了任务周转效率的质变。

---

## 🌟 AOS 8.0 核心特性 (Blitz Edition)

- **⚡ 单兵快路径 (Fast-lane)**：针对极简指令（查询、清理）自动跳过 DoD 生成与招聘流程，实现 **5s 级** 极速首响。
- **🔥 并发技能预温 (Concurrent Warm-up)**：任务启动瞬态并行初始化 `filesystem`, `github`, `scrape` 等 MCP 技能，消除了子任务冷启动的同步等待。
- **⚖️ 精准语义审计 (Selective Audit)**：物理判定任务属性，对于纯操作类任务只需客观断言通过即刻结项，节省大量云端 LLM 裁判开销。
- **📂 物理隔离工作区 (Workspace)**：由 `AOS 7.x` 延续的隔离机制，所有分析、部署都在带时间戳的沙盒中执行，严禁交叉污染。
- **💰 AEA 2.0 经济引擎**：内置智能算力路由，根据余额自动在 `PREMIUM` (高智商云端) 与 `LOCAL` (极速本地) 间动态升降档。
- **🛡️ 状态自愈消磁**：针对失败任务实施“按需消磁”，物理重置故障专家标志，支持断点续传且杜绝“假成功”残留。

---

## 🏗️ 性能指标 (SLA)

| 任务类型 | 平均延迟 (P50) | 优化点 |
| :--- | :--- | :--- |
| 查询类 (Status/Check) | **< 8s** | 命中 Fast-lane，跳过繁琐会议 |
| 分析类 (Analyze/Audit) | **< 90s** | 后台预加载技能，并发专家审计 |
| 部署类 (Deploy/Action) | **< 150s** | Docker 沙盒自愈 + 物理收据核验 |

---

## 🛠️ 指令矩阵

| 指令 | 描述 | 模式 |
| :--- | :--- | :--- |
| `/auto <需求>` | **全自治任务** | 自动分诊 (L1 Blitz / L2 Expert)，闭环交付 |
| `/search <需求>` | **深度调研** | 带预算的检索与排名，自动导出 JSON 报告 |
| `/analyze <URL/文件>` | **分层审计** | 支持本地/远程、物理证据提取、横向对比 |
| `/deploy <URL>` | **沙盒部署** | 一键 Docker 化构建，带环境自愈检查 |
| `/bb` | **黑板/收据** | 查看物理证据链、任务时间轴与事实记录 |
| `/wallet` | **财务快报** | 查看余额、日耗、预估跑道 (Runway) |

---

## 🚀 极速开始

1. **依赖安装**
   ```bash
   pip install -r requirements.txt
   ```
2. **环境配置**
   复制 `.env.example` 为 `.env`，填入 `GITHUB_TOKEN` 及 LLM API。
3. **性能微调**
   在 `.env` 中设置 `AOS_ENABLE_FAST_VERIFY=True` 开启 P1 极速验证模式。

---

## 🔗 License
MIT License. 极致自治，代码护航。
