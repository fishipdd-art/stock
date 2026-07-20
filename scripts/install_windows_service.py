"""
Windows service installer (runs scheduler as a Windows service).

Usage:
  python scripts/install_windows_service.py install
  python scripts/install_windows_service.py uninstall
  python scripts/install_windows_service.py start
  python scripts/install_windows_service.py stop
"""
from __future__ import annotations

import sys
from pathlib import Path


SERVICE_NAME = "SupplyChainStockAnalysis"
SERVICE_DISPLAY = "Supply Chain Stock Analysis Scheduler"
SERVICE_DESC = "Daily supply chain news and price collection"


def install():
    """Install as Windows service using NSSM or pywin32."""
    if sys.platform != "win32":
        print("[ERROR] This script only runs on Windows")
        sys.exit(1)

    project_root = Path(__file__).resolve().parent.parent
    python_exe = project_root / ".venv" / "Scripts" / "python.exe"
    main_py = project_root / "main.py"

    print("Installing Windows service...")
    print(f"  Python: {python_exe}")
    print(f"  Main: {main_py}")

    try:
        import win32serviceutil
        import win32service
        import win32event
        import servicemanager

        class StockAnalysisService(win32service.ServiceFramework):
            _svc_name_ = SERVICE_NAME
            _svc_display_name_ = SERVICE_DISPLAY
            _svc_description_ = SERVICE_DESC

            def __init__(self, args):
                win32service.ServiceFramework.__init__(self, args)
                self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)

            def SvcStop(self):
                self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
                win32event.SetEvent(self.hWaitStop)

            def SvcDoRun(self):
                servicemanager.LogMsg(
                    servicemanager.EVENTLOG_INFORMATION_TYPE,
                    servicemanager.PYS_SERVICE_STARTED,
                    (self._svc_name_, "")
                )
                self.main()

            def main(self):
                from scheduler import run_forever
                run_forever()

        win32serviceutil.HandleCommandLine(StockAnalysisService)
    except ImportError:
        print("[INFO] pywin32 not installed. Trying NSSM approach...")
        install_nssm(project_root, python_exe, main_py)


def install_nssm(project_root, python_exe, main_py):
    """Fallback: use NSSM (Non-Sucking Service Manager)."""
    print("Please ensure NSSM is installed and on PATH.")
    print(f"Then run:")
    print(f'  nssm install {SERVICE_NAME} "{python_exe}" "{main_py} run"')
    print(f'  nssm set {SERVICE_NAME} AppDirectory "{project_root}"')
    print(f'  nssm set {SERVICE_NAME} DisplayName "{SERVICE_DISPLAY}"')
    print(f'  nssm set {SERVICE_NAME} Description "{SERVICE_DESC}"')
    print(f'  nssm set {SERVICE_NAME} Start SERVICE_AUTO_START')


def uninstall():
    if sys.platform != "win32":
        print("[ERROR] This script only runs on Windows")
        sys.exit(1)
    try:
        import win32serviceutil
        win32serviceutil.RemoveService(SERVICE_NAME)
        print(f"Removed service: {SERVICE_NAME}")
    except ImportError:
        print(f"Run: nssm remove {SERVICE_NAME} confirm")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: install_windows_service.py {install|uninstall}")
        sys.exit(1)
    if sys.argv[1] == "install":
        install()
    elif sys.argv[1] == "uninstall":
        uninstall()
    else:
        print(f"Unknown: {sys.argv[1]}")
        sys.exit(1)