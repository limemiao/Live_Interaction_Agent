#!/usr/bin/env python3
"""B 站直播间实时语音转文字（本地 faster-whisper，不上传任何音频）。

工作方式:
    ffmpeg 拉直播流 -> 每 N 秒切一个 16kHz 单声道 wav 片段 -> faster-whisper 本地识别
    -> 带时间戳追加写入转录文件（逐行 flush，随时可看）

用法:
    python bili_live_asr.py --room-id 123456789 --duration 120
    python bili_live_asr.py --room-id 123456789 --model large-v3 --chunk 5

注意:
    - 模型首次运行会从 HuggingFace 下载（国内自动走 hf-mirror 镜像）
    - 需要 ffmpeg 在 PATH 中
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from agent.lexicon import asr_initial_prompt, correct_terms

# 国内直连 HuggingFace 通常不通，默认走镜像；已设置则不覆盖
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# Anaconda 自带的 libiomp5md.dll 会和 ctranslate2 的 OpenMP 冲突，官方给的兼容开关
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import requests  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
ROOM_PLAY_INFO = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"


class StreamError(RuntimeError):
    """取流失败（接口报错 / 没有可用地址）。

    这类失败往往是一次性的（限流、网关抖动），应该退避重试而不是退出进程。
    以前用 SystemExit，外层重连逻辑根本不接，一次接口非 0 就会让转录永久停摆。
    """

# Whisper 在纯音乐/静音段会稳定吐出的一批"字幕套话"，属于幻觉不是内容。
# 这类短语在直播里几乎不可能由主播说出，命中就整段丢弃。
HALLUCINATION_PATTERNS = [
    # 只匹配"组合特征"，避免把主播真的说"点个订阅"也误杀
    r"不吝点赞", r"点赞.*订阅.*转发", r"订阅.*转发.*打赏",
    r"打赏支持", r"明镜与点点",
    r"字幕\s*by", r"字幕由", r"字幕组", r"中文字幕",
    r"感谢观看", r"谢谢观看", r"我们下期再见", r"下期再见",
    r"独播剧场", r"优优独播", r"YoYo", r"Exclusive",
    # 解码偏置（initial_prompt）在静音段被模型原样吐出来的样子
    r"常见词：舰长",
]
HALLUCINATION_RE = re.compile("|".join(HALLUCINATION_PATTERNS))

# 歌词判定：直播里唱歌的段落识别出来多是"糊掉的长句"或整句英文。
# 英文占比是最可靠的信号——主播正常说话不会连续蹦出一串英文单词。
LYRIC_ASCII_MIN = 6        # 至少这么多个英文字母
LYRIC_ASCII_RATIO = 0.5    # 且占整句比例超过这个值


def looks_like_lyrics(text, avg_logprob=None, min_ascii=LYRIC_ASCII_MIN,
                      ascii_ratio=LYRIC_ASCII_RATIO):
    """判断一句转写是不是歌词。返回 (是否, 原因)。"""
    t = (text or "").strip()
    if not t:
        return False, ""
    letters = sum(1 for c in t if c.isascii() and c.isalpha())
    if letters >= min_ascii and letters / len(t) > ascii_ratio:
        return True, "英文占比 %.0f%%（%d 个字母）" % (
            letters / len(t) * 100, letters)
    return False, ""


# ---- 唱歌检测（AST / AudioSet）------------------------------------------------
# 在 29 个已人工标注的片段上做过留一验证：
#   ast_Speech <= 0.41 判为唱歌   ->  准确率 97%
#   两组分布完全不重叠（唱歌最大 0.409，说话最小 0.503）
# 注意：靠的是 Speech 概率，而不是模型自带的 Singing 类
# ——后者在本房间音频上全程不激活（实测 0.00~0.05）。
AST_MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"


def load_ast(device):
    from transformers import ASTFeatureExtractor, ASTForAudioClassification

    ext = ASTFeatureExtractor.from_pretrained(AST_MODEL)
    model = ASTForAudioClassification.from_pretrained(AST_MODEL)
    model.to(device).eval()
    idx = [i for i, n in model.config.id2label.items() if n == "Speech"]
    if not idx:
        raise RuntimeError("AST 模型里找不到 Speech 类别")
    return ext, model, idx[0]


def speech_probability(ext, model, speech_idx, path, device, sub_seconds=1.0):
    """取一个音频切片"属于说话"的概率。

    关键：**按 1 秒子窗算，取最大值**，而不是整段平均。
    理由（用人工复核的 6 段验证过）：
        纯背景音乐  每秒最大 0.24  -> 丢
        纯唱歌      每秒最大 0.15  -> 丢
        小声说话+哼唱 每秒最大 0.42~0.43 -> 留
    整段平均会把只在 1~2 秒里出现的小声说话稀释到阈值以下，
    这是之前误杀"小声说话"的根因。
    """
    import numpy as np
    import soundfile as sf
    import torch
    from math import gcd
    from scipy import signal

    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if sr != 16000:
        g = gcd(int(sr), 16000)
        mono = signal.resample_poly(mono, 16000 // g, sr // g).astype(np.float32)
    step = int(16000 * sub_seconds)
    best = 0.0
    for i in range(0, max(len(mono) - step + 1, 1), step):
        seg = mono[i:i + step]
        if len(seg) < 1600:
            break
        inputs = ext(seg, sampling_rate=16000, return_tensors="pt").to(device)
        with torch.no_grad():
            probs = torch.sigmoid(model(**inputs).logits)[0]
        best = max(best, float(probs[speech_idx]))
    return best


def load_cookies(path: Path) -> dict:
    import json

    if not path.exists():
        raise SystemExit(f"Cookie 文件不存在: {path}（先跑 bili_login.py）")
    return json.loads(path.read_text(encoding="utf-8")).get("cookies") or {}


def get_stream_url(room_id: int, cookies: dict):
    """拿直播流地址（优先 FLV 原画，其次 HLS）。

    返回 None 表示房间没开播；接口/解析失败抛 StreamError（可重试）。
    """
    params = {
        "room_id": room_id,
        "protocol": "0,1",
        "format": "0,1,2",
        "codec": "0,1",
        "qn": 10000,
        "platform": "web",
        "ptype": 8,
        "dolby": 5,
        "panorama": 1,
    }
    headers = {"User-Agent": UA, "Referer": f"https://live.bilibili.com/{room_id}"}
    resp = requests.get(
        ROOM_PLAY_INFO, params=params, headers=headers, cookies=cookies, timeout=20
    )
    try:
        data = resp.json()
    except ValueError as exc:
        raise StreamError("getRoomPlayInfo 响应不是 JSON: %s" % exc)
    if data.get("code") != 0:
        raise StreamError(f"getRoomPlayInfo 失败: {data.get('code')} "
                          f"{data.get('message')}")
    live_status = (data.get("data") or {}).get("live_status")
    if live_status != 1:
        return None          # 未开播，交给调用方决定是等待还是退出

    try:
        streams = data["data"]["playurl_info"]["playurl"]["stream"]
        for st in streams:
            for fmt in st["format"]:
                for codec in fmt["codec"]:
                    info = codec["url_info"][0]
                    return info["host"] + codec["base_url"] + info["extra"]
    except (KeyError, IndexError, TypeError) as exc:
        raise StreamError("直播流结构异常: %s" % exc)
    raise StreamError("没解析到可用的流地址。")


def _beat(path: Path, note: str = ""):
    """往转录文件写一行心跳（`#` 开头，下游会当作注释跳过）。

    为什么需要它：转录文件"不再增长"是判断采集是否还活着最直接的信号。
    没有心跳时，进程崩了、卡住了、房间没开播，三者从文件上看完全一样，
    下游的看门狗只能眼睁睁看着（实测踩过：ASR 启动即崩，界面上毫无提示）。
    """
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write("# [心跳] %s %s\n" % (time.strftime("%H:%M:%S"), note))
    except Exception:
        pass


def _wait_stream(args, cookies, beat_path: Path = None):
    """重连时重新取流地址；拿不到就按 --wait-live 决定等待还是放弃。

    返回 None 表示放弃（房间已下播、且没要求等待）。注意重连必须重新取地址：
    旧的流地址在流中断后就失效了，这也是为什么不能只重启 ffmpeg。

    接口异常（接口报错、网络抖动）不再让进程退出：有 --wait-live 就退避
    重试并持续写心跳，没有就当作拿不到流返回 None，交给调用方结束。
    """
    while True:
        try:
            url = get_stream_url(args.room_id, cookies)
        except Exception as exc:
            if beat_path:
                _beat(beat_path, "取流失败")
            print(f"[{time.strftime('%H:%M:%S')}] 取流失败："
                  f"{type(exc).__name__}: {exc}")
            if not args.wait_live:
                return None
            print(f"[{time.strftime('%H:%M:%S')}] {args.wait_interval}s 后重试 ...")
            time.sleep(args.wait_interval)
            continue
        if url is not None:
            return url
        if beat_path:
            _beat(beat_path, "等待开播")
        if not args.wait_live:
            print(f"[{time.strftime('%H:%M:%S')}] 房间未开播/已下播，停止。")
            return None
        print(f"[{time.strftime('%H:%M:%S')}] 未开播，"
              f"{args.wait_interval}s 后重试 ...")
        time.sleep(args.wait_interval)


def start_ffmpeg(stream_url: str, out_dir: Path, chunk_seconds: int,
                 separate: bool = False, is_local: bool = False) -> subprocess.Popen:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("chunk_*.wav"):
        # 尽力清理，失败不能致命：目录里残留的切片可能是上一个进程（或被强杀后
        # 遗留的孤儿 ffmpeg）还在写的文件，删不掉就留着——旧的切片没有用，
        # 但为此把整个转录进程搞崩就不值了（踩过一次：启动即 PermissionError）。
        try:
            old.unlink()
        except OSError:
            pass
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
    ]
    if not is_local:
        cmd += ["-user_agent", UA, "-referer", "https://live.bilibili.com/"]
    cmd += [
        "-i", stream_url,
        "-vn",                       # 丢掉视频轨
        # 分离模式下 demucs 要 44.1kHz 立体声；否则直接降到 Whisper 的 16kHz 单声道
        "-ac", "2" if separate else "1",
        "-ar", "44100" if separate else "16000",
        "-c:a", "pcm_s16le",
        "-f", "segment",
        "-segment_time", str(chunk_seconds),
        "-reset_timestamps", "1",
        str(out_dir / "chunk_%05d.wav"),
    ]
    creation = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    return subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, creationflags=creation,
    )


def _cleanup_old_chunks(root: Path, keep_hours: float = 2.0):
    """清掉以前遗留的切片目录（被强杀时会残留，孤儿 ffmpeg 可能还在写）。

    只删"够旧"的目录，且失败不管——正在被占用的那份留着就是了。
    """
    if not root.exists():
        return
    cutoff = time.time() - keep_hours * 3600
    for pattern in ("sess_*", "run_*"):      # run_* 是旧命名，一并清理
        for d in root.glob(pattern):
            try:
                if d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


def extract_vocals(separator, path: Path, device: str):
    """用 demucs 抠出人声轨。

    返回 (16kHz 单声道人声, 统计字典)。统计里带人声与伴奏的能量，
    用于判断这一段是不是"歌曲"——伴奏抬升是放歌/唱歌的物理特征，
    与语言无关（中文歌词用字符规则根本拦不住）。
    """
    import numpy as np
    import soundfile as sf
    import torch
    import torchaudio
    from demucs.apply import apply_model

    # 不用 torchaudio.load：torchaudio>=2.9 需要额外的 torchcodec，soundfile 已经装了
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    wav = torch.from_numpy(np.ascontiguousarray(data.T))    # (channels, samples)
    if wav.shape[0] == 1:
        wav = wav.repeat(2, 1)
    if sr != separator.samplerate:
        wav = torchaudio.functional.resample(wav, sr, separator.samplerate)
    wav = wav.to(device)
    ref = wav.mean(0)
    norm = (wav - ref.mean()) / (ref.std() + 1e-8)
    with torch.no_grad():
        stems = apply_model(separator, norm[None], device=device, progress=False)[0]
    stems = stems * ref.std() + ref.mean()
    vocals = stems[separator.sources.index("vocals")].mean(0)
    other = stems[separator.sources.index("other")].mean(0)
    v_rms = float(torch.sqrt((vocals ** 2).mean() + 1e-12))
    o_rms = float(torch.sqrt((other ** 2).mean() + 1e-12))
    voc16 = torchaudio.functional.resample(vocals.cpu(), separator.samplerate, 16000)
    stats = {
        "vocal_rms": round(v_rms, 5),
        "other_rms": round(o_rms, 5),
        "music_ratio": round(o_rms / (v_rms + 1e-9), 3),
    }
    return voc16.numpy().astype(np.float32), stats


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="B 站直播实时语音转文字（本地 faster-whisper）")
    ap.add_argument("--room-id", type=int, required=True)
    ap.add_argument("--cookie-file", default=str(here / "bili_cookies.json"))
    ap.add_argument("--model", default="small", help="tiny/base/small/medium/large-v3 或本地目录")
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    ap.add_argument("--compute-type", default=None, help="默认 cuda=float16 / cpu=int8")
    ap.add_argument("--chunk", type=int, default=5, help="音频切片秒数")
    ap.add_argument("--overlap", type=float, default=2.0,
                    help="每片转写时额外带上的前文秒数（解决硬切导致句首/"
                         "句尾被截断；0=关闭）")
    ap.add_argument("--language", default="zh")
    ap.add_argument("--duration", type=int, default=0, help="总时长秒数，0=一直跑")
    ap.add_argument("--out", default=None, help="转录文本输出路径")
    ap.add_argument("--beam", type=int, default=5, help="beam size，1 更快，5 更准")
    ap.add_argument(
        "--vad", action="store_true",
        help="开启 VAD 静音过滤。默认关闭：直播里的唱歌/轻声会被 VAD 判成非人声而整段丢掉",
    )
    ap.add_argument(
        "--separate", action="store_true",
        help="先用人声分离（demucs htdemucs，GPU）把音乐抠掉再识别；配 --vad 效果最好",
    )
    ap.add_argument("--input-file", default=None,
                    help="不用直播流，直接处理一个本地音频文件（用于离线验证）")
    ap.add_argument("--max-restarts", type=int, default=0,
                    help="断流后最多重连几次（0=不限；本地文件模式不重连）")
    ap.add_argument("--kill-ffmpeg-after", type=int, default=0,
                    help="测试用：跑 N 秒后杀掉 ffmpeg 模拟断流，验证重连（0=关闭）")
    ap.add_argument("--wait-live", action="store_true",
                    help="房间未开播时等待，开播后自动开始转录")
    ap.add_argument("--wait-interval", type=int, default=30,
                    help="等待开播的轮询间隔秒数")
    ap.add_argument("--music-ratio", type=float, default=0.75,
                    help="伴奏/人声 能量比超过此值判为歌曲段落并丢弃（需 --separate）。"
                         "0.6 会误杀「人声稍小但确实在说话」的片段，调到 0.75")
    ap.add_argument("--min-vocal-rms", type=float, default=0.006,
                    help="人声能量低于此值判为静音并丢弃（需 --separate）")
    ap.add_argument("--singing-filter", action="store_true",
                    help="用 AST 模型判断唱歌段落并丢弃转写（中文歌词也能拦，"
                         "首次运行会下载约 350MB 模型）")
    ap.add_argument("--ast-speech-max", type=float, default=0.32,
                    help="1 秒子窗的 AST Speech 概率最大值低于此值判为唱歌段落。"
                         "0.38 会在正常说话的短句上误杀（实测「这不是公主好吧?」"
                         "=0.356），调到 0.32 仍能拦住 0.31 以下的歌词")
    args = ap.parse_args()

    compute_type = args.compute_type or ("float16" if args.device == "cuda" else "int8")
    out_path = Path(args.out) if args.out else here / f"transcript_{args.room_id}_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    # 启动即写心跳：模型加载（冷启动可能要几分钟）期间文件也必须保持增长，
    # 否则下游看门狗会把"正在加载"误判成"转录进程死了"（实测误报过一次）。
    _beat(out_path, "启动，加载模型")
    # 每启动一次用一个独立目录：被强杀时残留的孤儿 ffmpeg 还在往旧目录写，
    # 新进程复用同名目录就会撞上被占用的文件（实测直接崩在 start_ffmpeg）。
    _cleanup_old_chunks(here / "output" / "asr_chunks", keep_hours=2)
    work_dir = (here / "output" / "asr_chunks"
                / ("sess_%s" % time.strftime("%m%d_%H%M%S")))

    if args.input_file:
        stream_url = str(Path(args.input_file))
        print(f"[{time.strftime('%H:%M:%S')}] 本地文件模式: {stream_url}")
    else:
        cookies = load_cookies(Path(args.cookie_file))
        print(f"[{time.strftime('%H:%M:%S')}] 获取直播流地址 ...")
        # 走 _wait_stream：它会在等待期间往转录文件写心跳，
        # 这样"等开播"和"进程死了"在下游看门狗眼里就是两件事
        stream_url = _wait_stream(args, cookies, out_path)
        if not stream_url:
            raise SystemExit("拿不到音频流（未开播或接口异常；"
                             "可加 --wait-live 等待）。")
        print(f"[{time.strftime('%H:%M:%S')}] 流地址已获取")

    model_ref = args.model
    candidate = Path(model_ref).expanduser()
    if not candidate.exists() and "/" in model_ref:
        from huggingface_hub import snapshot_download

        model_ref = snapshot_download(repo_id=model_ref, local_files_only=True)
    print(
        f"[{time.strftime('%H:%M:%S')}] 加载模型 {model_ref} "
        f"({args.device}/{compute_type}) ..."
    )
    from faster_whisper import WhisperModel

    model = WhisperModel(model_ref, device=args.device, compute_type=compute_type)

    separator = None
    if args.separate:
        print(f"[{time.strftime('%H:%M:%S')}] 加载人声分离模型 htdemucs ...")
        _beat(out_path, "加载人声分离模型")
        from demucs.pretrained import get_model

        separator = get_model("htdemucs")
        separator.to(args.device)
        separator.eval()
    ast_ext = ast_model = None
    ast_speech_idx = -1
    if args.singing_filter:
        print(f"[{time.strftime('%H:%M:%S')}] 加载唱歌检测模型 AST ...")
        _beat(out_path, "加载唱歌检测模型")
        ast_ext, ast_model, ast_speech_idx = load_ast(args.device)
    print(f"[{time.strftime('%H:%M:%S')}] 模型就绪")

    print(f"[{time.strftime('%H:%M:%S')}] 转录写入: {out_path}")

    started = time.time()
    # 本轮音频对应的墙钟起点。重连时会被重置为"新一轮 ffmpeg 启动的时刻"。
    round_base = [started]
    done: set[str] = set()
    transcript = out_path.open("a", encoding="utf-8")

    # 每秒字节数，用来判断切片是否太短（碎片）
    bytes_per_sec = (44100 * 2 * 2) if args.separate else (16000 * 1 * 2)

    # 上一片的人声尾巴（16kHz 单声道）。下一片转写时拼在前面，让 Whisper
    # 看到完整的跨片句子——固定切片硬切时，句首/句尾会被切掉半个词，
    # 表现在日志里就是"[11:22:48] 我头天写了"这种半截话。
    prev_tail = None

    def process_file(chunk: Path) -> None:
        nonlocal prev_tail
        import numpy as np
        name = chunk.name
        if name in done:
            return
        if chunk.stat().st_size < bytes_per_sec * 0.5:
            done.add(name)
            return
        done.add(name)
        t0 = time.time()
        stem_stats = None
        if separator is not None:
            audio, stem_stats = extract_vocals(separator, chunk, args.device)
        else:
            import soundfile as sf
            data, sr = sf.read(str(chunk), dtype="float32", always_2d=True)
            audio = data.mean(axis=1)
            if sr != 16000:
                from math import gcd
                from scipy import signal
                g = gcd(int(sr), 16000)
                audio = signal.resample_poly(
                    audio, 16000 // g, sr // g).astype(np.float32)

        overlap_s = max(0.0, float(args.overlap))
        if prev_tail is not None and overlap_s > 0 and len(prev_tail):
            window = np.concatenate([prev_tail, audio])
            skip_s = len(prev_tail) / 16000.0
        else:
            window = audio
            skip_s = 0.0

        segments, _ = model.transcribe(
            window,
            language=args.language,
            # 混音时 VAD 会把唱歌/轻声判成非人声而丢内容；分离出人声后 VAD 就可靠了
            vad_filter=args.vad,
            beam_size=args.beam,
            # 带前文时要用词级时间戳，才能只保留属于当前切片的部分
            word_timestamps=bool(skip_s > 0),
            no_repeat_ngram_size=3,
            repetition_penalty=1.15,
            condition_on_previous_text=False,
            # 带前文的窗口不再灌解码偏置：实测"偏置 + 2 秒前文"在音乐/换气
            # 处会把整片带成"请不吝点赞…"的幻觉，比窄上下文更糟。术语纠错
            # 还有 lexicon.correct_terms 兜底。
            initial_prompt=(asr_initial_prompt() if skip_s <= 0 else None),
        )
        if skip_s > 0:
            words = []
            for s in segments:
                for w in (s.words or []):
                    # 用 end 判断：跨在边界上的词宁可重复一个字，也不要在
                    # 两片里都被丢掉
                    if w.end > skip_s - 0.05:
                        words.append(w.word)
            text = "".join(words).strip()
            if not text:
                # 个别情况下没有词级时间戳，退回按 segment 截断
                text = "".join(
                    s.text for s in segments if s.end > skip_s).strip()
        else:
            text = "".join(s.text for s in segments).strip()

        # 术语纠错：Whisper 常把"舰长"听成"肩章"这类同音错词
        text, nfix = correct_terms(text)
        elapsed = time.time() - t0
        if nfix:
            print(f"  (术语纠错 x{nfix})")
        if text and HALLUCINATION_RE.search(text):
            print(f"  (丢弃幻觉) {text[:40]}")
            text = ""
        if text:
            is_lyric, why = looks_like_lyrics(text)
            if is_lyric:
                print(f"  (过滤歌词) {text[:40]}  <- {why}")
                text = ""
        # 歌曲段落判定：伴奏能量相对人声抬升，说明这段在放歌或唱歌。
        # 这是语言无关的信号，中文歌词也能拦住。
        if text and stem_stats:
            if stem_stats["music_ratio"] >= args.music_ratio:
                print("  (过滤歌曲) %s  <- 伴奏/人声=%.2f≥%.2f"
                      % (text[:36], stem_stats["music_ratio"],
                         args.music_ratio))
                text = ""
            elif stem_stats["vocal_rms"] < args.min_vocal_rms:
                print("  (丢弃静音) %s  <- 人声能量%.4f 过低"
                      % (text[:36], stem_stats["vocal_rms"]))
                text = ""
        # 唱歌判定：AST 的 Speech 概率。标定过的阈值，中文歌词同样有效。
        if text and ast_model is not None:
            p = speech_probability(ast_ext, ast_model, ast_speech_idx, chunk,
                                   args.device)
            if p <= args.ast_speech_max:
                print("  (过滤唱歌) %s  <- Speech 概率 %.3f≤%.2f"
                      % (text[:34], p, args.ast_speech_max))
                text = ""
        # 只有这一片确认是"说话"时才把尾巴传给下一片。唱歌/静音/幻觉片的
        # 尾巴会把下一片带偏——实测上一片是歌声时，下一片拼上 2 秒歌声会
        # 直接转成"请不吝点赞…"的幻觉，反而比不加还差。
        prev_tail = (audio[-int(overlap_s * 16000):]
                     if overlap_s > 0 and text else None)
        if text:
            idx = int(re.search(r"(\d+)", name).group(1))
            # 在线模式用墙上时钟；离线跑文件时必须用音频位置——
            # 离线处理远快于实时，墙上时钟会把时间轴压缩，导致
            # 日志里的时间跟音频实际位置对不上。
            if args.input_file:
                pos = (idx + 1) * args.chunk
                stamp = "+%d:%02d" % (pos // 60, pos % 60)
            else:
                # 用"本轮 ffmpeg 的启动时刻"而不是会话起点：重连之后音频是
                # 从当下继续的，继续用会话起点会让时间戳跳回开场（顺序乱掉，
                # 下游按时间戳入库也会跟着错位）。
                stamp = time.strftime(
                    "%H:%M:%S",
                    time.localtime(round_base[0] + idx * args.chunk))
            line = f"[{stamp}] {text}"
            if args.input_file:
                print(f"{line}   (处理 {elapsed:.1f}s)")
            else:
                lag = time.time() - (round_base[0] + (idx + 1) * args.chunk)
                print(f"{line}   (处理 {elapsed:.1f}s | 端到端延迟 {lag:.1f}s)")
            transcript.write(line + "\n")
            transcript.flush()
        try:
            chunk.unlink()          # 转完就删，避免磁盘堆积
        except OSError:
            pass

    attempt = 0
    ff = None
    try:
        # 外层循环：一圈 = 一次"拉流 → 转录"。断流/ffmpeg 退出都会回到这里重来，
        # 而不是像以前那样直接结束进程（网络抖一次就永久停摆，没人会发现）。
        while True:
            attempt += 1
            if attempt > 1:
                if args.input_file:
                    break                      # 本地文件没有"重连"这回事
                if args.max_restarts and (attempt - 1) > args.max_restarts:
                    print(f"[{time.strftime('%H:%M:%S')}] 已达 --max-restarts="
                          f"{args.max_restarts}，停止重连")
                    break
                stream_url = _wait_stream(args, cookies, out_path)
                if not stream_url:
                    break

            run_dir = work_dir / ("run_%02d" % attempt)
            done.clear()                       # 目录换了，文件名会重复，必须清空
            prev_tail = None                   # 新一轮音频和上一轮接不上
            ff = start_ffmpeg(stream_url, run_dir, args.chunk,
                              separate=args.separate,
                              is_local=bool(args.input_file))
            round_started = time.time()
            round_base[0] = round_started
            print(f"[{time.strftime('%H:%M:%S')}] 第 {attempt} 轮：ffmpeg 已拉起，"
                  f"开始转录（切片 {args.chunk}s）")
            reason = ""
            beat_last = time.time()

            while True:
                if args.duration and time.time() - started > args.duration:
                    break
                # 心跳：让下游看得出"采集还活着"，哪怕这段没人说话
                if time.time() - beat_last > 30:
                    _beat(out_path, "转录中（第 %d 轮）" % attempt)
                    beat_last = time.time()
                # 测试开关：跑一段后杀掉 ffmpeg，验证重连（正常使用为 0）
                if (args.kill_ffmpeg_after and ff.poll() is None
                        and time.time() - round_started > args.kill_ffmpeg_after):
                    print(f"[测试] 杀掉 ffmpeg 模拟断流"
                          f"（--kill-ffmpeg-after {args.kill_ffmpeg_after}）")
                    ff.kill()
                if ff.poll() is not None:
                    err = ff.stderr.read().decode("utf-8", "replace").strip()
                    reason = f"ffmpeg 退出 code={ff.returncode}"
                    print(f"\n[{time.strftime('%H:%M:%S')}] {reason}，"
                          f"处理剩余切片 ...")
                    if err:
                        print("ffmpeg 错误:", err[:500])
                    for left in sorted(run_dir.glob("chunk_*.wav")):
                        process_file(left)
                    break

                chunks = sorted(run_dir.glob("chunk_*.wav"))
                # 留最后一片，它可能还在写
                for chunk in chunks[:-1]:
                    process_file(chunk)
                time.sleep(0.5)

            if args.duration and time.time() - started > args.duration:
                break
            if args.input_file:
                break

            wait = min(30, 5 * attempt)
            transcript.write(
                "[%s] [缺口] %s；%d 秒后重连（这期间没有采集到音频）\n"
                % (time.strftime("%H:%M:%S"), reason, wait))
            transcript.flush()
            print(f"[{time.strftime('%H:%M:%S')}] [重连] {reason}，"
                  f"{wait}s 后重拉流 ...")
            time.sleep(wait)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，收尾中 ...")
    finally:
        transcript.close()
        if ff is not None and ff.poll() is None:
            ff.terminate()
            try:
                ff.wait(timeout=5)
            except subprocess.TimeoutExpired:
                ff.kill()

    shutil.rmtree(work_dir, ignore_errors=True)
    print("-" * 70)
    print(f"转录结束，文件: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
