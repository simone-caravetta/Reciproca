"""Makes the reciproca package importable without a browser or a display.

The package pulls in Selenium (through the browser/scraping modules) and
Tkinter (through gui.py) at import time. The logic under test touches neither,
so they are stubbed rather than installed - a test run must never be able to
open a real browser.

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
    "selenium.webdriver.common.by", "selenium.webdriver.common.keys",
    "selenium.webdriver.common.action_chains", "selenium.webdriver.support",
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


class By:
    """Selector constants, standing in for Selenium's By."""

    CSS_SELECTOR = "css selector"
    TAG_NAME = "tag name"
    XPATH = "xpath"
    NAME = "name"


sys.modules["selenium.webdriver.common.by"].By = By
sys.modules["selenium.webdriver.common.keys"].Keys = object
sys.modules["selenium.webdriver.common.action_chains"].ActionChains = object
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

    def get(self, *args, **kwargs):
        return []

    def curselection(self):
        return []


class FakeRoot:
    """Tk root stand-in: remembers scheduled callbacks instead of running them."""

    def __init__(self):
        self.scheduled = []

    def after(self, delay, callback):
        self.scheduled.append((delay, callback))

    def update_idletasks(self):
        pass


def install_fake_ui():
    """Point the GUI module's widget globals at fakes and reset the runtime state.

    Central on purpose: a widget added to reciproca/gui.py needs adding here
    once, rather than in every test that drives the state functions.

    Also attaches the fake-laden GUI module to the core's hooks, so a call like
    R.update_follow_ui_state() runs the real logic against the fake widgets -
    the same path the monolith's tests exercised.
    """
    import reciproca as R  # noqa: E402  (the stubs above made this safe)

    gui = R.gui
    gui.root = FakeRoot()
    gui.log_box = FakeWidget()
    gui.progress_bar = FakeWidget()
    gui.status_label = FakeWidget()
    gui.stats_label = FakeWidget()
    gui.uf_progress_bar = FakeWidget()
    gui.uf_status_label = FakeWidget()
    gui.uf_stats_label = FakeWidget()
    gui.browser_btn = FakeWidget()
    gui.start_btn = FakeWidget()
    gui.stop_btn = FakeWidget()
    gui.uf_browser_btn = FakeWidget()
    gui.uf_start_btn = FakeWidget()
    gui.uf_stop_btn = FakeWidget()
    gui.uf_data_label = FakeWidget()
    gui.account_label = FakeWidget()
    gui.queue_listbox = FakeWidget()
    gui.queue_count_label = FakeWidget()
    gui.main_queue_info = FakeWidget()
    gui.live_extraction_listbox = FakeWidget()
    gui.live_extraction_label = FakeWidget()

    state = R.state
    state.driver = None
    state.login_completed = False
    state.browser_opening.clear()
    state.session_running.clear()
    state.stop_requested.clear()
    state.scoring_stop.clear()
    state.active_threads[:] = []

    R.hooks.attach(gui)
    messagebox.shown.clear()
    messagebox.answer = True
