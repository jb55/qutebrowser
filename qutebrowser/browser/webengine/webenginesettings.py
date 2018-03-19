# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2016-2018 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""Bridge from QWebEngineSettings to our own settings.

Module attributes:
    ATTRIBUTES: A mapping from internal setting names to QWebEngineSetting enum
                constants.
"""

import os

import sip
from PyQt5.QtGui import QFont
from PyQt5.QtWebEngineWidgets import (QWebEngineSettings, QWebEngineProfile,
                                      QWebEngineScript)

from qutebrowser.browser import shared
from qutebrowser.browser.webengine import spell
from qutebrowser.config import config, websettings
from qutebrowser.utils import (utils, standarddir, javascript, qtutils,
                               message, log, objreg)

# The default QWebEngineProfile
default_profile = None
# The QWebEngineProfile used for private (off-the-record) windows
private_profile = None
# The global WebEngineSettings object
global_settings = None


class _SettingsWrapper:

    """Expose a QWebEngineSettings interface which acts on all profiles.

    For read operations, the default profile value is always used.
    """

    def __init__(self):
        self._settings = [default_profile.settings(),
                          private_profile.settings()]

    def setAttribute(self, *args, **kwargs):
        for settings in self._settings:
            settings.setAttribute(*args, **kwargs)

    def setFontFamily(self, *args, **kwargs):
        for settings in self._settings:
            settings.setFontFamily(*args, **kwargs)

    def setFontSize(self, *args, **kwargs):
        for settings in self._settings:
            settings.setFontSize(*args, **kwargs)

    def setDefaultTextEncoding(self, *args, **kwargs):
        for settings in self._settings:
            settings.setDefaultTextEncoding(*args, **kwargs)

    def testAttribute(self, *args, **kwargs):
        return self._settings[0].testAttribute(*args, **kwargs)

    def fontSize(self, *args, **kwargs):
        return self._settings[0].fontSize(*args, **kwargs)

    def fontFamily(self, *args, **kwargs):
        return self._settings[0].fontFamily(*args, **kwargs)

    def defaultTextEncoding(self, *args, **kwargs):
        return self._settings[0].defaultTextEncoding(*args, **kwargs)


class WebEngineSettings(websettings.AbstractSettings):

    """A wrapper for the config for QWebEngineSettings."""

    _ATTRIBUTES = {
        'content.xss_auditing':
            [QWebEngineSettings.XSSAuditingEnabled],
        'content.images':
            [QWebEngineSettings.AutoLoadImages],
        'content.javascript.enabled':
            [QWebEngineSettings.JavascriptEnabled],
        'content.javascript.can_open_tabs_automatically':
            [QWebEngineSettings.JavascriptCanOpenWindows],
        'content.javascript.can_access_clipboard':
            [QWebEngineSettings.JavascriptCanAccessClipboard],
        'content.plugins':
            [QWebEngineSettings.PluginsEnabled],
        'content.hyperlink_auditing':
            [QWebEngineSettings.HyperlinkAuditingEnabled],
        'content.local_content_can_access_remote_urls':
            [QWebEngineSettings.LocalContentCanAccessRemoteUrls],
        'content.local_content_can_access_file_urls':
            [QWebEngineSettings.LocalContentCanAccessFileUrls],
        'content.webgl':
            [QWebEngineSettings.WebGLEnabled],
        'content.local_storage':
            [QWebEngineSettings.LocalStorageEnabled],

        'input.spatial_navigation':
            [QWebEngineSettings.SpatialNavigationEnabled],
        'input.links_included_in_focus_chain':
            [QWebEngineSettings.LinksIncludedInFocusChain],

        'scrolling.smooth':
            [QWebEngineSettings.ScrollAnimatorEnabled],
    }

    _FONT_SIZES = {
        'fonts.web.size.minimum':
            QWebEngineSettings.MinimumFontSize,
        'fonts.web.size.minimum_logical':
            QWebEngineSettings.MinimumLogicalFontSize,
        'fonts.web.size.default':
            QWebEngineSettings.DefaultFontSize,
        'fonts.web.size.default_fixed':
            QWebEngineSettings.DefaultFixedFontSize,
    }

    _FONT_FAMILIES = {
        'fonts.web.family.standard': QWebEngineSettings.StandardFont,
        'fonts.web.family.fixed': QWebEngineSettings.FixedFont,
        'fonts.web.family.serif': QWebEngineSettings.SerifFont,
        'fonts.web.family.sans_serif': QWebEngineSettings.SansSerifFont,
        'fonts.web.family.cursive': QWebEngineSettings.CursiveFont,
        'fonts.web.family.fantasy': QWebEngineSettings.FantasyFont,
    }

    # Mapping from WebEngineSettings::initDefaults in
    # qtwebengine/src/core/web_engine_settings.cpp
    _FONT_TO_QFONT = {
        QWebEngineSettings.StandardFont: QFont.Serif,
        QWebEngineSettings.FixedFont: QFont.Monospace,
        QWebEngineSettings.SerifFont: QFont.Serif,
        QWebEngineSettings.SansSerifFont: QFont.SansSerif,
        QWebEngineSettings.CursiveFont: QFont.Cursive,
        QWebEngineSettings.FantasyFont: QFont.Fantasy,
    }

    def __init__(self, settings):
        super().__init__(settings)
        # Attributes which don't exist in all Qt versions.
        new_attributes = {
            # Qt 5.8
            'content.print_element_backgrounds': 'PrintElementBackgrounds',
        }
        for name, attribute in new_attributes.items():
            try:
                value = getattr(QWebEngineSettings, attribute)
            except AttributeError:
                continue

            self._ATTRIBUTES[name] = [value]


class ProfileSetter:

    """Helper to set various settings on a profile."""

    def __init__(self, profile):
        self._profile = profile

    def init_profile(self):
        """Initialize settings on the given profile."""
        self._init_js()
        self.init_stylesheet()
        self.set_http_headers()
        self.set_http_cache_size()
        self._profile.settings().setAttribute(
            QWebEngineSettings.FullScreenSupportEnabled, True)
        if qtutils.version_check('5.8'):
            self._profile.setSpellCheckEnabled(True)
            self.set_dictionary_language()

    def _inject_early_js(self, name, js_code, *,
                         world=QWebEngineScript.ApplicationWorld,
                         subframes=False):
        """Inject the given script to run early on a page load.

        This runs the script both on DocumentCreation and DocumentReady as on
        some internal pages, DocumentCreation will not work.

        That is a WORKAROUND for https://bugreports.qt.io/browse/QTBUG-66011
        """
        for injection in ['creation', 'ready']:
            injection_points = {
                'creation': QWebEngineScript.DocumentCreation,
                'ready': QWebEngineScript.DocumentReady,
            }
            script = QWebEngineScript()
            script.setInjectionPoint(injection_points[injection])
            script.setSourceCode(js_code)
            script.setWorldId(world)
            script.setRunsOnSubFrames(subframes)
            script.setName('_qute_{}_{}'.format(name, injection))
            self._profile.scripts().insert(script)

    def _remove_early_js(self, name):
        """Remove an early QWebEngineScript."""
        scripts = self._profile.scripts()
        for injection in ['creation', 'ready']:
            full_name = '_qute_{}_{}'.format(name, injection)
            script = scripts.findScript(full_name)
            if not script.isNull():
                scripts.remove(script)

    def _init_js(self):
        """Initialize global qutebrowser JavaScript."""
        js_code = javascript.wrap_global(
            'scripts',
            utils.read_file('javascript/scroll.js'),
            utils.read_file('javascript/webelem.js'),
            utils.read_file('javascript/caret.js'),
        )
        # FIXME:qtwebengine what about subframes=True?
        self._inject_early_js('js', js_code, subframes=True)

    def init_stylesheet(self):
        """Initialize custom stylesheets.

        Partially inspired by QupZilla:
        https://github.com/QupZilla/qupzilla/blob/v2.0/src/lib/app/mainapplication.cpp#L1063-L1101
        """
        self._remove_early_js('stylesheet')
        css = shared.get_user_stylesheet()
        js_code = javascript.wrap_global(
            'stylesheet',
            utils.read_file('javascript/stylesheet.js'),
            javascript.assemble('stylesheet', 'set_css', css),
        )
        self._inject_early_js('stylesheet', js_code, subframes=True)

    def set_http_headers(self):
        """Set the user agent and accept-language for the given profile.

        We override those per request in the URL interceptor (to allow for
        per-domain values), but this one still gets used for things like
        window.navigator.userAgent/.languages in JS.
        """
        self._profile.setHttpUserAgent(config.val.content.headers.user_agent)
        accept_language = config.val.content.headers.accept_language
        if accept_language is not None:
            self._profile.setHttpAcceptLanguage(accept_language)

    def set_http_cache_size(self):
        """Initialize the HTTP cache size for the given profile."""
        size = config.val.content.cache.size
        if size is None:
            size = 0
        else:
            size = qtutils.check_overflow(size, 'int', fatal=False)

        # 0: automatically managed by QtWebEngine
        self._profile.setHttpCacheMaximumSize(size)

    def set_persistent_cookie_policy(self):
        """Set the HTTP Cookie size for the given profile."""
        assert not self._profile.isOffTheRecord()
        if config.val.content.cookies.store:
            value = QWebEngineProfile.AllowPersistentCookies
        else:
            value = QWebEngineProfile.NoPersistentCookies
        self._profile.setPersistentCookiesPolicy(value)

    def set_dictionary_language(self, warn=True):
        """Load the given dictionaries."""
        filenames = []
        for code in config.val.spellcheck.languages or []:
            local_filename = spell.local_filename(code)
            if not local_filename:
                if warn:
                    message.warning("Language {} is not installed - see "
                                    "scripts/dictcli.py in qutebrowser's "
                                    "sources".format(code))
                continue

            filenames.append(local_filename)

        log.config.debug("Found dicts: {}".format(filenames))
        self._profile.setSpellCheckLanguages(filenames)


def _update_stylesheet():
    """Update the custom stylesheet in existing tabs."""
    css = shared.get_user_stylesheet()
    code = javascript.assemble('stylesheet', 'set_css', css)
    for win_id, window in objreg.window_registry.items():
        # We could be in the middle of destroying a window here
        if sip.isdeleted(window):
            continue
        tab_registry = objreg.get('tab-registry', scope='window',
                                  window=win_id)
        for tab in tab_registry.values():
            tab.run_js_async(code)


def _update_settings(option):
    """Update global settings when qwebsettings changed."""
    global_settings.update_setting(option)

    if option in ['scrolling.bar', 'content.user_stylesheets']:
        default_profile.setter.init_stylesheet()
        private_profile.setter.init_stylesheet()
        _update_stylesheet()
    elif option in ['content.headers.user_agent',
                    'content.headers.accept_language']:
        default_profile.setter.set_http_headers()
        private_profile.setter.set_http_headers()
    elif option == 'content.cache.size':
        default_profile.setter.set_http_cache_size()
        private_profile.setter.set_http_cache_size()
    elif (option == 'content.cookies.store' and
          # https://bugreports.qt.io/browse/QTBUG-58650
          qtutils.version_check('5.9', compiled=False)):
        default_profile.setter.set_persistent_cookie_policy()
        # We're not touching the private profile's cookie policy.
    elif option == 'spellcheck.languages':
        default_profile.setter.set_dictionary_language()
        private_profile.setter.set_dictionary_language(warn=False)


def _init_profiles():
    """Init the two used QWebEngineProfiles."""
    global default_profile, private_profile

    default_profile = QWebEngineProfile.defaultProfile()
    default_profile.setter = ProfileSetter(default_profile)
    default_profile.setCachePath(
        os.path.join(standarddir.cache(), 'webengine'))
    default_profile.setPersistentStoragePath(
        os.path.join(standarddir.data(), 'webengine'))
    default_profile.setter.init_profile()
    default_profile.setter.set_persistent_cookie_policy()

    private_profile = QWebEngineProfile()
    private_profile.setter = ProfileSetter(private_profile)
    assert private_profile.isOffTheRecord()
    private_profile.setter.init_profile()


def inject_userscripts():
    """Register user JavaScript files with the global profiles."""
    # The Greasemonkey metadata block support in QtWebEngine only starts at
    # Qt 5.8. With 5.7.1, we need to inject the scripts ourselves in response
    # to urlChanged.
    if not qtutils.version_check('5.8'):
        return

    # Since we are inserting scripts into profile.scripts they won't
    # just get replaced by new gm scripts like if we were injecting them
    # ourselves so we need to remove all gm scripts, while not removing
    # any other stuff that might have been added. Like the one for
    # stylesheets.
    greasemonkey = objreg.get('greasemonkey')
    for profile in [default_profile, private_profile]:
        scripts = profile.scripts()
        for script in scripts.toList():
            if script.name().startswith("GM-"):
                log.greasemonkey.debug('Removing script: {}'
                                       .format(script.name()))
                removed = scripts.remove(script)
                assert removed, script.name()

        # Then add the new scripts.
        for script in greasemonkey.all_scripts():
            # @run-at (and @include/@exclude/@match) is parsed by
            # QWebEngineScript.
            new_script = QWebEngineScript()
            new_script.setWorldId(QWebEngineScript.MainWorld)
            new_script.setSourceCode(script.code())
            new_script.setName("GM-{}".format(script.name))
            new_script.setRunsOnSubFrames(script.runs_on_sub_frames)
            log.greasemonkey.debug('adding script: {}'
                                   .format(new_script.name()))
            scripts.insert(new_script)


def init(args):
    """Initialize the global QWebSettings."""
    if args.enable_webengine_inspector:
        os.environ['QTWEBENGINE_REMOTE_DEBUGGING'] = str(utils.random_port())

    _init_profiles()
    config.instance.changed.connect(_update_settings)

    global global_settings
    global_settings = WebEngineSettings(_SettingsWrapper())
    global_settings.init_settings()


def shutdown():
    pass
