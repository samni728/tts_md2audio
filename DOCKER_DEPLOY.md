# TTS 批量转换工具 - Docker 部署指南

## 📋 概述

本项目支持 Docker 容器化部署，提供简单、可靠的部署方式。

## 🚀 快速开始

### 方法一：使用 Docker Compose（推荐）

```bash
# 1. 构建并启动服务
docker-compose up -d

# 2. 查看服务状态
docker-compose ps

# 3. 查看日志
docker-compose logs -f

# 4. 停止服务
docker-compose down
```

### 方法二：使用 Docker 命令

```bash
# 1. 构建镜像
docker build -t tts-converter .

# 2. 运行容器
docker run -d \
  --name tts-batch-converter \
  -p 5055:5055 \
  -v $(pwd)/uploads:/app/uploads \
  tts-converter

# 3. 查看容器状态
docker ps

# 4. 查看日志
docker logs -f tts-batch-converter

# 5. 停止容器
docker stop tts-batch-converter
docker rm tts-batch-converter
```

## 🔧 配置说明

### 环境变量

| 变量名       | 默认值       | 说明         |
| ------------ | ------------ | ------------ |
| `FLASK_HOST` | `0.0.0.0`    | 服务监听地址 |
| `FLASK_PORT` | `5055`       | 服务端口     |
| `FLASK_ENV`  | `production` | 运行环境     |

### 数据卷挂载

- `./uploads:/app/uploads` - 上传文件目录

## 📁 目录结构

```
tts_批量转化/
├── Dockerfile              # Docker镜像构建文件
├── docker-compose.yml      # Docker Compose配置
├── .dockerignore           # Docker忽略文件
├── requirements.txt        # Python依赖
├── app.py                  # 主应用文件
├── templates/              # 模板文件
└── uploads/                # 上传文件目录（挂载）
```

## 🌐 访问服务

部署成功后，通过以下地址访问：

- **Web 界面**: http://localhost:5055
- **健康检查**: http://localhost:5055/

## 🔍 故障排除

### 查看容器日志

```bash
docker-compose logs -f tts-converter
```

### 进入容器调试

```bash
docker-compose exec tts-converter bash
```

### 重启服务

```bash
docker-compose restart tts-converter
```

### 重新构建镜像

```bash
docker-compose build --no-cache tts-converter
```

## 📊 性能优化

### 生产环境建议

1. **资源限制**：

```yaml
services:
  tts-converter:
    deploy:
      resources:
        limits:
          cpus: "2.0"
          memory: 2G
        reservations:
          cpus: "1.0"
          memory: 1G
```

2. **健康检查**：

```yaml
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:5055/"]
  interval: 30s
  timeout: 10s
  retries: 3
  start_period: 40s
```

## 🔒 安全建议

1. **使用非 root 用户**（已在 Dockerfile 中配置）
2. **限制网络访问**
3. **定期更新基础镜像**
4. **使用 HTTPS**（生产环境）

## 📝 更新部署

```bash
# 拉取最新代码
git pull

# 重新构建并部署
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

## 🆘 支持

如遇到问题，请检查：

1. Docker 和 Docker Compose 版本
2. 端口是否被占用
3. 数据卷权限
4. 网络连接
