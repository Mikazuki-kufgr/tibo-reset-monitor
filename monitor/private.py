"""Generated local channel secrets; never included in published source."""
from __future__ import annotations
import json,os,pathlib

ALLOWED={'NTFY_TOPIC','NTFY_TOKEN','BARK_DEVICE_KEY','RESET_WEBHOOK_URL','RESET_WEBHOOK_SECRET'}

def load_channels(path: pathlib.Path):
    if not path.exists():
        return
    if path.stat().st_size > 65536:
        raise ValueError('private_channel_file_too_large')
    value=json.loads(path.read_text('utf-8-sig'))
    if not isinstance(value,dict) or set(value)-ALLOWED or any(not isinstance(v,str) or not v for v in value.values()):
        raise ValueError('invalid_private_channel_file')
    for key,val in value.items():
        os.environ[key]=val
