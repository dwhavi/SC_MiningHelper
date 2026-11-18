"""Retire only the requested ship controls, including existing custom v4 layouts."""
import sys,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sc_pad_control as m
class ShipFitTests(unittest.TestCase):
 def test_bundled_ship_has_nine_wheel_controls(self):
  layout=m._resolved_deck_layout({},overrides={})
  ship=next(p for p in layout['pages'] if p['id']=='ship')
  self.assertEqual({b['id'] for b in ship['buttons']},set([ship['wheel']['center']]+ship['wheel']['segments']+ship['wheel']['footer']))
  self.assertEqual(len(ship['buttons']),9)
  self.assertEqual(next(b for b in ship['buttons'] if b['id']=='landing-seq')['sequence'][0]['action'],'spaceship_docking.v_toggle_docking_request')
 def test_old_custom_layout_filtered_without_rewriting_files(self):
  layout=json.loads(Path(m._data_path('deck_layout.json')).read_text());ship=next(p for p in layout['pages'] if p['id']=='ship')
  removed=['scm','nav','dock','dock-camera','thirdperson','freelook']
  ship['buttons'] += [{'id':i,'action':'test.'+i,'keys':'','label':i} for i in removed]
  with tempfile.TemporaryDirectory() as tmp:
   file=Path(tmp)/'deck_layout.json';file.write_text(json.dumps(layout));before=file.read_bytes()
   with patch.object(m,'EXE_DIR',tmp):actual=m._resolved_deck_layout({},overrides={})
   self.assertEqual(file.read_bytes(),before)
  self.assertEqual(len(next(p for p in actual['pages'] if p['id']=='ship')['buttons']),9)
  self.assertEqual(next(p for p in actual['pages'] if p['id']=='mining'),next(p for p in m._resolved_deck_layout({},overrides={})['pages'] if p['id']=='mining'))
if __name__=='__main__':unittest.main()
