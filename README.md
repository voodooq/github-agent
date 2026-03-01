# 🚀 OpenClaw GitHub Agent (AOS 7.2)

基于 **AOS (Agentic OS)** 架构的混合算力多专家 GitHub 协同平台。它不仅仅是一个搜索工具，而是一个具备**经济意识 (CFO)**、**沙盒环境 (Docker)** 和**物理隔离 (Workspace)** 的全自治 Agent 系統。

---

## 🌟 AOS 7.2 核心特性

- **📂 物理隔離工作區 (Workspace)**：所有 `/search`, `/analyze`, `/review` 操作都會在獨立、帶時間戳的沙盒目錄中執行，確保數據互不干擾。
- **💰 CFO 經濟引擎 (AEA)**：內置錢包、ROI 評估與算力路由。根據餘額自動切換生存模式（飢餓/溫飽/土豪），預算不足自動降級本地模型。
- **🛡️ 專家團橫向對比**：支持同時對比多個 GitHub 項目，提供專業的評分矩陣（UX、架構、安全、活躍度）並給出明確推薦。
- **🚀 智能文件感官**：`/analyze` 和 `/review` 具備路徑識別與文件內容讀取能力，能自動從搜索報告中提取 URL 進行批量處理。
- **📦 Docker 沙盒部署**：支持 `/deploy` 一鍵將開源項目構建並部署至隔離容器，自動生成 Dockerfile 並進行自愈檢查。
- **📅 自治調度器**：內置 Scheduler，支持自然語言預約任務（如“每天早上 8 點複盤量化框架”）。

---

## 🛠️ 命令矩陣 (AOS 指令集)

| 指令 | 描述 | 優點 |
| :--- | :--- | :--- |
| `/search <需求>` | 授權檢索與排名矩陣 | 帶預算的深度調研，自動導出 `ranking_data.json` |
| `/analyze <URL/文件>` | 精準代碼審計與對比 | 支持讀取本地文件提取 URL，自動生成橫向對比報告 |
| `/review <URL/文件>` | 混合算力專家團聯合評審 | 5 大專家（架構/安全/UX/DevOps/生命力）併發審計 |
| `/deploy <URL>` | 一鍵 Docker 沙盒化部署 | 自動環境檢測、鏡像構建、映射運行 |
| `/auto <需求>` | **Hot Mode** 全自治任務 | AI 自主拆解、招聘專家、執行、歸檔 |
| `/bb` | 📖 黑板報告 | 查看任務事實、時間軸與物理證據鏈 |
| `/wallet` | 💰 財務簡報 | 查看當前餘額、日燃燒率、預估跑道 (Runway) |
| `/checkup` | 🛡️ 全量免疫掃描 | AOS 4.0 系統自檢與動態技能自愈 |

---

## 🏗️ 物理隔離協議 (Isolation Protocol)

所有執行結果（報告、數據、鏡像配置）均鎖定在本地 `Workspace/` 目錄下：
```text
Workspace/
├── Search/    # 存儲檢索報告與 ranking_data.json
├── Analyze/   # 存儲代碼審計報告與多項目對比
├── Review/    # 存儲專家團聯手彙報的 review_report.md
└── Deploy/    # 存儲生成的 Dockerfile 與佈署日誌
```
> **注意**：該目錄已被列入 `.gitignore`，確保您的隱私數據和分析報告不會洩露至公有倉庫。

---

## 🚀 快速開始

### 1. 依賴安裝
```bash
pip install -r requirements.txt
```

### 2. 環境配置
複製 `.env.example` 為 `.env`，配置您的 `GITHUB_TOKEN` 以及 LLM API Key。

### 3. 動算力分配 (CFO 策略)
在 `.env` 中設置預計初始金：
`AEA_INITIAL_BALANCE=10.0`
Agent 會根據餘額自動在 `PREMIUM` (雲端) 和 `LOCAL` (本地) 算力間切換。

---

## 🧠 專家團組成

| 專業維度 | 算力傾向 | 核心觀察點 |
| :--- | :--- | :--- |
| **UX/DX** | LOCAL | 文檔友好度、上手門檻、API 易用性 |
| **DevOps** | LOCAL | CI/CD 支持、Docker 友好性、構建鏈 |
| **Security** | PREMIUM | 硬編碼、依賴缺陷、靜態漏洞 (SAST) |
| **Arch** | PREMIUM | 擴展性、模式設計、並發設計 |
| **Liveliness**| LOCAL | 修 commit 頻率、Issue 響應速度 |

---

## 🔗 License

MIT License. 基於開源協議保護。
