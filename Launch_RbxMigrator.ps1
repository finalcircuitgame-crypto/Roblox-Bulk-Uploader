$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-Location -LiteralPath $PSScriptRoot

function Test-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [string[]]$PrefixArgs = @()
    )

    $probe = "import json,sys; print(json.dumps({'executable':sys.executable,'major':sys.version_info.major,'minor':sys.version_info.minor}))"
    try {
        $raw = (& $Command @PrefixArgs -c $probe 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($raw)) { return $null }
        $info = $raw | ConvertFrom-Json
        $major = [int]$info.major
        $minor = [int]$info.minor
        if (($major -gt 3) -or ($major -eq 3 -and $minor -ge 10)) {
            return [pscustomobject]@{
                Path = [string]$info.executable
                Version = "$major.$minor"
            }
        }
    } catch {
        return $null
    }
    return $null
}

function Find-Python {
    $py = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($null -ne $py) {
        # Prefer the Python line tested with RbxMigrator, then other supported versions.
        foreach ($spec in @('-3.13', '-3.14', '-3.12', '-3.11', '-3.10', '-3')) {
            $found = Test-PythonCandidate -Command $py.Source -PrefixArgs @($spec)
            if ($null -ne $found) { return $found }
        }
    }

    $pythonCommand = Get-Command 'python' -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommand -and $pythonCommand.Source -notmatch '\\WindowsApps\\') {
        $found = Test-PythonCandidate -Command $pythonCommand.Source
        if ($null -ne $found) { return $found }
    }

    # Check common per-user and machine install locations even if PATH has not refreshed yet.
    $roots = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python'),
        (Join-Path $env:ProgramFiles 'Python')
    )
    if (${env:ProgramFiles(x86)}) {
        $roots += (Join-Path ${env:ProgramFiles(x86)} 'Python')
    }
    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        foreach ($directory in (Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)) {
            $candidate = Join-Path $directory.FullName 'python.exe'
            if (Test-Path -LiteralPath $candidate) {
                $found = Test-PythonCandidate -Command $candidate
                if ($null -ne $found) { return $found }
            }
        }
    }
    return $null
}

try {
    Write-Host 'RbxMigrator v3.35 — first-run setup' -ForegroundColor Cyan
    Write-Host 'Checking for a compatible Python installation...' -ForegroundColor Cyan

    $python = Find-Python
    if ($null -eq $python) {
        $winget = Get-Command 'winget' -ErrorAction SilentlyContinue
        if ($null -ne $winget) {
            Write-Host 'Python 3.13+ was not found. Trying to install Python 3.13 with winget...' -ForegroundColor Yellow
            try {
                & $winget.Source install --id Python.Python.3.13 --exact --scope user --silent --accept-package-agreements --accept-source-agreements
            } catch {
                Write-Host 'winget installation did not complete; trying the official Python installer instead.' -ForegroundColor Yellow
            }
            Start-Sleep -Seconds 2
            $python = Find-Python
        }
    }

    if ($null -eq $python) {
        # Official python.org fallback. Installs for the current user and does not require admin rights.
        $pythonVersion = '3.13.17'
        $architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
        if ($architecture -eq 'Arm64') {
            $installerName = "python-$pythonVersion-arm64.exe"
        } elseif ([Environment]::Is64BitOperatingSystem) {
            $installerName = "python-$pythonVersion-amd64.exe"
        } else {
            $installerName = "python-$pythonVersion.exe"
        }
        $installerUrl = "https://www.python.org/ftp/python/$pythonVersion/$installerName"
        $installerPath = Join-Path $env:TEMP $installerName

        Write-Host "Downloading Python $pythonVersion from python.org..." -ForegroundColor Yellow
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $installerUrl -OutFile $installerPath -UseBasicParsing
        } catch {
            throw "Could not download Python from python.org. Check your internet connection and install Python 3.13+ manually. URL: $installerUrl. Details: $($_.Exception.Message)"
        }

        Write-Host 'Installing Python for the current Windows user...' -ForegroundColor Yellow
        $signature = Get-AuthenticodeSignature -FilePath $installerPath
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
            Remove-Item -LiteralPath $installerPath -Force -ErrorAction SilentlyContinue
            throw 'The downloaded Python installer did not have a valid Python Software Foundation signature; refusing to run it.'
        }

        $installerArgs = @(
            '/quiet',
            'InstallAllUsers=0',
            'PrependPath=0',
            'Include_pip=1',
            'Include_launcher=1',
            'InstallLauncherAllUsers=0',
            'Include_test=0',
            'Include_doc=0',
            'Shortcuts=0'
        )
        $installerProcess = Start-Process -FilePath $installerPath -ArgumentList $installerArgs -Wait -PassThru
        Remove-Item -LiteralPath $installerPath -Force -ErrorAction SilentlyContinue
        if ($installerProcess.ExitCode -ne 0 -and $installerProcess.ExitCode -ne 3010) {
            throw "Python installer exited with code $($installerProcess.ExitCode). Install Python 3.13+ manually and run this script again."
        }

        Start-Sleep -Seconds 2
        $python = Find-Python
    }

    if ($null -eq $python) {
        throw 'Python installation finished, but a compatible python.exe could not be found. Install Python 3.10 or newer and run this script again.'
    }
    Write-Host "Using Python $($python.Version): $($python.Path)" -ForegroundColor Green

    # Keep dependencies isolated in this extracted folder, avoiding administrator permissions.
    $venvPath = Join-Path $PSScriptRoot '.venv'
    $venvPython = Join-Path $venvPath 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Host 'Creating the RbxMigrator virtual environment...' -ForegroundColor Cyan
        & $python.Path -m venv $venvPath
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
            throw 'Could not create the Python virtual environment. Check that this folder is writable and try again.'
        }
    }

    & $venvPython -c 'import requests' 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Installing the required requests package...' -ForegroundColor Cyan
        $requirementsFile = Join-Path $PSScriptRoot 'requirements.txt'
        & $venvPython -m pip install -r $requirementsFile
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not install the requests dependency. Check your internet connection and try again.'
        }
    } else {
        Write-Host 'Required Python dependency is already installed.' -ForegroundColor Green
    }

    # Install a local Studio plugin only if a matching RbxMigrator plugin file is not already present.
    $pluginSource = Join-Path $PSScriptRoot 'RbxMigrator_Studio.plugin.lua'
    if (-not (Test-Path -LiteralPath $pluginSource)) {
        throw "Plugin source file is missing: $pluginSource"
    }
    $pluginDirectory = Join-Path $env:LOCALAPPDATA 'Roblox\Plugins'
    $installedPlugin = $null
    if (Test-Path -LiteralPath $pluginDirectory) {
        $installedPlugin = Get-ChildItem -LiteralPath $pluginDirectory -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '(?i)^RbxMigrator.*\.(lua|rbxm|rbxmx)$' } |
            Select-Object -First 1
    }

    if ($null -ne $installedPlugin) {
        Write-Host "RbxMigrator Studio plugin already exists ($($installedPlugin.Name)); leaving it unchanged." -ForegroundColor Green
    } else {
        New-Item -ItemType Directory -Path $pluginDirectory -Force | Out-Null
        $pluginDestination = Join-Path $pluginDirectory 'RbxMigrator.lua'
        Copy-Item -LiteralPath $pluginSource -Destination $pluginDestination -Force
        Write-Host "Installed the RbxMigrator Studio plugin to: $pluginDestination" -ForegroundColor Green
        Write-Host 'If Roblox Studio is already open, close and reopen it to load the plugin.' -ForegroundColor Yellow
    }

    # The batch launcher starts the app; pass it the exact venv interpreter we just prepared.
    $env:RBXMIGRATOR_PYTHON = $venvPython
    $batchLauncher = Join-Path $PSScriptRoot 'Launch_RbxMigrator.bat'
    if (-not (Test-Path -LiteralPath $batchLauncher)) {
        throw "Batch launcher is missing: $batchLauncher"
    }

    Write-Host 'Setup complete. Launching Launch_RbxMigrator.bat...' -ForegroundColor Green
    & $batchLauncher
    $appExitCode = $LASTEXITCODE
    if ($null -eq $appExitCode) { $appExitCode = 0 }
    exit $appExitCode
} catch {
    Write-Host ''
    Write-Host "RbxMigrator setup failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'No administrator rights are normally required. Review the message above, then try again.' -ForegroundColor Yellow
    Read-Host 'Press Enter to close this window'
    exit 1
}
