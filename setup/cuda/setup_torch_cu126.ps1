﻿# 把 CPU 版 torch 换成 CUDA 12.6 版（与已安装的 CUDA Toolkit 12.6 匹配）
# 轮子来自阿里云 pytorch-wheels 镜像，下载到 .\pytorch_wheels 后本地安装，装完删除
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$py      = 'python'
$mirror  = 'https://mirrors.sjtug.sjtu.edu.cn/pytorch-wheels/cu126'
$staging = '.\pytorch_wheels'
New-Item -ItemType Directory -Force -Path $staging | Out-Null

$wheels = @(
    'torch-2.11.0%2Bcu126-cp313-cp313-win_amd64.whl',
    'torchaudio-2.11.0%2Bcu126-cp313-cp313-win_amd64.whl'
)

foreach ($w in $wheels) {
    $name = [uri]::UnescapeDataString($w)
    $out  = Join-Path $staging $name
    if ((Test-Path -LiteralPath $out) -and (Get-Item -LiteralPath $out).Length -gt 10MB) {
        Write-Output ("[skip] 已存在: $name")
        continue
    }
    Write-Output ("[downloading] $name")
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        & curl.exe -sS -L -C - --retry 3 --retry-delay 3 --retry-all-errors -o $out "$mirror/$w"
        if ($LASTEXITCODE -eq 0) { break }
        Write-Output ("  第 $attempt 次中断，3 秒后断点续传 ...")
        Start-Sleep -Seconds 3
    }
    if (Test-Path -LiteralPath $out) {
        Write-Output ("  -> " + [math]::Round((Get-Item -LiteralPath $out).Length / 1MB, 1) + " MB")
    } else {
        Write-Output "  下载失败"; exit 1
    }
}

Write-Output "[uninstall] 移除 CPU 版 torch/torchaudio"
& $py -m pip uninstall -y torch torchaudio 2>&1 | Select-Object -Last 3

Write-Output "[install] 安装 CUDA 版"
$files = $wheels | ForEach-Object { Join-Path $staging ([uri]::UnescapeDataString($_)) }
& $py -m pip install --no-deps @files 2>&1 | Select-Object -Last 6

Write-Output "[verify]"
& $py -c "import torch, torchaudio; print('torch', torch.__version__); print('torchaudio', torchaudio.__version__); print('cuda available:', torch.cuda.is_available()); print('device:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'N/A'); print('cuda ver:', torch.version.cuda); x=torch.randn(2000,2000,device='cuda'); y=(x@x).sum().item(); print('gpu matmul ok, sum=', round(y,1))" 2>&1

Write-Output "[cleanup]"
Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
Write-Output "ALL_DONE"
