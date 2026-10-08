"""Check full-pipeline dependencies before downloading model weights."""
import importlib
import subprocess
import sys
import tempfile
from pathlib import Path
import numpy as np
import soundfile as sf
import torch

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root/'submodule/demucs'))
assert torch.cuda.is_available(), 'Select a Colab GPU runtime'
print('GPU:', torch.cuda.get_device_name(0), 'torch:', torch.__version__)
for name in ['funasr', 'voxcpm', 'demucs.api', 'openai', 'audiostretchy']:
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
