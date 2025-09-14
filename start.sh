#!/bin/bash

# TTS批量转换工具 - Docker启动脚本

echo "🚀 启动TTS批量转换工具..."

# 检查Docker是否运行
if ! docker info > /dev/null 2>&1; then
    echo "❌ Docker未运行，请先启动Docker Desktop"
    exit 1
fi

# 检查是否存在旧的容器
if docker ps -a --format 'table {{.Names}}' | grep -q "tts-batch-converter"; then
    echo "🔄 停止并删除旧容器..."
    docker-compose down
fi

# 构建并启动服务
echo "🔨 构建Docker镜像..."
docker-compose build

echo "🚀 启动服务..."
docker-compose up -d

# 等待服务启动
echo "⏳ 等待服务启动..."
sleep 5

# 检查服务状态
if docker-compose ps | grep -q "Up"; then
    echo "✅ 服务启动成功！"
    echo "🌐 访问地址: http://localhost:5000"
    echo "📊 查看日志: docker-compose logs -f"
    echo "🛑 停止服务: docker-compose down"
else
    echo "❌ 服务启动失败，请检查日志:"
    docker-compose logs
fi
