"""Offline regression tests for TM v3.31 routing. Does not contact Roblox/RBXDL."""
from __future__ import annotations
import importlib.util
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

mod_path = Path(__file__).parent / 'rbxmigrator_core.py'
spec = importlib.util.spec_from_file_location('tm331_core', mod_path)
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def portal_stub(folder, *results):
    folder.mkdir(parents=True, exist_ok=True)
    mapping = {}
    for asset in results:
        p=folder / f'{asset}.rbxm'
        p.write_bytes(b'\x3croblox\x3eassetdata')
        mapping[asset]=p
    return mapping


def new_client():
    statuses=[]
    events=[]
    client=core.RBXDLPortalDownloader(
        status_callback=statuses.append,
        item_callback=lambda asset, state, meta: events.append((asset,state,meta))
    )
    # Routing tests should never contact Roblox. Native fetch is tested separately.
    client._download_official_assets=lambda ids,out: {}
    return client, statuses, events


def test_uses_portal_without_browser():
    client, statuses, _ = new_client()
    client._bulk_session_available=lambda: (True,'OK')
    with TemporaryDirectory() as d:
        def portal(ids, out, **kwargs):
            assert ids==['111','222']
            assert kwargs['allow_interactive_auth'] is False
            assert kwargs['allow_single_fallback'] is False
            assert kwargs['bulk_already_validated'] is True
            return portal_stub(Path(out),*ids), {}
        client.download_batch=portal
        client._download_single_fallback=lambda *a,**k: (_ for _ in ()).throw(AssertionError('browser should not start'))
        result, error=client.download_single_batch(['111','222'],Path(d))
        assert len(result)==2 and not error
        assert any('Portal bulk HTTP backend' in s for s in statuses)
    client.close()


def test_partial_fallback_preserves_backend_successes():
    client, _, _ = new_client()
    client._bulk_session_available=lambda: (True,'OK')
    with TemporaryDirectory() as d:
        def portal(ids, out, **kwargs):
            return portal_stub(Path(out),'111'), {'222':'not available in Portal'}
        def fallback(ids, out, reason, **kwargs):
            assert ids==['222'], 'must not redownload already migrated asset'
            return portal_stub(Path(out),'222'),{}
        client.download_batch=portal
        client._download_single_fallback=fallback
        result, error=client.download_single_batch(['111','222','111'],Path(d),allow_browser_fallback=True)
        assert set(result)=={'111','222'} and not error
    client.close()


def test_absent_portal_single_fallback():
    client,_,_=new_client()
    client._bulk_session_available=lambda: (False,'No Key Access')
    with TemporaryDirectory() as d:
        client.download_batch=lambda *a,**k: (_ for _ in ()).throw(AssertionError('Portal called with no access'))
        def fallback(ids,out, reason, **kw):
            assert ids==['333']
            assert kw['retry_rate_limits_forever'] is False
            assert kw['priority']==2
            return {}, {'333':'public token required'}
        client._download_single_fallback=fallback
        result, error=client.download_single_batch(['333'],Path(d),priority=2,allow_browser_fallback=True)
        assert not result and error['333']=='public token required'
    client.close()


def test_backend_only_never_uses_ui():
    client,_,events=new_client()
    client._bulk_session_available=lambda: (False,'no session')
    with TemporaryDirectory() as d:
        client._download_single_fallback=lambda *a,**k: (_ for _ in ()).throw(AssertionError('browser started'))
        with mock.patch.dict(os.environ, {'TM_RBXDL_BACKEND_ONLY':'1'}):
            r,e=client.download_single_batch(['444'],Path(d))
        assert not r and 'API-only mode' in e['444']
        assert events[-1][1]=='failed'
    client.close()


def test_bulk_flags_refuse_browser_and_tokens():
    client,_,_=new_client()
    client._bulk_session_available=lambda: (False,'Expired')
    client._restore_bulk_access_interactively=lambda reason: (_ for _ in ()).throw(AssertionError('UI opened'))
    client._download_single_fallback=lambda *a, **k: (_ for _ in ()).throw(AssertionError('browser started'))
    with TemporaryDirectory() as d:
        try:
            client.download_batch(['555'],Path(d),allow_interactive_auth=False,allow_single_fallback=False)
        except core.RBXDLBulkUnavailable as exc:
            assert 'Expired' in str(exc)
        else:
            raise AssertionError('Expected explicit unavailable exception')
    client.close()


def test_backend_only_on_partial_result():
    client,_,_=new_client()
    client._bulk_session_available=lambda: (True, 'OK')
    with TemporaryDirectory() as d:
        client.download_batch=lambda ids,out,**kw: (portal_stub(Path(out),'111'), {'222':'portal denies asset'})
        client._download_single_fallback=lambda *a,**k: (_ for _ in ()).throw(AssertionError('browser started'))
        with mock.patch.dict(os.environ, {'TM_RBXDL_BACKEND_ONLY':'true'}):
            r,e=client.download_single_batch(['111','222'],Path(d))
        assert set(r)=={'111'} and 'portal denies asset' in e['222'] and 'API-only' in e['222']
    client.close()


def test_large_batch_propagates_backend_only_flags():
    client,_,_=new_client()
    ids=[str(i) for i in range(501)]
    calls=[]
    original = client.download_batch
    def delegated(items,out,**kw):
        if len(items)<501:
            calls.append((len(items),kw))
            return {}, {i:'missing' for i in items}
        return original(items,out,**kw)
    client.download_batch=delegated
    with TemporaryDirectory() as d:
        r,e=client.download_batch(ids,Path(d),allow_interactive_auth=False,allow_single_fallback=False,bulk_already_validated=True)
    assert len(calls)==2 and all(c['allow_single_fallback'] is False for n,c in calls)
    assert len(e)==501 and not r
    client.close()


def run_all():
    funcs = sorted((name, f) for name,f in globals().items() if name.startswith('test_') and callable(f))
    for name, fun in funcs:
        fun()
        print('PASS',name)
    print(f'All {len(funcs)} offline routing tests passed.')

if __name__=='__main__':
    run_all()
