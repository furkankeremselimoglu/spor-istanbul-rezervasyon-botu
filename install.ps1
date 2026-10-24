[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
# IBB Spor rezervasyon botu - tek satirlik kurulum (Windows).
# Kullanim: irm https://raw.githubusercontent.com/furkankeremselimoglu/spor-istanbul-rezervasyon-botu/main/install.ps1 | iex
# CI icin (bu dosyadan dogrudan calistirinca $args calisir): .\install.ps1 --ci
# Govde kendi kapsaminda calisir: iex altinda kullanicinin oturumunu degistirmez, yarim inen betik calismaz.
& {
    $ErrorActionPreference = 'Stop'

    $ciArg = if ($args -contains '--ci') { '--ci' } else { '' }
    $api = if ($env:IBB_RELEASE_API) { $env:IBB_RELEASE_API } else { 'https://api.github.com/repos/furkankeremselimoglu/spor-istanbul-rezervasyon-botu/releases/latest' }
    $dest = if ($env:IBB_INSTALL_DIR) { $env:IBB_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA 'Programs\spor-istanbul-rezervasyon-botu' }

    Write-Host "Surum bilgisi aliniyor..."
    $rel = Invoke-RestMethod -Uri $api -Headers @{ Accept = 'application/vnd.github+json' } -UseBasicParsing

    # shortcut: assumes one zip asset per release (true for make_release.py); if a 2nd asset is ever added, parse per-asset
    $asset = $rel.assets | Where-Object { $_.browser_download_url -like '*.zip' } | Select-Object -First 1
    if (-not $asset -or $asset.digest -notmatch '^sha256:[0-9a-f]{64}$') {
        throw "surum paketinin sha256 ozeti yok"
    }
    $digest = $asset.digest.Substring(7)

    $tmp = Join-Path $env:TEMP "ibbspor-install-$PID"
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    try {
        $zipPath = Join-Path $tmp 'release.zip'
        Write-Host "Indiriliyor: $($asset.browser_download_url)"
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath -UseBasicParsing

        $actual = (Get-FileHash -Path $zipPath -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $digest) {
            throw "sha256 uyusmuyor"
        }
        Write-Host "Dogrulandi"

        Expand-Archive -Path $zipPath -DestinationPath $tmp -Force
        $src = Join-Path $tmp 'spor-istanbul-rezervasyon-botu'
        if (-not (Test-Path -LiteralPath $src)) {
            throw "paket icerigi beklenen klasorde degil"
        }

        Write-Host "Kuruluyor: $dest"
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        Get-ChildItem -LiteralPath $src -Force | Copy-Item -Destination $dest -Recurse -Force

        Push-Location -LiteralPath $dest
        try {
            # Native stderr (pip/playwright progress) must not trip 'Stop' when output is redirected; exit code decides.
            $ErrorActionPreference = 'Continue'
            cmd /c install.bat $ciArg
            if ($LASTEXITCODE) {
                throw "install.bat basarisiz ($LASTEXITCODE)"
            }
        }
        finally {
            Pop-Location
        }

        Write-Host "Kurulum klasoru: $dest"
    }
    finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
} @args
