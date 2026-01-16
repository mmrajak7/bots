$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\Kayal.lnk")
$Shortcut.TargetPath = "C:\Users\mail2\Documents\Projects\BOTS\Scalper\launch_kayal.bat"
$Shortcut.WorkingDirectory = "C:\Users\mail2\Documents\Projects\BOTS\Scalper"
$Shortcut.Description = "Kayal Trading Terminal"
$Shortcut.Save()
Write-Host "Desktop shortcut created!"
