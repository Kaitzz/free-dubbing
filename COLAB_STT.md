# SenseVoice STT + Google Colab

1. 在 Colab 上传 `notebooks/SenseVoice_Colab.ipynb`，选择 GPU 运行时。
2. 按 cell 提示上传 `youdub-stt-colab.zip`（源码白名单打包，不包含 .env 或密钥）。
3. 上传音视频并选择语言。模型在 Colab 推理，音视频会进入 Google 的运行环境。
4. 下载结果 ZIP：`metadata/asr.json`、`metadata/asr_fixed.json` 和 `subtitles.srt`。
5. 原文 SRT 用于检查识别和时间轴。注意：现有网页 SRT 上传入口把文件视为已翻译字幕，
   会同时跳过 STT 和翻译；不要直接把此处的原文 SRT 当作译文上传。
   完整项目在 Colab 执行时，主 pipeline 已直接调用同一个 SenseVoice 适配器。
   此 notebook 是独立 STT 验证/导出工具，不是整个应用的云端部署，也不自动连接本机后端。

也可以在安装完整项目的 Colab 环境中直接跑 pipeline，默认 ASR 已切换为 SenseVoice。
单独跑 STT：`python scripts/run_stt.py audio.wav --language en --device cuda:0 --output new-output`
CPU 调试用 `--device cpu`。Colab 安装 `requirements-stt.txt`，保留它自带的 CUDA PyTorch。

## 时间戳与分句

VAD 在 CPU 找出说话片段，ASR 每次输入最长 30 秒；启用 CTC `output_timestamp=True`。
逐字/词检查单调性与片段边界，加回原音频偏移，汇总所有 VAD 窗口后再统一分段。VAD 边界不再强制切字幕；按句末标点、至少 1 秒停顿，以及最长 4.5 秒/65 字符分段。
这些是字幕片段，不保证每个都是语法完整句子。禁用 ITN，保留适合声学对齐的词序。
ASR 结果缺少或含有非法时间戳时明确失败，不用均分时间代替；空音频/无可识别人声明确报错。
VAD 和 ASR 在进程中串行复用模型，阶段结束通过已有显存回收逻辑释放两者。
长文件目前仍需在内存中解码，不能把没有云端一小时限制理解成无限内存流式处理。

## 复现与边界

FunASR 固定 1.4.16；模型默认 ID 为 HF 的 FunAudioLLM/SenseVoiceSmall 和 ModelScope 的 fsmn-vad。
默认模型仓库快照未锁定；严格复现时预下载固定 commit 的快照，并用本地目录配置模型。
禁用运行时版本检查和远程模型代码信任；这不等于依赖已完整安全审计。
尚未在当前机器运行真实模型/GPU。单元测试使用受控模型返回，真实准确率、速度与 Colab 依赖兼容性需运行 notebook 验证。
历史任务请重做 ASR 阶段，避免复用旧 Whisper 的阶段缓存。

## 已有结果重新分段（无需 GPU 或模型推理）

用新版源码运行 `python scripts/run_stt.py old-output/metadata/asr.json --resegment --output new-output`。
使用原始 `asr.json`，其中保留词级时间戳；`asr_fixed.json` 和 SRT 缺少这些信息，不能用于此入口。
输出到新目录，保留旧结果。Notebook 最后提供可选单元：上传原始 JSON 后重新分段并下载。
只调整分段和英文缩写空格，不重新识别或用模型改写文字。

字幕密度以短语阅读为目标：用户提供的英文 SRT 片段仅作保序与粗粒度校准，15–18 条是参考密度，并非强制条数。实际处理使用词级时间戳，可进一步切开样例中的长片段，最终条数可能不同。翻译继续批量携带上下文，字幕不需要合并成长句才能翻译。

分段在句末标点或至少 1 秒停顿之间整体选择切分点，保持 4.5 秒/65 字符上限（单个输入词本身超限时保留它）。优先较明显的停顿与逗号，惩罚过短字幕、英文一两个词的尾段及冠词/介词结尾；必要时将前一条的词移到尾段。保留独立短答句和长静音边界，不保证语法分析级的完整短语。新增测试使用合成词级时间戳验证孤立尾词场景，不将 SRT 内均分时间冒充真实对齐。
