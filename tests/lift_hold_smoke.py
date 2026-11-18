"""Browser + real settings API using a temporary PC folder and synthetic XML."""
import sys,tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sc_pad_control as m
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
D=Path(__file__).resolve().parents[1]
O=Path('/downloads/lift-hold-preview');O.mkdir(exist_ok=True)
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
 page.locator('#keySection').select_option('3')
 page.locator('[data-setting-id="launch-seq"]').click()
 assert page.locator('#keySave').is_enabled() # editable even when XML is absent
 rows=page.locator('#seqSteps .step-row')
 assert rows.count()==3
 rows.nth(0).locator('select').select_option('f5')
 rows.nth(0).locator('input[value="lalt"]').check()
 rows.nth(1).locator('select').select_option('space')
 rows.nth(2).locator('select').select_option('f6')
 rows.nth(0).locator('input[data-step]').fill('2500')
 assert page.locator('#liftHoldMs').input_value()=='1500'
 page.locator('#liftHoldMs').fill('250')
 page.locator('#keySave').click()
 page.wait_for_function("document.getElementById('keyMessage').textContent.includes('PC에 시퀀스')")
 b=next(bt for bt in m._deck_buttons(m._resolved_deck_layout(cfg)) if bt['id']=='launch-seq')
 assert not b['unbound']
 assert [st['key'] for st in b['steps']]==['lalt+f5','space','f6']
 assert b['steps'][0]['delay_ms']==2500
 assert b['steps'][1]['hold_ms']==250
 rows.nth(1).evaluate("e=>e.scrollIntoView({block:'center'})")
 page.screenshot(path=str(O/'tablet-sequence-keys.png'))
 page.locator('#keyClose').click();page.reload();page.locator('#settingsOpen').click()
 page.locator('#keySection').select_option('3');page.locator('[data-setting-id="launch-seq"]').click()
 assert rows.nth(0).locator('select').input_value()=='f5'
 assert page.locator('#liftHoldMs').input_value()=='250'
 assert rows.nth(0).locator('input[value="lalt"]').is_checked()
 page.set_viewport_size({'width':390,'height':844})
 rows.nth(1).evaluate("e=>e.scrollIntoView({block:'center'})");page.screenshot(path=str(O/'phone-sequence-keys.png'))
 rows.nth(2).locator('.step-reset').click();page.locator('#keySave').click()
 page.wait_for_function("document.getElementById('keyMessage').textContent.includes('PC에 시퀀스')")
 b=next(bt for bt in m._deck_buttons(m._resolved_deck_layout(cfg)) if bt['id']=='launch-seq')
 assert b['unbound'] and b['steps'][2]['key']==''
 assert b['steps'][0]['key']=='lalt+f5' and b['steps'][0]['delay_ms']==2500
 page.locator('#keyReset').click()
 page.wait_for_function("document.getElementById('keyMessage').textContent.includes('전체 단계')")
 assert 'launch-seq' not in m._read_key_overrides()
 # LANDING has no Space option. All step editors remain available without XML.
 page.locator('[data-setting-id="landing-seq"]').click()
 assert page.locator('#liftHoldMs').count()==0
 assert rows.count()==2 and rows.locator('select option[value="space"]').count()==0
 assert page.locator('#keySave').is_enabled()
 assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
 assert not errors,errors
 browser.close()
print('lift hold browser/API: default 1500ms, save/reload 250ms, separate delay, reset, launch-only, tablet/phone; inherited checks: edit without XML, modifiers, Space exception, save/reload, partial XML reset, full reset, phone editor and LANDING safe options passed')
