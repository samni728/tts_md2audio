#!/bin/bash

# TTS批量转换工具 - 中国部署问题修复脚本

echo "🔧 修复中国部署问题..."

# 检查Docker是否运行
if ! docker info > /dev/null 2>&1; then
    echo "❌ Docker未运行，请先启动Docker服务"
    echo "💡 启动命令: sudo systemctl start docker"
    exit 1
fi

echo "📋 问题分析："
echo "   - 阿里云镜像仓库访问权限不足"
echo "   - 需要更换为更稳定的国内镜像源"

echo ""
echo "🔄 解决方案："
echo "   1. 使用腾讯云镜像源（推荐）"
echo "   2. 使用官方镜像+国内加速器（备用）"

echo ""
echo "请选择解决方案："
echo "1) 使用腾讯云镜像源（推荐）"
echo "2) 使用官方镜像+国内加速器"
echo "3) 退出"
read -p "请输入选择 (1-3): " choice

case $choice in
    1)
        echo "✅ 使用腾讯云镜像源..."
        # 确保使用修复后的Dockerfile.china
        echo "🔨 构建Docker镜像（腾讯云镜像源）..."
        docker-compose -f docker-compose.china.yml build --no-cache
        ;;
    2)
        echo "✅ 使用官方镜像+国内加速器..."
        # 临时替换Dockerfile
        cp Dockerfile.china.backup Dockerfile.china.temp
        mv Dockerfile.china Dockerfile.china.original
        mv Dockerfile.china.temp Dockerfile.china
        
        echo "🔨 构建Docker镜像（官方镜像+加速器）..."
        docker-compose -f docker-compose.china.yml build --no-cache
        
        # 恢复原始Dockerfile
        mv Dockerfile.china.original Dockerfile.china
        ;;
    3)
        echo "👋 退出修复脚本"
        exit 0
        ;;
    *)
        echo "❌ 无效选择，退出"
        exit 1
        ;;
esac

echo ""
echo "🚀 启动服务..."
docker-compose -f docker-compose.china.yml up -d

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
    echo ""
    echo "💡 如果问题仍然存在，请尝试："
    echo "   1. 检查网络连接"
    echo "   2. 手动拉取镜像: docker pull python:3.11-slim"
    echo "   3. 使用官方Dockerfile: docker-compose -f docker-compose.yml up -d"
fi
