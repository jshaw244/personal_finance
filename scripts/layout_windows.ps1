Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32 {
  [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr hWnd, int X, int Y, int nWidth, int nHeight, bool bRepaint);
}
"@

function Set-WindowGrid {
  param(
    [Parameter(Mandatory=$true)][int[]]$Pids
  )

  # Primary monitor working area
  Add-Type -AssemblyName System.Windows.Forms
  $wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
  $halfW = [int]($wa.Width / 2)
  $halfH = [int]($wa.Height / 2)

  $slots = @(
    @{X=$wa.Left;          Y=$wa.Top;           W=$halfW; H=$halfH}, # top-left
    @{X=$wa.Left+$halfW;   Y=$wa.Top;           W=$halfW; H=$halfH}, # top-right
    @{X=$wa.Left;          Y=$wa.Top+$halfH;    W=$halfW; H=$halfH}, # bottom-left
    @{X=$wa.Left+$halfW;   Y=$wa.Top+$halfH;    W=$halfW; H=$halfH}  # bottom-right
  )

  for ($i=0; $i -lt [Math]::Min($Pids.Count, 4); $i++) {
    $p = Get-Process -Id $Pids[$i] -ErrorAction Stop
    # Wait for the main window handle to appear
    $tries = 0
    while ($p.MainWindowHandle -eq 0 -and $tries -lt 50) {
      Start-Sleep -Milliseconds 100
      $p.Refresh()
      $tries++
    }
    if ($p.MainWindowHandle -ne 0) {
      $s = $slots[$i]
      [Win32]::MoveWindow($p.MainWindowHandle, $s.X, $s.Y, $s.W, $s.H, $true) | Out-Null
    }
  }
}
