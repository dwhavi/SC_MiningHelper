"""Viewport-fit flight deck checks. Synthetic bindings, intercepted input only."""
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sc_pad_control as m
from playwright.sync_api import sync_playwright
D=Path(__file__).resolve().parents[1];O=Path('/downloads/ship-fit-preview');O.mkdir(exist_ok=True)
layout=m._resolved_deck_layout({},overrides={});layout['actionmaps_active']=True
for i,b in enumerate(m._deck_buttons(layout)):
 if not b.get('blocked_reason'):b.update(keys='SEQ' if b.get('sequence') else 'f'+str(i%12+1),unbound=False)
state=dict(running=False,step=1,total=3,label='LAUNCH',button_id='launch-seq');requests=[]
with sync_playwright() as p:
 browser=p.chromium.launch()
 page=browser.new_page(viewport={'width':1024,'height':768},has_touch=True)
 page.add_init_script("localStorage.setItem('sc_pair','123456')")
 errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  path=r.request.url.split('deck.test',1)[-1]
  if path in ['/api/layout','/api/key-settings']:data=layout
  elif path=='/api/sequence':
   if r.request.method=='POST':requests.append(json.loads(r.request.post_data));state['running']=True
   data=state
  elif path=='/api/sequence-cancel':state['running']=False;data={'ok':True}
  elif path=='/api/mining-scan':data={'running':False}
  elif path=='/api/input':requests.append(json.loads(r.request.post_data));data={'ok':True}
  elif path.startswith('/api/'):data={'ok':True}
  else:return r.fulfill(content_type='text/html',body=(D/'control_deck.html').read_text())
  r.fulfill(content_type='application/json',body=json.dumps(data))
 page.route('http://deck.test/**',route);page.goto('http://deck.test/')
 page.get_by_role('button',name='조종·착륙',exact=True).click()
 assert page.locator('#grid .group').count()==0
 assert page.locator('#pageCount').text_content()=='9 / 9 연결'
 assert page.locator('.wb').count()==9
 for w,h in [(1024,768),(768,1024),(1280,800),(390,844),(844,390),(360,640),(800,600)]:
  page.set_viewport_size({'width':w,'height':h});page.evaluate("document.getElementById('log').replaceChildren()");page.wait_for_timeout(100)
  dims=page.evaluate('({w:document.documentElement.scrollWidth,h:document.documentElement.scrollHeight,vw:innerWidth,vh:innerHeight})')
  assert dims['w']<=w and dims['h']<=h+1,dims
  for selector in ['#powerControls','#common','#tabs','#wheelWrap','#utilityRow','#policyNote','[data-bid="ready"] .wcenter','[data-bid="wheel-power"] .wseg','[data-bid="wheel-engine"] .wseg']:
   box=page.locator(selector).bounding_box()
   assert box and box['x']>=-1 and box['y']>=-1 and box['x']+box['width']<=w+1 and box['y']+box['height']<=h+1,(w,h,selector,box)
  page.mouse.wheel(0,1200);page.wait_for_timeout(50);assert page.evaluate('scrollY')==0
  page.screenshot(path=str(O/f'{w}x{h}.png'))
  if (w,h) in [(1024,768),(844,390)]:
   page.locator('[data-bid="launch-seq"]').dispatch_event('pointerdown');page.wait_for_timeout(200)
   assert page.locator('#seqBar').is_visible()
   box=page.locator('#seqCancel').bounding_box();assert box['y']+box['height']<=h
   wheel=page.locator('#wheelPanel').bounding_box();bar=page.locator('#seqBar').bounding_box();assert wheel['y']+wheel['height']<=bar['y']+1 or bar['x']+bar['width']<=wheel['x']+1
   assert page.evaluate('document.documentElement.scrollHeight<=innerHeight+1')
   page.screenshot(path=str(O/f'{w}x{h}-sequence.png'))
   page.locator('#seqCancel').click();page.wait_for_timeout(200)
 # Same page, rotate and open the still-scrollable settings dialog.
 page.set_viewport_size({'width':390,'height':844});page.locator('#settingsOpen').click()
 page.wait_for_selector('#keyDialog[open]');assert page.locator('#keyDialog').is_visible();page.locator('#keyClose').click()
 page.get_by_role('button',name='채굴',exact=True).click()
 assert not page.locator('body').evaluate("e=>e.classList.contains('ship-fit')")
 assert page.evaluate('document.documentElement.scrollHeight>innerHeight')
 assert not errors,errors
 browser.close()
print('ship fit: 7 viewports, portrait/landscape/resize, all 9 wheel controls + power/common/utilities visible, no page scroll, sequence stop in flow, settings and mining unaffected')
