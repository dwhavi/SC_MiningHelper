import sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_deck
import sc_pad_control as m
class LiftTests(unittest.TestCase):
 setUp=test_deck.DeckTests.setUp
 tearDown=test_deck.DeckTests.tearDown
 def btn(self):return next(b for b in m._deck_buttons(self.client.get('/api/layout').json()) if b['id']=='launch-seq')
 def save(self,**data):return self.client.post('/api/key-settings',headers=self.headers,json={'button_id':'launch-seq',**data})
 def test_default_save_preserve_restart_reset(self):
  self.assertEqual(self.btn()['steps'][1]['hold_ms'],1500)
  self.assertEqual(self.save(lift_hold_ms=250).status_code,200)
  self.assertTrue(self.btn()['custom_hold']);self.assertEqual(self.btn()['steps'][1]['hold_ms'],250)
  self.save(delays=[300,400,500]);self.save(step_keys=['f5','space','f6'])
  self.assertEqual(self.btn()['steps'][1]['hold_ms'],250)
  self.assertEqual(m._read_key_overrides()['launch-seq']['lift_hold_ms'],250)
  self.assertEqual(self.btn()['steps'][1]['delay_ms'],400)
  self.save();self.assertEqual(self.btn()['steps'][1]['hold_ms'],1500);self.assertFalse(self.btn()['custom_hold'])
 def test_bounds_type_and_auth(self):
  for value in [0,49,5001,-1]:self.assertEqual(self.save(lift_hold_ms=value).status_code,400)
  for value in [True,200.5,'300']:self.assertEqual(self.save(lift_hold_ms=value).status_code,422)
  for value in [50,5000]:self.assertEqual(self.save(lift_hold_ms=value).status_code,200)
  self.assertEqual(self.client.post('/api/key-settings',json={'button_id':'launch-seq','lift_hold_ms':250}).status_code,401)
  self.assertEqual(self.client.post('/api/key-settings',headers=self.headers,json={'button_id':'landing-seq','lift_hold_ms':250}).status_code,400)
 def test_bad_saved_hold_falls_back(self):
  for value in [True,10,9000,'500']:
   d=m._resolved_deck_layout(self.cfg,{'launch-seq':{'action':'sequence','lift_hold_ms':value}})
   b=next(b for b in m._deck_buttons(d) if b['id']=='launch-seq');self.assertEqual(b['steps'][1]['hold_ms'],1500)
 def test_runner_uses_custom_duration_and_releases(self):
  self.save(lift_hold_ms=250,step_keys=['f5','space','f6'],delays=[200,200,200])
  waits=[]
  def wait(dt):waits.append(dt);return False
  with patch.object(m,'_game_focused',return_value=True),patch.object(m,'_combo_press'),patch.object(m,'_combo_down') as down,patch.object(m,'_combo_up') as up,patch.object(m._SEQ_ABORT,'wait',side_effect=wait):
   m._run_sequence(self.cfg,self.btn())
  down.assert_called_once_with('space');up.assert_called_once_with('space');self.assertAlmostEqual(sum(waits),.85)
if __name__=='__main__':unittest.main()
