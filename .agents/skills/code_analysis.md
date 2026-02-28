---
description: 代码架构分析与逻辑追踪方法论
---

# 代码深度分析技能

## 分析流程

### Step 1: 识别项目技术栈
- 检查入口文件: `main.py`, `app.py`, `index.ts`, `index.js`, `main.go`
- 检查依赖声明: `requirements.txt`, `pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml`
- 检查构建配置: `Dockerfile`, `docker-compose.yml`, `.github/workflows/`

### Step 2: 架构逆向
- 目录结构推断分层: `src/`, `api/`, `service/`, `model/`, `utils/`
- 识别设计模式: MVC / 分层架构 / 微服务 / Monorepo
- 找到核心入口点并追踪关键调用链

### Step 3: 质量评估
- **耦合度**: 模块间是否通过接口/抽象类解耦？
- **测试覆盖**: 是否有 `tests/` 目录？测试框架是什么？
- **文档质量**: README 是否包含安装、使用、API 说明？
- **安全扫描**: 是否有硬编码密钥、`eval()`、`os.system()` 等危险调用？

## 常见代码异味 (Code Smells)

| 异味 | 描述 | 严重度 |
|------|------|--------|
| God Class | 单个类/文件超过 500 行 | 中 |
| 硬编码配置 | 数据库连接串或 API Key 写死在代码中 | 高 |
| 无错误处理 | 裸 `except:` 或无 try-catch | 中 |
| 循环依赖 | 模块 A 导入 B，B 又导入 A | 高 |
| 魔法数字 | 代码中出现无注释的常量（如 `if status == 3`） | 低 |

## 安全审计检查清单

1. 搜索 `eval(`, `exec(`, `os.system(`, `subprocess.call(` → 命令注入风险
2. 搜索 `dangerouslySetInnerHTML` → XSS 风险
3. 搜索 `password`, `secret`, `token`, `api_key` → 硬编码凭据
4. 检查 LICENSE 文件 → GPL 对商业化的影响
5. 检查依赖版本 → 是否存在已知 CVE
