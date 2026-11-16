"""Run with xvfb-run on Linux. Uses the real control UI and clipboard, no game."""
from pathlib import Path
import sys
from unittest.mock import patch, MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sc_pad_control as m
from PIL import ImageGrab
out=Path('/downloads/control-preview');out.mkdir(exist_ok=True)
with patch.object(m,'OverlayWindow',return_value=MagicMock()),patch.object(m,'lan_ip',return_value='192.168.100.200'),patch.object(m.ControlApp,'_start_hotkeys'):
 app=m.ControlApp(dict(m.DEFAULTS,pairing_code='123456',port=65535),m.ScanState({}, {}, {}, {}))
 app.root.update()
 assert app.connection_url.get()=='http://192.168.100.200:65535'
 assert app.connection_entry.cget('state')=='readonly'
 app.copy_connection_address();app.root.update()
 assert app.root.clipboard_get()=='http://192.168.100.200:65535'
 assert '123456' in app.pair_label.cget('text')
 assert app.copy_address_button.cget('text')=='복사됨'
 for height in (640,520):
  app.root.geometry(f'360x{height}+60+60');app.root.update()
  def bounds(w):return w.winfo_rootx(),w.winfo_rooty(),w.winfo_width(),w.winfo_height()
  entry=bounds(app.connection_entry);copy=bounds(app.copy_address_button);content=bounds(app.content._parent_canvas)
  assert entry[1]<content[1] and copy[1]+copy[3]<content[1]
  assert entry[0]+entry[2]<=app.root.winfo_rootx()+app.root.winfo_width()
  app.content._parent_canvas.yview_moveto(1);app.root.update()
  assert bounds(app.connection_entry)==entry
  x,y=app.root.winfo_rootx(),app.root.winfo_rooty()
  ImageGrab.grab(bbox=(x,y,x+app.root.winfo_width(),y+app.root.winfo_height())).save(out/f'control-{height}.png')
 # copy does not reuse the old network address
 with patch.object(m,'lan_ip',return_value='10.1.2.3'):
  app.copy_connection_address();assert app.root.clipboard_get()=='http://10.1.2.3:65535'
 app.root.destroy()
print('control-window checks: top URL/code, readonly/selectable entry, exact clipboard URL, refresh on copy, fixed above scrolling settings, 360x640 and 360x520')
