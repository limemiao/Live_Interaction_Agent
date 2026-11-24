"""唱歌/歌词检测：从音频本身判断人声是在"说"还是在"唱"。

为什么不用文本规则：中文歌词糊出来就是普通中文句子，任何基于字符的
规则都拦不住。而"唱歌"在声学上有明确特征，且**与语言无关**：

  1. 拖长音 —— 唱歌的音符持续 0.3~2 秒，说话的元音段只有几十毫秒
  2. 音高稳定 —— 唱歌在单个音符内基频几乎不变，说话的音高是连续滑动的
  3. 旋律跨度 —— 一个窗口内唱歌的音高极差通常超过一个八度
  4. 有声占比 —— 唱歌几乎全程发声，说话有大量停顿和清辅音

实现说明：基频检测用**自己写的 FFT 自相关**，不用 librosa。
实测 librosa 在本机异常慢（yin 处理 8 秒片段超过 2 分钟），
而这个实现在同样的片段上是毫秒级，且省掉一个重依赖。
"""

import numpy as np

SR = 8000          # 只需检测 80~900Hz，8kHz 采样足够，比 16k 快一倍


def resample_to(audio, sr, target=SR):
    """多相滤波重采样。scipy 的实现是 O(n)，比 FFT 法更稳。"""
    from scipy import signal

    if sr == target:
        return audio.astype(np.float32), sr
    from math import gcd
    g = gcd(int(sr), int(target))
    up, down = target // g, sr // g
    return signal.resample_poly(audio, up, down).astype(np.float32), target


def f0_track(x, sr=SR, frame=512, hop=160, fmin=80, fmax=900,
             conf_thresh=0.35, rms_thresh=0.004):
    """基于 FFT 自相关的基频提取。返回 (f0, 置信度, 帧 RMS)，无声帧 f0 为 nan。"""
    if len(x) < frame:
        return np.array([]), np.array([]), np.array([])
    n_frames = 1 + (len(x) - frame) // hop
    idx = np.arange(frame)[None, :] + hop * np.arange(n_frames)[:, None]
    win = np.hanning(frame).astype(np.float32)
    frames = x[idx] * win

    rms = np.sqrt((frames ** 2).mean(axis=1))
    n = 1 << int(np.ceil(np.log2(2 * frame)))
    spec = np.fft.rfft(frames, n=n, axis=1)
    ac = np.fft.irfft(np.abs(spec) ** 2, n=n, axis=1)[:, :frame]
    ac = ac / (ac[:, :1] + 1e-9)          # 归一化到 0~1

    min_lag = max(int(sr / fmax), 1)
    max_lag = min(int(sr / fmin), frame - 1)
    seg = ac[:, min_lag:max_lag]
    peak = seg.argmax(axis=1) + min_lag
    conf = seg.max(axis=1)
    f0 = sr / np.maximum(peak, 1)
    voiced = (conf > conf_thresh) & (rms > rms_thresh)
    f0 = np.where(voiced, f0, np.nan)
    return f0, conf, rms


def _hz_to_semitone(f0):
    return 12 * np.log2(np.maximum(f0, 1e-6) / 55.0)


def analyze(audio, sr=SR, fmin=80, fmax=900, hop=160):
    """对一个音频窗口做基频分析，返回特征字典。"""
    x, sr = resample_to(np.asarray(audio, dtype=np.float32), sr)
    f0, conf, rms = f0_track(x, sr=sr, fmin=fmin, fmax=fmax, hop=hop)
    if len(f0) == 0:
        return None

    frame_s = hop / sr
    voiced = ~np.isnan(f0)
    voiced_frac = float(voiced.mean())

    # 矢量化的连续有声段切分
    marks = np.flatnonzero(np.diff(np.r_[0, voiced.astype(np.int8), 0]))
    starts, ends = marks[::2], marks[1::2]
    run_frames = ends - starts
    keep = run_frames * frame_s >= 0.05      # 丢掉清辅音碎片
    long_runs = run_frames[keep] * frame_s
    starts, ends = starts[keep], ends[keep]
    mean_run = float(long_runs.mean()) if len(long_runs) else 0.0
    max_run = float(long_runs.max()) if len(long_runs) else 0.0

    # 段内音高稳定性：段数不多，这里循环开销可忽略
    stabilities = []
    for a, b in zip(starts, ends):
        if b - a >= 5:
            st = _hz_to_semitone(f0[a:b])
            stabilities.append(float(np.std(st)))
    pitch_std = float(np.mean(stabilities)) if stabilities else 99.0

    valid = f0[voiced]
    if len(valid) >= 5:
        st_all = _hz_to_semitone(valid)
        pitch_range = float(np.percentile(st_all, 95) - np.percentile(st_all, 5))
    else:
        pitch_range = 0.0

    return {
        "voiced_frac": round(voiced_frac, 3),
        "mean_run_s": round(mean_run, 3),
        "max_run_s": round(max_run, 3),
        "pitch_std_st": round(pitch_std, 2),
        "pitch_range_st": round(pitch_range, 2),
        "rms": round(float(np.sqrt((x ** 2).mean() + 1e-12)), 4),
    }


# 判定阈值：先在真实音频上看分布，再定。见 calibrate 脚本输出的对比表。
THRESHOLDS = {
    "mean_run_s": 0.22,
    "pitch_std_st": 1.10,
    "pitch_range_st": 6.0,
    "voiced_frac": 0.55,
}


def judge(feat, thresholds=None):
    """按声学特征打分。返回 (是否唱歌, 得分 0~1, 命中依据)。"""
    if not feat:
        return False, 0.0, []
    t = dict(THRESHOLDS)
    t.update(thresholds or {})
    hits, score = [], 0.0
    if feat["mean_run_s"] >= t["mean_run_s"]:
        score += 1
        hits.append("拖长音%.2fs" % feat["mean_run_s"])
    if feat["pitch_std_st"] <= t["pitch_std_st"]:
        score += 1
        hits.append("音高稳%.2f" % feat["pitch_std_st"])
    if feat["pitch_range_st"] >= t["pitch_range_st"]:
        score += 1
        hits.append("跨度%.1f" % feat["pitch_range_st"])
    if feat["voiced_frac"] >= t["voiced_frac"]:
        score += 1
        hits.append("有声%.2f" % feat["voiced_frac"])
    return score / 4.0 >= 0.75, score / 4.0, hits
