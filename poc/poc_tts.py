# -*- coding: utf-8 -*-
"""
IndexTTS-2.5 CPU 推理 POC
1) Python 3.13 + torch 2.14.0+cpu 加载验证
2) 零样本声音克隆（英文参考音色 poc/test_audio.wav -> 中文语句）
3) 时长控制验证：API 为 duration_factor（0.5-2.0 相对系数，非目标秒数），
   采用两步法：先生成自然时长 T_nat，再换算 factor=目标/T_nat 后生成，
   另测 factor=0.5/2.0 边界线性度
4) 每次生成记录耗时与 psutil RSS
"""
import os
import sys
import time
import json

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rss_mb():
    import psutil
    return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024


def main():
    os.chdir(BASE)  # indextts 内部相对路径 ./checkpoints/hf_cache 需要
    os.environ.setdefault('HF_ENDPOINT', 'https://hf-mirror.com')
    os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')

    import torch
    import soundfile as sf
    import psutil
    import torchaudio

    # ---- 兼容补丁：torchaudio >= 2.9 的 save/load 需要 TorchCodec 后端 ----
    # 本机 torchaudio 2.11 无 TorchCodec，用 soundfile 实现等价替换。
    # （indextts 侧 utils/common.py 仅用到 16bit PCM WAV 写入，语义一致）
    def _sf_save(path, wav, sr, **kwargs):
        data = wav.detach().cpu().to(torch.float32).numpy()
        if data.ndim == 2:
            data = data.T  # (C, T) -> (T, C)
        sf.write(str(path), data, int(sr), subtype='PCM_16')

    def _sf_load(path, *args, **kwargs):
        data, sr = sf.read(str(path), dtype='float32', always_2d=True)
        return torch.from_numpy(data.T), sr

    torchaudio.save = _sf_save
    torchaudio.load = _sf_load
    # --------------------------------------------------------------------

    model_dir = os.path.join(BASE, 'models', 'tts', 'IndexTTS-2.5')
    cfg_path = os.path.join(model_dir, 'config.yaml')
    ref_audio = os.path.join(BASE, 'poc', 'test_audio.wav')
    out_dir = os.path.join(BASE, 'poc')
    text = "今天天气真不错，我们一起去公园散步吧。"

    assert os.path.isfile(cfg_path), f"missing {cfg_path}"
    assert os.path.isfile(ref_audio), f"missing {ref_audio}"
    info = sf.info(ref_audio)
    print(f"[env] python={sys.version.split()[0]} torch={torch.__version__} "
          f"threads={torch.get_num_threads()} physical_cores={psutil.cpu_count(logical=False)}", flush=True)
    print(f"[env] ref_audio: {info.duration:.2f}s sr={info.samplerate} ch={info.channels}", flush=True)

    # 辅助模型预热下载（w2v-bert-2.0 / MaskGCT codec / campplus / bigvgan），不混入推理计时
    t0 = time.time()
    from indextts.utils.model_download import ensure_models_available
    try:
        ensure_models_available(model_dir)
        print(f"[aux] models ready in {time.time()-t0:.1f}s rss={rss_mb():.0f}MB", flush=True)
    except Exception as e:
        print(f"[aux] FAILED: {e!r}", flush=True)
        sys.exit(1)

    from indextts.infer_v2_5 import IndexTTS2
    t0 = time.time()
    tts = IndexTTS2(cfg_path=cfg_path, model_dir=model_dir, use_bf16=False, device="cpu")
    load_s = time.time() - t0
    print(f"[load] IndexTTS2 loaded in {load_s:.1f}s rss={rss_mb():.0f}MB", flush=True)

    results = []

    def gen(tag, factor):
        out_path = os.path.join(out_dir, f"gen_{tag}.wav")
        t0 = time.time()
        tts.infer(spk_audio_prompt=ref_audio, text=text, lang="ZH",
                  output_path=out_path, duration_factor=factor)
        wall = time.time() - t0
        dur = sf.info(out_path).duration
        results.append(dict(tag=tag, duration_factor=factor,
                            actual_s=round(dur, 3), wall_s=round(wall, 1),
                            rss_mb=round(rss_mb())))
        print(f"[gen] {tag}: factor={factor} actual={dur:.2f}s wall={wall:.1f}s "
              f"rss={rss_mb():.0f}MB", flush=True)
        return dur

    # 1) 自然时长
    t_nat = gen("natural", 1.0)

    # 2) 目标 2.0s / 6.0s：由自然时长换算 factor（API 上限 0.5-2.0）
    f_short = max(0.5, min(2.0, 2.0 / t_nat))
    f_long = max(0.5, min(2.0, 6.0 / t_nat))
    d_short = gen("short_2s", round(f_short, 4))
    d_long = gen("long_6s", round(f_long, 4))

    # 3) 边界 factor 线性度
    d_f05 = gen("factor05", 0.5)
    d_f20 = gen("factor20", 2.0)

    print("\n===== SUMMARY =====", flush=True)
    for r in results:
        print(f"  {r['tag']:<10} factor={r['duration_factor']:<7} actual={r['actual_s']:>6.2f}s "
              f"wall={r['wall_s']:>6.1f}s rss={r['rss_mb']}MB", flush=True)
    print(f"natural duration      : {t_nat:.2f}s", flush=True)
    print(f"target 2.0s -> factor={f_short:.3f} actual={d_short:.2f}s err={d_short-2.0:+.2f}s", flush=True)
    print(f"target 6.0s -> factor={f_long:.3f} actual={d_long:.2f}s err={d_long-6.0:+.2f}s", flush=True)
    print(f"factor 0.5 ratio: {d_f05/t_nat:.3f} (expect 0.5)", flush=True)
    print(f"factor 2.0 ratio: {d_f20/t_nat:.3f} (expect 2.0)", flush=True)
    rtf = [r['wall_s']/r['actual_s'] for r in results]
    print(f"RTF range: {min(rtf):.2f} - {max(rtf):.2f} (wall/audio)", flush=True)

    with open(os.path.join(out_dir, 'poc_tts_results.json'), 'w', encoding='utf-8') as f:
        json.dump(dict(load_s=round(load_s, 1), text=text, results=results), f,
                  ensure_ascii=False, indent=2)
    print("[done]", flush=True)


if __name__ == '__main__':
    main()
