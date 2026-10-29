#Requires -Version 5.1
<#
.SYNOPSIS
  Manage the autoreview-bot Compose stack (base + optional overlays).

.EXAMPLE
  .\scripts\stack.ps1 up
  .\scripts\stack.ps1 up -Observability -Container
  .\scripts\stack.ps1 status
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("up", "down", "restart", "logs", "status")]
    [string]$Command,

    [switch]$Observability,
    [switch]$Container,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

function Get-DotEnvValue([string]$Name) {
    $envVal = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($envVal)) {
        return $envVal.Trim()
    }
    $envFile = Join-Path $Root ".env"
    if (-not (Test-Path $envFile)) {
        return ""
    }
    foreach ($line in Get-Content -LiteralPath $envFile) {
        $trim = $line.Trim()
        if ($trim.StartsWith("#") -or -not $trim.Contains("=")) {
            continue
        }
        $parts = $trim.Split("=", 2)
        if ($parts[0].Trim() -eq $Name) {
            return $parts[1].Trim().Trim('"').Trim("'")
        }
    }
    return ""
}

function Assert-LauncherToken {
    $token = Get-DotEnvValue "LAUNCHER_TOKEN"
    if ([string]::IsNullOrWhiteSpace($token) -or $token.Length -lt 16) {
        Write-Host @"
LAUNCHER_TOKEN is missing or shorter than 16 characters (required for -Container).

Generate one (do not commit it; put it in .env):

  PowerShell:
    -join ((1..32) | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) })
    # or:
    [Convert]::ToHexString((1..32 | ForEach-Object { Get-Random -Max 256 }))

  openssl:
    openssl rand -hex 32

Then set in .env:
  LAUNCHER_TOKEN=<paste value>
"@
        exit 1
    }
}

function Get-ComposeArgs {
    $composeArgs = @("-f", "docker-compose.yml")
    if ($Observability) {
        $composeArgs += @("-f", "docker-compose.observability.yml")
    }
    if ($Container) {
        $composeArgs += @("-f", "docker-compose.container-runner.yml")
    }
    return $composeArgs
}

if ($Container) {
    Assert-LauncherToken
}

$composeArgs = Get-ComposeArgs

switch ($Command) {
    "up" {
        & docker compose @composeArgs up -d --build @Rest
        exit $LASTEXITCODE
    }
    "down" {
        & docker compose @composeArgs down @Rest
        exit $LASTEXITCODE
    }
    "restart" {
        & docker compose @composeArgs restart @Rest
        exit $LASTEXITCODE
    }
    "logs" {
        & docker compose @composeArgs logs -f --tail=200 @Rest
        exit $LASTEXITCODE
    }
    "status" {
        & docker compose @composeArgs ps @Rest
        exit $LASTEXITCODE
    }
}
