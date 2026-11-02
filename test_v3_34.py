"""TM v3.34 API-first / opt-in browser tests. No network or browser access."""
import importlib.util
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
import threading

base = Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('tm34core',base/'rbxmigrator_core.py')
core=importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def fixture():
    c=core.RBXDLPortalDownloader()
    c._download_single_fallback=lambda *a,**kw: (_ for _ in ()).throw(AssertionError('browser not opted in'))
    return c


def fake_file(folder,name):
    folder=Path(folder);folder.mkdir(exist_ok=True,parents=True)
    file=folder/f'{name}.mp3';file.write_bytes(b'ID3' + b'\0'*30)
    return file


def test_native_all_skips_portal_and_browser():
    c=fixture()
    with TemporaryDirectory() as tmp:
        c._download_official_assets=lambda ids,p: {i:fake_file(p,i) for i in ids}
        c._bulk_session_available=lambda: (_ for _ in ()).throw(AssertionError('portal should not be queried'))
        r,e=c.download_single_batch(['10','20'],Path(tmp))
        assert list(r)==['10','20'] and e=={}
    c.close()


def test_partial_native_portal_exact_remaining():
    c=fixture()
    with TemporaryDirectory() as tmp:
        c._download_official_assets=lambda ids,p:{'10':fake_file(p,'10')}
        c._bulk_session_available=lambda:(True,'OK')
        def portal(ids,p,**kwargs):
            assert ids==['20','30']
            assert kwargs['allow_single_fallback'] is False
            assert kwargs['allow_interactive_auth'] is False
            return {'20':fake_file(p,'20')},{'30':'denied'}
        c.download_batch=portal
        r,e=c.download_single_batch(['10','20','30','10'],Path(tmp))
        assert set(r)=={'10','20'}
        assert 'denied' in e['30'] and 'API-only' in e['30']
    c.close()


def test_no_portal_session_no_browser_by_default():
    c=fixture()
    with TemporaryDirectory() as tmp:
        c._download_official_assets=lambda ids,p:{}
        c._bulk_session_available=lambda:(False,'No Key Access')
        c.download_batch=lambda *a,**k: (_ for _ in ()).throw(AssertionError('unauthorized bulk'))
        r,e=c.download_single_batch(['400'],Path(tmp))
        assert r=={} and 'No Key Access' in e['400'] and 'API-only' in e['400']
    c.close()


def test_explicit_opt_in_uses_single_for_unresolved_only():
    c=core.RBXDLPortalDownloader()
    with TemporaryDirectory() as tmp:
        c._download_official_assets=lambda ids,p:{'10':fake_file(p,'10')}
        c._bulk_session_available=lambda:(True,'OK')
        c.download_batch=lambda ids,p,**k:({'20':fake_file(p,'20')},{'30':'unavailable'})
        def browser(ids,p,reason,**kw):
            assert ids==['30'] and kw['retry_rate_limits_forever'] is False
            return {'30':fake_file(p,'30')},{}
        c._download_single_fallback=browser
        r,e=c.download_single_batch(['10','20','30'],Path(tmp),allow_browser_fallback=True)
        assert set(r)=={'10','20','30'} and e=={}
    c.close()


def test_legacy_strict_mode_blocks_even_opted_in_browser():
    c=fixture()
    with TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{'TM_RBXDL_BACKEND_ONLY':'1'}):
        c._download_official_assets=lambda ids,p:{}
        c._bulk_session_available=lambda:(False,'No Key Access')
        r,e=c.download_single_batch(['400'],Path(tmp),allow_browser_fallback=True)
        assert 'API-only' in e['400']
    c.close()


def test_retry_restores_ids_and_can_enable_browser_fallback():
    # Real WebMigratorState methods; no browser, network, or scheduler started.
    import sys
    sys.path.insert(0,str(base))
    import RbxMigrator as web
    with TemporaryDirectory() as tmp, mock.patch.object(web,'DOWNLOADS_DIR',Path(tmp)):
        state=web.WebMigratorState.__new__(web.WebMigratorState)
        state.download_jobs_lock=threading.RLock()
        state.download_queue_condition=threading.Condition(state.download_jobs_lock)
        state.download_jobs={}
        state.emit_change=lambda:None
        original=state.create_download_job({'name':'Music','priority':'Normal','source':'12345678\n98765432'})
        j=state.download_jobs[original]
        assert j['allowBrowserFallback'] is False
        j.update(state='failed',failed=2)
        retry=state.retry_download_job(original,{'allowBrowserFallback':True})
        again=state.download_jobs[retry]
        assert retry!=original and again['allowBrowserFallback'] is True
        assert again['_ids']==j['_ids'] and j['state']=='failed'


if __name__=='__main__':
    funcs=sorted((name,fn) for name,fn in globals().items() if name.startswith('test_') and callable(fn))
    for name,fn in funcs:
        fn();print('PASS',name)
    print(f'All {len(funcs)} v3.34 tests passed')
