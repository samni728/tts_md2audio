# 国内服务器部署指南

## 🚨 问题说明

在国内服务器上部署时，可能会遇到以下网络问题：

- Docker Hub 连接超时
- 镜像拉取失败
- 网络连接不稳定

## 🔧 解决方案

### 方案 1：使用国内部署脚本（推荐）

```bash
# 1. 使用国内版本启动脚本
./start-china.sh
```

### 方案 2：手动配置 Docker 镜像加速器

```bash
# 1. 创建Docker daemon配置
sudo mkdir -p /etc/docker
sudo cp daemon.json /etc/docker/daemon.json

# 2. 重启Docker服务
sudo systemctl restart docker

# 3. 使用国内版本构建
docker-compose -f docker-compose.china.yml build
docker-compose -f docker-compose.china.yml up -d
```

### 方案 3：手动拉取镜像

```bash
# 1. 手动拉取Python镜像
docker pull registry.cn-hangzhou.aliyuncs.com/library/python:3.11-slim

# 2. 重新标记镜像
docker tag registry.cn-hangzhou.aliyuncs.com/library/python:3.11-slim python:3.11-slim

# 3. 使用原始配置构建
docker-compose build
docker-compose up -d
```

### 方案 4：使用代理

如果你有代理服务器：

```bash
# 配置Docker使用代理
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/http-proxy.conf > /dev/null <<EOF
[Service]
Environment="HTTP_PROXY=http://your-proxy:port"
Environment="HTTPS_PROXY=http://your-proxy:port"
Environment="NO_PROXY=localhost,127.0.0.1"
EOF

sudo systemctl daemon-reload
sudo systemctl restart docker
```

## 📋 国内镜像源列表

### Docker 镜像加速器

- 中科大镜像：`https://docker.mirrors.ustc.edu.cn`
- 网易镜像：`https://hub-mirror.c.163.com`
- 百度镜像：`https://mirror.baidubce.com`
- 腾讯云镜像：`https://ccr.ccs.tencentyun.com`

### Python 包镜像源

- 清华大学：`https://pypi.tuna.tsinghua.edu.cn/simple`
- 阿里云：`https://mirrors.aliyun.com/pypi/simple`
- 中科大：`https://pypi.mirrors.ustc.edu.cn/simple`

## 🚀 快速部署命令

```bash
# 一键部署（推荐）
./start-china.sh

# 查看服务状态
docker-compose -f docker-compose.china.yml ps

# 查看日志
docker-compose -f docker-compose.china.yml logs -f

# 停止服务
docker-compose -f docker-compose.china.yml down
```

## 🔍 故障排除

### 1. 网络连接问题

```bash
# 测试网络连接
ping docker.mirrors.ustc.edu.cn
curl -I https://docker.mirrors.ustc.edu.cn
```

### 2. Docker 服务问题

```bash
# 检查Docker状态
sudo systemctl status docker

# 重启Docker服务
sudo systemctl restart docker
```

### 3. 镜像拉取问题

```bash
# 清理Docker缓存
docker system prune -a

# 重新构建
docker-compose -f docker-compose.china.yml build --no-cache
```

## 📞 技术支持

如果仍然遇到问题，请检查：

1. 服务器网络连接
2. Docker 服务状态
3. 防火墙设置
4. DNS 配置
