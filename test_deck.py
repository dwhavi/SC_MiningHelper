"""Regression tests for the input-only deck. No game or live key events required."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import sc_pad_control as m

class DeckTests(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(m.DEFAULTS, pairing_code='123456')
        self.state = m.ScanState({}, {}, {}, {})
        self.client = TestClient(m.create_app(self.state, self.cfg))
        self.headers = {'X-Pairing-Code': '123456'}
        self.tmp = tempfile.TemporaryDirectory()
    def tearDown(self):
        self.tmp.cleanup()
        m._ACTIVE_HOLDS.clear()
    def xml(self, text):
        path=Path(self.tmp.name)/'actionmaps.xml';path.write_text(text)
        self.cfg['actionmaps_path']=str(path)
        m._AM_CACHE['path']=None
    def test_pairing(self):
        self.assertEqual(self.client.get('/api/auth').status_code,401)
        self.assertEqual(self.client.get('/api/auth',headers=self.headers).status_code,200)
    def test_no_empty_code_bypass(self):
        self.cfg['pairing_code']=''
        self.assertEqual(self.client.get('/api/auth').status_code,401)
    def test_rate_limit(self):
        for _ in range(4):self.assertEqual(self.client.get('/api/auth').status_code,401)
        self.assertEqual(self.client.get('/api/auth').status_code,429)
        self.assertEqual(self.client.get('/api/auth',headers=self.headers).status_code,429)
    def test_focus_guard(self):
        with patch.object(m,'_game_focused',return_value=False),patch.object(m,'_send_input',return_value=(True,'ok')) as send:
            self.assertEqual(self.client.post('/api/input',headers=self.headers,json={'type':'press','key':'u'}).status_code,409)
            send.assert_not_called()
            self.assertEqual(self.client.post('/api/input',headers=self.headers,json={'type':'up','key':'u'}).status_code,200)
    def test_layout_defaults_no_paths(self):
        d=self.client.get('/api/layout').json()
        self.assertEqual(len(d['pages']),3)
        self.assertNotIn('actionmaps_path',d)
        self.assertFalse(d['actionmaps_active'])
    def test_rebind(self):
        self.xml('<ActionMaps><actionmap name="seat_general"><action name="v_toggle_mining_mode"><rebind input="kb1_lalt+m"/></action></actionmap></ActionMaps>')
        d=self.client.get('/api/layout').json()
        self.assertEqual(d['pages'][0]['buttons'][0]['keys'],'lalt+m')
        self.assertNotIn('actionmaps_path',d)
    def test_joystick_partial_keeps_default(self):
        self.xml('<ActionMaps><actionmap name="seat_general"><action name="v_toggle_mining_mode"><rebind input="js1_button7"/></action></actionmap></ActionMaps>')
        b=self.client.get('/api/layout').json()['pages'][0]['buttons'][0]
        self.assertEqual(b['keys'],'m');self.assertFalse(b.get('unbound',False))
    def test_explicit_unbind(self):
        self.xml('<ActionMaps><actionmap name="seat_general"><action name="v_toggle_mining_mode"><rebind input="kb1_ "/></action></actionmap></ActionMaps>')
        b=self.client.get('/api/layout').json()['pages'][0]['buttons'][0]
        self.assertTrue(b['unbound']);self.assertEqual(b['keys'],'')
    def test_invalid_xml(self):
        self.xml('<broken')
        self.assertFalse(self.client.get('/api/layout').json()['actionmaps_active'])
    def test_unsupported_rebind_does_not_override(self):
        self.xml('<ActionMaps><actionmap name="seat_general"><action name="v_toggle_mining_mode"><rebind input="kb1_HMD_Yaw"/></action></actionmap></ActionMaps>')
        self.assertEqual(self.client.get('/api/layout').json()['pages'][0]['buttons'][0]['keys'],'m')
    def test_no_arbitrary_auto_profile(self):
        self.assertIsNone(m._resolve_actionmaps_path({'actionmaps_path':''}))
    def test_default_keys_parse(self):
        d=json.loads(Path('deck_layout.json').read_text())
        for page in d['pages']:
            for btn in page['buttons']:
                m._parse_combo(btn['keys'])
                self.assertNotIn(btn['keys'].lower(),['w','a','s','d'])
    def test_invalid_combo(self):
        for key in ['bad','mouse9','w+s','']:
            with self.assertRaises(ValueError):m._parse_combo(key)
    def test_tap_one_down_up(self):
        with patch.object(m.sys,'platform','win32'),patch.object(m,'_emit_vk') as emit,patch.object(m.time,'sleep'):
            self.assertTrue(m._send_input('press','lalt+mouse1')[0])
            self.assertEqual([c.args for c in emit.call_args_list],[(164,0),(4097,0),(4097,2),(164,2)])
    def test_hold_is_not_auto_repeat(self):
        with patch.object(m.sys,'platform','win32'),patch.object(m,'_emit_vk') as emit:
            m._send_input('down','u');m._send_input('down','u');m._send_input('refresh','u');m._send_input('up','u')
            self.assertEqual([c.args for c in emit.call_args_list],[(85,0),(85,2)])
    def test_wheel_rejects_hold(self):
        with patch.object(m.sys,'platform','win32'),patch.object(m,'_emit_vk') as emit:
            self.assertFalse(m._send_input('down','mwheel_up')[0]);emit.assert_not_called()
    def test_failed_main_key_releases_modifier(self):
        calls=[]
        def emit(vk, flags=0):
            calls.append((vk,flags))
            if vk == 4097 and flags == 0:raise OSError("rejected")
        with patch.object(m.sys,'platform','win32'),patch.object(m,'_emit_vk',side_effect=emit):
            self.assertFalse(m._send_input('press','lalt+mouse1')[0])
            self.assertEqual(calls[-1],(164,2))
    def test_release_attempts_all_keys(self):
        calls=[]
        def emit(vk,flags=0):
            calls.append((vk,flags))
            if vk==4097:raise OSError("rejected")
        with patch.object(m,'_emit_vk',side_effect=emit):
            with self.assertRaises(OSError):m._combo_up('lalt+mouse1')
            self.assertEqual(calls[-1],(164,2))
    def test_pairing_does_not_clear_results(self):
        self.state.update_scan(rs=6340,phase='test')
        old=self.state.get()
        self.client.get('/api/auth',headers=self.headers)
        self.assertEqual(self.state.get(),old)

if __name__=='__main__':unittest.main()
