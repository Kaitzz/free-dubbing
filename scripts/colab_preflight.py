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
assert torch.cuda.is_available(), 'Select a Colab GPU runtime'
print('GPU:', torch.cuda.get_device_name(0), 'torch:', torch.__version__)
modules = ['funasr', 'openai', 'audiostretchy', 'requests']
if os.getenv('DUBBING_TTS_PROVIDER','voxcpm') == 'voxcpm':
    modules.append('voxcpm')
for name in modules:
    importlib.import_module(name)
    print('Import OK:', name)
subprocess.run(['ffprobe', '-version'], check=True, stdout=subprocess.DEVNULL)
filters = subprocess.check_output(['ffmpeg', '-hide_banner', '-filters'], text=True)
assert 'subtitles' in filters, 'FFmpeg must include libass/subtitles support'
fonts = subprocess.check_output(['fc-list', ':lang=zh'], text=True)
assert 'Noto' in fonts, 'Install fonts-noto-cjk for Chinese subtitles'
from torchcodec.decoders import AudioDecoder
with tempfile.TemporaryDirectory() as temp:
    audio = Path(temp)/'check.wav'
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)
    AudioDecoder(str(audio)).get_all_samples()
print('Preflight passed: CUDA, imports, audio decoding, FFmpeg and Chinese fonts. Model inference still needs a real run.')
