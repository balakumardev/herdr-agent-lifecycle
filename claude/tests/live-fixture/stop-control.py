import json
import os
import sys

event = json.load(sys.stdin)
if os.environ.get('LIFECYCLE_PROBE_MODE') == 'stop-veto' and not event.get('stop_hook_active'):
    print(json.dumps({'decision': 'block', 'reason': 'Local fixture: produce the final reconciliation response.'}))
