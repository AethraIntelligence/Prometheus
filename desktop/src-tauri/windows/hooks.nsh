; Phase 13: an unsupported Windows is told so before anything is installed.
; The supported matrix is in docs/release.md and domain/safety/platform.py;
; Windows 10 22H2 is build 19045.
!include "WinVer.nsh"

!macro NSIS_HOOK_PREINSTALL
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP "Prometheus needs Windows 10 22H2 or newer. Nothing was installed."
    Abort
  ${EndIf}
  ${IfNot} ${AtLeastBuild} 19045
    MessageBox MB_ICONSTOP "Prometheus needs Windows 10 22H2 (build 19045) or newer. Nothing was installed."
    Abort
  ${EndIf}
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "Prometheus needs 64-bit Windows on an x64 processor. Nothing was installed."
    Abort
  ${EndIf}
!macroend
