"""Regression tests for the input-only deck. No game or live key events required."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import sc_pad_control as m

_SEQ_XML = ('<ActionMaps>'
            '<actionmap name="spaceship_movement">'
            '<action name="v_atc_request"><rebind input="kb1_lalt+n"/></action>'
            '<action name="v_strafe_up"><rebind input="kb1_space"/></action>'
            '<action name="v_toggle_landing_system"><rebind input="kb1_k"/></action>'
            '</actionmap>'
            '<actionmap name="spaceship_docking">'
            '<action name="v_toggle_docking_request"><rebind input="kb1_lalt+d"/></action>'
            '</actionmap></ActionMaps>')

class DeckTests(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(m.DEFAULTS, pairing_code='123456')
        self.state = m.ScanState({}, {}, {}, {})
        self.client = TestClient(m.create_app(self.state, self.cfg))
        self.headers = {'X-Pairing-Code': '123456'}
        self.tmp = tempfile.TemporaryDirectory()
        self.override_patch = patch.object(m, '_override_path', return_value=str(Path(self.tmp.name)/'deck_key_overrides.json'))
        self.override_patch.start()
    def tearDown(self):
        self.override_patch.stop()
        self.tmp.cleanup()
        m._ACTIVE_HOLDS.clear()
        m._SEQ_ABORT.clear()
        m._seq_set(running=False)
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
        self.assertEqual(len(d['pages']),4)
        self.assertNotIn('actionmaps_path',d)
        self.assertFalse(d['actionmaps_active'])
    def test_rebind(self):
        self.xml('<ActionMaps><actionmap name="seat_general"><action name="v_set_mining_mode"><rebind input="kb1_lalt+m"/></action></actionmap></ActionMaps>')
        d=self.client.get('/api/layout').json()
        self.assertEqual(d['pages'][0]['buttons'][0]['keys'],'lalt+m')
        self.assertNotIn('actionmaps_path',d)
    def test_joystick_partial_stays_disabled(self):
        self.xml('<ActionMaps><actionmap name="seat_general"><action name="v_set_mining_mode"><rebind input="js1_button7"/></action></actionmap></ActionMaps>')
        b=self.client.get('/api/layout').json()['pages'][0]['buttons'][0]
        self.assertEqual(b['keys'],'');self.assertTrue(b['unbound'])
    def test_explicit_unbind(self):
        self.xml('<ActionMaps><actionmap name="seat_general"><action name="v_set_mining_mode"><rebind input="kb1_ "/></action></actionmap></ActionMaps>')
        b=self.client.get('/api/layout').json()['pages'][0]['buttons'][0]
        self.assertTrue(b['unbound']);self.assertEqual(b['keys'],'')
    def test_invalid_xml(self):
        self.xml('<broken')
        self.assertFalse(self.client.get('/api/layout').json()['actionmaps_active'])
    def test_unsupported_rebind_does_not_override(self):
        self.xml('<ActionMaps><actionmap name="seat_general"><action name="v_set_mining_mode"><rebind input="kb1_HMD_Yaw"/></action></actionmap></ActionMaps>')
        self.assertEqual(self.client.get('/api/layout').json()['pages'][0]['buttons'][0]['keys'],'')
    def test_no_arbitrary_auto_profile(self):
        self.assertIsNone(m._resolve_actionmaps_path({'actionmaps_path':''}))
    def test_default_has_no_guessed_keys(self):
        d=self.client.get('/api/layout').json()
        self.assertEqual(len(d['common_buttons']),4)
        for btn in m._deck_buttons(d):
            if btn['id']=='escape':
                self.assertEqual(btn['keys'],'esc');self.assertFalse(btn['unbound'])
            else:
                self.assertEqual(btn['keys'],'');self.assertTrue(btn['unbound'])
    def test_server_blocks_missing_key_and_arbitrary_timed(self):
        with patch.object(m,'_game_focused',return_value=True),patch.object(m,'_send_input',return_value=(True,'ok')) as send:
            for body in [{'type':'press','key':'u'}, {'type':'timed','key':'esc','ms':5000}, {'type':'down','key':'esc'}]:
                self.assertEqual(self.client.post('/api/input',headers=self.headers,json=body).status_code,400)
            send.assert_not_called()
            self.assertEqual(self.client.post('/api/input',headers=self.headers,json={'type':'press','key':'esc'}).status_code,200)
    def test_server_accepts_confirmed_mode_only(self):
        self.xml('<ActionMaps><actionmap name="seat_general"><action name="v_set_mining_mode"><rebind input="kb1_lalt+m"/></action></actionmap></ActionMaps>')
        with patch.object(m,'_game_focused',return_value=True),patch.object(m,'_send_input',return_value=(True,'ok')) as send:
            self.assertEqual(self.client.post('/api/input',headers=self.headers,json={'type':'press','key':'lalt+m'}).status_code,200)
            self.assertEqual(self.client.post('/api/input',headers=self.headers,json={'type':'down','key':'lalt+m'}).status_code,400)
            self.assertEqual(send.call_count,1)
    def test_xml_duplicate_keys_disabled(self):
        self.xml('<ActionMaps><actionmap name="seat_general"><action name="v_set_mining_mode"><rebind input="kb1_lalt+m"/></action><action name="v_set_scan_mode"><rebind input="kb1_lalt+m"/></action></actionmap></ActionMaps>')
        d=self.client.get('/api/layout').json()
        for b in d['pages'][0]['buttons'][:2]:
            self.assertTrue(b['unbound']);self.assertIn('중복',b['disabled_reason'])
    def test_secondary_xml_binding_collision_also_blocks(self):
        self.xml('<ActionMaps><actionmap name="seat_general"><action name="v_set_mining_mode"><rebind input="kb1_lalt+m"/></action><action name="v_set_scan_mode"><rebind input="kb1_lalt+v"/><rebind input="kb1_lalt+m"/></action></actionmap></ActionMaps>')
        b=self.client.get('/api/layout').json()['pages'][0]['buttons'][0]
        self.assertTrue(b['unbound']);self.assertIn('중복',b['disabled_reason'])
    def test_bare_movement_key_disabled(self):
        self.xml('<ActionMaps><actionmap name="seat_general"><action name="v_set_mining_mode"><rebind input="kb1_w"/></action></actionmap></ActionMaps>')
        b=self.client.get('/api/layout').json()['pages'][0]['buttons'][0]
        self.assertTrue(b['unbound']);self.assertIn('이동',b['disabled_reason'])
    def test_no_quantum_or_contextual_inventory_guess(self):
        d=self.client.get('/api/layout').json()
        all_buttons=list(m._deck_buttons(d))
        self.assertFalse(any('quantum' in b.get('action','') for b in all_buttons))
        for id in ['inventory','exit-seat']:
            b=next(b for b in all_buttons if b['id']==id)
            self.assertTrue(b['unbound']);self.assertEqual(b['action'],'')
    def test_stale_layout_uses_bundled_without_overwriting(self):
        path=Path(self.tmp.name)/'deck_layout.json'
        old='{"version":3,"pages":[{"name":"old","buttons":[{"keys":"w"}]}]}'
        path.write_text(old)
        with patch.object(m,'EXE_DIR',self.tmp.name):
            d=self.client.get('/api/layout').json()
        self.assertEqual(d['version'],4);self.assertIn('migration_notice',d)
        self.assertEqual(path.read_text(),old)
    def test_xml_removed_revokes_input(self):
        self.xml('<ActionMaps><actionmap name="seat_general"><action name="v_set_mining_mode"><rebind input="kb1_lalt+m"/></action></actionmap></ActionMaps>')
        self.assertFalse(self.client.get('/api/layout').json()['pages'][0]['buttons'][0]['unbound'])
        self.cfg['actionmaps_path']=''
        with patch.object(m,'_game_focused',return_value=True),patch.object(m,'_send_input') as send:
            self.assertEqual(self.client.post('/api/input',headers=self.headers,json={'type':'press','key':'lalt+m'}).status_code,400)
            send.assert_not_called()
    def test_power_top_is_xml_only_and_server_validated(self):
        self.xml('<ActionMaps><actionmap name="spaceship_power"><action name="v_engineering_assignment_weapons_increase"><rebind input="kb1_f5"/></action></actionmap></ActionMaps>')
        d=self.client.get('/api/layout').json()
        self.assertEqual(len(d['power_buttons']),7)
        self.assertEqual(len(next(p for p in d['pages'] if p['id']=='power')['buttons']),5)
        b=next(b for b in d['power_buttons'] if b['id']=='weapons-increase')
        self.assertFalse(b['unbound']);self.assertEqual(b['keys'],'f5')
        self.assertTrue(next(b for b in d['power_buttons'] if b['id']=='engine-increase')['unbound'])
        with patch.object(m,'_game_focused',return_value=True),patch.object(m,'_send_input',return_value=(True,'ok')) as send:
            self.assertEqual(self.client.post('/api/input',headers=self.headers,json={'type':'press','key':'f5'}).status_code,200)
            self.assertEqual(self.client.post('/api/input',headers=self.headers,json={'type':'press','key':'f6'}).status_code,400)
            self.assertEqual(send.call_count,1)
    def test_existing_v4_distribution_is_lifted_without_write(self):
        d=json.loads(Path('deck_layout.json').read_text())
        buttons=d.pop('power_buttons')
        next(p for p in d['pages'] if p['id']=='power')['buttons'][:0]=buttons
        path=Path(self.tmp.name)/'deck_layout.json';text=json.dumps(d);path.write_text(text)
        with patch.object(m,'EXE_DIR',self.tmp.name):
            resolved=self.client.get('/api/layout').json()
        self.assertEqual(len(resolved['power_buttons']),7)
        self.assertFalse(any(b.get('group')=='출력 배분' for p in resolved['pages'] for b in p['buttons']))
        self.assertEqual(path.read_text(),text)
    def set_key(self, id='weapons-increase', key='f5', auth=True):
        return self.client.post('/api/key-settings',headers=self.headers if auth else {},json={'button_id':id,'keys':key})
    def test_settings_require_authentication(self):
        self.assertEqual(self.client.get('/api/key-settings').status_code,401)
        self.assertEqual(self.set_key(auth=False).status_code,401)
        self.assertFalse(Path(m._override_path()).exists())
    def test_override_persistence_reset_and_no_layout_write(self):
        old=Path('deck_layout.json').read_text()
        r=self.set_key();self.assertEqual(r.status_code,200)
        btn=next(b for b in m._deck_buttons(r.json()['layout']) if b['id']=='weapons-increase')
        self.assertEqual(btn['keys'],'f5');self.assertEqual(btn['keys_source'],'override')
        self.assertEqual(m._read_key_overrides()['weapons-increase']['keys'],'f5')
        self.assertEqual(Path('deck_layout.json').read_text(),old)
        with patch.object(m,'_game_focused',return_value=True),patch.object(m,'_send_input',return_value=(True,'ok')) as send:
            self.assertEqual(self.client.post('/api/input',headers=self.headers,json={'type':'press','key':'f5'}).status_code,200)
            send.assert_called_once()
        self.assertEqual(self.set_key(key='').status_code,200)
        self.assertTrue(next(b for b in m._deck_buttons(self.client.get('/api/layout').json()) if b['id']=='weapons-increase')['unbound'])
    def test_override_reset_restores_xml(self):
        self.xml('<ActionMaps><actionmap name="spaceship_power"><action name="v_engineering_assignment_weapons_increase"><rebind input="kb1_f5"/></action></actionmap></ActionMaps>')
        self.assertEqual(self.set_key(key='lalt+f9').status_code,200)
        r=self.set_key(key='');b=next(b for b in m._deck_buttons(r.json()['layout']) if b['id']=='weapons-increase')
        self.assertEqual(b['keys'],'f5');self.assertEqual(b['keys_source'],'actionmaps')
    def test_override_rejects_movement_macros_modifiers_and_paths(self):
        for key in ['w','lalt+w','space','lctrl+up','f5,f6','f5;f6','lalt','lmeta+f5','f5+f6','f5++','lalt+ lctrl+f5','lalt+tab','lalt+f4','lctrl+esc','lctrl+lalt+delete','lctrl+lalt+f8','x'*65]:
            self.assertEqual(self.set_key(key=key).status_code,400,key)
        for id in ['../file','unknown','escape','exit-seat','inventory']:
            self.assertEqual(self.set_key(id=id).status_code,400,id)
        self.assertFalse(Path(m._override_path()).exists())
    def test_override_rejects_effective_and_xml_duplicates(self):
        self.assertEqual(self.set_key().status_code,200)
        self.assertEqual(self.set_key(id='engine-increase').status_code,400)
        self.assertEqual(self.set_key(id='engine-increase',key='esc').status_code,400)
        self.xml('<ActionMaps><actionmap name="other"><action name="unsafe"><rebind input="kb1_f10"/></action></actionmap></ActionMaps>')
        self.assertEqual(self.set_key(key='f10').status_code,400)
    def test_override_hold_wheel_rejected(self):
        self.assertEqual(self.set_key(id='ping',key='mwheel_up').status_code,400)
        self.assertEqual(self.set_key(id='mining-up',key='mwheel_up').status_code,200)
    def test_override_write_failure_does_not_report_success(self):
        with patch.object(m.os,'replace',side_effect=OSError('readonly')):
            self.assertEqual(self.set_key().status_code,500)
        self.assertFalse(Path(m._override_path()).exists())
    def test_override_action_change_does_not_reuse_key(self):
        Path(m._override_path()).write_text('{"weapons-increase":{"keys":"f5","action":"wrong.action"}}')
        b=next(b for b in m._deck_buttons(self.client.get('/api/layout').json()) if b['id']=='weapons-increase')
        self.assertTrue(b['unbound'])
    def test_invalid_combo(self):
        for key in ['bad','mouse9','w+s','']:
            with self.assertRaises(ValueError):m._parse_combo(key)
    def test_tap_one_down_up(self):
        with patch.object(m.sys,'platform','win32'),patch.object(m,'_emit_vk') as emit,patch.object(m.time,'sleep'):
            self.assertTrue(m._send_input('press','lalt+mouse1')[0])
            self.assertEqual([c.args for c in emit.call_args_list],[(164,0),(4097,0),(4097,2),(164,2)])
    def test_tap_uses_human_like_timing(self):
        events=[]
        def emit(vk,flags=0): events.append(('key',vk,flags))
        def sleep(s): events.append(('sleep',round(s,3)))
        with patch.object(m.sys,'platform','win32'),patch.object(m,'_emit_vk',side_effect=emit),patch.object(m.time,'sleep',side_effect=sleep):
            self.assertTrue(m._send_input('press','ralt+r')[0])
        keys=[(e[1],e[2]) for e in events if e[0]=='key']
        self.assertEqual(keys,[(165,0),(82,0),(82,2),(165,2)])
        main_down=events.index(('key',82,0))
        self.assertTrue(any(e[0]=='sleep' and e[1]>=m._TAP_SETTLE for e in events[:main_down]))
        sleeps=[e[1] for e in events if e[0]=='sleep']
        self.assertGreaterEqual(sum(sleeps),m._TAP_HOLD+m._TAP_SETTLE)
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

    def _seq_button(self, bid='launch-seq'):
        d = self.client.get('/api/layout').json()
        ship = [p for p in d['pages'] if p['id'] == 'ship'][0]
        return [b for b in ship['buttons'] if b['id'] == bid][0]
    def _wait_seq_done(self, timeout=5.0):
        import time as _t
        end = _t.time() + timeout
        while _t.time() < end:
            d = self.client.get('/api/sequence', headers=self.headers).json()
            if not d.get('running'):
                return d
            _t.sleep(0.02)
        self.fail('sequence did not finish')

    def test_sequence_locked_without_xml(self):
        b = self._seq_button()
        self.assertTrue(b['unbound'])
        self.assertIn('시퀀스 키 지정 필요', b['disabled_reason'])
    def test_sequence_resolves_from_xml(self):
        self.xml(_SEQ_XML)
        b = self._seq_button()
        self.assertFalse(b['unbound'])
        self.assertEqual(b['keys'], 'SEQ')
        self.assertEqual([s['key'] for s in b['steps']], ['lalt+n', 'space', 'k'])
        self.assertEqual(b['steps'][1]['hold_ms'], 1500)
        landing = self._seq_button('landing-seq')
        self.assertEqual([s['key'] for s in landing['steps']], ['lalt+d', 'k'])
    def test_sequence_run_order_and_completion(self):
        self.xml(_SEQ_XML)
        calls = []
        with patch.object(m, '_game_focused', return_value=True), \
             patch.object(m, '_combo_press', side_effect=lambda k: calls.append(('press', k))), \
             patch.object(m, '_combo_down', side_effect=lambda k: calls.append(('down', k))), \
             patch.object(m, '_combo_up', side_effect=lambda k: calls.append(('up', k))), \
             patch.object(m.time, 'sleep'):
            with patch.object(m._SEQ_ABORT, 'wait', return_value=False):
                m._run_sequence(self.cfg, self._seq_button())
            d = dict(m._SEQ_STATE)
        self.assertEqual(calls, [('press', 'lalt+n'), ('down', 'space'), ('up', 'space'), ('press', 'k')])
        self.assertIn('완료', d['msg'])
    def test_sequence_requires_focus_at_start(self):
        self.xml(_SEQ_XML)
        with patch.object(m, '_game_focused', return_value=False):
            r = self.client.post('/api/sequence', headers=self.headers, json={'button_id': 'launch-seq'})
            self.assertEqual(r.status_code, 409)
    def test_sequence_rejects_unbound(self):
        r = self.client.post('/api/sequence', headers=self.headers, json={'button_id': 'launch-seq'})
        self.assertEqual(r.status_code, 400)
    def test_sequence_single_run_conflict(self):
        m._seq_set(running=True, button_id='x', step=1, total=2, label='x', msg='')
        try:
            self.xml(_SEQ_XML)
            with patch.object(m, '_game_focused', return_value=True):
                r = self.client.post('/api/sequence', headers=self.headers, json={'button_id': 'launch-seq'})
                self.assertEqual(r.status_code, 409)
                r = self.client.post('/api/input', headers=self.headers, json={'type': 'press', 'key': 'k'})
                self.assertEqual(r.status_code, 409)
                r = self.client.post('/api/input', headers=self.headers, json={'type': 'up', 'key': 'k'})
                self.assertNotEqual(r.status_code, 409)
        finally:
            m._seq_set(running=False, button_id='', step=0, total=0, label='', msg='')
    def test_sequence_cancel_always_allowed(self):
        self.xml(_SEQ_XML)
        with patch.object(m, '_game_focused', return_value=True), \
             patch.object(m, '_combo_press'), patch.object(m, '_combo_down'), patch.object(m, '_combo_up'), \
             patch.object(m.time, 'sleep'):
            self.assertEqual(self.client.post('/api/sequence', headers=self.headers,
                                              json={'button_id': 'launch-seq'}).status_code, 200)
            self.assertEqual(self.client.post('/api/sequence-cancel', headers=self.headers).status_code, 200)
            d = self._wait_seq_done()
        self.assertEqual(d['msg'], '취소했습니다')
    def test_sequence_focus_loss_stops_before_next_key(self):
        self.xml(_SEQ_XML)
        b = self._seq_button()
        with patch.object(m, '_game_focused', side_effect=[True, False]), \
             patch.object(m, '_combo_press') as press, \
             patch.object(m._SEQ_ABORT, 'wait', return_value=False):
            m._run_sequence(self.cfg, b)
        self.assertEqual(press.call_count, 1)
        self.assertIn('중단', m._SEQ_STATE['msg'])
    def test_sequence_cancel_during_lift_releases_space(self):
        self.xml(_SEQ_XML)
        b = self._seq_button()
        b['steps'] = b['steps'][1:]
        with patch.object(m, '_game_focused', return_value=True), \
             patch.object(m, '_combo_down') as down, patch.object(m, '_combo_up') as up, \
             patch.object(m, '_combo_press') as press, \
             patch.object(m._SEQ_ABORT, 'wait', return_value=True):
            m._run_sequence(self.cfg, b)
        down.assert_called_once_with('space')
        up.assert_called_once_with('space')
        press.assert_not_called()
        self.assertEqual(m._SEQ_STATE['msg'], '취소했습니다')
    def test_sequence_holds_prevent_start_and_auth_required(self):
        self.xml(_SEQ_XML)
        self.assertEqual(self.client.post('/api/sequence', json={'button_id':'launch-seq'}).status_code,401)
        self.assertEqual(self.client.get('/api/sequence').status_code,401)
        self.assertEqual(self.client.post('/api/sequence-cancel').status_code,401)
        with patch.object(m, '_game_focused', return_value=True):
            m._ACTIVE_HOLDS['x'] = 1
            self.assertEqual(self.client.post('/api/sequence', headers=self.headers,
                json={'button_id':'launch-seq'}).status_code,409)
    def test_sequence_conflicting_xml_and_custom_actions_locked(self):
        self.xml(_SEQ_XML.replace('</ActionMaps>', '<actionmap name="other"><action name="x"><rebind input="kb1_k"/></action></actionmap></ActionMaps>'))
        self.assertTrue(self._seq_button()['unbound'])
        btn = {'id':'arbitrary','sequence':[{'action':'other.x'}], 'unbound':True}
        m._resolve_sequence(btn, {'other.x':'k'}, {}, {})
        self.assertTrue(btn['unbound'])

    def test_sequence_delay_override_save_validate_reset(self):
        self.xml(_SEQ_XML)
        bad = self.client.post('/api/key-settings', headers=self.headers,
                               json={'button_id': 'launch-seq', 'delays': [100, 500, 500]})
        self.assertEqual(bad.status_code, 400)
        bad = self.client.post('/api/key-settings', headers=self.headers,
                               json={'button_id': 'launch-seq', 'delays': [500]})
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post('/api/key-settings', headers=self.headers,
                              json={'button_id': 'launch-seq', 'delays': [3000, 800, 400]})
        self.assertEqual(ok.status_code, 200)
        b = self._seq_button()
        self.assertTrue(b['custom_delays'])
        self.assertEqual([s['delay_ms'] for s in b['steps']], [3000, 800, 400])
        rst = self.client.post('/api/key-settings', headers=self.headers, json={'button_id': 'launch-seq'})
        self.assertEqual(rst.status_code, 200)
        b = self._seq_button()
        self.assertFalse(b['custom_delays'])
        self.assertEqual(b['steps'][0]['delay_ms'], 2000)

    def test_sequence_step_keys_save_without_xml_and_persist_reset(self):
        payload={'button_id':'launch-seq','step_keys':['f5','space','f6'], 'delays':[500,700,900]}
        r=self.client.post('/api/key-settings',headers=self.headers,json=payload)
        self.assertEqual(r.status_code,200,r.text)
        b=self._seq_button();self.assertFalse(b['unbound'])
        self.assertEqual([st['key'] for st in b['steps']],['f5','space','f6'])
        self.assertTrue(b['custom_step_keys'])
        self.assertEqual(m._read_key_overrides()['launch-seq']['step_keys']['spaceship_movement.v_atc_request'],'f5')
        self.xml(_SEQ_XML)
        payload['step_keys']=['','space','f6']
        r=self.client.post('/api/key-settings',headers=self.headers,json=payload)
        self.assertEqual(r.status_code,200,r.text)
        b=self._seq_button();self.assertEqual(b['steps'][0]['key'],'lalt+n')
        self.assertEqual(b['steps'][0]['keys_source'],'actionmaps')
        self.assertEqual(b['steps'][2]['keys_source'],'override')
        self.client.post('/api/key-settings',headers=self.headers,json={'button_id':'launch-seq'})
        self.assertFalse(self._seq_button()['custom_step_keys'])
        self.assertEqual(self._seq_button()['steps'][2]['key'],'k')
    def test_sequence_step_keys_reject_unsafe_and_conflicts(self):
        self.xml(_SEQ_XML)
        for keys in [['w','space','f6'], ['space','space','f6'], ['f5','lalt+space','f6'],
                     ['lalt+f4','space','f6'], ['f5','mwheel_up','f6'], ['f5','space','f5'],
                     ['k','space','f6'], ['f5'], ['f5','space','lalt+d']]:
            r=self.client.post('/api/key-settings',headers=self.headers,json={'button_id':'launch-seq','step_keys':keys})
            self.assertEqual(r.status_code,400,(keys,r.text))
        self.assertNotIn('launch-seq',m._read_key_overrides())
    def test_sequence_step_partial_edit_and_delays_preserve_keys(self):
        r=self.client.post('/api/key-settings',headers=self.headers,json={'button_id':'launch-seq','step_keys':['f5','','']})
        self.assertEqual(r.status_code,200,r.text)
        self.assertTrue(self._seq_button()['unbound'])
        r=self.client.post('/api/key-settings',headers=self.headers,json={'button_id':'launch-seq','delays':[400,600,800]})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self._seq_button()['steps'][0]['key'],'f5')
    def test_sequence_step_conflicts_with_manual_override(self):
        self.client.post('/api/key-settings',headers=self.headers,json={'button_id':'weapons-increase','keys':'f5'})
        r=self.client.post('/api/key-settings',headers=self.headers,json={'button_id':'launch-seq','step_keys':['f5','','']})
        self.assertEqual(r.status_code,400,r.text)
    def test_sequence_step_custom_same_action_is_allowed(self):
        self.xml(_SEQ_XML)
        self.client.post('/api/key-settings',headers=self.headers,json={'button_id':'gear','keys':'f6'})
        r=self.client.post('/api/key-settings',headers=self.headers,json={'button_id':'launch-seq','step_keys':['','space','f6']})
        self.assertEqual(r.status_code,200,r.text)

    def test_physical_modifiers_ignore_layout_mapping(self):
        for vk, expected in [(0xA5,(0x38,9)),(0xA3,(0x1d,9)),(0xA4,(0x38,8)),
                             (0xA2,(0x1d,8)),(0xA0,(0x2a,8)),(0xA1,(0x36,8))]:
            with patch.object(m,'_game_focused') as unused:
                mapper=__import__('unittest.mock',fromlist=['Mock']).Mock(return_value=0)
                self.assertEqual(m._keyboard_scan(vk,0,mapper),expected)
                self.assertEqual(m._keyboard_scan(vk,2,mapper),(expected[0],expected[1]|2))
                mapper.assert_not_called()
    def test_scan_zero_and_e1_are_rejected(self):
        for scan in (0,0xE100,0xE145):
            with self.assertRaises(ValueError):m._keyboard_scan(0x52,0,lambda *a:scan)
        self.assertEqual(m._keyboard_scan(0x52,0,lambda *a:0x13),(0x13,8))
        self.assertEqual(m._keyboard_scan(0x2e,2,lambda *a:0xE053),(0x53,11))
    def test_diagnostic_log_is_bounded_and_local(self):
        with patch.object(m,'EXE_DIR',self.tmp.name):
            m._trace_input(vk=165,scan=56,flags=9,result=1)
            p=Path(self.tmp.name)/'input_diagnostics.jsonl'
            self.assertEqual(json.loads(p.read_text())['scan'],56)
            p.write_text('x'*(256*1024+1))
            m._trace_input(vk=165,result=0)
            self.assertTrue(Path(str(p)+'.1').exists())
            self.assertLess(p.stat().st_size,1024)
    def test_wheel_power_xml_only_and_shared_actions(self):
        self.xml('<ActionMaps><actionmap name="spaceship_power"><action name="v_power_toggle"><rebind input="kb1_u"/></action><action name="v_power_toggle_thrusters"><rebind input="kb1_i"/></action></actionmap></ActionMaps>')
        d=self.client.get('/api/layout').json();ship=next(p for p in d['pages'] if p['id']=='ship')
        self.assertEqual(ship['wheel']['footer'],['wheel-power','wheel-engine'])
        for bid,key in [('wheel-power','u'),('wheel-engine','i')]:
            b=next(b for b in ship['buttons'] if b['id']==bid)
            self.assertFalse(b['unbound']);self.assertEqual(b['keys'],key)
            self.assertEqual(self.client.post('/api/key-settings',headers=self.headers,json={'button_id':bid,'keys':'f5'}).status_code,400)
        self.cfg['actionmaps_path']='';m._AM_CACHE['path']=None
        d=self.client.get('/api/layout').json()
        for b in m._deck_buttons(d):
            if b['id'] in ('wheel-power','wheel-engine'):self.assertTrue(b['unbound'])

if __name__=='__main__':unittest.main()
