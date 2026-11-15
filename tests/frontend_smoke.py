import json
from pathlib import Path
from playwright.sync_api import sync_playwright
D=Path(__file__).resolve().parents[1]
with sync_playwright() as p:
 b=p.chromium.launch(headless=True)
 page=b.new_page(viewport={'width':1024,'height':768})
 requests=[]; errors=[]; auth=[200]
 page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  url=r.request.url
  if '/api/layout' in url:return r.fulfill(status=200,content_type='application/json',body=(D/'deck_layout.json').read_text())
  if '/api/auth' in url:return r.fulfill(status=auth[0],content_type='application/json',body='{"ok":true}')
  if '/api/' in url:
   requests.append((url,json.loads(r.request.post_data or '{}')))
   return r.fulfill(status=200,content_type='application/json',body='{"ok":true}')
  return r.fulfill(status=200,content_type='text/html',body=(D/'control_deck.html').read_text())
 page.route('http://deck.test/**',route)
 page.goto('http://deck.test/');page.wait_for_selector('#grid button')
 page.get_by_role('button',name='채굴 모드',exact=False).click()
 page.wait_for_timeout(100)
 assert [x[1]['type'] for x in requests]==['press'],requests
 requests.clear()
 h=page.get_by_role('button',name='스캔 핑',exact=False)
 box=h.bounding_box();page.mouse.move(box['x']+10,box['y']+10);page.mouse.down();page.mouse.up();page.wait_for_timeout(100)
 assert [x[1]['type'] for x in requests]==['down','up'],requests
 requests.clear()
 for i in range(10):page.get_by_role('button',name='선박',exact=True).click();page.get_by_role('button',name='채굴',exact=True).click()
 assert page.evaluate('S.releases.length')==10
 # A failed pairing network check cannot be called successful.
 auth[0]=401
 page.reload();page.wait_for_selector('#pairOverlay.show')
 page.locator('#pairCode').fill('123456')
 auth[0]=500;page.locator('#pairGo').click();page.wait_for_timeout(100)
 assert page.locator('#pairOverlay').evaluate("e=>e.classList.contains('show')")
 auth[0]=200;page.locator('#pairGo').click();page.wait_for_timeout(100)
 assert not page.locator('#pairOverlay').evaluate("e=>e.classList.contains('show')")
 print('frontend checks: tap once, hold down/up, bounded listeners, pairing failure/success')
 print('browser errors:',errors)
 b.close()
