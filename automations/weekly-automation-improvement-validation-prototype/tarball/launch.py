import json,os,runpy
from pathlib import Path
os.environ.update(json.loads(Path('/var/lib/agent-canvas/flow1-self-improvement-20261027/runtime.json').read_text()))
runpy.run_path(str(Path(__file__).with_name('main.py')),run_name='__main__')
