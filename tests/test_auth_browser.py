"""Never open the consent page in a text browser.

On a headless Pi, webbrowser.get() doesn't fail — it returns lynx. The
consent page then can't run Etsy's JavaScript, and the terminal fills with
cookie prompts instead of an approval screen. That is a dead end, not a
degraded experience, so it must be treated as having no browser at all.
"""

import webbrowser

import pytest

from etsy_auto_print import auth


class FakeBrowser:
    def __init__(self, name):
        self.name = name


@pytest.mark.parametrize("browser", ["lynx", "w3m", "links", "elinks", "www-browser"])
def test_text_browsers_do_not_count(monkeypatch, browser):
    monkeypatch.setattr(auth.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(webbrowser, "get", lambda *a: FakeBrowser(browser))
    assert auth._graphical_browser_available() is False


def test_a_full_path_to_a_text_browser_is_still_a_text_browser(monkeypatch):
    monkeypatch.setattr(auth.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(webbrowser, "get", lambda *a: FakeBrowser("/usr/bin/lynx"))
    assert auth._graphical_browser_available() is False


def test_a_real_browser_counts(monkeypatch):
    monkeypatch.setattr(auth.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(webbrowser, "get", lambda *a: FakeBrowser("firefox"))
    assert auth._graphical_browser_available() is True


def test_no_display_means_no_browser(monkeypatch):
    # The Pi over SSH: lynx exists, X does not.
    monkeypatch.setattr(auth.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(webbrowser, "get", lambda *a: FakeBrowser("firefox"))
    assert auth._graphical_browser_available() is False


def test_wayland_counts_as_a_display(monkeypatch):
    monkeypatch.setattr(auth.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(webbrowser, "get", lambda *a: FakeBrowser("firefox"))
    assert auth._graphical_browser_available() is True


def test_no_browser_registered_at_all(monkeypatch):
    monkeypatch.setattr(auth.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")

    def boom(*a):
        raise webbrowser.Error("no browser")

    monkeypatch.setattr(webbrowser, "get", boom)
    assert auth._graphical_browser_available() is False


def test_macos_always_counts(monkeypatch):
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert auth._graphical_browser_available() is True


# --- what authorize() actually does with that -------------------------------


class Config:
    keystring = "k"
    shared_secret = "s"
    redirect_port = 8231

    @property
    def redirect_uri(self):
        return f"http://localhost:{self.redirect_port}/callback"


def run_authorize(monkeypatch, graphical):
    """authorize() up to the point it would open a browser."""
    opened = []
    monkeypatch.setattr(auth, "_graphical_browser_available", lambda: graphical)
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

    class Server:
        def __init__(self, *a):
            pass

        def handle_request(self):
            pass

        def server_close(self):
            pass

    class Thread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            pass

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(auth.http.server, "HTTPServer", Server)
    monkeypatch.setattr(auth.threading, "Thread", Thread)
    monkeypatch.setattr(auth._CallbackHandler, "result", {})

    with pytest.raises(auth.AuthError):     # times out; we only want the side effects
        auth.authorize(Config(), open_browser=True)
    return opened


def test_headless_opens_nothing(monkeypatch, capsys):
    assert run_authorize(monkeypatch, graphical=False) == []
    out = capsys.readouterr().out
    assert "No graphical browser" in out


def test_headless_prints_the_tunnel_command_with_the_real_port(monkeypatch, capsys):
    run_authorize(monkeypatch, graphical=False)
    assert "ssh -L 8231:localhost:8231" in capsys.readouterr().out


def test_a_desktop_still_gets_its_browser_opened(monkeypatch, capsys):
    opened = run_authorize(monkeypatch, graphical=True)
    assert len(opened) == 1 and "etsy.com/oauth/connect" in opened[0]
    assert "No graphical browser" not in capsys.readouterr().out


def test_no_browser_flag_opens_nothing_even_on_a_desktop(monkeypatch):
    opened = []
    monkeypatch.setattr(auth, "_graphical_browser_available", lambda: True)
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

    class Server:
        def __init__(self, *a):
            pass

        def handle_request(self):
            pass

        def server_close(self):
            pass

    class Thread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            pass

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(auth.http.server, "HTTPServer", Server)
    monkeypatch.setattr(auth.threading, "Thread", Thread)
    monkeypatch.setattr(auth._CallbackHandler, "result", {})
    with pytest.raises(auth.AuthError):
        auth.authorize(Config(), open_browser=False)
    assert opened == []
