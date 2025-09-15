#!/bin/bash

# TTS批量转换工具 - 国内服务器Docker启动脚本

echo "🚀 启动TTS批量转换工具（国内服务器版本）..."

# 检查Docker是否运行
if ! docker info > /dev/null 2>&1; then
    echo "❌ Docker未运行，请先启动Docker服务"
    echo "💡 启动命令: sudo systemctl start docker"
    exit 1
fi

# 配置Docker镜像加速器
echo "🔧 配置Docker镜像加速器..."
sudo mkdir -p /etc/docker
sudo cp daemon.json /etc/docker/daemon.json
sudo systemctl restart docker

echo "⏳ 等待Docker服务重启..."
sleep 3

# 检查是否存在旧的容器
if docker ps -a --format 'table {{.Names}}' | grep -q "tts-batch-converter"; then
    echo "🔄 停止并删除旧容器..."
    docker-compose -f docker-compose.china.yml down
fi

# 构建并启动服务
echo "🔨 构建Docker镜像（使用国内镜像源）..."
docker-compose -f docker-compose.china.yml build

echo "🚀 启动服务..."
docker-compose -f docker-compose.china.yml up -d

# 等待服务启动
echo "⏳ 等待服务启动..."
sleep 5

# 检查服务状态
if docker-compose -f docker-compose.china.yml ps | grep -q "Up"; then
    echo "✅ 服务启动成功！"
    echo "🌐 访问地址: http://localhost:5055"
    echo "📊 查看日志: docker-compose -f docker-compose.china.yml logs -f"
    echo "🛑 停止服务: docker-compose -f docker-compose.china.yml down"
else
    echo "❌ 服务启动失败，请检查日志:"
    docker-compose -f docker-compose.china.yml logs
fi
