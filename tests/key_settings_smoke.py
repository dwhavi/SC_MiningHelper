"""Browser + real settings API using a temporary PC folder and synthetic XML."""
import sys,tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sc_pad_control as m
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
D=Path(__file__).resolve().parents[1]
O=Path('/downloads/key-settings-preview');O.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory() as tmp, patch.object(m,'EXE_DIR',tmp), sync_playwright() as p:
 cfg=dict(m.DEFAULTS,pairing_code='123456')
 client=TestClient(m.create_app(m.ScanState({}, {}, {}, {}),cfg))
 browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1024,'height':768})
 page.add_init_script("localStorage.setItem('sc_pair','123456')")
 errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  path=r.request.url.split('deck.test',1)[-1]
  if path.startswith('/api/'):
   res=client.request(r.request.method,path,headers={'X-Pairing-Code':r.request.headers.get('x-pairing-code',''),'Content-Type':'application/json'},content=r.request.post_data)
   return r.fulfill(status=res.status_code,content_type='application/json',body=res.text)
  return r.fulfill(content_type='text/html',body=(D/'control_deck.html').read_text())
 page.route('http://deck.test/**',route);page.goto('http://deck.test/');page.wait_for_selector('#settingsOpen')
 page.locator('#settingsOpen').click();page.wait_for_selector('#keyDialog[open]')
 assert page.locator('#keyList button').count()==7
 page.locator('#keyMain').select_option('f5');page.locator('#keySave').click()
 page.wait_for_function("document.getElementById('keyMessage').textContent.includes('PC에 저장')")
 assert page.locator('[data-id="weapons-increase"]').is_enabled()
 assert m._read_key_overrides()['weapons-increase']['keys']=='f5'
 page.screenshot(path=str(O/'tablet-saved.png'))
 # conflicting choice is rejected without replacing the persisted setting
 page.locator('[data-setting-id="engine-increase"]').click();page.locator('#keyMain').select_option('f5');page.locator('#keySave').click()
 page.wait_for_function("document.getElementById('keyMessage').textContent.includes('중복')")
 assert 'engine-increase' not in m._read_key_overrides()
 page.locator('[data-setting-id="weapons-increase"]').click();page.locator('#keyReset').click()
 page.wait_for_function("document.getElementById('keyMessage').textContent.includes('지웠습니다')")
 assert page.locator('[data-id="weapons-increase"]').is_disabled()
 page.locator('#keyUnassigned').check();assert page.locator('#keyList button').count()==7
 page.set_viewport_size({'width':390,'height':844})
 page.locator('#keyMain').select_option('f6');page.locator('#keyMods input[value="lalt"]').check()
 page.screenshot(path=str(O/'phone-edit.png'))
 assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
 box=page.locator('#keyDialog').bounding_box();assert box['width']<=390 and box['height']<=844
 page.locator('#keySave').click();page.wait_for_function("document.getElementById('keyMessage').textContent.includes('PC에 저장')")
 page.locator('#keyClose').click();page.reload();page.wait_for_selector('[data-id="weapons-increase"]')
 assert '사용자' in page.locator('[data-id="weapons-increase"]').inner_text()
 # expired/wrong pairing cannot open settings
 page.evaluate("S.code='wrong'");page.locator('#settingsOpen').click();page.wait_for_selector('#pairOverlay.show')
 assert not page.locator('#keyDialog').evaluate('e=>e.open')
 assert not errors,errors
 browser.close()
print('key settings browser/API checks: popup auth, 7 unassigned power actions, save/persist/reload, custom source, duplicate rejection, reset, filter, modifiers, phone sizing; JS errors: 0')
