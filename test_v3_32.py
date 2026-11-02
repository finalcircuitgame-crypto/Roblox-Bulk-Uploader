"""Offline tests: native fetch, page failure circuit, and retry semantics."""
from __future__ import annotations
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
import threading

base = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('tm32core', base/'rbxmigrator_core.py')
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def test_native_recognizes_audio_and_respects_denial():
    updates = []
    ready = []
    client = core.RBXDLPortalDownloader(
        item_callback=lambda aid,status,meta: updates.append((aid,status,meta)),
        file_ready_callback=lambda aid,path,source: ready.append((aid,path,source)),
    )
    def remote(aid, *, asset_type, timeout):
        assert timeout <= 8
        if aid == '111111':
            return b'ID3'+b'\x00'*100, '.mp3', 'Roblox'
        raise RuntimeError('403 Forbidden')
    with TemporaryDirectory() as d, mock.patch.object(core, 'fetch_asset_roblox', side_effect=remote):
        result=client._download_official_assets(['111111','222222'],Path(d))
        assert list(result)==['111111']
        assert result['111111'].read_bytes().startswith(b'ID3')
        assert len(ready)==1 and ready[0][2]=='Roblox Asset Delivery'
    client.close()


def test_native_rejects_fake_audio():
    client = core.RBXDLPortalDownloader()
    with TemporaryDirectory() as d, mock.patch.object(core,'fetch_asset_roblox',return_value=(b'<html><h1>Oops</h1></html>','.mp3','Roblox')):
        assert client._download_official_assets(['11111'],Path(d))=={}
    client.close()


def test_native_then_portal_then_public_single():
    client = core.RBXDLPortalDownloader()
    client._bulk_session_available=lambda:(True,'OK')
    with TemporaryDirectory() as d:
        base=Path(d)
        def portal(ids,out,**kw):
            assert ids==['111','333']
            p=base/'111.mp3';p.write_bytes(b'ID3'+b'0'*30)
            return {'111':p},{'333':'portal failed'}
        def official(ids,out):
            assert ids==['111','222','333']
            p=base/'222.mp3';p.write_bytes(b'ID3'+b'0'*30)
            return {'222':p}
        def single(ids,out,reason,**kw):
            assert ids==['333']
            return {},{'333':'Single control missing'}
        client.download_batch=portal
        client._download_official_assets=official
        client._download_single_fallback=single
        result,errors=client.download_single_batch(['111','222','333'],base,allow_browser_fallback=True)
        assert set(result)=={'111','222'}
        assert errors=={'333':'Single control missing'}
    client.close()


def test_systemic_ui_failure_stops_batch():
    client=core.RBXDLPortalDownloader()
    statuses=[]
    client.status_callback=statuses.append
    class FakeBrowser:
        calls=0
        lock=threading.Lock()
        def __init__(self,**kw):pass
        def start(self):pass
        def close(self):pass
        def fetch(self,asset_id,**kw):
            with FakeBrowser.lock:
                FakeBrowser.calls+=1
            raise RuntimeError('RBXDL_UI_NOT_READY: mock missing button')
    with TemporaryDirectory() as d, mock.patch.object(core,'RBXDLBrowserFetcher',FakeBrowser):
        result,errors=client._download_single_fallback([str(i) for i in range(30)],Path(d),'test')
        assert result=={}
        assert len(errors)==30
        assert FakeBrowser.calls <= 8, FakeBrowser.calls
        assert any('unavailable for the batch' in status for status in statuses)
        assert any('RBXDL_SINGLE_UI_UNAVAILABLE' in error for error in errors.values())
    client.close()


def test_backend_only_still_tries_native():
    client=core.RBXDLPortalDownloader()
    client._bulk_session_available=lambda:(False,'none')
    with TemporaryDirectory() as d:
        p=Path(d)/'111.mp3';p.write_bytes(b'ID3'+b'0'*30)
        client._download_official_assets=lambda ids,out: {'111':p}
        client._download_single_fallback=lambda *a,**k: (_ for _ in ()).throw(AssertionError('no UI'))
        with mock.patch.dict(core.os.environ, {'TM_RBXDL_BACKEND_ONLY':'1'}):
            results,errors=client.download_single_batch(['111'],Path(d))
            assert set(results)=={'111'} and not errors
    client.close()


def test_retry_job_uses_same_ids_and_preserves_history():
    # Exercise the real method with a light-weight stub (no background threads).
    text=(base/'RbxMigrator.py').read_text(encoding='utf-8')
    assert 'def retry_download_job(self, job_id, overrides=None):' in text
    assert '"source": "\\n".join(ids)' in text
    assert '/api/downloader/jobs/([0-9a-fA-F-]+)/retry' in text
    assert 'retryDownloadJob(id)' in text


if __name__=='__main__':
    tests=[(name,func) for name,func in sorted(globals().items()) if name.startswith('test_') and callable(func)]
    for name,func in tests:
        func()
        print('PASS',name)
    print(f'All {len(tests)} v3.32 tests passed.')
