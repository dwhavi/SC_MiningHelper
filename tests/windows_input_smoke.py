"""Native Windows ABI/event construction check. SendInput is intercepted, no real keys."""
import sys,ctypes
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sc_pad_control as m
if sys.platform!='win32':
 print('native Windows input test skipped on non-Windows');sys.exit(0)
real=ctypes.WinDLL('user32',use_last_error=True)
class Function:
 def __init__(self,fn):self.fn=fn
 def __call__(self,*a):return self.fn(*a)
calls=[]
def send(count,ptr,size):
 inp=ptr._obj
 assert size==ctypes.sizeof(type(inp))==40
 calls.append((inp.u.ki.wVk,inp.u.ki.wScan,inp.u.ki.dwFlags))
 return 1
class API:
 MapVirtualKeyW=real.MapVirtualKeyW
 SendInput=Function(send)
with patch.object(ctypes,'WinDLL',return_value=API()),patch.object(m,'_trace_input'):
 m._emit_vk(0xA5);m._emit_vk(0x52);m._emit_vk(0x52,2);m._emit_vk(0xA5,2)
assert calls==[(0,0x38,9),(0,0x13,8),(0,0x13,10),(0,0x38,11)],calls
print('Native Windows INPUT size and RALT+R scan/extended/release events passed; no keys emitted')
