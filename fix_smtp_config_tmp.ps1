$ErrorActionPreference = "Stop"
$root = "C:\Users\Дмитрий\Desktop\мгюа_projects\мгюа_project1"

$envExamplePath = Join-Path $root ".env.example"
$envExample = Get-Content -LiteralPath $envExamplePath -Raw -Encoding UTF8
$envExample = $envExample -replace 'AI_MONITOR_SMTP_PASSWORD=.*', 'AI_MONITOR_SMTP_PASSWORD='
$envExample = $envExample -replace 'AI_MONITOR_SMTP_PORT=.*', 'AI_MONITOR_SMTP_PORT=465'
if ($envExample -notmatch 'AI_MONITOR_SMTP_SSL=') {
    $envExample = $envExample -replace '(AI_MONITOR_SMTP_TIMEOUT=.*)', "`$1`r`nAI_MONITOR_SMTP_SSL=true`r`nAI_MONITOR_SMTP_TLS=false"
} else {
    $envExample = $envExample -replace 'AI_MONITOR_SMTP_SSL=.*', 'AI_MONITOR_SMTP_SSL=true'
    $envExample = $envExample -replace 'AI_MONITOR_SMTP_TLS=.*', 'AI_MONITOR_SMTP_TLS=false'
}
Set-Content -LiteralPath $envExamplePath -Value $envExample -Encoding UTF8

$emailConfigPath = Join-Path $root "email_config.json"
$config = Get-Content -LiteralPath $emailConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$config.smtp_port = 465
$config.use_tls = $false
$config.use_ssl = $true
$config.timeout = 90
$config | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $emailConfigPath -Encoding UTF8

$envPath = Join-Path $root ".env"
$envText = Get-Content -LiteralPath $envPath -Raw -Encoding UTF8
$envText = $envText -replace 'AI_MONITOR_SMTP_PORT=.*', 'AI_MONITOR_SMTP_PORT=465'
if ($envText -notmatch 'AI_MONITOR_SMTP_SSL=') {
    $envText = $envText.TrimEnd() + "`r`nAI_MONITOR_SMTP_SSL=true`r`nAI_MONITOR_SMTP_TLS=false`r`n"
} else {
    $envText = $envText -replace 'AI_MONITOR_SMTP_SSL=.*', 'AI_MONITOR_SMTP_SSL=true'
    $envText = $envText -replace 'AI_MONITOR_SMTP_TLS=.*', 'AI_MONITOR_SMTP_TLS=false'
}
Set-Content -LiteralPath $envPath -Value $envText -Encoding UTF8

Write-Host "SMTP config normalized: Yandex 465 SSL. Password value was not printed."
