# -*- coding: utf-8 -*-
"""试听打分 · 业务逻辑（无 Qt、无主程序 import、重依赖一律惰性）。

移植来源（主程序仓库）：
  song_review.py → parse_lrc() / load_lyrics() / score() 的歌词层字段
  audio_fp.py    → md5_file() / fingerprint() / find_dups() 的指纹去重思路
  fetch_audio.py → download() 带 cookie 直连取试听音频
  audio_fix.py   → 首尾空白 / 削波 / 峰值 的诊断项

与主程序的差别（不假装）
------------------------
主程序 `score()` 调 `ai_detect.judge()`（本地 AI 判定服务），插件不背这个服务，
所以打分改成**可解释的启发式**：歌词密度、行数、重复率、时长、段落结构五项
各自给分并列出依据。分数是相对排序参考，不是平台审核结论。
音频客观特征（空白/削波/峰值）需要 numpy/soundfile + ffmpeg，缺依赖时返回
INFO 而不报错 —— 这条是插件能装在小 EXE 里的前提。

所有路径走 ctx.account_path / ctx.account_*，绝不自拼。
"""
from __future__ import annotations

import datetime
import hashlib
import os
import re

CACHE_NAME = "review_works_cache.json"
RESULT_NAME = "review_last.json"
AUDIO_DIR = "试听音频"
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg")

_TS = re.compile(r"^\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]")
_SECTION = re.compile(r"^\s*[\[\(（【][^\]\)）】]{1,40}[\]\)）】]\s*$")


# ---------------------------------------------------------------- 歌词
def parse_lrc(raw):
    """[(秒, 文本)]，按时间排序；无时间轴返回 []（主程序同名函数）。"""
    items = []
    for ln in (raw or "").splitlines():
        m = _TS.match(ln.strip())
        if not m:
            continue
        mm, ss, frac = m.group(1), m.group(2), m.group(3)
        t = int(mm) * 60 + int(ss) + (int(frac) / (10 ** len(frac)) if frac else 0)
        text = _TS.sub("", ln).strip()
        if text:
            items.append((round(t, 2), text))
    items.sort(key=lambda x: x[0])
    return items


def load_lyrics(work):
    """作品 dict → {text, lrc, lines}。歌词随 work-list / song-info 带下来。"""
    raw = work.get("lyrics") or work.get("Lyrics") or ""
    lrc = parse_lrc(raw)
    if lrc:
        text = "\n".join(t for _, t in lrc)
        raw = text
    lines = [x.strip() for x in re.sub(r"\[\d{1,2}:\d{2}.*?\]", "", raw).split("\n")
             if x.strip() and not _SECTION.match(x.strip())]
    return {"text": raw or "", "lrc": lrc or None, "lines": lines}


def score(work):
    """可解释的启发式打分，返回 0~100 + 每维依据。

    替代主程序 `ai_detect.judge()`（本地 AI 服务），仅为相对排序参考。
    """
    ly = load_lyrics(work)
    lines = ly["lines"]
    text = ly["text"]
    parts = []

    # 1) 歌词量（25）
    n = len(lines)
    v1 = 25 if n >= 16 else (18 if n >= 10 else (10 if n >= 4 else 0))
    parts.append({"dim": "歌词量", "got": v1, "max": 25,
                  "why": "%d 行有效歌词" % n})

    # 2) 句长节奏（20）：8~20 字最稳
    lens = [len(x) for x in lines] or [0]
    avg = sum(lens) / float(len(lens))
    v2 = 20 if 8 <= avg <= 20 else (12 if 5 <= avg <= 26 else 5)
    parts.append({"dim": "句长节奏", "got": v2, "max": 20,
                  "why": "平均句长 %.1f 字" % avg})

    # 3) 重复率（25）：整句重复越少越好
    uniq = len(set(lines))
    rep = 1.0 - (uniq / float(len(lines))) if lines else 1.0
    v3 = int(round(25 * (1.0 - min(rep, 0.8))))
    parts.append({"dim": "重复率", "got": v3, "max": 25,
                  "why": "%d/%d 句唯一（重复 %.0f%%）" % (uniq, len(lines), rep * 100)})

    # 4) 结构（15）：有段落标注或时间轴说明结构完整
    has_struct = bool(ly["lrc"]) or bool(re.search(r"[\[\(（]?(副歌|主歌|chorus|verse)",
                                                  text, re.I))
    v4 = 15 if has_struct else 6
    parts.append({"dim": "结构", "got": v4, "max": 15,
                  "why": "有 LRC 时间轴/段落标注" if has_struct else "纯文本，无段落结构"})

    # 5) 时长（15）：60~300 秒
    dur = int(work.get("duration") or 0)
    v5 = 15 if 60 <= dur <= 300 else (8 if dur else 5)
    parts.append({"dim": "时长", "got": v5, "max": 15,
                  "why": ("%d 秒" % dur) if dur else "未知"})

    total = sum(p["got"] for p in parts)
    return {
        "title": work.get("title") or "",
        "song_id": work.get("song_id") or "",
        "score": total,
        "grade": "A" if total >= 80 else ("B" if total >= 65 else
                 ("C" if total >= 45 else "D")),
        "parts": parts,
        "lyric_text": text,
        "duration": dur,
        "note": "启发式相对排序参考，非平台审核结论（未接本地 AI 判定服务）",
    }


# ---------------------------------------------------------------- 音频
def md5_file(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def audio_path(ctx, work):
    """试听音频落点：账号目录/试听音频/<清洗后的歌名>.mp3。"""
    safe = re.sub(r"[\\/:*?\"<>|\r\n]", "_", (work.get("title") or "untitled")).strip()
    return ctx.account_path(os.path.join(AUDIO_DIR, safe[:60] + ".mp3"))


def _clip_url(ctx, work):
    """song_info → clips[].clipUrl（主程序 fetch_audio 用的是同一条直链）。"""
    sid = work.get("song_id") or work.get("SongID")
    if not sid:
        return ""
    info = ctx.api.song_info(sid)
    data = (info or {}).get("data") or {}
    for c in (data.get("clips") or []):
        u = c.get("clipUrl") or c.get("url") or c.get("playUrl")
        if u:
            return u
    return data.get("audioUrl") or data.get("playUrl") or ""


def fetch_audio(ctx, work, force=False):
    """带 cookie 直连下载试听音频。返回 {ok,path,error}。"""
    dest = audio_path(ctx, work)
    if os.path.isfile(dest) and not force and os.path.getsize(dest) > 1024:
        return {"ok": True, "path": dest, "cached": True}
    url = _clip_url(ctx, work)
    if not url:
        return {"ok": False, "path": "", "error": "该作品无音频直链（clips 为空）"}
    try:
        import requests
        ck = ctx.api.cookies() if hasattr(ctx.api, "cookies") else ""
        s = requests.Session()
        s.trust_env = False
        r = s.get(url, headers={"Cookie": ck, "User-Agent": "Mozilla/5.0"},
                  timeout=120, proxies={"http": None, "https": None})
        r.raise_for_status()
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            f.write(r.content)
        if os.path.getsize(tmp) < 1024:
            os.remove(tmp)
            return {"ok": False, "path": "", "error": "下载内容过小，疑似非音频"}
        os.replace(tmp, dest)
        return {"ok": True, "path": dest, "size": os.path.getsize(dest)}
    except Exception as e:
        return {"ok": False, "path": "", "error": str(e)[:160]}


def fingerprint(path):
    """音频指纹向量。numpy/librosa 惰性 import；缺依赖返回 {error}，不抛。"""
    try:
        import numpy as np          # noqa: F401
        import librosa
    except Exception as e:
        return {"error": "需要 numpy + librosa（%s）" % e}
    try:
        y, sr = librosa.load(path, sr=16000, mono=True, duration=60)
        if y.size < sr * 3:
            return {"error": "音频短于 3 秒"}
        mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=20)
        v = mfcc.mean(axis=1)
        v = v / (np.linalg.norm(v) + 1e-9)
        return {"v": [float(x) for x in v], "dur": float(len(y) / sr),
                "md5": md5_file(path)}
    except Exception as e:
        return {"error": "解码失败：%s" % str(e)[:120]}


def audio_health(path):
    """首尾空白 / 峰值 / 削波。numpy+soundfile 惰性；缺依赖返回 info。"""
    try:
        import numpy as np
        import soundfile as sf
    except Exception as e:
        return {"ok": False, "info": "需要 numpy + soundfile（%s）" % e}
    try:
        y, sr = sf.read(path, always_2d=False)
        if getattr(y, "ndim", 1) > 1:
            y = y.mean(axis=1)
        peak = float(np.max(np.abs(y))) if y.size else 0.0
        clipped = float(np.mean(np.abs(y) >= 0.999)) if y.size else 0.0
        win = max(1, int(sr * 0.02))
        energy = np.array([np.sqrt(np.mean(y[i:i + win] ** 2))
                           for i in range(0, max(0, len(y) - win), win)])
        thr = max(1e-4, peak * 0.01)
        silent = energy < thr
        head = 0.0
        for i, s in enumerate(silent[:int(10 * sr / win)]):
            if not s:
                break
            head += win / float(sr)
        return {"ok": True, "peak_db": round(20 * __import__("math").log10(peak + 1e-9), 1),
                "clipped": clipped > 0.0001, "head_silence": round(head, 2),
                "duration": round(len(y) / float(sr), 1)}
    except Exception as e:
        return {"ok": False, "info": "音频分析失败：%s" % str(e)[:120]}


# ---------------------------------------------------------------- 批量
def _slim(w):
    ii = w.get("IssueInfo") or {}
    return {
        "title": (w.get("Title") or "").strip(),
        "vid": (w.get("VID") or "").strip(),
        "song_id": str(w.get("SongID") or ""),
        "duration": int(w.get("Duration") or 0),
        "lyrics": w.get("Lyrics") or "",
        "issued": bool(ii.get("Issued")),
    }


def collect_works(ctx, refresh=False, max_pages=8):
    if not refresh:
        c = ctx.account_load_json(CACHE_NAME)
        if isinstance(c, dict) and c.get("works"):
            return c["works"]
    works = []
    try:
        for page in range(1, max_pages + 1):
            r = ctx.api.work_list(page=page, size=200)
            rows = r.get("works") or []
            works.extend(_slim(w) for w in rows)
            total = int(r.get("total") or 0)
            if not rows or (total and len(works) >= total):
                break
    except Exception as e:
        ctx.warn("拉取作品失败：%s" % e)
        works = (ctx.account_load_json(CACHE_NAME) or {}).get("works") or []
    ctx.account_save_json(CACHE_NAME, {
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "works": works})
    return works


def find_dups(paths):
    """按 md5 + 指纹余弦相似度找重复音频。返回 [[pathA,pathB,sim],...]。"""
    fps = {}
    for p in paths:
        if not os.path.isfile(p):
            continue
        f = fingerprint(p)
        if f.get("v"):
            fps[p] = f
    if list(fps.values()) and list(fps.values())[0].get("error"):
        return {"ok": False, "info": list(fps.values())[0]["error"], "pairs": []}
    out = []
    keys = list(fps)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if fps[a].get("md5") and fps[a]["md5"] == fps[b].get("md5"):
                out.append([a, b, 1.0])
                continue
            va, vb = fps[a]["v"], fps[b]["v"]
            sim = float(sum(x * y for x, y in zip(va, vb)))
            if sim >= 0.98:
                out.append([a, b, round(sim, 4)])
    return {"ok": True, "checked": len(fps), "pairs": out}


def run_review(ctx, refresh=False, limit=0, with_audio=False):
    """批量打分（+ 可选音频体检/去重），返回结构化报告。"""
    works = collect_works(ctx, refresh=refresh)
    if limit:
        works = works[:int(limit)]
    rows = []
    for w in works:
        try:
            r = score(w)
            if with_audio:
                f = fetch_audio(ctx, w)
                r["audio"] = f
                if f.get("ok"):
                    r["health"] = audio_health(f["path"])
            rows.append(r)
        except Exception as e:
            rows.append({"title": w.get("title"), "score": 0, "grade": "D",
                         "parts": [], "error": str(e)})
    rows.sort(key=lambda x: x.get("score", 0), reverse=True)
    out = {
        "ok": True,
        "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scored": len(rows),
        "avg": round(sum(x.get("score", 0) for x in rows) / len(rows), 1) if rows else 0,
        "top": rows[:20], "rows": rows, "errors": [],
        "note": "启发式打分（未接主程序本地 AI 判定服务）",
    }
    if with_audio:
        paths = [r["audio"]["path"] for r in rows
                 if (r.get("audio") or {}).get("ok")]
        out["dups"] = find_dups(paths[:60])
    ctx.account_save_json(RESULT_NAME, out)
    return out


def load_last(ctx):
    return ctx.account_load_json(RESULT_NAME)
