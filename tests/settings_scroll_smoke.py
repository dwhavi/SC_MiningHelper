"""Touch and wheel scrolling regression for the complete ship settings list."""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sc_pad_control as m
from playwright.sync_api import sync_playwright
D=Path(__file__).resolve().parents[1]
O=Path('/downloads/settings-scroll-preview');O.mkdir(exist_ok=True)
layout=m._resolved_deck_layout({},overrides={})
with sync_playwright() as p:
 browser=p.chromium.launch()
 for width,height in [(1024,768),(768,1024),(390,844),(844,390)]:
  context=browser.new_context(viewport={'width':width,'height':height},has_touch=True,is_mobile=True)
  page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  page.add_init_script("localStorage.setItem('sc_pair','123456')")
  def route(r):
   path=r.request.url.split('deck.test',1)[-1]
   if path in ['/api/layout','/api/key-settings']:data=layout
   elif path=='/api/sequence':data={'running':False}
   elif path.startswith('/api/'):data={'ok':True}
   else:return r.fulfill(content_type='text/html',body=(D/'control_deck.html').read_text())
   r.fulfill(content_type='application/json',body=json.dumps(data))
  page.route('http://deck.test/**',route);page.goto('http://deck.test/')
  page.locator('#settingsOpen').click();page.locator('#keySection').select_option('3')
  assert page.locator('#keyList button').count()==9
  lst=page.locator('#keyList');lst.scroll_into_view_if_needed()
  geom=lst.evaluate('e=>({client:e.clientHeight,scroll:e.scrollHeight,touch:getComputedStyle(e.querySelector("button")).touchAction})')
  print(width,height,geom)
  assert geom['scroll']>geom['client']+50,'list must overflow rather than shrink rows'
  box=lst.bounding_box();session=context.new_cdp_session(page)
  # Start on a button: this is where users put their finger, not the list gap.
  x=box['x']+box['width']/2;y=box['y']+box['height']-15
  session.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]})
  for i in range(1,13):
   session.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x,'y':y-min(box['height']-30,260)*i/12}]})
   page.wait_for_timeout(25)
  session.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
  page.wait_for_timeout(500)
  assert lst.evaluate('e=>e.scrollTop')>30,'finger swipe on list button must scroll'
  # Continue with real wheel input and verify the final item can be selected.
  page.mouse.move(box['x']+box['width']/2,box['y']+min(box['height']-15,80))
  page.mouse.wheel(0,1500);page.wait_for_timeout(400)
  last=page.locator('[data-setting-id="wheel-engine"]')
  last.click();assert page.locator('#keyButtonName').inner_text()=='엔진 전원'
  page.screenshot(path=str(O/f'{width}x{height}-last-selected.png'))
  # The outer dialog must also allow reaching the editor actions on short screens.
  page.locator('#keySave').scroll_into_view_if_needed()
  rect=page.locator('#keySave').bounding_box()
  assert rect['y']>=0 and rect['y']+rect['height']<=height
  page.screenshot(path=str(O/f'{width}x{height}-editor-actions.png'))
  assert not errors,errors
  context.close()
 browser.close()
print('settings scroll: touch swipe on rows + wheel + last ship item selection at four viewport sizes passed')
