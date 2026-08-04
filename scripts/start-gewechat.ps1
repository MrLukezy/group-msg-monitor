# 启动本机 GeWeChat（Docker），供桌面端扫码登录使用。
# 依赖：Docker Desktop 已安装并处于运行状态。
$ErrorActionPreference = "Stop"

$ImageAliyun = "registry.cn-hangzhou.aliyuncs.com/gewe/gewe:latest"
$ImageAlt = "registry.cn-chengdu.aliyuncs.com/tu1h/wechotd:alpine"
$Name = "gewe"
$TempDir = Join-Path $PSScriptRoot "..\data\gewe_temp"
$TempDir = [System.IO.Path]::GetFullPath($TempDir)

function Assert-Docker {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "未找到 docker 命令。请先安装并启动 Docker Desktop，然后重新打开终端再执行本脚本。"
  }
  docker info 1>$null 2>$null
  if ($LASTEXITCODE -ne 0) {
    throw "Docker 引擎未就绪。请打开 Docker Desktop，等待其完全启动后再试。"
  }
}

function Ensure-Image {
  $existing = docker images -q gewe 2>$null
  if ($existing) {
    Write-Host "已有本地镜像 gewe"
    return
  }
  Write-Host "拉取镜像 $ImageAliyun ..."
  docker pull $ImageAliyun
  if ($LASTEXITCODE -eq 0) {
    docker tag $ImageAliyun gewe
    return
  }
  Write-Host "官方镜像拉取失败，尝试备用镜像 $ImageAlt ..."
  docker pull $ImageAlt
  if ($LASTEXITCODE -ne 0) {
    throw "镜像拉取失败，请检查网络后重试。"
  }
  docker tag $ImageAlt gewe
}

Assert-Docker
New-Item -ItemType Directory -Force -Path $TempDir | Out-Null
Ensure-Image

$running = docker ps -q -f "name=^/${Name}$"
if ($running) {
  Write-Host "容器 $Name 已在运行"
} else {
  $exists = docker ps -aq -f "name=^/${Name}$"
  if ($exists) {
    Write-Host "启动已有容器 $Name ..."
    docker start $Name | Out-Null
  } else {
    Write-Host "创建并启动容器 $Name ..."
    # Windows Docker：挂载本地目录；privileged + init 兼容原版镜像
    docker run -d `
      --name $Name `
      --restart unless-stopped `
      --privileged `
      -p 2531:2531 `
      -p 2532:2532 `
      -v "${TempDir}:/root/temp" `
      gewe `
      /usr/sbin/init
    if ($LASTEXITCODE -ne 0) {
      Write-Host "带 /usr/sbin/init 启动失败，改用默认入口..."
      docker rm -f $Name 2>$null | Out-Null
      docker run -d `
        --name $Name `
        --restart unless-stopped `
        -p 2531:2531 `
        -p 2532:2532 `
        -v "${TempDir}:/root/temp" `
        gewe
    }
  }
}

Write-Host "等待 API 就绪 (2531)..."
$ok = $false
for ($i = 1; $i -le 36; $i++) {
  try {
    $resp = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:2531/v2/api/tools/getTokenId" -ContentType "application/json" -Body "{}" -TimeoutSec 5
    if ($resp.ret -eq 200 -or $resp.data) {
      Write-Host "GeWeChat 已就绪: http://127.0.0.1:2531/v2/api"
      Write-Host ("token 预览: " + ([string]$resp.data).Substring(0, [Math]::Min(12, ([string]$resp.data).Length)) + "...")
      $ok = $true
      break
    }
  } catch {
    Start-Sleep -Seconds 5
  }
}
if (-not $ok) {
  Write-Host "容器已启动，但 API 尚未响应。请稍等 1～2 分钟后重试扫码，或执行: docker logs $Name"
  docker ps -a --filter "name=$Name"
  exit 2
}
