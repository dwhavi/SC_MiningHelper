import sys,time,unittest,threading
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_deck
import sc_pad_control as m
class MiningTests(unittest.TestCase):
 setUp=test_deck.DeckTests.setUp
 xml=test_deck.DeckTests.xml
 def tearDown(self):
  m._MINING_ABORT.clear();m._mining_update(running=False);test_deck.DeckTests.tearDown(self)
 def ping(self):
  self.xml('<ActionMaps><actionmap name="spaceship_radar"><action name="v_invoke_ping"><rebind input="kb1_tab"/></action></actionmap></ActionMaps>')
 def test_mining_auth_interval_missing(self):
  self.assertEqual(self.client.get('/api/mining-scan').status_code,401)
  self.assertEqual(self.client.post('/api/mining-scan',json={'enabled':False}).status_code,401)
  self.assertEqual(self.client.post('/api/mining-scan',headers=self.headers,json={'enabled':True,'interval_ms':200}).status_code,400)
  with patch.object(m,'_game_focused',return_value=True):
   self.assertEqual(self.client.post('/api/mining-scan',headers=self.headers,json={'enabled':True}).status_code,400)
 def test_start_single_worker_stop_and_serialized_manual_input(self):
  self.ping()
  with patch.object(m,'_game_focused',return_value=True),patch.object(m,'_start_mining_thread'):
   r=self.client.post('/api/mining-scan',headers=self.headers,json={'enabled':True});self.assertEqual(r.status_code,200,r.text)
   self.assertEqual(self.client.get('/api/mining-scan',headers=self.headers).json()['interval_ms'],5000)
   self.assertEqual(self.client.post('/api/mining-scan',headers=self.headers,json={'enabled':True}).status_code,409)
   with patch.object(m,'_send_input',return_value=(True,'ok')):
    self.assertEqual(self.client.post('/api/input',headers=self.headers,json={'type':'press','key':'esc'}).status_code,200)
   self.assertEqual(self.client.post('/api/mining-scan',headers=self.headers,json={'enabled':False}).status_code,200)
   self.assertTrue(m._MINING_ABORT.is_set())
 def test_runner_release_cancel(self):
  self.ping();m._mining_update(running=True,heartbeat=time.monotonic())
  with patch.object(m,'_game_focused',return_value=True),patch.object(m,'_combo_down') as down,patch.object(m,'_combo_up') as up,patch.object(m._MINING_ABORT,'wait',return_value=True):
   m._mining_runner(self.cfg)
  down.assert_called_once_with('tab');up.assert_called_once_with('tab');self.assertFalse(m._mining_status()['running'])
 def test_runner_focus_and_heartbeat(self):
  self.ping()
  for heartbeat,focus in [(0,True),(time.monotonic(),False)]:
   m._mining_update(running=True,heartbeat=heartbeat)
   with patch.object(m,'_game_focused',return_value=focus),patch.object(m,'_combo_down') as down:
    self.cfg['scan_enabled']=True;m._mining_runner(self.cfg);down.assert_not_called()
 def test_tracker_vertical_reacquire_ambiguity(self):
  tracker=m.SignatureTracker();table={'ship_ores':{'Test':2000}}
  region=m.mining_search_region({'width':1920,'height':1080})
  def token(n,y):return dict(text=str(n),confidence=.95,left=region['width']*.45,top=y,width=60,height=20)
  self.assertIsNone(tracker.detect([token(4000,10)],region,table,1))
  self.assertEqual(tracker.detect([token(4000,600)],region,table,2),4000)
  self.assertGreater(tracker.region['top'],500)
  self.assertIsNone(tracker.detect([token(4000,60),token(6000,180)],region,table,3))
  self.assertIsNone(tracker.region)
  self.assertIsNone(tracker.detect([token(320,50),token(1850,100)],region,table,4))
 def test_auto_fixed_interval_and_stop(self):
  self.ping();m._mining_update(running=True,interval_ms=1000,heartbeat=time.monotonic());times=[]
  with patch.object(m,'_game_focused',return_value=True),patch.object(m,'_combo_down',side_effect=lambda k:times.append(time.monotonic())),patch.object(m,'_combo_up') as up:
   t=threading.Thread(target=m._mining_runner,args=(self.cfg,));t.start();time.sleep(1.4);m._MINING_ABORT.set();t.join(1)
   self.assertEqual(len(times),2);self.assertGreaterEqual(times[1]-times[0],.95);self.assertLess(times[1]-times[0],1.2);self.assertEqual(up.call_count,2)
 def test_sequence_conflict_and_focus_start(self):
  self.ping()
  with patch.object(m,'_game_focused',return_value=False):
   self.assertEqual(self.client.post('/api/mining-scan',headers=self.headers,json={'enabled':True}).status_code,409)
  with patch.object(m,'_seq_running',return_value=True):
   self.assertEqual(self.client.post('/api/mining-scan',headers=self.headers,json={'enabled':True}).status_code,409)
  m._mining_update(running=True)
  self.assertEqual(self.client.post('/api/sequence',headers=self.headers,json={'button_id':'launch-seq'}).status_code,409)
 def test_bound_key_revocation_stops(self):
  self.ping();self.cfg['actionmaps_path']='';m._mining_update(running=True,heartbeat=time.monotonic())
  with patch.object(m,'_game_focused',return_value=True),patch.object(m,'_combo_down') as down:
   m._mining_runner(self.cfg);down.assert_not_called()
  self.assertFalse(m._mining_status()['running'])
 def test_low_confidence_and_ocr_coordinates(self):
  t=m.SignatureTracker();s={'left':100,'top':30,'width':500,'height':700};table={'ship_ores':{'T':2000}}
  tok=dict(text='4000',confidence=.2,left=250,top=60,width=60,height=20)
  self.assertIsNone(t.detect([tok],s,table,1));self.assertIsNone(t.region)
  token=m._ocr_tokens([([[20,40],[100,40],[100,80],[20,80]],'4000',.95)])[0]
  self.assertEqual((token['left'],token['top'],token['width'],token['height']),(10,20,40,20))
 def test_ocr_failure_retries_fresh_frame_without_input(self):
  from unittest.mock import Mock
  engine=Mock();cap=Mock();tracker=Mock();tracker.detect.side_effect=[None,12000]
  with patch.object(m,'_combo_down') as down:
   self.assertEqual(m.read_mining_signature(engine,cap,{},tracker,{},lambda:True),12000)
   self.assertEqual(cap.grab.call_count,2);self.assertEqual(engine.read_tokens.call_count,2);down.assert_not_called()
 def test_ocr_exception_retry_and_stop_guard(self):
  from unittest.mock import Mock
  engine=Mock();cap=Mock();tracker=Mock();tracker.detect.return_value=12000
  engine.read_tokens.side_effect=[RuntimeError('temporary'),[]]
  self.assertEqual(m.read_mining_signature(engine,cap,{},tracker,{},lambda:True),12000)
  self.assertEqual(cap.grab.call_count,2)
  cap.reset_mock();self.assertIsNone(m.read_mining_signature(engine,cap,{},tracker,{},lambda:False));cap.grab.assert_not_called()
  engine.read_tokens.side_effect=RuntimeError('persistent')
  with self.assertRaises(RuntimeError):m.read_mining_signature(engine,cap,{},tracker,{},lambda:True)
 def test_off_interrupts_five_second_wait(self):
  self.ping();m._mining_update(running=True,interval_ms=5000,heartbeat=time.monotonic())
  sent=threading.Event()
  with patch.object(m,'_game_focused',return_value=True),patch.object(m,'_combo_down',side_effect=lambda k:sent.set()),patch.object(m,'_combo_up') as up:
   t=threading.Thread(target=m._mining_runner,args=(self.cfg,));t.start();self.assertTrue(sent.wait(1))
   start=time.monotonic();self.client.post('/api/mining-scan',headers=self.headers,json={'enabled':False});t.join(.5)
   self.assertFalse(t.is_alive());self.assertLess(time.monotonic()-start,.5);up.assert_called_once_with('tab')
if __name__=='__main__':unittest.main()
