#!/usr/bin/env python3
"""Native desktop entry point with a private, loopback-only workbench server."""

from __future__ import annotations

import argparse
from http.client import HTTPConnection
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import webbrowser

from app_paths import DATA_DIR_ENV, RESOURCE_ROOT, default_desktop_data_dir


APP_TITLE = "COBOL 业务分析工作台"
CONFIG_TEMPLATE = """# 桌面版接口配置；只保存在本机，不随应用分发。
# 保存后重新载入工作台页面。请使用 UTF-8 编码。
COMPANY_API_BASE_URL=
COMPANY_API_KEY=
COMPANY_CHAT_MODEL=
COMPANY_API_STYLE=openai_compatible

# 可选：私有框架资料的绝对路径，或相对此配置目录的路径。
# FRAMEWORK_REFERENCE_PATH=framework/reference.md
"""


class NativeWindowUnavailable(RuntimeError):
    """A native window could not start; browser compatibility remains possible."""


def prepare_profile(directory: Path | None = None) -> Path:
    selected = directory or os.environ.get(DATA_DIR_ENV) or default_desktop_data_dir()
    root = Path(selected).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    configuration = root / ".env"
    if configuration.is_symlink():
        raise ValueError("DESKTOP_CONFIG_SYMLINK")
    try:
        descriptor = os.open(configuration, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not configuration.is_file():
            raise ValueError("DESKTOP_CONFIG_NOT_FILE") from None
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(CONFIG_TEMPLATE)
    os.environ[DATA_DIR_ENV] = str(root)
    return root


class DesktopServer:
    """Own the background server and stop accepting requests when the UI exits."""

    def __init__(self, *, app=None):
        # Import only after prepare_profile, so configuration never falls back
        # to a source checkout's private deployment files.
        from web_app import create_server

        self.server = create_server(0, app=app)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.1}, daemon=True,
                                       name="desktop-workbench-server")
        self._lock = threading.Lock()
        self._closed = False

    def start(self):
        self.thread.start()
        return self

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.server.app.cancel_event.set()
            if self.thread.is_alive():
                self.server.shutdown()
            self.server.server_close()
            if self.thread.is_alive():
                self.thread.join(timeout=5)


def native_folder_picker(window, webview):
    def choose(initial_path):
        selected = window.create_file_dialog(webview.FileDialog.FOLDER,
                                             directory=str(initial_path), allow_multiple=False)
        return str(selected[0]) if selected else None
    return choose


def _windows_libraries():
    import ctypes

    return (ctypes, ctypes.WinDLL("shell32", use_last_error=True),
            ctypes.WinDLL("ole32", use_last_error=True),
            ctypes.WinDLL("user32", use_last_error=True))


def windows_folder_picker(initial_path: Path) -> str | None:
    """Use the Windows Shell dialog on this request thread, without a runtime."""
    ctypes, shell, ole, user = _windows_libraries()
    pointer = ctypes.c_void_p
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, pointer, ctypes.c_uint,
                                      ctypes.c_ssize_t, ctypes.c_ssize_t)

    class BrowseInfo(ctypes.Structure):
        _fields_ = [("owner", pointer), ("root", pointer),
                    ("display_name", ctypes.POINTER(ctypes.c_wchar)),
                    ("title", ctypes.c_wchar_p), ("flags", ctypes.c_uint),
                    ("callback", callback_type), ("parameter", ctypes.c_ssize_t),
                    ("image", ctypes.c_int)]

    ole.CoInitializeEx.argtypes = [pointer, ctypes.c_uint]
    ole.CoInitializeEx.restype = ctypes.c_int32
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoTaskMemFree.argtypes = [pointer]
    ole.CoTaskMemFree.restype = None
    shell.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BrowseInfo)]
    shell.SHBrowseForFolderW.restype = pointer
    shell.SHGetPathFromIDListEx.argtypes = [pointer, ctypes.POINTER(ctypes.c_wchar),
                                          ctypes.c_uint, ctypes.c_uint]
    shell.SHGetPathFromIDListEx.restype = ctypes.c_int
    user.SendMessageW.argtypes = [pointer, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    user.SendMessageW.restype = ctypes.c_ssize_t
    initial = ctypes.create_unicode_buffer(str(initial_path))

    @callback_type
    def initialize_dialog(window, event, _parameter, _data):
        if event == 1:  # BFFM_INITIALIZED; select the suggested Unicode path.
            user.SendMessageW(window, 0x467, 1, ctypes.addressof(initial))
        return 0

    # Each local HTTP request has its own thread; balance COM only when this
    # invocation initialized it successfully (including the S_FALSE result).
    if ole.CoInitializeEx(None, 0x2) not in {0, 1}:
        raise OSError("FOLDER_PICKER_UNAVAILABLE")
    selected = None
    try:
        display = ctypes.create_unicode_buffer(260)
        info = BrowseInfo(None, None, display, "请选择源码或输出文件夹", 0x51,
                          initialize_dialog, 0, 0)
        selected = shell.SHBrowseForFolderW(ctypes.byref(info))
        if not selected:
            return None
        result = ctypes.create_unicode_buffer(32768)
        if not shell.SHGetPathFromIDListEx(selected, result, len(result), 0):
            raise OSError("FOLDER_PICKER_UNAVAILABLE")
        return result.value
    finally:
        if selected:
            ole.CoTaskMemFree(selected)
        ole.CoUninitialize()


def browser_folder_picker(initial_path: Path) -> str | None:
    if sys.platform == "win32":
        return windows_folder_picker(initial_path)
    if sys.platform == "darwin":
        from web_app import _pick_macos_folder
        return _pick_macos_folder(initial_path)
    raise OSError("FOLDER_PICKER_UNAVAILABLE")


def windows_message_box(message: str, flags: int) -> int:
    import ctypes

    function = ctypes.WinDLL("user32", use_last_error=True).MessageBoxW
    function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    result = function(None, message, APP_TITLE, flags)
    if result == 0:
        raise OSError("DESKTOP_CONTROLLER_UNAVAILABLE")
    return result


def browser_controller(origin: str, root: Path):
    """A visible OS dialog owns the otherwise windowless server's lifetime."""
    while True:
        actions = ("是：打开工作台页面\n否：编辑接口配置，保存后刷新页面\n取消或关闭此窗口：退出工作台并停止分析"
                   if sys.platform == "win32" else
                   "打开页面：进入工作台\n接口配置：编辑配置，保存后刷新页面\n退出：停止工作台与分析")
        message = ("工作台正在系统浏览器中运行。请保留此控制窗口，切换到浏览器进行分析。\n\n"
                   f"{actions}\n\n"
                   f"本机地址：{origin}\n数据目录：{root}\n\n"
                   "关闭浏览器标签页不会退出程序；结束使用时请在此退出工作台。")
        if sys.platform == "win32":
            choice = windows_message_box(message, 0x43)  # Yes / No / Cancel, information.
        elif sys.platform == "darwin":
            script = ('on run argv\n'
                      ' set answer to display dialog (item 1 of argv) with title (item 2 of argv) '
                      'buttons {"退出", "接口配置", "打开页面"} default button "打开页面" cancel button "退出"\n'
                      ' return button returned of answer\nend run')
            result = subprocess.run(["/usr/bin/osascript", "-e", script, message, APP_TITLE],
                                    capture_output=True, text=True, check=False)
            choice = {"打开页面": 6, "接口配置": 7}.get(result.stdout.strip(), 2)
        elif sys.stdin is not None and sys.stdin.isatty():
            choice = {"1": 6, "2": 7}.get(input(f"{origin}\n1 打开页面 / 2 编辑配置 / 回车退出："), 2)
        else:
            raise RuntimeError("DESKTOP_CONTROLLER_UNAVAILABLE")
        if choice == 6:
            webbrowser.open(origin)
        elif choice == 7:
            open_profile(root, edit_config=True)
        else:
            return


def run_browser(root: Path) -> int:
    service = DesktopServer().start()
    try:
        service.server.app.folder_picker = browser_folder_picker
        # If the default browser cannot be opened, the visible controller also
        # gives the local URL so the user can open it in their existing browser.
        webbrowser.open(service.server.origin)
        browser_controller(service.server.origin, root)
    finally:
        service.close()
    return 0


def open_profile(root: Path, *, edit_config=False):
    target = root / ".env" if edit_config else root
    if sys.platform == "win32":
        if edit_config:
            subprocess.Popen(["notepad.exe", str(target)])
        else:
            os.startfile(str(target))
    elif sys.platform == "darwin":
        subprocess.Popen(["/usr/bin/open", "-a", "TextEdit", str(target)] if edit_config
                         else ["/usr/bin/open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


def configure_tls():
    import certifi

    if "SSL_CERT_FILE" not in os.environ:
        os.environ["SSL_CERT_FILE"] = certifi.where()


def show_startup_error():
    message = ("工作台未能启动。请检查应用数据目录中的 desktop.log。"
               "如果公司限制运行应用或本机服务，请联系 IT 确认允许的运行方式。")
    try:
        if sys.platform == "win32":
            windows_message_box(message, 0x10)
        elif sys.platform == "darwin":
            script = "on run argv\n display alert (item 1 of argv) message (item 2 of argv) as critical\nend run"
            subprocess.run(["/usr/bin/osascript", "-e", script, APP_TITLE, message],
                           check=False, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.stderr is not None:
            print(message, file=sys.stderr)
    except Exception:
        if sys.stderr is not None:
            print(message, file=sys.stderr)


def self_test() -> dict:
    """Verify the packaged resources and HTTP server without a GUI or model call."""
    from web_app import STATIC_FILES, WorkbenchState

    for name in set(STATIC_FILES.values()):
        path = RESOURCE_ROOT / "web" / name
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            raise RuntimeError("DESKTOP_WEB_RESOURCE_MISSING")
    fixture = RESOURCE_ROOT / "fixtures" / "framework-workbench"
    if not (fixture / "manifest.json").is_file() or not (fixture / "source").is_dir():
        raise RuntimeError("DESKTOP_DEMO_RESOURCE_MISSING")
    # A fresh in-memory state deliberately avoids restoring user source data.
    service = DesktopServer(app=WorkbenchState()).start()
    try:
        connection = HTTPConnection("127.0.0.1", service.server.server_port, timeout=5)
        try:
            for route in STATIC_FILES:
                connection.request("GET", route)
                response = connection.getresponse()
                if response.status != 200 or not response.read():
                    raise RuntimeError("DESKTOP_STATIC_HTTP_FAILED")
            connection.request("GET", "/api/state")
            response = connection.getresponse()
            state = json.loads(response.read())
            if response.status != 200 or not state.get("session_token"):
                raise RuntimeError("DESKTOP_STATE_HTTP_FAILED")
        finally:
            connection.close()
    finally:
        service.close()
    if service.thread.is_alive():
        raise RuntimeError("DESKTOP_SERVER_SHUTDOWN_FAILED")
    return {"ok": True, "static_files": len(set(STATIC_FILES.values())),
            "loopback_server": True, "server_stopped": True, "model_calls": 0}


def run_desktop(root: Path) -> int:
    try:
        import webview
        from webview.menu import Menu, MenuAction
    except Exception as exc:
        raise NativeWindowUnavailable(type(exc).__name__) from None

    # A server/profile failure is unrelated to renderer availability; do not
    # retry that broken state through another presentation mode.
    service = DesktopServer().start()
    shown = threading.Event()
    try:
        webview.settings["ALLOW_FILE_URLS"] = False
        webview.settings["ALLOW_DOWNLOADS"] = True
        window = webview.create_window(APP_TITLE, service.server.origin, width=1360,
                                       height=900, min_size=(1000, 680), text_select=True)
        service.server.app.folder_picker = native_folder_picker(window, webview)
        window.events.closed += service.close
        window.events.shown += shown.set
        menu = [Menu("工作台", [
            MenuAction("编辑接口配置", lambda: open_profile(root, edit_config=True)),
            MenuAction("打开数据目录", lambda: open_profile(root)),
            MenuAction("重新载入页面", lambda: window.load_url(service.server.origin)),
            MenuAction("退出", window.destroy),
        ])]
        webview.start(debug=False, private_mode=True, menu=menu,
                      gui="edgechromium" if sys.platform == "win32" else None)
    except Exception as exc:
        if not shown.is_set():
            raise NativeWindowUnavailable(type(exc).__name__) from None
        raise
    finally:
        service.close()
    return 0


def launch(root: Path, *, browser=False) -> int:
    if browser:
        return run_browser(root)
    try:
        return run_desktop(root)
    except NativeWindowUnavailable as exc:
        if sys.platform != "win32":
            raise
        logging.warning("NATIVE_WINDOW_UNAVAILABLE %s; using browser", exc)
        return run_browser(root)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="Override this user's desktop data directory.")
    parser.add_argument("--open-config", action="store_true", help="Open local API settings and exit.")
    parser.add_argument("--browser", action="store_true", help="Use the existing system browser without an embedded renderer.")
    parser.add_argument("--self-test", action="store_true", help="Check packaged resources without opening a window.")
    parser.add_argument("--self-test-result", "--self-test-report", dest="self_test_result", type=Path,
                        help="Write the headless check result to this JSON file.")
    args = parser.parse_args(argv)
    if args.self_test_result and not args.self_test:
        parser.error("--self-test-result requires --self-test")
    try:
        root = prepare_profile(args.data_dir)
        logging.basicConfig(filename=root / "desktop.log", level=logging.WARNING,
                            format="%(asctime)s %(levelname)s %(message)s")
        if args.open_config:
            open_profile(root, edit_config=True)
            return 0
        configure_tls()
        if args.self_test:
            import ssl
            if not args.browser:
                import webview
                from webview.menu import Menu

            ssl.create_default_context()
            result = self_test()
            result["desktop_dependencies"] = not args.browser
            result["browser_mode"] = args.browser
            result["tls_context"] = True
            text = json.dumps(result, ensure_ascii=False)
            if args.self_test_result:
                args.self_test_result.write_text(text + "\n", encoding="utf-8")
            if sys.stdout is not None:
                print(text)
            return 0
        return launch(root, browser=args.browser)
    except Exception as exc:
        # Record the exception class only; runtime exception messages may
        # contain local source paths or configuration supplied by the user.
        logging.error("DESKTOP_START_FAILED %s", type(exc).__name__)
        if sys.stderr is not None:
            print("工作台未能启动。请查看应用数据目录中的 desktop.log。", file=sys.stderr)
        if not args.self_test:
            show_startup_error()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
