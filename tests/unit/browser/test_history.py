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

"""Tests for the global page history."""

import logging

import pytest
from PyQt5.QtCore import QUrl

from qutebrowser.browser import history
from qutebrowser.utils import objreg, urlutils, usertypes
from qutebrowser.commands import cmdexc
from qutebrowser.misc import sql


@pytest.fixture(autouse=True)
def prerequisites(config_stub, fake_save_manager, init_sql, fake_args):
    """Make sure everything is ready to initialize a WebHistory."""
    fake_args.debug_flags = []
    config_stub.data = {'general': {'private-browsing': False}}


@pytest.fixture()
def hist(tmpdir):
    return history.WebHistory()


class TestSpecialMethods:

    def test_iter(self, hist):
        urlstr = 'http://www.example.com/'
        url = QUrl(urlstr)
        hist.add_url(url, atime=12345)

        assert list(hist) == [(urlstr, '', 12345, False)]

    def test_len(self, hist):
        assert len(hist) == 0

        url = QUrl('http://www.example.com/')
        hist.add_url(url)

        assert len(hist) == 1

    def test_contains(self, hist):
        hist.add_url(QUrl('http://www.example.com/'), title='Title',
                     atime=12345)
        assert 'http://www.example.com/' in hist
        assert 'www.example.com' not in hist
        assert 'Title' not in hist
        assert 12345 not in hist


class TestGetting:

    def test_get_recent(self, hist):
        hist.add_url(QUrl('http://www.qutebrowser.org/'), atime=67890)
        hist.add_url(QUrl('http://example.com/'), atime=12345)
        assert list(hist.get_recent()) == [
            ('http://www.qutebrowser.org/', '', 67890, False),
            ('http://example.com/', '', 12345, False),
        ]

    def test_entries_between(self, hist):
        hist.add_url(QUrl('http://www.example.com/1'), atime=12345)
        hist.add_url(QUrl('http://www.example.com/2'), atime=12346)
        hist.add_url(QUrl('http://www.example.com/3'), atime=12347)
        hist.add_url(QUrl('http://www.example.com/4'), atime=12348)
        hist.add_url(QUrl('http://www.example.com/5'), atime=12348)
        hist.add_url(QUrl('http://www.example.com/6'), atime=12349)
        hist.add_url(QUrl('http://www.example.com/7'), atime=12350)

        times = [x.atime for x in hist.entries_between(12346, 12349)]
        assert times == [12349, 12348, 12348, 12347]

    def test_entries_before(self, hist):
        hist.add_url(QUrl('http://www.example.com/1'), atime=12346)
        hist.add_url(QUrl('http://www.example.com/2'), atime=12346)
        hist.add_url(QUrl('http://www.example.com/3'), atime=12347)
        hist.add_url(QUrl('http://www.example.com/4'), atime=12348)
        hist.add_url(QUrl('http://www.example.com/5'), atime=12348)
        hist.add_url(QUrl('http://www.example.com/6'), atime=12348)
        hist.add_url(QUrl('http://www.example.com/7'), atime=12349)
        hist.add_url(QUrl('http://www.example.com/8'), atime=12349)

        times = [x.atime for x in
                 hist.entries_before(12348, limit=3, offset=2)]
        assert times == [12348, 12347, 12346]


class TestDelete:

    def test_clear(self, qtbot, tmpdir, hist, mocker):
        hist.add_url(QUrl('http://example.com/'))
        hist.add_url(QUrl('http://www.qutebrowser.org/'))

        m = mocker.patch('qutebrowser.browser.history.message.confirm_async',
                         new=mocker.Mock, spec=[])
        hist.clear()
        assert m.called

    def test_clear_force(self, qtbot, tmpdir, hist):
        hist.add_url(QUrl('http://example.com/'))
        hist.add_url(QUrl('http://www.qutebrowser.org/'))
        hist.clear(force=True)
        assert not len(hist)
        assert not len(hist.completion)

    @pytest.mark.parametrize('raw, escaped', [
        ('http://example.com/1', 'http://example.com/1'),
        ('http://example.com/1 2', 'http://example.com/1%202'),
    ])
    def test_delete_url(self, hist, raw, escaped):
        hist.add_url(QUrl('http://example.com/'), atime=0)
        hist.add_url(QUrl(escaped), atime=0)
        hist.add_url(QUrl('http://example.com/2'), atime=0)

        before = set(hist)
        completion_before = set(hist.completion)

        hist.delete_url(QUrl(raw))

        diff = before.difference(set(hist))
        assert diff == {(escaped, '', 0, False)}

        completion_diff = completion_before.difference(set(hist.completion))
        assert completion_diff == {(raw, '', 0)}


class TestAdd:

    @pytest.fixture()
    def mock_time(self, mocker):
        m = mocker.patch('qutebrowser.browser.history.time')
        m.time.return_value = 12345
        return 12345

    @pytest.mark.parametrize(
        'url, atime, title, redirect, history_url, completion_url', [
            ('http://www.example.com', 12346, 'the title', False,
             'http://www.example.com', 'http://www.example.com'),
            ('http://www.example.com', 12346, 'the title', True,
             'http://www.example.com', None),
            ('http://www.example.com/sp ce', 12346, 'the title', False,
             'http://www.example.com/sp%20ce', 'http://www.example.com/sp ce'),
            ('https://user:pass@example.com', 12346, 'the title', False,
             'https://user@example.com', 'https://user@example.com'),
        ]
    )
    def test_add_url(self, qtbot, hist,
                     url, atime, title, redirect, history_url, completion_url):
        hist.add_url(QUrl(url), atime=atime, title=title, redirect=redirect)
        assert list(hist) == [(history_url, title, atime, redirect)]
        if completion_url is None:
            assert not len(hist.completion)
        else:
            assert list(hist.completion) == [(completion_url, title, atime)]

    def test_no_sql_history(self, hist, fake_args):
        fake_args.debug_flags = 'no-sql-history'
        hist.add_url(QUrl('https://www.example.com/'), atime=12346,
                     title='Hello World', redirect=False)
        assert not list(hist)

    def test_invalid(self, qtbot, hist, caplog):
        with caplog.at_level(logging.WARNING):
            hist.add_url(QUrl())
        assert not list(hist)
        assert not list(hist.completion)

    @pytest.mark.parametrize('environmental', [True, False])
    @pytest.mark.parametrize('completion', [True, False])
    def test_error(self, monkeypatch, hist, message_mock, caplog,
                   environmental, completion):
        def raise_error(url, replace=False):
            if environmental:
                raise sql.SqlEnvironmentError("Error message")
            else:
                raise sql.SqlBugError("Error message")

        if completion:
            monkeypatch.setattr(hist.completion, 'insert', raise_error)
        else:
            monkeypatch.setattr(hist, 'insert', raise_error)

        if environmental:
            with caplog.at_level(logging.ERROR):
                hist.add_url(QUrl('https://www.example.org/'))
            msg = message_mock.getmsg(usertypes.MessageLevel.error)
            assert msg.text == "Failed to write history: Error message"
        else:
            with pytest.raises(sql.SqlBugError):
                hist.add_url(QUrl('https://www.example.org/'))

    @pytest.mark.parametrize('level, url, req_url, expected', [
        (logging.DEBUG, 'a.com', 'a.com', [('a.com', 'title', 12345, False)]),
        (logging.DEBUG, 'a.com', 'b.com', [('a.com', 'title', 12345, False),
                                           ('b.com', 'title', 12345, True)]),
        (logging.WARNING, 'a.com', '', [('a.com', 'title', 12345, False)]),
        (logging.WARNING, '', '', []),
        (logging.WARNING, 'data:foo', '', []),
        (logging.WARNING, 'a.com', 'data:foo', []),
    ])
    def test_from_tab(self, hist, caplog, mock_time,
                      level, url, req_url, expected):
        with caplog.at_level(level):
            hist.add_from_tab(QUrl(url), QUrl(req_url), 'title')
        assert set(hist) == set(expected)

    def test_exclude(self, hist, config_stub):
        """Excluded URLs should be in the history but not completion."""
        config_stub.val.completion.web_history.exclude = ['*.example.org']
        url = QUrl('http://www.example.org/')
        hist.add_from_tab(url, url, 'title')
        assert list(hist)
        assert not list(hist.completion)


class TestHistoryInterface:

    @pytest.fixture
    def hist_interface(self, hist):
        # pylint: disable=invalid-name
        QtWebKit = pytest.importorskip('PyQt5.QtWebKit')
        from qutebrowser.browser.webkit import webkithistory
        QWebHistoryInterface = QtWebKit.QWebHistoryInterface
        # pylint: enable=invalid-name
        hist.add_url(url=QUrl('http://www.example.com/'), title='example')
        interface = webkithistory.WebHistoryInterface(hist)
        QWebHistoryInterface.setDefaultInterface(interface)
        yield
        QWebHistoryInterface.setDefaultInterface(None)

    def test_history_interface(self, qtbot, webview, hist_interface):
        html = b"<a href='about:blank'>foo</a>"
        url = urlutils.data_url('text/html', html)
        with qtbot.waitSignal(webview.loadFinished):
            webview.load(url)


class TestInit:

    @pytest.fixture
    def cleanup_init(self):
        # prevent test_init from leaking state
        yield
        hist = objreg.get('web-history', None)
        if hist is not None:
            hist.setParent(None)
            objreg.delete('web-history')
        try:
            from PyQt5.QtWebKit import QWebHistoryInterface
            QWebHistoryInterface.setDefaultInterface(None)
        except ImportError:
            pass

    @pytest.mark.parametrize('backend', [usertypes.Backend.QtWebEngine,
                                         usertypes.Backend.QtWebKit])
    def test_init(self, backend, qapp, tmpdir, monkeypatch, cleanup_init):
        if backend == usertypes.Backend.QtWebKit:
            pytest.importorskip('PyQt5.QtWebKitWidgets')
        else:
            assert backend == usertypes.Backend.QtWebEngine

        monkeypatch.setattr(history.objects, 'backend', backend)
        history.init(qapp)
        hist = objreg.get('web-history')
        assert hist.parent() is qapp

        try:
            from PyQt5.QtWebKit import QWebHistoryInterface
        except ImportError:
            QWebHistoryInterface = None

        if backend == usertypes.Backend.QtWebKit:
            default_interface = QWebHistoryInterface.defaultInterface()
            assert default_interface._history is hist
        else:
            assert backend == usertypes.Backend.QtWebEngine
            if QWebHistoryInterface is None:
                default_interface = None
            else:
                default_interface = QWebHistoryInterface.defaultInterface()
            # For this to work, nothing can ever have called
            # setDefaultInterface before (so we need to test webengine before
            # webkit)
            assert default_interface is None


class TestImport:

    def test_import_txt(self, hist, data_tmpdir, monkeypatch, stubs):
        monkeypatch.setattr(history, 'QTimer', stubs.InstaTimer)
        histfile = data_tmpdir / 'history'
        # empty line is deliberate, to test skipping empty lines
        histfile.write('''12345 http://example.com/ title
                        12346 http://qutebrowser.org/
                        67890 http://example.com/path

                        68891-r http://example.com/path/other ''')

        hist.import_txt()

        assert list(hist) == [
            ('http://example.com/', 'title', 12345, False),
            ('http://qutebrowser.org/', '', 12346, False),
            ('http://example.com/path', '', 67890, False),
            ('http://example.com/path/other', '', 68891, True)
        ]

        assert not histfile.exists()
        assert (data_tmpdir / 'history.bak').exists()

    def test_existing_backup(self, hist, data_tmpdir, monkeypatch, stubs):
        monkeypatch.setattr(history, 'QTimer', stubs.InstaTimer)
        histfile = data_tmpdir / 'history'
        bakfile = data_tmpdir / 'history.bak'
        histfile.write('12345 http://example.com/ title')
        bakfile.write('12346 http://qutebrowser.org/')

        hist.import_txt()

        assert list(hist) == [('http://example.com/', 'title', 12345, False)]

        assert not histfile.exists()
        assert bakfile.read().split('\n') == ['12346 http://qutebrowser.org/',
                                              '12345 http://example.com/ title']

    @pytest.mark.parametrize('line', [
        '',
        '#12345 http://example.com/commented',

        # https://bugreports.qt.io/browse/QTBUG-60364
        '12345 http://.com/',
        '12345 https://.com/',
        '12345 http://www..com/',
        '12345 https://www..com/',

        # issue #2646
        ('12345 data:text/html;'
         'charset=UTF-8,%3C%21DOCTYPE%20html%20PUBLIC%20%22-'),
    ])
    def test_skip(self, hist, data_tmpdir, monkeypatch, stubs, line):
        """import_txt should skip certain lines silently."""
        monkeypatch.setattr(history, 'QTimer', stubs.InstaTimer)
        histfile = data_tmpdir / 'history'
        histfile.write(line)

        hist.import_txt()

        assert not histfile.exists()
        assert not len(hist)

    @pytest.mark.parametrize('line', [
        'xyz http://example.com/bad-timestamp',
        '12345',
        'http://example.com/no-timestamp',
        '68891-r-r http://example.com/double-flag',
        '68891-x http://example.com/bad-flag',
        '68891 http://.com',
    ])
    def test_invalid(self, hist, data_tmpdir, monkeypatch, stubs, caplog,
                     line):
        """import_txt should fail on certain lines."""
        monkeypatch.setattr(history, 'QTimer', stubs.InstaTimer)
        histfile = data_tmpdir / 'history'
        histfile.write(line)

        with caplog.at_level(logging.ERROR):
            hist.import_txt()

        assert any(rec.msg.startswith("Failed to import history:")
                   for rec in caplog.records)

        assert histfile.exists()

    def test_nonexistent(self, hist, data_tmpdir, monkeypatch, stubs):
        """import_txt should do nothing if the history file doesn't exist."""
        monkeypatch.setattr(history, 'QTimer', stubs.InstaTimer)
        hist.import_txt()


class TestDump:

    def test_debug_dump_history(self, hist, tmpdir):
        hist.add_url(QUrl('http://example.com/1'), title="Title1", atime=12345)
        hist.add_url(QUrl('http://example.com/2'), title="Title2", atime=12346)
        hist.add_url(QUrl('http://example.com/3'), title="Title3", atime=12347)
        hist.add_url(QUrl('http://example.com/4'), title="Title4", atime=12348,
                     redirect=True)
        histfile = tmpdir / 'history'
        hist.debug_dump_history(str(histfile))
        expected = ['12345 http://example.com/1 Title1',
                    '12346 http://example.com/2 Title2',
                    '12347 http://example.com/3 Title3',
                    '12348-r http://example.com/4 Title4']
        assert histfile.read() == '\n'.join(expected)

    def test_nonexistent(self, hist, tmpdir):
        histfile = tmpdir / 'nonexistent' / 'history'
        with pytest.raises(cmdexc.CommandError):
            hist.debug_dump_history(str(histfile))


class TestRebuild:

    def test_delete(self, hist):
        hist.insert({'url': 'example.com/1', 'title': 'example1',
                     'redirect': False, 'atime': 1})
        hist.insert({'url': 'example.com/1', 'title': 'example1',
                     'redirect': False, 'atime': 2})
        hist.insert({'url': 'example.com/2%203', 'title': 'example2',
                     'redirect': False, 'atime': 3})
        hist.insert({'url': 'example.com/3', 'title': 'example3',
                     'redirect': True, 'atime': 4})
        hist.insert({'url': 'example.com/2 3', 'title': 'example2',
                     'redirect': False, 'atime': 5})
        hist.completion.delete_all()

        hist2 = history.WebHistory()
        assert list(hist2.completion) == [
            ('example.com/1', 'example1', 2),
            ('example.com/2 3', 'example2', 5),
        ]

    def test_no_rebuild(self, hist):
        """Ensure that completion is not regenerated unless empty."""
        hist.add_url(QUrl('example.com/1'), redirect=False, atime=1)
        hist.add_url(QUrl('example.com/2'), redirect=False, atime=2)
        hist.completion.delete('url', 'example.com/2')

        hist2 = history.WebHistory()
        assert list(hist2.completion) == [('example.com/1', '', 1)]

    def test_user_version(self, hist, monkeypatch):
        """Ensure that completion is regenerated if user_version changes."""
        hist.add_url(QUrl('example.com/1'), redirect=False, atime=1)
        hist.add_url(QUrl('example.com/2'), redirect=False, atime=2)
        hist.completion.delete('url', 'example.com/2')

        hist2 = history.WebHistory()
        assert list(hist2.completion) == [('example.com/1', '', 1)]

        monkeypatch.setattr(history, '_USER_VERSION',
                            history._USER_VERSION + 1)
        hist3 = history.WebHistory()
        assert list(hist3.completion) == [
            ('example.com/1', '', 1),
            ('example.com/2', '', 2),
        ]

    def test_force_rebuild(self, hist):
        """Ensure that completion is regenerated if we force a rebuild."""
        hist.add_url(QUrl('example.com/1'), redirect=False, atime=1)
        hist.add_url(QUrl('example.com/2'), redirect=False, atime=2)
        hist.completion.delete('url', 'example.com/2')

        hist2 = history.WebHistory()
        assert list(hist2.completion) == [('example.com/1', '', 1)]
        hist2.metainfo['force_rebuild'] = True

        hist3 = history.WebHistory()
        assert list(hist3.completion) == [
            ('example.com/1', '', 1),
            ('example.com/2', '', 2),
        ]
        assert not hist3.metainfo['force_rebuild']

    def test_exclude(self, config_stub, hist):
        """Ensure that patterns in completion.web_history.exclude are ignored.

        This setting should only be used for the completion.
        """
        config_stub.val.completion.web_history.exclude = ['*.example.org']
        assert hist.metainfo['force_rebuild']

        hist.add_url(QUrl('http://example.com'), redirect=False, atime=1)
        hist.add_url(QUrl('http://example.org'), redirect=False, atime=2)

        hist2 = history.WebHistory()
        assert list(hist2.completion) == [('http://example.com', '', 1)]

    def test_unrelated_config_change(self, config_stub, hist):
        config_stub.val.history_gap_interval = 1234
        assert not hist.metainfo['force_rebuild']


class TestCompletionMetaInfo:

    @pytest.fixture
    def metainfo(self):
        return history.CompletionMetaInfo()

    def test_contains_keyerror(self, metainfo):
        with pytest.raises(KeyError):
            'does_not_exist' in metainfo  # pylint: disable=pointless-statement

    def test_getitem_keyerror(self, metainfo):
        with pytest.raises(KeyError):
            metainfo['does_not_exist']  # pylint: disable=pointless-statement

    def test_setitem_keyerror(self, metainfo):
        with pytest.raises(KeyError):
            metainfo['does_not_exist'] = 42

    def test_contains(self, metainfo):
        assert 'force_rebuild' in metainfo

    def test_modify(self, metainfo):
        assert not metainfo['force_rebuild']
        metainfo['force_rebuild'] = True
        assert metainfo['force_rebuild']
