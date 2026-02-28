---
description: Docker 构建与部署常见问题排错指南
---

# Docker 构建与部署排错技能

## 常见构建错误与修复

### 1. Python 依赖安装失败
**症状**: `pip install` 报错 `Could not find a version that satisfies the requirement`
**修复思路**:
- 检查 `requirements.txt` 中是否有 Windows-only 的包（如 `pywin32`）
- 尝试降低 Python 版本（如从 3.12 降到 3.11）
- 添加 `--no-cache-dir` 和 `--trusted-host pypi.org` 参数

### 2. Node.js 构建失败
**症状**: `npm install` 报 `node-gyp` 编译错误
**修复思路**:
- 基础镜像需要安装 `build-essential` 和 `python3`
- 使用 `node:18-alpine` 时需额外安装 `g++ make`
- 检查 `.npmrc` 是否有私有 registry 配置

### 3. 端口映射问题
**症状**: 容器启动了但外部无法访问
**修复思路**:
- 确保应用绑定到 `0.0.0.0` 而非 `127.0.0.1`
- Flask: `app.run(host='0.0.0.0')`
- Next.js: `next start -H 0.0.0.0`
- FastAPI: `uvicorn main:app --host 0.0.0.0`

### 4. 基础镜像缺少系统库
**症状**: `ImportError: libXXX.so.X: cannot open shared object file`
**修复思路**:
- Alpine: `apk add --no-cache libffi-dev openssl-dev`
- Debian: `apt-get install -y libpq-dev gcc`
- 考虑使用 `-slim` 镜像替代 `-alpine`（glibc vs musl 兼容性）

### 5. 多阶段构建优化
**适用场景**: 镜像体积过大（>1GB）
**模板**:
```dockerfile
FROM node:18-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci --production
COPY . .
RUN npm run build

FROM node:18-alpine
WORKDIR /app
COPY --from=builder /app/dist ./dist
COPY --from=builder /app/node_modules ./node_modules
EXPOSE 3000
CMD ["node", "dist/index.js"]
```

## 健康检查失败的排查

1. **先验证端口是否监听**: `docker exec <id> netstat -tlnp`
2. **检查应用日志**: `docker logs <id> --tail 50`
3. **HTTP 状态码含义**:
   - `200`: 服务正常
   - `404`: 后端活了但路由未挂载（通常正常）
   - `500`: 应用内部错误（数据库连接等）
   - `502/503`: 反向代理配置有误
