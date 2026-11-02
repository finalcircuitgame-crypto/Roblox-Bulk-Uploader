from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rbxmigrator_core as core

class FakeButton:
    def __init__(self,enabled=True): self.enabled=enabled
    def is_visible(self): return True
    def is_enabled(self): return self.enabled

class FakeLocator:
    def __init__(self,page,kind): self.page=page; self.kind=kind
    def count(self):
        if self.kind=='input': return 1 if self.page.time>=2 else 0
        if self.kind=='button': return 1 if self.page.time>=4 else 0
        return 0
    def nth(self,index): return FakeButton(enabled=self.page.time>=5)

class FakePage:
    def __init__(self): self.time=0
    def locator(self,selector):
        if selector.startswith('input'): return FakeLocator(self,'input')
        if 'Download' in selector: return FakeLocator(self,'button')
        return FakeLocator(self,'none')
    def get_by_role(self,role,**kwargs): return FakeLocator(self,'button')
    def wait_for_timeout(self,millis): self.time += 1
    def is_closed(self): return False

class BrowserTests(unittest.TestCase):
    def test_controls_appear_after_hydration(self):
        f=core.RBXDLBrowserFetcher()
        page=FakePage()
        self.assertIsNotNone(f._find_asset_input(page))
        self.assertIsNotNone(f._find_download_button(page))
        self.assertGreaterEqual(page.time,5)

    def test_ui_failure_retries_twice(self):
        f=core.RBXDLBrowserFetcher()
        attempts=[]
        def flaky(_):
            attempts.append(1)
            if len(attempts)<3: raise RuntimeError('RBXDL_UI_NOT_READY: mock')
            return b'data','.rbxm','browser'
        f._fetch_once=flaky
        f._reset_browser=lambda:None
        with patch.object(core.time,'sleep'):
            self.assertEqual(f.fetch('123')[0],b'data')
        self.assertEqual(len(attempts),3)

    def test_not_authorized_is_not_retried(self):
        f=core.RBXDLBrowserFetcher()
        attempts=[]
        def denied(_):
            attempts.append(1)
            raise RuntimeError('403 Forbidden - User is not authorized to access Asset')
        f._fetch_once=denied
        with self.assertRaisesRegex(RuntimeError,'403 Forbidden'):
            f.fetch('123')
        self.assertEqual(len(attempts),1)

    def test_ui_error_bounded(self):
        f=core.RBXDLBrowserFetcher()
        attempts=[]
        def missing(_):
            attempts.append(1)
            raise RuntimeError('RBXDL_UI_NOT_READY: no button')
        f._fetch_once=missing
        f._reset_browser=lambda:None
        with patch.object(core.time,'sleep'):
            with self.assertRaisesRegex(RuntimeError,'after 3 page loads'):
                f.fetch('123')
        self.assertEqual(len(attempts),3)

if __name__=='__main__':unittest.main(verbosity=2)
