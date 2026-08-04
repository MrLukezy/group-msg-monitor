@echo off
chcp 65001 >nul
REM 不依赖 Docker Desktop：在 WSL Ubuntu 内启动 dockerd + GeWeChat 容器
echo [1/3] Ensure dockerd in WSL...
wsl -d Ubuntu -u root -- bash -lc "if ! pgrep -x dockerd >/dev/null; then nohup dockerd >/var/log/dockerd.log 2>&1 & sleep 3; fi; docker info >/dev/null"

echo [2/3] Start GeWeChat container (alpine)...
wsl -d Ubuntu -u root -- bash -lc "mkdir -p /root/temp; docker inspect gewe >/dev/null 2>&1 && docker start gewe || docker run -d --name=gewe --restart=unless-stopped -v /root/temp:/root/temp -p 2531:2531 -p 2532:2532 gewe-alpine || docker run -d --name=gewe --restart=unless-stopped -v /root/temp:/root/temp -p 2531:2531 -p 2532:2532 registry.cn-chengdu.aliyuncs.com/tu1h/wechotd:alpine"

echo [3/3] Wait for API...
wsl -d Ubuntu -u root -- bash -lc "for i in $(seq 1 24); do code=$(curl -sS -m 5 -o /tmp/g.json -w '%%{http_code}' -X POST http://127.0.0.1:2531/v2/api/tools/getTokenId -H 'Content-Type: application/json' -d '{}' || echo 000); echo try=$i code=$code; if [ \"$code\" = \"200\" ]; then python3 -c \"import json;d=json.load(open('/tmp/g.json'));print('ret',d.get('ret'),'ok',bool(d.get('data')))\"; exit 0; fi; sleep 5; done; exit 1"
if errorlevel 1 (
  echo GeWeChat API not ready.
  pause
  exit /b 1
)
echo.
echo Ready: http://127.0.0.1:2531/v2/api
echo Open desktop app -^> Channels -^> WeChat GeWeChat -^> Scan login.
pause
