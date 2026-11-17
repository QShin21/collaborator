# A stand-in window for the UI Automation checks: one native multi-line text box.
# It is NOT Codex — it only lets tests/test_windows.py exercise reading and filling
# an input box through UI Automation on a real Windows desktop.
param([string]$Title = "Banxing UIA Stand-in")

# Windows PowerShell can run WinForms in an older .NET compatibility mode where
# Ctrl+A does not select all in a multi-line TextBox. This switch enables the
# documented modern behavior *for this stand-in process only*; the paste test
# still sends real Ctrl+A / Ctrl+V to a native multi-line edit control.
[System.AppContext]::SetSwitch("Switch.System.Windows.Forms.DoNotSupportSelectAllShortcutInMultilineTextBox", $false)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.Width = 760
$form.Height = 420
$form.StartPosition = "CenterScreen"
$form.TopMost = $true

$box = New-Object System.Windows.Forms.TextBox
$box.Multiline = $true
$box.AcceptsReturn = $true
$box.ShortcutsEnabled = $true
$box.Dock = "Fill"
$box.Font = New-Object System.Drawing.Font("Segoe UI", 11)
# ASCII only: Windows PowerShell reads a script without a BOM in the ANSI code page.
$box.Text = "stand-in draft 12345"
$form.Controls.Add($box)

$form.Add_Shown({ $form.Activate(); $box.Focus(); $box.SelectionStart = $box.Text.Length })
[System.Windows.Forms.Application]::Run($form)
