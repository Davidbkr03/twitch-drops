param(
	[string]$Repo = "Davidbkr03/twitch-drops",            # default GitHub repo
	[string]$Branch = "main",
	[string]$ZipUrl = "",          # If provided, overrides Repo/Branch
	[string]$InstallDir = "",      # If empty, a folder named 'TwitchDropAutomator' will be created under the current directory
	[switch]$Quiet,
	[switch]$SkipSelfUpdate         # internal use: prevents recursive self-update
)

# @installer_version: 1.0.0
$LocalInstallerVersion = '1.0.0'

function Compare-SemVer([string]$a, [string]$b) {
	try { $va = [version]$a } catch { $va = [version]::new(0,0,0) }
	try { $vb = [version]$b } catch { $vb = [version]::new(0,0,0) }
	return $va.CompareTo($vb)
}

function Get-RemoteInstallerVersion([string]$content) {
	if ([string]::IsNullOrWhiteSpace($content)) { return $null }
	$match = [regex]::Match($content, '(?im)^\s*#\s*@installer_version:\s*([0-9]+(?:\.[0-9]+){0,3})\s*$')
	if ($match.Success) { return $match.Groups[1].Value }
	$match2 = [regex]::Match($content, "\$LocalInstallerVersion\s*=\s*'([^']+)'")
	if ($match2.Success) { return $match2.Groups[1].Value }
	return $null
}

# Self-update: fetch latest install.ps1 from the target repo and re-run it if newer
if (-not $SkipSelfUpdate) {
	try {
		if (-not [string]::IsNullOrWhiteSpace($Repo) -and -not [string]::IsNullOrWhiteSpace($Branch)) {
			# Ensure TLS1.2
			try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}
			$rawUrl = "https://raw.githubusercontent.com/$Repo/$Branch/install.ps1"
			$remote = $null
			try {
				$remote = (Invoke-WebRequest -Uri $rawUrl -UseBasicParsing -TimeoutSec 20).Content
			} catch {}
			if ($remote) {
				$remoteVer = Get-RemoteInstallerVersion $remote
				if ($remoteVer) {
					$cmp = Compare-SemVer $LocalInstallerVersion $remoteVer
					if ($cmp -lt 0) {
						Write-Host "[INFO] New installer available (local v$LocalInstallerVersion → remote v$remoteVer)." -ForegroundColor Cyan
						$proceed = $true
						if (-not $Quiet) {
							$resp = Read-Host "Update installer and continue? (Y/N)"
							if ($resp -notin @('y','Y')) { $proceed = $false }
						}
						if ($proceed) {
							$tmpUpdate = Join-Path $env:TEMP ("install_new_" + [guid]::NewGuid().ToString() + ".ps1")
							$remote | Set-Content -Path $tmpUpdate -Encoding UTF8
							# Re-run the new installer with the same parameters, but skip self-update to avoid loops
							$argList = @('-NoProfile','-ExecutionPolicy','Bypass','-File',"`"$tmpUpdate`"",'-Repo',"`"$Repo`"",'-Branch',"`"$Branch`"")
							if ($ZipUrl) { $argList += @('-ZipUrl',"`"$ZipUrl`"") }
							if ($InstallDir) { $argList += @('-InstallDir',"`"$InstallDir`"") }
							if ($Quiet) { $argList += '-Quiet' }
							$argList += '-SkipSelfUpdate'
							Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -WindowStyle Hidden | Out-Null
							exit 0
						}
					}
				}
			}
		}
	} catch {}
}

function Write-Info($msg) { Write-Host "[INFO] $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "[ERROR] $msg" -ForegroundColor Red }

# 1) Warning about data location
$cwd = (Get-Location).Path
if ([string]::IsNullOrWhiteSpace($InstallDir)) {
	$InstallDir = Join-Path $cwd 'TwitchDropAutomator'
}
Write-Host "This installer will set up Twitch Drop Automator at:" -ForegroundColor Yellow
Write-Host "  $InstallDir" -ForegroundColor Yellow
Write-Host "All data (logs, user data) will be stored INSIDE this folder." -ForegroundColor Yellow
if (-not $Quiet) {
	$resp = Read-Host "Continue? (Y/N)"
	if ($resp -notin @('y','Y')) { Write-Warn "Aborted by user."; exit 1 }
}

# 2) Determine GitHub download URL
if ([string]::IsNullOrWhiteSpace($ZipUrl)) {
	if ([string]::IsNullOrWhiteSpace($Repo)) {
		Write-Warn "No -Repo or -ZipUrl provided. Please enter your GitHub repo (format: owner/repo)."
		$Repo = Read-Host "GitHub repo (owner/repo)"
		if ([string]::IsNullOrWhiteSpace($Repo)) { Write-Err "A GitHub repo is required."; exit 1 }
	}
	$ZipUrl = "https://github.com/$Repo/archive/refs/heads/$Branch.zip"
}
Write-Info "Repo archive: $ZipUrl"

# 3) Create install directory
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

# 4) Download and extract sources
$tmp = New-Item -ItemType Directory -Force -Path (Join-Path $env:TEMP ("tda_" + [guid]::NewGuid().ToString()))
$zip = Join-Path $tmp 'repo.zip'
try {
	Write-Info "Downloading repository…"
	Invoke-WebRequest -Uri $ZipUrl -OutFile $zip -UseBasicParsing
	Write-Info "Extracting…"
	Expand-Archive -Path $zip -DestinationPath $tmp -Force
	# Move contents of the first extracted folder into InstallDir
	$root = Get-ChildItem -Path $tmp | Where-Object { $_.PSIsContainer -and $_.Name -notlike "*__MACOSX*" } | Select-Object -First 1
	if (-not $root) { Write-Err "Unexpected archive layout."; exit 1 }
	# Copy all files to InstallDir
	Copy-Item -Path (Join-Path $root.FullName '*') -Destination $InstallDir -Recurse -Force
}
finally {
	Remove-Item -Force -ErrorAction SilentlyContinue $zip
}

# 5) Ensure Python is available
function Get-PyPath([string]$versionMajorMinor) {
	$py = "py"
	try {
		$ver = & $py -$versionMajorMinor -c "import sys;print(sys.executable)" 2>$null
		if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($ver)) { return $ver.Trim() }
	} catch {}
	return $null
}

function Ensure-Python {
	$desired = @('3.11','3.12','3.10','3')
	foreach ($v in $desired) {
		$path = Get-PyPath $v
		if ($path) { return @($path,$v) }
	}
	Write-Warn "Python not found. Attempting to install Python 3.11 via winget…"
	# Try winget
	try {
		$winget = Get-Command winget -ErrorAction SilentlyContinue
		if ($winget) {
			& winget install -e --id Python.Python.3.11 --accept-package-agreements --accept-source-agreements --silent | Out-Null
			Start-Sleep -Seconds 5
			$path = Get-PyPath '3.11'
			if ($path) { return @($path,'3.11') }
		}
	} catch {}
	# Fallback: download official installer
	Write-Warn "Falling back to python.org web installer (3.11 x64)."
	$pyUrl = 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe'
	$pyExe = Join-Path $tmp 'python_installer.exe'
	Invoke-WebRequest -Uri $pyUrl -OutFile $pyExe -UseBasicParsing
	# Quiet install to current user, add to PATH
	& $pyExe /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 SimpleInstall=1 | Out-Null
	Start-Sleep -Seconds 5
	$path = Get-PyPath '3.11'
	if ($path) { return @($path,'3.11') }
	Write-Err "Python installation failed. Please install Python manually and re-run."
	exit 1
}

$pyInfo = Ensure-Python
$pyExePath = $pyInfo[0]
$pyVer = $pyInfo[1]
Write-Info "Using Python $pyVer at: $pyExePath"

# 6) Create virtual environment
$venvDir = Join-Path $InstallDir 'venv'
if (-not (Test-Path $venvDir)) {
	Write-Info "Creating venv…"
	& $pyExePath -m venv $venvDir
	if ($LASTEXITCODE -ne 0) { Write-Err "Failed to create virtual environment."; exit 1 }
}
$venvPy = Join-Path $venvDir 'Scripts\python.exe'
$venvPip = Join-Path $venvDir 'Scripts\pip.exe'

# 7) Install dependencies
Write-Info "Upgrading pip…"
& $venvPy -m pip install --upgrade pip
if (Test-Path (Join-Path $InstallDir 'requirements.txt')) {
	Write-Info "Installing requirements…"
	& $venvPip install -r (Join-Path $InstallDir 'requirements.txt')
} else {
	Write-Warn "requirements.txt not found. Skipping."
}

# 8) Install Playwright browsers
Write-Info "Installing Playwright browsers…"
& $venvPy -m playwright install

# 9) Ask to enable startup
$startupChoice = 'N'
if (-not $Quiet) {
	$startupChoice = Read-Host "Start at boot? (Y/N)"
}
$startupChoice = ($startupChoice).ToUpper()
if ($startupChoice -eq 'Y') {
	$startupFolder = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
	if (-not (Test-Path $startupFolder)) { New-Item -ItemType Directory -Force -Path $startupFolder | Out-Null }
	# Create a shortcut pointing to run_automator.bat with working directory set
	$batPath = Join-Path $InstallDir 'run_automator.bat'
	if (-not (Test-Path $batPath)) {
		# Create a default batch if missing
		@(
			'@echo off',
			'cd /d "%~dp0"',
			'.\venv\Scripts\pythonw.exe twitch_drop_automator.py'
		) | Set-Content -Path $batPath -Encoding ASCII
	}
	$shortcutPath = Join-Path $startupFolder 'Twitch Drop Automator.lnk'
	$wsh = New-Object -ComObject WScript.Shell
	$sc = $wsh.CreateShortcut($shortcutPath)
	$sc.TargetPath = $batPath
	$sc.WorkingDirectory = $InstallDir
	$ico = Join-Path $InstallDir 'tray.ico'
	if (Test-Path $ico) { $sc.IconLocation = $ico }
	$sc.Save()
	Write-Info "Startup shortcut created: $shortcutPath"
}

# 10) Launch now
Write-Info "Launching Twitch Drop Automator…"
$batPath = Join-Path $InstallDir 'run_automator.bat'
if (-not (Test-Path $batPath)) {
	@(
		'@echo off',
		'cd /d "%~dp0"',
		'.\venv\Scripts\pythonw.exe twitch_drop_automator.py'
	) | Set-Content -Path $batPath -Encoding ASCII
}
if (Test-Path $batPath) {
	Start-Process -FilePath $batPath -WorkingDirectory $InstallDir -WindowStyle Hidden | Out-Null
	Write-Host "Tip: To log in the first time, right-click the tray icon and untick 'Headless mode'." -ForegroundColor Yellow
	Write-Host "The app will restart and open a browser window. After login, you can re-enable headless." -ForegroundColor Yellow
} else {
	Write-Warn "Could not find run_automator.bat to launch automatically."
}

Write-Host "\nInstall complete." -ForegroundColor Green
Write-Host "- Folder: $InstallDir"
Write-Host "- To run later: double-click 'run_automator.bat' in the install folder." 