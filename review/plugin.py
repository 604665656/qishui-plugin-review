# -*- coding: utf-8 -*-
"""试听打分 插件 · 页签 + 无界面任务。

契约：setup / build / run_task / summary / on_unload
PySide6 只在 build 内 import；重依赖（numpy/librosa/soundfile）在 engine 里惰性加载。
"""
from __future__ import annotations

import os

from . import engine_review as E


class ReviewPlugin(object):
    def __init__(self):
        self.ctx = None
        self._worker = None
        self._text = None
        self._status = None

    def setup(self, ctx):
        self.ctx = ctx
        try:
            ctx.on_account_switch(lambda uid: setattr(self, "_last", None))
        except Exception:
            pass

    # ------------------------------------------------------------ build
    def build(self, ctx):
        try:
            from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout,
                                           QLabel, QPushButton, QTextEdit)
            from PySide6.QtCore import QThread, Signal
            from PySide6.QtGui import QFont
        except Exception as e:
            from PySide6 import QtWidgets
            return QtWidgets.QLabel("无法加载界面依赖：%s" % e)
        try:
            return self._build_ui(ctx, QWidget, QVBoxLayout, QHBoxLayout,
                                  QLabel, QPushButton, QTextEdit, QFont,
                                  QThread, Signal)
        except Exception as e:
            try:
                lab = QLabel("「试听打分」界面加载失败：%s" % e)
                lab.setWordWrap(True)
                return lab
            except Exception:
                return QWidget()

    def _build_ui(self, ctx, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                  QPushButton, QTextEdit, QFont, QThread, Signal):
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        bar = QHBoxLayout()
        t = QLabel("试听打分")
        t.setFont(QFont("", 13))
        self._status = QLabel("")
        self._status.setStyleSheet("color:#888;")
        b1 = QPushButton("打分（歌词）")
        b2 = QPushButton("打分 + 音频体检")
        b3 = QPushButton("重新拉曲库")
        bar.addWidget(t)
        bar.addStretch(1)
        bar.addWidget(self._status)
        for b in (b1, b2, b3):
            bar.addWidget(b)
        lay.addLayout(bar)
        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setFont(QFont("Consolas", 10))
        lay.addWidget(self._text, 1)
        last = E.load_last(ctx)
        if last:
            self._render(last)
            self._set_status("上次 %s" % last.get("at", ""))
        else:
            self._text.setPlainText("（暂无打分记录。点「打分（歌词）」无需网络/浏览器即可跑；"
                                    "「音频体检」需已登录且装了 numpy/soundfile。）")

        def _run(refresh=False, with_audio=False):
            if self._worker is not None:
                try:
                    if self._worker.isRunning():
                        return
                except Exception:
                    pass
            self._set_status("正在打分…")
            W = _make_worker(QThread, Signal, ctx, refresh, with_audio)
            w = W()
            self._worker = w
            w.done.connect(lambda: self._done(w))
            w.start()

        b1.clicked.connect(lambda: _run())
        b2.clicked.connect(lambda: _run(False, True))
        b3.clicked.connect(lambda: _run(True))
        return root

    def _render(self, rep):
        lines = ["【试听打分】%s" % rep.get("at", ""),
                 "共 %s 首 ｜ 均分 %s" % (rep.get("scored"), rep.get("avg")), ""]
        for r in (rep.get("top") or [])[:20]:
            lines.append("  %-3s %3s 分  %s" % (r.get("grade"), r.get("score"),
                                                r.get("title") or "(无歌名)"))
            for p in (r.get("parts") or [])[:2]:
                lines.append("        %s %s/%s：%s" %
                             (p.get("dim"), p.get("got"), p.get("max"), p.get("why")))
            if r.get("health"):
                h = r["health"]
                lines.append("        音频：%s" %
                             (("峰值 %s dBFS / 削波 %s / 首空白 %ss" %
                               (h.get("peak_db"), h.get("clipped"),
                                h.get("head_silence"))) if h.get("ok")
                              else h.get("info", "")))
        d = rep.get("dups") or {}
        if d.get("ok") and d.get("pairs"):
            lines += ["", "【疑似重复音频】%d 对" % len(d["pairs"])]
            for a, b, sim in d["pairs"][:10]:
                lines.append("  %.4f  %s  <->  %s" %
                             (sim, os.path.basename(a), os.path.basename(b)))
        elif d.get("info"):
            lines += ["", "指纹去重：%s" % d["info"]]
        lines += ["", rep.get("note", "")]
        self._text.setPlainText("\n".join(str(x) for x in lines))

    def _set_status(self, s):
        if self._status is not None:
            try:
                self._status.setText(s)
            except Exception:
                pass

    def _done(self, w):
        rep = getattr(w, "result", None) or {}
        try:
            self._render(rep)
            self._set_status("完成：%s 首 / 均分 %s" %
                             (rep.get("scored", 0), rep.get("avg", 0)))
        except Exception:
            pass
        self._worker = None

    # ------------------------------------------------------------ 无界面
    def run_task(self, refresh=False, limit=0, with_audio=False):
        ctx = self.ctx
        if ctx is None:
            return {"ok": False, "errors": ["ctx 未初始化"]}
        try:
            return E.run_review(ctx, refresh=refresh, limit=limit,
                                with_audio=with_audio)
        except Exception as e:
            return {"ok": False, "errors": [str(e)]}

    def summary(self):
        ctx = self.ctx
        rep = E.load_last(ctx) if ctx is not None else None
        if not rep:
            return "试听打分：暂无记录"
        return "试听打分：%s 首 / 均分 %s" % (rep.get("scored", 0), rep.get("avg", 0))

    def on_unload(self):
        try:
            if self._worker is not None:
                self._worker.quit()
                self._worker.wait(1000)
        except Exception:
            pass
        self.ctx = None


def _make_worker(QThread, Signal, ctx, refresh, with_audio):
    class _Worker(QThread):
        done = Signal(object)

        def run(inner):
            try:
                r = E.run_review(ctx, refresh=refresh, with_audio=with_audio)
            except Exception as e:
                r = {"ok": False, "errors": [str(e)]}
            inner.result = r
            inner.done.emit(r)
    return _Worker
