#!/bin/bash
# ECS Lumen Web 部署脚本
# 解压并执行 workbench-install.sh
#
# 认证方式（密码不再硬编码在脚本中）：
#   1) 推荐：配置 SSH 免密登录
#      ssh-copy-id -i ~/.ssh/lumen_web_deploy.pub root@47.101.195.11
#   2) 或：通过环境变量传入密码
#      export ECS_PASSWORD='你的ECS密码'
# 说明：若 SSH 免密可用，则不会触发密码交互，无需设置 ECS_PASSWORD。

export ECS_PASSWORD="${ECS_PASSWORD:-}"

echo "=== 正在部署 Lumen Web 到 ECS 47.101.195.11 ==="
echo ""

expect -c '
set timeout 300
spawn ssh -o StrictHostKeyChecking=no root@47.101.195.11 "rm -rf /tmp/lumen_bundle && mkdir -p /tmp/lumen_bundle && tar -xzf /root/lumen_web_workbench_bundle.tar.gz -C /tmp/lumen_bundle && bash /tmp/lumen_bundle/deploy/workbench-install.sh"
expect {
    "password:" {
        if { $env(ECS_PASSWORD) eq "" } {
            puts "\n❌ 需要密码但未设置 ECS_PASSWORD 环境变量，请先 export ECS_PASSWORD=... 或配置 SSH 免密登录"
            exit 1
        }
        send "$env(ECS_PASSWORD)\r"
        exp_continue
    }
    "Permission denied" { puts "\n❌ 认证失败！"; exit 1 }
    eof { puts "\n=== 部署脚本执行完成 ===" }
    timeout { puts "\n❌ 超时 (5分钟)" }
}
'

echo ""
echo "=== 完毕 ==="
