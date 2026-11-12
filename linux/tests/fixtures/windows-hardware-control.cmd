@echo off
setlocal
rem Observe the actual Windows compatibility failure; a timeout is not a pass.
:wait
find "CHWReqChecker: checked TpmVersion, found HardBlock." X:\$WINDOWS.~BT\Sources\Panther\setupact.log >nul 2>&1
if errorlevel 1 goto delay
find "CHWReqChecker: checked Memory, found HardBlock." X:\$WINDOWS.~BT\Sources\Panther\setupact.log >nul 2>&1
if errorlevel 1 goto delay
find "CHWReqChecker: checked UefiSecureBoot, found HardBlock." X:\$WINDOWS.~BT\Sources\Panther\setupact.log >nul 2>&1
if not errorlevel 1 goto blocked
:delay
cscript.exe //nologo X:\RufusWait.vbs 5000 >nul
goto wait
:blocked
for %%D in (C D E F G H I J K L M N O P Q R S T U V W Y Z) do if exist "%%D:\sources\boot.wim" set "media=%%D:"
if not defined media exit /b 1
> "%media%\rufus-hardware-control.txt" echo RUFUS_HARDWARE_CONTROL @NONCE@
reg query HKLM\SYSTEM\Setup\LabConfig >> "%media%\rufus-hardware-control.txt" 2>&1
copy /y X:\$WINDOWS.~BT\Sources\Panther\setuperr.log "%media%\rufus-control-errors.log" >nul
copy /y X:\$WINDOWS.~BT\Sources\Panther\setupact.log "%media%\rufus-control-activity.log" >nul
rem Retain a screenshot of the blocked Setup UI before ending the private VM.
cscript.exe //nologo X:\RufusWait.vbs 40000 >nul
wpeutil shutdown
