# Run inside the existing notebook namespace (root/python/state/run_env/result_zip).
previous_process = globals().get('process')
if previous_process is not None:
    assert previous_process.poll() is not None, '上一个任务仍在运行'
run_env.update(VOXCPM_LOW_MEMORY_INIT='true', VOXCPM_OPTIMIZE='false')
command = [python, '-u', 'scripts/colab_pipeline.py', '--state', str(state), '--result', str(result_zip)]
if state.exists():
    command += ['--resume']
    if previous_process is not None and previous_process.returncode != 0:
        command += ['--recover-dead-pid', str(previous_process.pid)]
else:
    command += ['--video', str(video), '--direction', direction, '--output-mode', output_mode]
process = subprocess.Popen(command, env=run_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    for line in process.stdout:
        print(line, end='', flush=True)
    returncode = process.wait()
except KeyboardInterrupt:
    process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    raise
assert returncode == 0, f'流水线退出码 {returncode}；负值表示信号终止。保留当前运行时，修复后续跑。'
