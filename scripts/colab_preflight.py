"""Check full-pipeline dependencies before downloading model weights."""
import importlib
import importlib.metadata
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import numpy as np
import soundfile as sf
import torch

root = Path(__file__).resolve().parents[1]
os.environ['PATH'] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH','')
node_version = subprocess.check_output(['node','--version'],text=True).strip()
assert int(node_version.lstrip('v').split('.')[0]) >= 22, 'YouTube requires Node >=22'
print('YouTube runtime:', node_version, 'yt-dlp:', importlib.metadata.version('yt-dlp'), 'EJS:', importlib.metadata.version('yt-dlp-ejs'))
gpu = torch.cuda.is_available()
if gpu:
    print('GPU:', torch.cuda.get_device_name(0), 'torch:', torch.__version__)
else:
    print('No GPU on this runtime (torch:', torch.__version__ + '): the worker will only take subtitle-only tasks.')
modules = ['funasr', 'openai', 'audiostretchy', 'requests']
if os.getenv('DUBBING_TTS_PROVIDER','voxcpm') == 'voxcpm':
    modules.append('voxcpm')
for name in modules:
    importlib.import_module(name)
    print('Import OK:', name)
subprocess.run(['ffprobe', '-version'], check=True, stdout=subprocess.DEVNULL)
from torchcodec.decoders import AudioDecoder
with tempfile.TemporaryDirectory() as temp:
    audio = Path(temp)/'check.wav'
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)
    AudioDecoder(str(audio)).get_all_samples()
print('Preflight passed:', 'CUDA,' if gpu else 'CPU only,', 'imports, audio decoding and FFmpeg. Model inference still needs a real run.')
