"""Real browser regression checks; input calls are intercepted, never sent to a game."""
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sc_pad_control as m
from playwright.sync_api import sync_playwright
D=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
 # Deliberately small synthetic XML fixture, not the user's profile or game defaults.
 xml=Path(tmp)/'test.xml'
 xml.write_text('<ActionMaps><actionmap name="seat_general"><action name="v_set_mining_mode"><rebind input="kb1_lalt+m"/></action></actionmap><actionmap name="spaceship_radar"><action name="v_invoke_ping"><rebind input="kb1_tab"/></action></actionmap></ActionMaps>')
 layout=[m._resolved_deck_layout({'actionmaps_path':str(xml)})]
 b=p.chromium.launch(headless=True)
 page=b.new_page(viewport={'width':1024,'height':768})
 requests=[]; errors=[]; auth=[200]
 page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  url=r.request.url
  if '/api/layout' in url:return r.fulfill(status=200,content_type='application/json',body=json.dumps(layout[0]))
  if '/api/auth' in url:return r.fulfill(status=auth[0],content_type='application/json',body='{"ok":true}')
  if '/api/' in url:
   requests.append((url,json.loads(r.request.post_data or '{}')))
   return r.fulfill(status=200,content_type='application/json',body='{"ok":true}')
  return r.fulfill(status=200,content_type='text/html',body=(D/'control_deck.html').read_text())
 page.route('http://deck.test/**',route)
 page.goto('http://deck.test/');page.wait_for_selector('#grid button')
 assert page.locator('#tabs button').count()==4
 assert page.locator('#common button').count()==4
 assert page.locator('[data-id="laser"]').is_disabled()
 page.locator('[data-id="mining-mode"]').click();page.wait_for_timeout(80)
 assert [x[1]['type'] for x in requests]==['press'],requests
 requests.clear()
 h=page.locator('[data-id="ping"]');box=h.bounding_box()
 page.mouse.move(box['x']+10,box['y']+10);page.mouse.down();page.wait_for_timeout(50)
 page.evaluate('releaseAll()');page.mouse.up();page.wait_for_timeout(80)
 assert [x[1]['type'] for x in requests]==['down','up'],requests
 requests.clear()
 for i in range(10):
  page.get_by_role('button',name='조종·착륙',exact=True).click()
  page.get_by_role('button',name='채굴',exact=True).click()
 assert page.evaluate('S.releases.length')==3
 # No XML means only the literal ESC button is enabled, including after reload.
 layout[0]=m._resolved_deck_layout({})
 page.reload();page.wait_for_selector('#grid button')
 assert page.locator('#grid button:not(:disabled)').count()==0
 assert page.locator('#common button:not(:disabled)').count()==1
 auth[0]=401;page.reload();page.wait_for_selector('#pairOverlay.show')
 page.locator('#pairCode').fill('123456')
 auth[0]=500;page.locator('#pairGo').click();page.wait_for_timeout(80)
 assert page.locator('#pairOverlay').evaluate("e=>e.classList.contains('show')")
 auth[0]=200;page.locator('#pairGo').click();page.wait_for_timeout(80)
 assert not page.locator('#pairOverlay').evaluate("e=>e.classList.contains('show')")
 assert not errors,errors
 print('frontend checks: 4 pages, common row, disabled missing binds, single tap, one hold release, bounded listeners, no-XML fail closed, pairing failure/success; JS errors: 0')
 b.close()
