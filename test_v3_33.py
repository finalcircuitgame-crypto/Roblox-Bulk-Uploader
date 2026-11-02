"""Offline tests for v3.33 recovery of intermittent browser UI issues."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
import importlib.util
import threading

base = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('tm33core', base/'rbxmigrator_core.py')
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def test_four_scattered_button_failures_must_not_stop_batch():
    """This reproduces the supplied report's specific premature circuit trip."""
    class FakeBrowser:
        calls = []
        def __init__(self, **kwargs): pass
        def start(self): pass
        def close(self): pass
        def fetch(self, asset_id, **kwargs):
            FakeBrowser.calls.append(asset_id)
            if asset_id in {'3','10','15','20'}:
                raise RuntimeError('RBXDL_UI_NOT_READY: mock intermittent missing button')
            return b'ID3' + b'\0'*32, '.mp3', 'RBXDL browser'

    client=core.RBXDLPortalDownloader()
    ids=[str(i) for i in range(1,31)]
    with TemporaryDirectory() as folder, mock.patch.object(core,'RBXDLBrowserFetcher', FakeBrowser), \
        mock.patch.object(core,'RBXDL_SINGLE_WORKERS',1):
        mapping, errors=client._download_single_fallback(ids, Path(folder),'test')
        assert len(FakeBrowser.calls)==30, FakeBrowser.calls
        assert len(mapping)==26, mapping.keys()
        assert set(errors)=={'3','10','15','20'}
        assert not any('RBXDL_SINGLE_UI_UNAVAILABLE' in v for v in errors.values())
    client.close()


def test_four_consecutive_button_failures_still_stop_batch():
    class FakeBrowser:
        calls=0
        def __init__(self,**kwargs): pass
        def start(self): pass
        def close(self): pass
        def fetch(self, asset_id, **kwargs):
            FakeBrowser.calls+=1
            raise RuntimeError('RBXDL_UI_NOT_READY: mock continuously broken site')
    client=core.RBXDLPortalDownloader()
    with TemporaryDirectory() as folder, mock.patch.object(core,'RBXDLBrowserFetcher', FakeBrowser), \
        mock.patch.object(core,'RBXDL_SINGLE_WORKERS',1):
        mapping,errors=client._download_single_fallback([str(i) for i in range(30)],Path(folder),'test')
        assert len(mapping)==0
        assert len(errors)==30
        assert FakeBrowser.calls==4
        assert sum('RBXDL_SINGLE_UI_UNAVAILABLE' in v for v in errors.values())==26
    client.close()


def test_single_use_token_retries_once_with_fresh_page():
    with mock.patch.object(core.RBXDLBrowserFetcher,'_fetch_once',side_effect=[
        RuntimeError('RBXDL browser flow reported an error: single-use security token is invalid'),
        (b'ID3'+b'\0'*10,'.mp3','RBXDL browser')]) as fetch, \
        mock.patch.object(core.time,'sleep',lambda _:None):
        browser=core.RBXDLBrowserFetcher()
        data, suffix, provider=browser.fetch('12345678')
        assert data.startswith(b'ID3') and suffix=='.mp3'
        assert fetch.call_count==2


def test_persistent_bad_token_does_not_loop_forever():
    with mock.patch.object(core.RBXDLBrowserFetcher,'_fetch_once',side_effect=RuntimeError('single-use security token is invalid')) as fetch, \
        mock.patch.object(core.time,'sleep',lambda _:None):
        browser=core.RBXDLBrowserFetcher()
        try:
            browser.fetch('12345678')
        except RuntimeError as exc:
            assert 'invalid' in str(exc)
        else:
            raise AssertionError('Expected an error after bounded token retries')
        assert fetch.call_count==2


def test_correct_playwright_title_signature():
    text=(base/'rbxmigrator_core.py').read_text(encoding='utf-8')
    assert 'page.title()' in text
    assert 'page.title(timeout' not in text

if __name__ == '__main__':
    tests=[(k,v) for k,v in globals().items() if k.startswith('test_') and callable(v)]
    for name, func in tests:
        func()
        print('PASS',name)
    print(f'All {len(tests)} v3.33 tests passed.')
