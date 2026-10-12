# run.ps1 — ассистент разработчика с RAG по документации и MCP (день 31).
#
#   .\run.ps1                                  # чат о самом проекте ассистента
#   .\run.ps1 -Project "C:\папка\мой-проект"   # чат о другом проекте (лучше папка git-репозитория)
#   .\run.ps1 -Mode index -Project "..."       # только собрать индекс документации
#   .\run.ps1 -Mode test                       # проверки ТЗ на имитации (ключ и интернет не нужны)
#
# Ключ AITunnel спрашивается скрытым вводом и в файлы не сохраняется.
# Если Windows блокирует запуск:  powershell -ExecutionPolicy Bypass -File .\run.ps1

param(
    [ValidateSet("ask", "index", "test")]
    [string]$Mode = "ask",
    [string]$Project = $PSScriptRoot,
    [ValidateSet("aitunnel", "ollama")]
    [string]$Llm = "aitunnel",
    [string]$Model = ""
)

Set-Location $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

function Find-Python {
    $installed = @(Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | ForEach-Object { $_.FullName })
    foreach ($candidate in @("py", "python") + $installed) {
        try {
            $output = & $candidate --version 2>&1
            if ($output -match "Python \d") { return $candidate }
        }
        catch { }
    }
    return $null
}

$py = Find-Python
if (-not $py) { Write-Host "Python не найден. Установите: winget install Python.Python.3.12 --scope user" -ForegroundColor Red; exit 1 }
Write-Host "Python: $py" -ForegroundColor Green

# numpy нужен для поиска по документации; ставится один раз
& $py -c "import numpy" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Ставлю numpy (один раз)..." -ForegroundColor Cyan
    & $py -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Write-Host "Не удалось установить numpy. Проверьте интернет и повторите." -ForegroundColor Red; exit 1 }
}

if ($Mode -eq "test") {
    & $py .\test_day31.py
    Write-Host "`nОтчёт: $PSScriptRoot\output\test_report.md" -ForegroundColor Green
    exit $LASTEXITCODE
}

if (-not (Test-Path $Project)) { Write-Host "Папка проекта не найдена: $Project" -ForegroundColor Red; exit 1 }
$Project = (Resolve-Path $Project).Path

$assistantArgs = @(".\assistant.py", "--project", $Project, "--llm", $Llm)
if ($Model) { $assistantArgs += @("--model", $Model) }

if ($Mode -eq "index") {
    & $py @assistantArgs --build
    exit $LASTEXITCODE
}

Write-Host "Проект: $Project" -ForegroundColor Cyan
Write-Host "Команды в чате: /help <вопрос>, /branch, /files, /reindex, /exit`n" -ForegroundColor Yellow
& $py @assistantArgs
exit $LASTEXITCODE
