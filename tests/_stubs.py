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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


class FakeWidget:
    """Records what config() was last told, standing in for a ttk widget."""

    def __init__(self):
        self.settings = {}

    def config(self, **kwargs):
        self.settings.update(kwargs)

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
