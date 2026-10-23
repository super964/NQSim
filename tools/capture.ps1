param(
  [string]$Out = "shot.png",
  [switch]$Light,
  [int]$Settle = 2
)

# 必须先声明 DPI 感知,否则 GetWindowRect 返回的是被虚拟化的逻辑坐标(会截掉窗口右侧/底部)
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class DpiAwake {
  [DllImport("shcore.dll")] public static extern int SetProcessDpiAwareness(int v);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
"@
try { [DpiAwake]::SetProcessDpiAwareness(2) | Out-Null } catch { }
try { [DpiAwake]::SetProcessDPIAware() | Out-Null } catch { }

Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class W {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint msg, IntPtr wp, IntPtr lp);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
}
"@

$found = [IntPtr]::Zero
$cb = [W+EnumProc]{
  param($h, $p)
  if ([W]::IsWindowVisible($h)) {
    $sb = New-Object System.Text.StringBuilder 512
    [void][W]::GetWindowText($h, $sb, 512)
    if ($sb.ToString() -like "*纳指期货*" -or $sb.ToString() -like "*NQ Trading*") { $script:found = $h; return $false }
  }
  return $true
}
[void][W]::EnumWindows($cb, [IntPtr]::Zero)
$h = $found
if ($h -eq [IntPtr]::Zero) { Write-Output "NO_WINDOW"; exit 1 }

if ($Light) {
  [void][W]::ShowWindow($h, 3)
  Start-Sleep -Milliseconds 400
  [void][W]::SetForegroundWindow($h)
  Start-Sleep -Milliseconds 400
  [void][W]::PostMessage($h, 0x0100, [IntPtr]0x71, [IntPtr]::Zero)   # WM_KEYDOWN F2
  [void][W]::PostMessage($h, 0x0101, [IntPtr]0x71, [IntPtr]::Zero)   # WM_KEYUP F2
}
Start-Sleep -Seconds $Settle

$r = New-Object W+RECT
[void][W]::GetWindowRect($h, [ref]$r)
$w = $r.R - $r.L; $hh = $r.B - $r.T
$bmp = New-Object System.Drawing.Bitmap($w, $hh)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
[void][W]::PrintWindow($h, $hdc, 2)
$g.ReleaseHdc($hdc)
$g.Dispose()
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Output "SAVED $Out ($w x $hh)"
