"""Synthetic XML browser preview and sequence controls; no game input."""
import sys,json,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sc_pad_control as m
from playwright.sync_api import sync_playwright
D=Path(__file__).resolve().parents[1]
O=Path('/downloads/radial-release');O.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
 raw=json.loads((D/'deck_layout.json').read_text())
 actions={b.get('action') for b in m._deck_buttons(raw) if b.get('action') and not b.get('blocked_reason')}
 for b in m._deck_buttons(raw):
  actions.update(st['action'] for st in b.get('sequence',[]))
 maps={}
 for i,a in enumerate(sorted(actions)):
  am,ac=a.split('.',1);key='space' if a.endswith('.v_strafe_up') else ['rctrl','ralt','lctrl+lshift','rctrl+rshift','lalt+lshift'][i//12]+'+f'+str(i%12+1)
  if a=='spaceship_power.v_power_toggle':key='u'
  if a=='spaceship_power.v_power_toggle_thrusters':key='i'
  maps.setdefault(am,[]).append(f'<action name="{ac}"><rebind input="kb1_{key}"/></action>')
 xml=Path(tmp)/'synthetic.xml';xml.write_text('<ActionMaps>'+''.join(f'<actionmap name="{am}">'+''.join(v)+'</actionmap>' for am,v in maps.items())+'</ActionMaps>')
 layout=m._resolved_deck_layout({'actionmaps_path':str(xml)},overrides={})
 # Explicit unique fixture keys for wheel only, never represented as game defaults.
 ship=next(x for x in layout['pages'] if x['id']=='ship')
 keys={'ready':'ralt+r','gear':'n','vtol':'k','ship-light':'l','night-vision':'ralt+l'}
 for bt in ship['buttons']:
  if bt['id'] in keys:bt.update(keys=keys[bt['id']],unbound=False)
  if bt.get('sequence'):bt.update(keys='SEQ',unbound=False)
 state={'running':False,'step':0,'total':3,'label':'LAUNCH','button_id':'launch-seq'};req=[];errors=[]
 browser=p.chromium.launch();page=browser.new_page(viewport={'width':1024,'height':1100})
 page.add_init_script("localStorage.setItem('sc_pair','123456')")
 page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  path=r.request.url.split('deck.test',1)[-1]
  if path=='/api/layout':data=layout
  elif path=='/api/key-settings':data=layout
  elif path=='/api/sequence' and r.request.method=='GET':data=state
  elif path=='/api/sequence':
   req.append(json.loads(r.request.post_data));state.update(running=True,step=1);data={'ok':True}
  elif path=='/api/sequence-cancel':state.update(running=False,msg='취소했습니다');data={'ok':True}
  elif path.startswith('/api/'):data={'ok':True}
  else:return r.fulfill(content_type='text/html',body=(D/'control_deck.html').read_text())
  r.fulfill(content_type='application/json',body=json.dumps(data))
 page.route('http://deck.test/**',route);page.goto('http://deck.test/')
 page.get_by_role('button',name='조종·착륙',exact=True).click()
 assert page.locator('#wheelPower button').count()==2
 left=page.locator('#wheelPower button').nth(0).bounding_box();right=page.locator('#wheelPower button').nth(1).bounding_box()
 assert left['x']<right['x'] and abs(left['y']-right['y'])<2
 assert page.locator('.wb').count()==7
 assert page.locator('.wb.seq').count()==2
 assert page.locator('#grid [data-id="gear"]').count()==0
 page.locator('#wheelPanel').screenshot(path=str(O/'tablet-idle.png'))
 page.locator('[data-bid="launch-seq"]').dispatch_event('pointerdown')
 page.wait_for_timeout(500);assert page.locator('#seqBar').is_visible();assert len(req)==1
 page.locator('[data-bid="landing-seq"]').dispatch_event('pointerdown');assert len(req)==1
 page.screenshot(path=str(O/'tablet-running.png'))
 page.evaluate('window.scrollTo(0,document.body.scrollHeight)')
 assert page.locator('#seqCancel').bounding_box()['y']<1100
 page.locator('#seqCancel').click();page.wait_for_timeout(500)
 assert not page.locator('#seqBar').is_visible()
 page.set_viewport_size({'width':390,'height':844});page.evaluate('window.scrollTo(0,200)')
 assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
 page.locator('#wheelPanel').screenshot(path=str(O/'phone-wheel.png'))
 page.locator('#settingsOpen').click();page.locator('#keySection').select_option('3')
 page.locator('[data-setting-id="launch-seq"]').click()
 assert page.locator('#seqSteps input[data-step]').count()==3
 assert not page.locator('#keyMain').is_visible()
 page.screenshot(path=str(O/'phone-delays.png'))
 assert not errors,errors
 browser.close()
print('radial browser checks: 6 wedges + center, no duplicate grid controls, single sequence, fixed cancel, phone width, delay editor, 0 JS errors')
