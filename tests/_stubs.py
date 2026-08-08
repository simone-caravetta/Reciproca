"""Makes reciproca.py importable without a browser or a display.

reciproca.py pulls in Selenium and Tkinter at import time for the GUI and the
browser. The logic under test touches neither, so they are stubbed rather than
installed - a test run must never be able to open a real browser.

Importing this module is what installs the stubs, so it has to come before
`import reciproca`.
"""
import os
import sys
import types

STUBBED = [
    "selenium", "selenium.common", "selenium.common.exceptions",
    "selenium.webdriver", "selenium.webdriver.chrome",
    "selenium.webdriver.chrome.service", "selenium.webdriver.common",
    "selenium.webdriver.common.by", "selenium.webdriver.support",
    "selenium.webdriver.support.ui", "webdriver_manager", "webdriver_manager.chrome",
    "tkinter", "tkinter.ttk", "tkinter.scrolledtext", "tkinter.messagebox",
    "tkinter.filedialog",
]

for _name in STUBBED:
    sys.modules.setdefault(_name, types.ModuleType(_name))

_exceptions = sys.modules["selenium.common.exceptions"]


class WebDriverException(Exception):
    """Stands in for Selenium's base error, which the liveness probe catches."""


_exceptions.WebDriverException = WebDriverException
for _name in ("NoSuchElementException", "StaleElementReferenceException", "TimeoutException"):
    setattr(_exceptions, _name, type(_name, (WebDriverException,), {}))
sys.modules["selenium.webdriver.chrome.service"].Service = object
sys.modules["selenium.webdriver.common.by"].By = object
sys.modules["selenium.webdriver.support"].expected_conditions = object
sys.modules["selenium.webdriver.support.ui"].WebDriverWait = object
sys.modules["webdriver_manager.chrome"].ChromeDriverManager = object
for _attr in ("ttk", "scrolledtext", "messagebox", "filedialog"):
    setattr(sys.modules["tkinter"], _attr, sys.modules["tkinter." + _attr])
sys.modules["tkinter"].END = "end"

messagebox = sys.modules["tkinter.messagebox"]
messagebox.shown = []       # (kind, title, message) for every dialog raised
messagebox.answer = True    # what the ask* dialogs reply


def _dialog(kind):
    def show(title, message=None, **kwargs):
        messagebox.shown.append((kind, title, message))
        return messagebox.answer
    return show


for _kind in ("showinfo", "showwarning", "showerror", "askyesno", "askyesnocancel"):
    setattr(messagebox, _kind, _dialog(_kind))

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


class FakeWidget:
    """Records what config() was last told, standing in for a ttk widget."""

    def __init__(self):
        self.settings = {}

    def config(self, **kwargs):
        self.settings.update(kwargs)

    # Progress bars are addressed by item, as in widget['value'] = 0
    def __setitem__(self, key, value):
        self.settings[key] = value

    def __getitem__(self, key):
        return self.settings.get(key)

    @property
    def state(self):
        return self.settings.get('state')

    # The log box is written to as a text widget
    def insert(self, *args, **kwargs):
        pass

    def see(self, *args, **kwargs):
        pass

    def delete(self, *args, **kwargs):
        pass


class FakeRoot:
    """Tk root stand-in: remembers scheduled callbacks instead of running them."""

    def __init__(self):
        self.scheduled = []

    def after(self, delay, callback):
        self.scheduled.append((delay, callback))

    def update_idletasks(self):
        pass


def install_fake_ui(module):
    """Point the module's widget globals at fakes and clear the browser state.

    Central on purpose: a widget added to reciproca.py needs adding here once,
    rather than in every test that drives the state functions.
    """
    module.root = FakeRoot()
    module.log_box = FakeWidget()
    module.browser_btn = FakeWidget()
    module.start_btn = FakeWidget()
    module.stop_btn = FakeWidget()
    module.uf_browser_btn = FakeWidget()
    module.uf_start_btn = FakeWidget()
    module.uf_stop_btn = FakeWidget()
    module.uf_data_label = FakeWidget()
    module.driver = None
    module.browser_opening.clear()
    module.session_running.clear()
    module.stop_requested.clear()
    module.active_threads[:] = []
    messagebox.shown.clear()
    messagebox.answer = True
