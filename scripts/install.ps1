$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.Net.Http

function Save-LimitedHttpsFile {
    param(
        [Parameter(Mandatory = $true)][Uri]$Uri,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][long]$MaximumBytes
    )

    if ($Uri.Scheme -ne "https" -or $Uri.Host -ne "raw.githubusercontent.com") {
        throw "Refusing untrusted installer URL: $Uri"
    }

    $Handler = [Net.Http.HttpClientHandler]::new()
    $Handler.AllowAutoRedirect = $false
    $Client = [Net.Http.HttpClient]::new($Handler)
    $Client.Timeout = [TimeSpan]::FromSeconds(30)
    $Response = $null
    $InputStream = $null
    $OutputStream = $null
    try {
        $Response = $Client.GetAsync(
            $Uri,
            [Net.Http.HttpCompletionOption]::ResponseHeadersRead
        ).GetAwaiter().GetResult()
        if ([int]$Response.StatusCode -ge 300 -and [int]$Response.StatusCode -lt 400) {
            throw "Refusing installer redirect to $($Response.Headers.Location)"
        }
        $Response.EnsureSuccessStatusCode() | Out-Null
        if (
            $null -ne $Response.Content.Headers.ContentLength -and
            $Response.Content.Headers.ContentLength -gt $MaximumBytes
        ) {
            throw "Installer download exceeded $MaximumBytes bytes."
        }

        $InputStream = $Response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $OutputStream = [IO.File]::Open(
            $Destination,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        $Buffer = [byte[]]::new(65536)
        $Total = 0L
        while (($Read = $InputStream.Read($Buffer, 0, $Buffer.Length)) -gt 0) {
            $Total += $Read
            if ($Total -gt $MaximumBytes) {
                throw "Installer download exceeded $MaximumBytes bytes."
            }
            $OutputStream.Write($Buffer, 0, $Read)
        }
    }
    finally {
        if ($null -ne $OutputStream) { $OutputStream.Dispose() }
        if ($null -ne $InputStream) { $InputStream.Dispose() }
        if ($null -ne $Response) { $Response.Dispose() }
        $Client.Dispose()
        $Handler.Dispose()
    }
}

$Repository = "as791/brain-hub"
$Ref = if ($env:BRAINHUB_REF) { $env:BRAINHUB_REF } else { "main" }
if ($Ref -notmatch "^[A-Za-z0-9._/-]+$" -or $Ref.Contains("..") -or $Ref.StartsWith("/")) {
    throw "Invalid BRAINHUB_REF: $Ref"
}

$PythonCommand = $null
$PythonPrefix = @()
$Candidates = @(
    @{ Command = "py"; Prefix = @("-3") },
    @{ Command = "python"; Prefix = @() },
    @{ Command = "python3"; Prefix = @() }
)

foreach ($Candidate in $Candidates) {
    $CandidateCommand = [string]$Candidate.Command
    $CandidatePrefix = [string[]]$Candidate.Prefix
    if (-not (Get-Command $CandidateCommand -ErrorAction SilentlyContinue)) {
        continue
    }
    & $CandidateCommand @CandidatePrefix -c "import sys; raise SystemExit(sys.version_info < (3, 11))" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $PythonCommand = $CandidateCommand
        $PythonPrefix = $CandidatePrefix
        break
    }
}

if (-not $PythonCommand) {
    throw "Brain Hub requires Python 3.11 or newer. Install it from https://www.python.org/downloads/windows/ and rerun this command."
}

$LocalInstaller = Join-Path $PSScriptRoot "install.py"
$LocalRoot = Split-Path $PSScriptRoot -Parent
if ((Test-Path $LocalInstaller -PathType Leaf) -and (Test-Path (Join-Path $LocalRoot "pyproject.toml") -PathType Leaf)) {
    & $PythonCommand @PythonPrefix $LocalInstaller --source $LocalRoot @args
    exit $LASTEXITCODE
}

$TemporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("brainhub-install-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $TemporaryRoot | Out-Null
$Installer = Join-Path $TemporaryRoot "install.py"
$InstallerUrl = "https://raw.githubusercontent.com/$Repository/$Ref/scripts/install.py"

try {
    Save-LimitedHttpsFile -Uri $InstallerUrl -Destination $Installer -MaximumBytes 1MB
    & $PythonCommand @PythonPrefix $Installer --source "https://github.com/$Repository" --ref $Ref @args
    exit $LASTEXITCODE
}
finally {
    Remove-Item -LiteralPath $TemporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
}
