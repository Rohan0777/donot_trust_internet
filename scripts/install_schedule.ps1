# 일일 수집을 Windows 작업 스케줄러에 등록한다.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_schedule.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_schedule.ps1 -Remove
#
# 하루 2회 도는 이유: 4chan은 살아있는 카탈로그만 제공하고 만료된 스레드는
# 영구 삭제된다. 폴링 간격이 길수록 그 사이에 사라진 스레드를 통째로 놓친다.
# 뉴스는 소급이 되므로 1회로 충분하지만, 어차피 같은 파이프라인이라 함께 돈다.
#
# 노트북이 꺼져 있어 놓친 날은 --catchup 범위 안에서 자동 보충된다(4chan 제외).

param(
    [string]$TaskName = "TrustNoInternet-Daily",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "등록 해제: $TaskName"
    exit 0
}

$python = (Get-Command python).Source
if (-not $python) { throw "python을 PATH에서 찾을 수 없습니다." }

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "-m scripts.daily --catchup 7" -WorkingDirectory $root

# 09:10 = 국내 장 시작 직후, 21:10 = 미국 장 시작 무렵.
$triggers = @(
    (New-ScheduledTaskTrigger -Daily -At 9:10AM),
    (New-ScheduledTaskTrigger -Daily -At 9:10PM)
)

# 노트북 환경 전제: 배터리로도 돌고, 부팅이 늦어 놓친 실행은 곧바로 따라잡는다.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings -Description "인터넷을 믿지 마세요 — 일일 수집/채점" -Force | Out-Null

Write-Host "등록 완료: $TaskName  (매일 09:10, 21:10)"
Write-Host "  작업 폴더 : $root"
Write-Host "  로그      : logs\daily.log"
Write-Host ""
Write-Host "즉시 실행 확인:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "상태 조회    :  Get-ScheduledTaskInfo -TaskName $TaskName"
