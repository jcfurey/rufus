# Test instrumentation, launched in the real first user's session after OOBE.
$ErrorActionPreference = 'Stop'
$nonce = '@NONCE@'
$expectedUser = '@USER@'
$report = [ordered]@{ nonce = $nonce; stage = 'first-logon'; errors = @() }
function Read-Value($path, $name) {
    try { return Get-ItemPropertyValue -LiteralPath $path -Name $name -ErrorAction Stop }
    catch { return $null }
}
function Send-Record($name, $value) {
    $port = New-Object System.IO.Ports.SerialPort 'COM1',115200,'None',8,'One'
    try {
        $port.Open()
        $port.WriteLine($name + ' ' + $value)
        Start-Sleep -Seconds 2
    } finally { $port.Dispose() }
}
try {
    Send-Record 'RUFUS_OOBE_CHECK_STARTED' $nonce
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $session = (Get-Process -Id $PID).SessionId
    $report.username = $env:USERNAME
    $report.identity = $identity.Name
    $report.sid = $identity.User.Value
    $report.session = $session
    $report.interactive = [Environment]::UserInteractive
    $report.profile = $env:USERPROFILE
    $report.systemRoot = $env:SystemRoot
    $deadline = (Get-Date).AddMinutes(15)
    do {
        $imageState = Read-Value 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Setup\State' 'ImageState'
        $setup = Read-Value 'HKLM:\SYSTEM\Setup' 'SystemSetupInProgress'
        $oobe = Read-Value 'HKLM:\SYSTEM\Setup' 'OOBEInProgress'
        $explorer = @(Get-Process explorer -ErrorAction SilentlyContinue | Where-Object SessionId -eq $session)
        if ($imageState -eq 'IMAGE_STATE_COMPLETE' -and $setup -eq 0 -and $oobe -eq 0 -and $explorer.Count -gt 0) { break }
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)
    $report.setup = @{ imageState = $imageState; systemSetupInProgress = $setup; oobeInProgress = $oobe }
    $report.explorerPids = @($explorer | ForEach-Object Id)
    $report.explorerOwners = @(Get-CimInstance Win32_Process -Filter "name='explorer.exe'" |
        Where-Object SessionId -eq $session | ForEach-Object { (Invoke-CimMethod -InputObject $_ -MethodName GetOwnerSid).Sid })
    $user = Get-LocalUser -Name $expectedUser
    $report.account = @{ name = $user.Name; sid = $user.SID.Value; enabled = $user.Enabled; source = [string]$user.PrincipalSource }
    $report.timezone = (Get-TimeZone).Id
    $report.culture = (Get-Culture).Name
    $report.systemLocale = (Get-WinSystemLocale).Name
    $report.uiLanguage = [Globalization.CultureInfo]::CurrentUICulture.Name
    $report.languages = @(Get-WinUserLanguageList | ForEach-Object LanguageTag)
    $report.version = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' |
        Select-Object ProductName,EditionID,CurrentBuild,UBR,DisplayVersion
    $report.privacy = @{
        advertising = Read-Value 'HKCU:\Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo' 'Enabled'
        tailored = Read-Value 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Privacy' 'TailoredExperiencesWithDiagnosticDataEnabled'
        telemetry = Read-Value 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\DataCollection' 'AllowTelemetry'
        implicitText = Read-Value 'HKCU:\Software\Microsoft\InputPersonalization' 'RestrictImplicitTextCollection'
        implicitInk = Read-Value 'HKCU:\Software\Microsoft\InputPersonalization' 'RestrictImplicitInkCollection'
        location = Read-Value 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location' 'Value'
        protectYourPC = Read-Value 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE' 'ProtectYourPC'
    }
    $report.preventDeviceEncryption = Read-Value 'HKLM:\SYSTEM\CurrentControlSet\Control\BitLocker' 'PreventDeviceEncryption'
    $report.bitlocker = @(& manage-bde.exe -status $env:SystemDrive 2>&1 | ForEach-Object { [string]$_ })
    $report.bypassNRO = Read-Value 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE' 'BypassNRO'
    $report.portableOperatingSystem = Read-Value 'HKLM:\SYSTEM\CurrentControlSet\Control' 'PortableOperatingSystem'
    $report.sanPolicy = Read-Value 'HKLM:\SYSTEM\CurrentControlSet\Services\partmgr\Parameters' 'SanPolicy'
    $report.accountDetails = @(& net.exe user $expectedUser 2>&1 | ForEach-Object { [string]$_ })
    $marker = Join-Path $env:USERPROFILE ('rufus-oobe-' + $nonce + '.txt')
    [IO.File]::WriteAllText($marker, $nonce)
    $report.userMarker = $marker
    $report.userMarkerReadback = [IO.File]::ReadAllText($marker)
} catch {
    $report.errors += [string]$_
}
$json = $report | ConvertTo-Json -Depth 8 -Compress
[IO.File]::WriteAllText('C:\rufus-oobe.json', $json)
try { Send-Record 'RUFUS_OOBE_EVIDENCE' ([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))) }
catch { [IO.File]::WriteAllText('C:\rufus-oobe-serial-error.txt', [string]$_) }
& shutdown.exe /s /t 0
