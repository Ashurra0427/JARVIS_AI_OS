"""
interface/panels/chat_panel.py
────────────────────────────────
Main chat area:
  • Chat message history (scrollable), generously spaced
  • User bubbles (right-aligned, dark card) and JARVIS replies
    (left-aligned, full card with avatar + cyan header) — both now
    width-capped for readability instead of only the user bubble being
    capped while JARVIS replies stretched edge-to-edge on wide windows
  • Copy-to-clipboard button on every message bubble (Phase 9)
  • Thinking indicator
  • Multi-line, auto-growing input bar (Phase 9): Enter sends,
    Shift+Enter inserts a newline — replaces the old single-line
    QLineEdit, which made it impossible to compose a multi-line message
    at all and made the "Ctrl+Enter to send" shortcut redundant with
    plain Enter already submitting.
  • Real "clear chat" support (Phase 9) — the Ctrl+Shift+C shortcut
    previously claimed to clear the chat but was wired to
    hide_thinking() instead, and the panel had no clearing capability
    at all to wire it to. See ChatPanel.clear_messages().
  • P-20: Markdown rendering for JARVIS replies (bold, italic, code, headings, lists)
"""
from __future__ import annotations

import re
import time
from typing import Optional

from PySide6.QtCore import Qt, Signal, Slot, QTimer, QSize, QEvent
from PySide6.QtGui import (
    QColor, QFont, QPainter, QBrush, QPen,
    QTextCursor, QKeySequence, QShortcut,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QScrollArea, QFrame, QLineEdit, QPushButton,
    QSizePolicy, QSpacerItem, QTextEdit, QFileDialog,
    QApplication,
)


# ── Markdown → HTML converter (P-20) ─────────────────────────────────────────

def _md_to_html(text: str) -> str:
    """
    Lightweight Markdown → HTML for Qt RichText.
    Handles: headings, bold, italic, inline code, code blocks, bullet lists.
    """
    # Escape existing HTML entities first
    text = text.replace("&", "&amp;").replace("<br>", "\n")  # preserve existing <br>
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("&lt;br&gt;", "\n")  # restore escaped newlines

    # Code blocks (``` … ```) — convert before inline processing
    def _code_block(m: re.Match) -> str:
        lang = m.group(1) or ""
        code = m.group(2)
        lang_label = f'<span style="color:#7a8fa8;font-size:9px">{lang}</span><br>' if lang else ""
        return (
            f'<div style="background:#0b1929;border:1px solid #1e3352;border-radius:8px;'
            f'padding:12px 16px;margin:8px 0;font-family:Cascadia Code,Consolas,monospace;'
            f'font-size:11px;color:#e2eaf6;white-space:pre">{lang_label}{code}</div>'
        )
    text = re.sub(r"```(\w*)\n?(.*?)```", _code_block, text, flags=re.DOTALL)

    # Inline code
    text = re.sub(r"`([^`]+)`",
                  r'<code style="background:#0b1929;color:#00d4ff;border-radius:3px;'
                  r'padding:1px 5px;font-family:monospace;font-size:11px">\1</code>', text)

    # Headings (### ## #)
    text = re.sub(r"^### (.+)$",
                  r'<b style="color:#e2eaf6;font-size:13px">\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r"^## (.+)$",
                  r'<b style="color:#e2eaf6;font-size:14px">\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r"^# (.+)$",
                  r'<b style="color:#00d4ff;font-size:15px">\1</b>', text, flags=re.MULTILINE)

    # Bold and italic
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text)
    text = re.sub(r"\*\*(.+?)\*\*",     r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*",         r"<i>\1</i>", text)
    text = re.sub(r"__(.+?)__",         r"<b>\1</b>", text)
    text = re.sub(r"_(.+?)_",           r"<i>\1</i>", text)

    # Bullet lists (- item or * item)
    def _list_block(m: re.Match) -> str:
        items = re.findall(r"^[*\-]\s+(.+)$", m.group(0), flags=re.MULTILINE)
        rows = "".join(
            f'<li style="margin:3px 0;color:#c8d8e8">{it}</li>' for it in items
        )
        return f'<ul style="margin:6px 0 6px 20px;padding:0">{rows}</ul>'
    text = re.sub(r"(^[*\-]\s+.+$(\n[*\-]\s+.+$)*)", _list_block,
                  text, flags=re.MULTILINE)

    # Numbered lists
    def _num_list_block(m: re.Match) -> str:
        items = re.findall(r"^\d+\.\s+(.+)$", m.group(0), flags=re.MULTILINE)
        rows = "".join(
            f'<li style="margin:3px 0;color:#c8d8e8">{it}</li>' for it in items
        )
        return f'<ol style="margin:6px 0 6px 20px;padding:0">{rows}</ol>'
    text = re.sub(r"(^\d+\.\s+.+$(\n\d+\.\s+.+$)*)", _num_list_block,
                  text, flags=re.MULTILINE)

    # Horizontal rule
    text = re.sub(r"^---+$",
                  '<hr style="border:none;border-top:1px solid #1e3352;margin:10px 0">',
                  text, flags=re.MULTILINE)

    # Newlines → <br>
    text = text.replace("\n", "<br>")

    return text


from interface.themes.palette import (
    BG_WINDOW, BG_SURFACE, BG_ELEVATED, BG_CARD,
    BORDER_DEFAULT, BORDER_ACCENT,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, TEXT_ACCENT,
    ACCENT_CYAN, ACCENT_GREEN, ACCENT_BLUE,
    q, clamp,
    CHAT_BUBBLE_MIN_W, CHAT_BUBBLE_MAX_W, CHAT_BUBBLE_RATIO,
    CHAT_BUBBLE_JARVIS_MAX_W, CHAT_BUBBLE_JARVIS_RATIO,
)


# ── Copy-to-clipboard button (Phase 9) ────────────────────────────────────────
# Shared by both bubble types so the hover/flash behavior is identical
# everywhere a message can be copied from.

_COPY_BTN_QSS = f"""
    QPushButton {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: 5px;
        color: {TEXT_MUTED};
        font-size: 12px;
        padding: 0px;
    }}
    QPushButton:hover {{
        background: {BG_ELEVATED};
        border-color: {BORDER_ACCENT};
        color: {ACCENT_CYAN};
    }}
"""

_COPY_BTN_QSS_DONE = f"""
    QPushButton {{
        background: transparent;
        border: 1px solid {ACCENT_GREEN};
        border-radius: 5px;
        color: {ACCENT_GREEN};
        font-size: 12px;
        padding: 0px;
    }}
"""


def _make_copy_button(owner: "MessageBubble") -> QPushButton:
    """Build a small copy-to-clipboard icon button bound to `owner`.
    `owner` must implement `_raw_text` (str attribute)."""
    btn = QPushButton("⧉")
    btn.setFixedSize(24, 24)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setToolTip("Copy message")
    btn.setStyleSheet(_COPY_BTN_QSS)

    def _copy() -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(getattr(owner, "_raw_text", ""))
        btn.setText("✓")
        btn.setToolTip("Copied!")
        btn.setStyleSheet(_COPY_BTN_QSS_DONE)
        QTimer.singleShot(1200, _reset)

    def _reset() -> None:
        try:
            btn.setText("⧉")
            btn.setToolTip("Copy message")
            btn.setStyleSheet(_COPY_BTN_QSS)
        except RuntimeError:
            pass  # the underlying Qt widget was already deleted (e.g. chat cleared)

    btn.clicked.connect(_copy)
    return btn


# ── Individual message bubble ─────────────────────────────────────────────────

class MessageBubble(QWidget):
    """Single chat message."""

    def __init__(self, text: str, sender: str = "user",
                 timestamp: str = "", provider: str = "",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._sender = sender
        self._raw_text = text
        self._build(text, sender, timestamp, provider)

    def _build(self, text: str, sender: str,
               timestamp: str, provider: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 6, 10, 6)
        root.setSpacing(4)

        if sender == "user":
            self._build_user(root, text, timestamp)
        else:
            self._build_jarvis(root, text, timestamp, provider)

    def _build_user(self, layout, text: str, ts: str) -> None:
        # Right-aligned user bubble
        outer = QHBoxLayout()
        outer.addStretch(1)

        bubble = QFrame()
        # Responsive cap instead of a fixed 520px — see set_bubble_max_width().
        # Seeded here with the smallest sane value; ChatPanel widens it to
        # match the actual window size right after layout.
        bubble.setMaximumWidth(CHAT_BUBBLE_MIN_W)
        self._frame = bubble
        bubble.setStyleSheet(f"""
            QFrame {{
                background: {BG_ELEVATED};
                border: 1px solid {BORDER_ACCENT};
                border-radius: 14px;
                border-bottom-right-radius: 4px;
            }}
        """)
        blay = QVBoxLayout(bubble)
        blay.setContentsMargins(18, 12, 14, 12)
        blay.setSpacing(6)

        hrow = QHBoxLayout()
        hrow.setSpacing(6)
        you = QLabel("You")
        you.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 11px; font-weight: 700; letter-spacing: 0.5px;")
        hrow.addWidget(you)
        hrow.addStretch(1)
        hrow.addWidget(_make_copy_button(self))
        blay.addLayout(hrow)

        msg = QLabel(text)
        msg.setWordWrap(True)
        msg.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        msg.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12.5px; line-height: 1.6; background: transparent;")
        blay.addWidget(msg)

        ts_row = QHBoxLayout()
        ts_row.addStretch()
        ts_lbl = QLabel(f"{ts} ✓✓" if ts else "")
        ts_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px;")
        ts_row.addWidget(ts_lbl)
        blay.addLayout(ts_row)

        outer.addWidget(bubble)
        layout.addLayout(outer)

    def _build_jarvis(self, layout, text: str, ts: str, provider: str) -> None:
        # Left-aligned JARVIS reply — now width-capped like the user
        # bubble (Phase 9 fix: this used to have NO cap at all and would
        # stretch edge-to-edge across the whole chat column on a wide
        # window, which read poorly for anything but short replies).
        outer = QHBoxLayout()

        bubble = QFrame()
        bubble.setMaximumWidth(CHAT_BUBBLE_MIN_W)
        self._frame = bubble
        bubble.setStyleSheet(f"""
            QFrame {{
                background: {BG_CARD};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 14px;
                border-top-left-radius: 4px;
            }}
        """)
        blay = QVBoxLayout(bubble)
        blay.setContentsMargins(20, 16, 18, 14)
        blay.setSpacing(8)

        # Header row
        hrow = QHBoxLayout()
        hrow.setSpacing(8)
        avatar = QLabel("🔷")
        avatar.setStyleSheet(f"color: {ACCENT_CYAN}; font-size: 14px;")
        hrow.addWidget(avatar)
        name = QLabel("JARVIS")
        name.setStyleSheet(f"color: {ACCENT_CYAN}; font-size: 12px; font-weight: 700; letter-spacing: 1px;")
        hrow.addWidget(name)
        if provider:
            prov_lbl = QLabel(f"via {provider}")
            prov_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px;")
            hrow.addWidget(prov_lbl)
        hrow.addStretch()
        hrow.addWidget(_make_copy_button(self))
        blay.addLayout(hrow)

        # Message text — render Markdown (P-20)
        html_text = _md_to_html(text)
        msg = QLabel()
        msg.setWordWrap(True)
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setOpenExternalLinks(True)
        msg.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        msg.setText(html_text)
        msg.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 12.5px; line-height: 1.7; background: transparent;")
        blay.addWidget(msg)
        self._msg_label = msg

        if ts:
            ts_lbl = QLabel(ts)
            ts_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px;")
            ts_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            blay.addWidget(ts_lbl)

        outer.addWidget(bubble)
        outer.addStretch(1)
        layout.addLayout(outer)

    def set_max_width(self, width: int) -> None:
        """Adjust this bubble's max width in response to a chat panel
        resize. Applies to both user and JARVIS bubbles (Phase 9 fix —
        previously only user bubbles had a cap to adjust here at all)."""
        frame = getattr(self, "_frame", None)
        if frame is not None:
            frame.setMaximumWidth(width)

    def update_text(self, text: str) -> None:
        """Re-render this bubble's body text. Used by the voice-sync reveal
        in main_window.py to progressively fill in the reply in step with
        TTS playback, rather than dumping the full reply in instantly.
        Also keeps _raw_text (what the copy button copies) in sync."""
        self._raw_text = text
        if self._sender != "jarvis" or not hasattr(self, "_msg_label"):
            return
        self._msg_label.setText(_md_to_html(text))


# ── Thinking indicator ────────────────────────────────────────────────────────

class ThinkingIndicator(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._dots = 0
        self._visible = False
        self.setFixedHeight(44)
        self.hide()

        t = QTimer(self)
        t.timeout.connect(self._tick)
        t.start(400)

    def _tick(self):
        self._dots = (self._dots + 1) % 4
        self.update()

    def show_thinking(self, agent: str = "JARVIS"):
        self._agent = agent
        self._visible = True
        self.show()

    def hide_thinking(self):
        self._visible = False
        self.hide()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QColor(TEXT_ACCENT))
        f = QFont("Rajdhani", 11)
        p.setFont(f)
        agent = getattr(self, "_agent", "JARVIS")
        dots = "●" * (self._dots + 1) + "○" * (3 - self._dots)
        p.drawText(28, 0, self.width(), self.height(),
                   Qt.AlignmentFlag.AlignVCenter,
                   f"{agent} is thinking {dots}")
        p.end()


# ── Auto-growing multi-line input (Phase 9) ───────────────────────────────────

class _ChatTextEdit(QTextEdit):
    """
    Multi-line chat input that grows from one line up to a max height
    then scrolls internally, with chat-app-standard key handling:
    Enter submits, Shift+Enter inserts a newline.

    Replaces the previous QLineEdit, which made composing a multi-line
    message impossible and made the separate "Ctrl+Enter to send"
    global shortcut redundant (plain Enter already submitted a
    single-line QLineEdit).
    """

    submit_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setTabChangesFocus(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setPlaceholderText("Message JARVIS…  (Enter to send, Shift+Enter for a new line)")
        self._min_h = 44
        self._max_h = 168
        self.setFixedHeight(self._min_h)
        self.document().contentsChanged.connect(self._auto_grow)

    def _auto_grow(self) -> None:
        # Explicitly sync the document's wrap width to the current
        # viewport before measuring — QTextEdit doesn't always keep
        # document().size() in sync with the actual rendered width on
        # its own (e.g. before the widget has been shown/resized once),
        # which previously made the very first grow computation silently
        # measure against a stale/zero width.
        self.document().setTextWidth(self.viewport().width())
        doc_h = int(self.document().size().height()) + 20
        new_h = max(self._min_h, min(self._max_h, doc_h))
        if new_h != self.height():
            self.setFixedHeight(new_h)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._auto_grow()

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)  # Shift+Enter -> newline
            else:
                self.submit_requested.emit()
            return
        super().keyPressEvent(event)


# ── Chat input bar ────────────────────────────────────────────────────────────

class ChatInputBar(QWidget):
    message_submitted = Signal(str)
    files_attached    = Signal(list)   # list[str] of file paths
    mic_clicked       = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._attached_files: list[str] = []
        self.setStyleSheet(f"""
            QWidget {{
                background: {BG_SURFACE};
                border-top: 1px solid {BORDER_DEFAULT};
            }}
        """)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Attachment chip row (hidden until files are added)
        self._chip_row = QWidget()
        self._chip_row.setStyleSheet("background: transparent;")
        self._chip_layout = QHBoxLayout(self._chip_row)
        self._chip_layout.setContentsMargins(20, 10, 20, 0)
        self._chip_layout.setSpacing(6)
        self._chip_row.setVisible(False)
        root.addWidget(self._chip_row)

        bar = QWidget()
        bar.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(20, 12, 20, 12)
        lay.setSpacing(10)

        # Input field — auto-growing multi-line (Phase 9)
        self._input = _ChatTextEdit()
        self._input.setStyleSheet(f"""
            QTextEdit {{
                background: {BG_ELEVATED};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 14px;
                color: {TEXT_PRIMARY};
                font-size: 12.5px;
                padding: 10px 16px;
            }}
            QTextEdit:focus {{
                border-color: {ACCENT_CYAN};
            }}
        """)
        self._input.submit_requested.connect(self._submit)
        lay.addWidget(self._input, 1)

        # Attachment
        attach = self._icon_btn("📎", "Attach files")
        attach.setFixedSize(42, 42)
        attach.clicked.connect(self._open_file_dialog)
        lay.addWidget(attach, 0, Qt.AlignmentFlag.AlignBottom)

        # Mic
        mic = self._icon_btn("🎤", "Voice")
        mic.setFixedSize(42, 42)
        mic.clicked.connect(self.mic_clicked)
        lay.addWidget(mic, 0, Qt.AlignmentFlag.AlignBottom)

        # Send
        send = QPushButton("➤")
        send.setToolTip("Send message (Enter)")
        send.setFixedSize(42, 42)
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT_CYAN};
                border: none;
                border-radius: 21px;
                color: #000;
                font-size: 16px;
                font-weight: 700;
            }}
            QPushButton:hover {{ background: #00a8e0; }}
            QPushButton:pressed {{ background: #007aaa; }}
        """)
        send.clicked.connect(self._submit)
        lay.addWidget(send, 0, Qt.AlignmentFlag.AlignBottom)

        root.addWidget(bar)

    def _icon_btn(self, icon: str, tip: str) -> QPushButton:
        btn = QPushButton(icon)
        btn.setToolTip(tip)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_ELEVATED};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 21px;
                color: {TEXT_SECONDARY};
                font-size: 14px;
            }}
            QPushButton:hover {{
                border-color: {ACCENT_CYAN};
                color: {ACCENT_CYAN};
            }}
        """)
        return btn

    # ── Attachments ───────────────────────────────────────────────────

    def _open_file_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add files",
            "",
            "All Files (*);;Images (*.png *.jpg *.jpeg *.gif *.webp);;"
            "Documents (*.pdf *.docx *.txt *.md);;"
            "Code (*.py *.js *.ts *.json *.yaml *.yml)",
        )
        if not paths:
            return
        for p in paths:
            if p not in self._attached_files:
                self._attached_files.append(p)
        self._refresh_chips()
        self.files_attached.emit(list(self._attached_files))

    def _refresh_chips(self):
        # Clear existing chips
        while self._chip_layout.count():
            item = self._chip_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not self._attached_files:
            self._chip_row.setVisible(False)
            return

        self._chip_row.setVisible(True)
        for path in self._attached_files:
            self._chip_layout.addWidget(self._make_chip(path))
        self._chip_layout.addStretch(1)

    def _make_chip(self, path: str) -> QWidget:
        import os
        name = os.path.basename(path)
        chip = QWidget()
        chip.setStyleSheet(f"""
            QWidget {{
                background: {BG_ELEVATED};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 12px;
            }}
        """)
        lay = QHBoxLayout(chip)
        lay.setContentsMargins(10, 4, 6, 4)
        lay.setSpacing(6)

        label = QLabel(f"📄 {name}")
        label.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 10px; background: transparent; border: none;")
        label.setToolTip(path)
        lay.addWidget(label)

        remove = QPushButton("✕")
        remove.setFixedSize(18, 18)
        remove.setCursor(Qt.CursorShape.PointingHandCursor)
        remove.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                color: {TEXT_MUTED};
                font-size: 10px;
            }}
            QPushButton:hover {{ color: {ACCENT_CYAN}; }}
        """)
        remove.clicked.connect(lambda: self._remove_file(path))
        lay.addWidget(remove)
        return chip

    def _remove_file(self, path: str):
        if path in self._attached_files:
            self._attached_files.remove(path)
        self._refresh_chips()
        self.files_attached.emit(list(self._attached_files))

    def attached_files(self) -> list[str]:
        return list(self._attached_files)

    def clear_attachments(self):
        self._attached_files.clear()
        self._refresh_chips()

    # ── Submit ────────────────────────────────────────────────────────

    def _submit(self):
        text = self._input.toPlainText().strip()
        if text or self._attached_files:
            self.message_submitted.emit(text)
            self._input.clear()
            self.clear_attachments()

    def submit(self) -> None:
        """Public trigger for external callers (e.g. main_window's global
        Ctrl+Enter shortcut) instead of reaching into private internals."""
        self._submit()

    def focus_input(self) -> None:
        self._input.setFocus()

    def text(self) -> str:
        return self._input.toPlainText()

    def set_text(self, text: str):
        self._input.setPlainText(text)
        cursor = self._input.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._input.setTextCursor(cursor)


# ── Main chat panel ───────────────────────────────────────────────────────────

class ChatPanel(QWidget):
    """Full chat panel: header + scrollable messages + input."""

    message_submitted = Signal(str)
    files_attached     = Signal(list)
    mic_clicked       = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatPanel")
        self.setStyleSheet(f"background: {BG_WINDOW};")
        self._bubbles: list["MessageBubble"] = []
        self._build()
        self._add_welcome()

    def _current_bubble_max_width(self) -> int:
        return clamp(self.width() * CHAT_BUBBLE_RATIO, CHAT_BUBBLE_MIN_W, CHAT_BUBBLE_MAX_W)

    def _current_jarvis_max_width(self) -> int:
        return clamp(self.width() * CHAT_BUBBLE_JARVIS_RATIO, CHAT_BUBBLE_MIN_W, CHAT_BUBBLE_JARVIS_MAX_W)

    def _target_width_for(self, bubble: "MessageBubble") -> int:
        return (
            self._current_jarvis_max_width()
            if bubble._sender == "jarvis"
            else self._current_bubble_max_width()
        )

    def _register_bubble(self, bubble: "MessageBubble") -> None:
        self._bubbles.append(bubble)
        bubble.set_max_width(self._target_width_for(bubble))

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        for bubble in self._bubbles:
            bubble.set_max_width(self._target_width_for(bubble))

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        hdr = QWidget()
        hdr.setFixedHeight(56)
        hdr.setStyleSheet(f"background: {BG_SURFACE}; border-bottom: 1px solid {BORDER_DEFAULT};")
        hlay = QHBoxLayout(hdr)
        hlay.setContentsMargins(28, 0, 20, 0)
        hlay.setSpacing(10)
        title = QLabel("Chat with JARVIS")
        title.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 15px; font-weight: 700; letter-spacing: 0.3px;")
        hlay.addWidget(title)
        hlay.addStretch()

        clear_btn = QPushButton("🗑  Clear chat")
        clear_btn.setToolTip("Clear chat history (Ctrl+Shift+C)")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 8px;
                color: {TEXT_SECONDARY};
                font-size: 11px;
                padding: 6px 12px;
            }}
            QPushButton:hover {{
                border-color: {ACCENT_CYAN};
                color: {ACCENT_CYAN};
            }}
        """)
        clear_btn.clicked.connect(self.clear_messages)
        hlay.addWidget(clear_btn)
        root.addWidget(hdr)

        # Scroll area for messages
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(f"background: {BG_WINDOW};")

        self._msg_container = QWidget()
        self._msg_container.setStyleSheet(f"background: {BG_WINDOW};")
        self._msg_layout = QVBoxLayout(self._msg_container)
        # Generous, professional breathing room around the message column
        # (Phase 9: was 12px all round with 8px between messages — cramped
        # on anything but a small window).
        self._msg_layout.setContentsMargins(28, 20, 28, 20)
        self._msg_layout.setSpacing(16)
        # Top spacer pushes messages to the bottom when there are few of them
        self._msg_layout.addStretch(1)
        # Thinking indicator always lives at the very end
        self._thinking = ThinkingIndicator()
        self._msg_layout.addWidget(self._thinking)
        # Track message count so we can insert at the right index
        self._msg_count = 0

        self._scroll.setWidget(self._msg_container)
        root.addWidget(self._scroll, 1)

        # Input bar
        self._input_bar = ChatInputBar()
        self._input_bar.message_submitted.connect(self.message_submitted)
        self._input_bar.mic_clicked.connect(self.mic_clicked)
        self._input_bar.files_attached.connect(self.files_attached)
        root.addWidget(self._input_bar)

    def _add_welcome(self) -> None:
        """Insert a contextual welcome message bubble on first load."""
        import datetime
        hour = datetime.datetime.now().hour
        if hour < 12:
            greeting = "Good morning"
        elif hour < 17:
            greeting = "Good afternoon"
        else:
            greeting = "Good evening"
        msg = (
            f"{greeting}! I'm JARVIS — your AI operating system. "
            "I can help you with research, code, automation, file management, and more. "
            "How can I assist you today?"
        )
        self.add_jarvis_message(msg, provider="JARVIS")

    def _ts(self) -> str:
        return time.strftime("%I:%M %p")

    def _scroll_to_bottom(self):
        QTimer.singleShot(50, lambda: self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum()
        ))

    # ── Public API ────────────────────────────────────────────────────

    def add_user_message(self, text: str) -> None:
        """Insert a user message bubble."""
        bubble = MessageBubble(text, sender="user", timestamp=self._ts())
        # Layout is: [stretch, msg0, msg1, ..., thinking]
        # Insert before thinking (last item), i.e. at count-1
        self._msg_layout.insertWidget(self._msg_layout.count() - 1, bubble)
        self._register_bubble(bubble)
        self._msg_count += 1
        self._scroll_to_bottom()

    def add_jarvis_message(self, text: str, provider: str = "") -> None:
        """Insert a JARVIS reply bubble and hide thinking."""
        self._thinking.hide_thinking()
        bubble = MessageBubble(text, sender="jarvis",
                               timestamp=self._ts(), provider=provider)
        # Layout: [stretch, msg0, msg1, ..., thinking]
        # Insert before thinking (last item)
        self._msg_layout.insertWidget(self._msg_layout.count() - 1, bubble)
        self._register_bubble(bubble)
        if not hasattr(self, '_msg_count'): self._msg_count = 0
        self._msg_count += 1
        self._scroll_to_bottom()

    def add_jarvis_message_placeholder(self, provider: str = "") -> "MessageBubble":
        """Insert an empty JARVIS reply bubble and return it so the caller
        can fill its text in progressively (see main_window's voice-sync
        reveal). This keeps the on-screen reply appearing in step with the
        spoken TTS audio instead of the text popping in fully before the
        voice has even started."""
        self._thinking.hide_thinking()
        bubble = MessageBubble("", sender="jarvis",
                               timestamp=self._ts(), provider=provider)
        self._msg_layout.insertWidget(self._msg_layout.count() - 1, bubble)
        self._register_bubble(bubble)
        if not hasattr(self, '_msg_count'): self._msg_count = 0
        self._msg_count += 1
        self._scroll_to_bottom()
        return bubble

    def clear_messages(self) -> None:
        """
        Remove every message bubble from the chat and show the welcome
        message again.

        Phase 9 fix: this method didn't exist before. The Ctrl+Shift+C
        shortcut was labeled "Clear chat history" in the shortcut-help
        dialog but was actually wired to hide_thinking() — which just
        hides the "thinking…" indicator — because there was nothing to
        wire it to. Now both the shortcut and the header's "Clear chat"
        button call this.
        """
        # Layout is [stretch(0), msg0, msg1, ..., thinking(last)].
        # Repeatedly take the item right after the stretch until only
        # the stretch and the thinking indicator remain.
        while self._msg_layout.count() > 2:
            item = self._msg_layout.takeAt(1)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._bubbles.clear()
        self._msg_count = 0
        self._add_welcome()

    @Slot(str)
    def show_thinking(self, agent: str = "JARVIS") -> None:
        self._thinking.show_thinking(agent)
        self._scroll_to_bottom()

    @Slot()
    def hide_thinking(self) -> None:
        self._thinking.hide_thinking()

    @Slot(str)
    def show_stt_partial(self, text: str) -> None:
        """
        Phase 9.3: Show live STT partial transcript in the input bar as
        the user is speaking, so they get real-time visual feedback.
        The input bar placeholder is temporarily replaced with the partial;
        once the final stt_result arrives, set_input_text() replaces it with
        the confirmed transcript and the placeholder resets on the next clear.
        """
        if text:
            self._input_bar.set_text(text)

    @Slot(str)
    def on_stt_result(self, text: str) -> None:
        """
        Phase 9.3: Confirmed STT transcript arrives — put it in the input
        bar so the user can review/edit before sending.
        """
        self._input_bar.set_text(text)

    # ── Phase 10.1 — token-by-token streaming ─────────────────────────────

    def start_stream_bubble(self, agent: str = "JARVIS", provider: str = "") -> "MessageBubble":
        """
        Phase 10.1: Open a new JARVIS reply bubble for streaming.
        The bubble starts empty; call append_stream_delta() to grow it chunk
        by chunk, then finish_stream_bubble() when chat_stream_end arrives.

        Returns the bubble so the caller (main_window) can hold a reference
        for the subsequent delta/end calls.
        """
        self._thinking.hide_thinking()
        bubble = MessageBubble("", sender="jarvis",
                               timestamp=self._ts(), provider=provider)
        self._msg_layout.insertWidget(self._msg_layout.count() - 1, bubble)
        self._register_bubble(bubble)
        if not hasattr(self, '_msg_count'): self._msg_count = 0
        self._msg_count += 1
        self._scroll_to_bottom()
        return bubble

    @staticmethod
    def append_stream_delta(bubble: "MessageBubble", delta: str) -> None:
        """
        Phase 10.1: Append a token delta to an in-progress streaming bubble.
        Accumulates text on the bubble's internal buffer and re-renders.
        """
        if not hasattr(bubble, "_stream_text"):
            bubble._stream_text = ""
        bubble._stream_text += delta
        bubble.update_text(bubble._stream_text)

    @staticmethod
    def finish_stream_bubble(bubble: "MessageBubble") -> None:
        """
        Phase 10.1: Called on chat_stream_end — finalises the streaming
        bubble.  Does a final re-render to ensure Markdown is fully applied
        to the complete accumulated text (delta-by-delta rendering works but
        may leave partial code fences open mid-stream).
        """
        if hasattr(bubble, "_stream_text"):
            bubble.update_text(bubble._stream_text)

    def set_input_text(self, text: str) -> None:
        self._input_bar.set_text(text)

    def clear_input(self) -> None:
        """Clear the chat input field (P-22 shortcut hook)."""
        self._input_bar.set_text("")

    def attached_files(self) -> list[str]:
        return self._input_bar.attached_files()

    def clear_attachments(self) -> None:
        self._input_bar.clear_attachments()

    def submit_current_input(self) -> None:
        """Public trigger for the global Ctrl+Enter shortcut (main_window)
        so it doesn't need to reach into ChatInputBar's private internals."""
        self._input_bar.submit()

    def focus_input(self) -> None:
        """Public trigger for the global Ctrl+L shortcut (main_window)."""
        self._input_bar.focus_input()
