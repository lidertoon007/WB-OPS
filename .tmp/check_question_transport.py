import inspect
from unittest.mock import AsyncMock,patch
from test_send_reply import WBTransportTests

def run(c):
 try:
  yielded=c.send(None)
 except StopIteration:return
 else:raise AssertionError('Unexpected I/O suspension: '+repr(yielded))
 finally:c.close()
for name in sorted(n for n in dir(WBTransportTests) if n.startswith('test_')):
 with patch('app.services.wb_client.asyncio.sleep',new=AsyncMock()):
  run(getattr(WBTransportTests(name),name)())
 print('PASS',name)
