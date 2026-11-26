"""把代理注册/运行为 Windows 服务（pywin32）。**仅 Windows；尚未在真实 Windows 上验证。**

安装服务由 install-agent.ps1 用 sc.exe 完成（不依赖 pywin32 的 HandleCommandLine，避免 PyInstaller 下的路径问题）：
  sc.exe create UKeyAgent binPath= "\"C:\\Program Files\\UKeyAgent\\agent.exe\" service-run --config C:\\ProgramData\\UKeyAgent\\agent.yaml" start= auto obj= LocalSystem
SCM 启动 `agent.exe service-run` → run_service() → 本模块的 ServiceFramework。
"""
import asyncio
import sys
import threading

SERVICE_NAME = "UKeyAgent"
SERVICE_DISPLAY = "UKey Agent"


def run_service(config_path: str) -> int:
    if sys.platform != "win32":
        print("service-run 只能在 Windows 上使用", file=sys.stderr)
        return 2
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    class UKeyAgentService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY
        _svc_description_ = "UKey 管理服务器的客户端代理（演示）"

        def __init__(self, args):
            super().__init__(args)
            self.hwnd = win32event.CreateEvent(None, 0, 0, None)
            self.loop = None
            self.stop_ev = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self.loop and self.stop_ev:
                self.loop.call_soon_threadsafe(self.stop_ev.set)
            win32event.SetEvent(self.hwnd)

        def SvcDoRun(self):
            servicemanager.LogInfoMsg("UKeyAgent starting")
            from agent.agent import AgentConfig, run_agent, setup_logging

            cfg = AgentConfig.load(config_path)
            setup_logging(cfg.log_dir)

            def target():
                self.loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self.loop)
                self.stop_ev = asyncio.Event()
                self.loop.run_until_complete(run_agent(cfg, mock=False, stop=self.stop_ev))

            t = threading.Thread(target=target, daemon=True)
            t.start()
            win32event.WaitForSingleObject(self.hwnd, win32event.INFINITE)
            t.join(timeout=15)

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(UKeyAgentService)
    servicemanager.StartServiceCtrlDispatcher()
    return 0
