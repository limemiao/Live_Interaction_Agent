"""细粒度内容判定：把 8 秒切片拆成 1 秒子窗逐段分类，再聚合出结论。

为什么必须降粒度：8 秒切片里经常"音乐 + 跟唱 + 说话"混在一起
（用户实测描述："前面是音乐，然后主播有一小段跟唱，然后后面说了一句话"），
整体算一个特征必然被稀释，阈值两头不讨好。

子窗只在 1 秒内判定，内部内容单一，判据才站得住：
  - 无周期性人声      -> music   （纯音乐，丢掉）
  - 有声音但音高稳定  -> sing    （唱歌/哼唱，丢掉）
  - 有声音且音高抖动  -> speech  （说话，保留）

聚合规则偏保守：**只要有一段像说话就保留**。宁可留下噪声，
也不要误杀小声说话——用户已经确认过小声说话是最容易被误杀的。
"""

import numpy as np

from .singing import SR, f0_track, resample_to, _hz_to_semitone

SUB_SECONDS = 1.0

# 子窗判定阈值
VOICED_MIN = 0.18        # 有声帧占比低于此值：这个子窗里没人声
STABLE_MIN = 0.45        # 稳定音高帧占比高于此值：像唱歌
RUN_MIN_S = 0.12         # 视作"一个音符"的最短持续
STD_MAX_ST = 1.2         # 音符内音高标准差上限（半音）


def classify_sub(x, sr=SR):
    """判定 1 秒子窗：返回 (类型, 明细)。类型 ∈ music/sing/speech"""
    f0, conf, rms = f0_track(x, sr=sr, hop=160)
    if len(f0) == 0:
        return "music", {}
    voiced = ~np.isnan(f0)
    vf = float(voiced.mean())
    frame_s = 160 / sr

    marks = np.flatnonzero(np.diff(np.r_[0, voiced.astype(np.int8), 0]))
    starts, ends = marks[::2], marks[1::2]
    stable_frames = 0
    for a, b in zip(starts, ends):
        dur = (b - a) * frame_s
        if dur < RUN_MIN_S:
            continue
        if b - a >= 4:
            std = float(np.std(_hz_to_semitone(f0[a:b])))
            if std <= STD_MAX_ST:
                stable_frames += (b - a)
    stable_frac = stable_frames / max(int(voiced.sum()), 1)

    detail = {"voiced_frac": round(vf, 3), "stable_frac": round(stable_frac, 3)}
    if vf < VOICED_MIN:
        return "music", detail
    if stable_frac >= STABLE_MIN:
        return "sing", detail
    return "speech", detail


def classify_chunk(audio, sr=16000, sub_seconds=SUB_SECONDS):
    """把一个切片拆成子窗逐段分类，再聚合。

    返回 (结论, 子窗明细列表)。结论 ∈ music/sing/speech/none
    """
    x, sr = resample_to(np.asarray(audio, dtype=np.float32), sr)
    step = int(sr * sub_seconds)
    if len(x) < step:
        return "none", []
    subs = []
    for i in range(0, len(x) - step + 1, step):
        kind, detail = classify_sub(x[i:i + step], sr=sr)
        detail["kind"] = kind
        subs.append(detail)
    if not subs:
        return "none", []

    kinds = [s["kind"] for s in subs]
    n_speech = kinds.count("speech")
    n_sing = kinds.count("sing")
    n_music = kinds.count("music")
    # 保守聚合：只要有一段像说话就保留
    if n_speech:
        verdict = "speech"
    elif n_sing:
        verdict = "sing"
    elif n_music == len(kinds):
        verdict = "music"
    else:
        verdict = "speech"
    return verdict, subs
