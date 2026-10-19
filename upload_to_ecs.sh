#!/bin/bash
# ECS 部署包上传脚本
# 目标: 将 lumen_web_workbench_bundle.tar.gz 上传到 ECS /root/
#
# 认证方式（密码不再硬编码在脚本中）：
#   1) 推荐：配置 SSH 免密登录
#      ssh-copy-id -i ~/.ssh/lumen_web_deploy.pub root@47.101.195.11
#   2) 或：通过环境变量传入密码
#      export ECS_PASSWORD='你的ECS密码'
# 说明：若 SSH 免密可用，则不会触发密码交互，无需设置 ECS_PASSWORD。

LOCAL_FILE="/Users/chenyanxi/Desktop/lumen_web_export/lumen_web_workbench_bundle.tar.gz"
SERVER="root@47.101.195.11"
REMOTE_PATH="/root/"

export ECS_PASSWORD="${ECS_PASSWORD:-}"

echo "=== 正在上传部署包到 ECS (47.101.195.11) ==="

expect -c "
set timeout 120
spawn scp -o StrictHostKeyChecking=no \"$LOCAL_FILE\" \"$SERVER:$REMOTE_PATH\"
expect {
    \"password:\" {
        if { \$env(ECS_PASSWORD) eq \"\" } {
            puts \"\n❌ 需要密码但未设置 ECS_PASSWORD 环境变量，请先 export ECS_PASSWORD=... 或配置 SSH 免密登录\"
            exit 1
        }
        send \"\$env(ECS_PASSWORD)\r\"
        exp_continue
    }
    \"Permission denied\" { puts \"\n❌ 认证失败！\"; exit 1 }
    \"No such file\" { puts \"\n❌ 本地文件不存在！\"; exit 1 }
    eof { puts \"\n✅ 上传成功！\" }
    timeout { puts \"\n❌ 连接超时！\"; exit 1 }
}
"

echo "=== 完毕 ==="
