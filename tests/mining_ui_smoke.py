"""Synthetic browser preview: no game input or real mineral reading."""
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sc_pad_control as m
from playwright.sync_api import sync_playwright
D=Path(__file__).resolve().parents[1];O=Path('/downloads/mining-fixed-preview');O.mkdir(exist_ok=True)
layout=m._resolved_deck_layout({},overrides={})
for i,b in enumerate(m._deck_buttons(layout)):
 if not b.get('sequence') and not b.get('blocked_reason'):b.update(keys='f'+str(i%12+1),unbound=False)
state=dict(running=False,pulses=0,interval_ms=5000,tracking='중앙 HUD 탐색 중');requests=[];errors=[]
with sync_playwright() as p:
 browser=p.chromium.launch();page=browser.new_page(viewport={'width':1024,'height':1100})
 page.add_init_script("localStorage.setItem('sc_pair','123456')")
 page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  path=r.request.url.split('deck.test',1)[-1]
  if path=='/api/layout':data=layout
  elif path=='/api/mining-scan':
   if r.request.method=='POST':
    body=json.loads(r.request.post_data);requests.append(body);state.update(running=body['enabled'],interval_ms=body['interval_ms'],pulses=int(body['enabled']))
   data=state
  elif path.startswith('/api/'):data={'ok':True}
  else:return r.fulfill(content_type='text/html',body=(D/'control_deck.html').read_text())
  r.fulfill(content_type='application/json',body=json.dumps(data))
 page.route('http://deck.test/**',route);page.goto('http://deck.test/')
 page.wait_for_selector('[data-bid="mining-auto"]')
 assert page.locator('#miningMode').count()==0
 assert page.locator('#miningLabel').text_content()=='스캔'
 assert page.locator('#miningInterval').input_value()=='5'
 assert not page.locator('#miningInterval').is_disabled()
 page.evaluate('window.scrollTo(0,250)');page.screenshot(path=str(O/'tablet-off.png'))
 page.locator('[data-bid="mining-auto"]').dispatch_event('pointerdown');page.wait_for_timeout(200)
 assert requests==[{'enabled':True,'interval_ms':5000}],requests
 assert page.locator('#miningInterval').is_disabled()
 assert page.locator('#miningLampText').text_content()=='ON'
 page.screenshot(path=str(O/'tablet-on.png'))
 page.set_viewport_size({'width':390,'height':844});page.evaluate('window.scrollTo(0,180)')
 assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
 page.screenshot(path=str(O/'phone-on.png'))
 page.locator('[data-bid="mining-auto"]').dispatch_event('pointerdown');page.wait_for_timeout(200)
 assert requests[-1]['enabled']==False
 assert page.locator('#miningLampText').text_content()=='OFF'
 page.locator('#miningInterval').fill('8')
 page.locator('[data-bid="mining-auto"]').dispatch_event('pointerdown');page.wait_for_timeout(200)
 assert requests[-1]=={'enabled':True,'interval_ms':8000}
 page.reload();page.wait_for_timeout(1300)
 assert page.locator('#miningInterval').input_value()=='8'
 assert page.locator('.mining-on .scan-lamp').count()==1
 page.get_by_role('button',name='조종·착륙',exact=True).click();page.wait_for_timeout(100)
 page.evaluate('window.scrollTo(0,900)');assert page.locator('#miningStop').bounding_box()['y']<844
 page.locator('#miningStop').click();page.wait_for_timeout(200)
 assert not page.locator('#miningBar').is_visible()
 assert not errors,errors
 browser.close()
print('mining UI: fixed 5s default, ON/OFF toggle, editable 8s, reload restores active interval, lamp, fixed stop across pages, phone width, zero JS errors')
