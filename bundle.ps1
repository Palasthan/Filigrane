param(
  [string]$Version = "1.0.0",
  [switch]$Sign,
  [string]$CertPfx = "",
  [string]$CertPassword = "",
  [string]$CertThumbprint = "",
  [string]$CertSubject = "",
  [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"

function Find-InnoCompiler {
  $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
  if ($cmd) {
    return $cmd.Source
  }

  $candidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
  )
  return ($candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1)
}

function Find-SignTool {
  $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
  if ($cmd) {
    return $cmd.Source
  }

  $kitRoots = @(
    "${env:ProgramFiles(x86)}\Windows Kits\10\bin",
    "$env:ProgramFiles\Windows Kits\10\bin"
  ) | Where-Object { $_ -and (Test-Path $_) }

  foreach ($root in $kitRoots) {
    $candidate = Get-ChildItem -Path $root -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
      Sort-Object FullName -Descending |
      Select-Object -First 1
    if ($candidate) {
      return $candidate.FullName
    }
  }

  return $null
}

function Get-SignPassword {
  if ($CertPassword) {
    return $CertPassword
  }
  if ($env:FILIGRANE_SIGN_PFX_PASSWORD) {
    return $env:FILIGRANE_SIGN_PFX_PASSWORD
  }
  return ""
}

function Validate-SigningConfiguration {
  if (-not $Sign) {
    return
  }

  $hasPfx = -not [string]::IsNullOrWhiteSpace($CertPfx)
  $hasStoreSelector = (-not [string]::IsNullOrWhiteSpace($CertThumbprint)) -or (-not [string]::IsNullOrWhiteSpace($CertSubject))

  if (-not $hasPfx -and -not $hasStoreSelector) {
    throw "Signing enabled but no certificate provided. Use -CertPfx (recommended) or -CertThumbprint / -CertSubject."
  }
}

function Invoke-CodeSign {
  param(
    [Parameter(Mandatory = $true)][string]$SignToolPath,
    [Parameter(Mandatory = $true)][string]$TargetPath
  )

  if (-not (Test-Path $TargetPath)) {
    throw "Cannot sign missing file: $TargetPath"
  }

  $args = @(
    "sign",
    "/fd", "SHA256",
    "/tr", $TimestampUrl,
    "/td", "SHA256"
  )

  if (-not [string]::IsNullOrWhiteSpace($CertPfx)) {
    $args += @("/f", $CertPfx)
    $pwd = Get-SignPassword
    if (-not [string]::IsNullOrWhiteSpace($pwd)) {
      $args += @("/p", $pwd)
    }
  }
  elseif (-not [string]::IsNullOrWhiteSpace($CertThumbprint)) {
    $args += @("/sha1", $CertThumbprint)
  }
  elseif (-not [string]::IsNullOrWhiteSpace($CertSubject)) {
    $args += @("/n", $CertSubject)
  }
  else {
    $args += "/a"
  }

  $args += $TargetPath

  Write-Host "==> Signing $TargetPath"
  & $SignToolPath @args
  if ($LASTEXITCODE -ne 0) {
    throw "signtool failed for $TargetPath (exit code $LASTEXITCODE)"
  }
}

Validate-SigningConfiguration

$appExe = ".\\dist\\Filigrane\\Filigrane.exe"
$setupExe = ".\\installer-dist\\Filigrane-Setup-$Version.exe"

Write-Host "==> Build PyInstaller (version $Version)"

pyinstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name Filigrane `
  --icon=img\filig.ico `
  --add-data "img;img" `
  --collect-all PIL `
  --collect-all rawpy `
  --hidden-import PIL._tkinter_finder `
  .\filigrane.pyw

if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller build failed."
}

$signToolPath = $null
if ($Sign) {
  $signToolPath = Find-SignTool
  if (-not $signToolPath) {
    throw "signtool.exe not found. Install Windows SDK signing tools."
  }

  Invoke-CodeSign -SignToolPath $signToolPath -TargetPath $appExe
}

$iscc = Find-InnoCompiler
if (-not $iscc) {
  Write-Warning "Inno Setup (ISCC.exe) not found. Installer was not generated."
  Write-Host "App only: $appExe"
  exit 0
}

if (-not (Test-Path ".\\installer.iss")) {
  throw "Missing installer.iss at project root."
}

Write-Host "==> Build Inno Setup installer"
& $iscc "/DMyAppVersion=$Version" ".\\installer.iss"
if ($LASTEXITCODE -ne 0) {
  throw "Inno Setup compilation failed."
}

if ($Sign) {
  Invoke-CodeSign -SignToolPath $signToolPath -TargetPath $setupExe
}

Write-Host "Done."
Write-Host "App:   $appExe"
Write-Host "Setup: $setupExe"
