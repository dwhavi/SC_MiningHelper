"""Browser + real settings API using a temporary PC folder and synthetic XML."""
import sys,tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sc_pad_control as m
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
D=Path(__file__).resolve().parents[1]
O=Path('/downloads/power-alias-preview');O.mkdir(exist_ok=True)
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
 page.locator('#keySection').select_option('3');page.locator('[data-setting-id="wheel-power"]').click()
 assert page.locator('#keyMain').is_enabled() and page.locator('#keySave').is_enabled()
 assert '공유' in page.locator('#keyMessage').inner_text()
 page.locator('#keyMain').select_option('u');page.locator('#keySave').click()
 page.wait_for_function("document.getElementById('keyMessage').textContent.includes('PC에 저장')")
 assert m._read_key_overrides()['power-all']['keys']=='u'
 page.locator('#keySection').select_option('4');page.locator('[data-setting-id="power-all"]').click()
 assert page.locator('#keyMain').input_value()=='u'
 page.locator('#keyMain').select_option('f5');page.locator('#keySave').click()
 page.wait_for_function("document.getElementById('keyMessage').textContent.includes('PC에 저장')")
 page.locator('#keySection').select_option('3');page.locator('[data-setting-id="wheel-power"]').click()
 assert page.locator('#keyMain').input_value()=='f5'
 page.screenshot(path=str(O/'tablet-shared-setting.png'))
 page.set_viewport_size({'width':390,'height':844})
 page.locator('[data-setting-id="wheel-engine"]').click();page.locator('#keyMain').select_option('i');page.locator('#keySave').click()
 page.wait_for_function("document.getElementById('keyMessage').textContent.includes('PC에 저장')")
 page.locator('#keyClose').click();page.reload();page.get_by_role('button',name='조종·착륙',exact=True).click()
 assert page.locator('[data-bid="wheel-power"]').evaluate("e=>!e.classList.contains('off')")
 assert page.locator('[data-bid="wheel-engine"]').evaluate("e=>!e.classList.contains('off')")
 page.locator('#wheelPanel').screenshot(path=str(O/'phone-unlocked-arcs.png'))
 page.locator('#settingsOpen').click();page.locator('#keySection').select_option('3');page.locator('[data-setting-id="wheel-power"]').click()
 page.locator('#keyReset').click();page.wait_for_function("document.getElementById('keyMessage').textContent.includes('지웠습니다')")
 assert 'power-all' not in m._read_key_overrides()
 assert not errors,errors
 browser.close()
print('power aliases browser/API: editable no XML, side-to-power-page and reverse sync, save/reload, both arcs unlocked, reset passed')
