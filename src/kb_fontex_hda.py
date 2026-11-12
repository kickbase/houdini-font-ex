
"""HDA PythonModule — Font Ex: Font parameter menu opens a Qt list with hover preview.

Self-contained for HDA embed: no filesystem paths or repo layout assumptions.
State lives on ``hou.session`` only; fonts come from Houdini menus and parms.
"""

from __future__ import annotations

import contextlib
from typing import Iterator

import hou
from hutil.Qt import QtCore, QtGui, QtWidgets

_PHM_NODE_KEY = "kb_fontex_phm_node_path"
_MENU_WARMUP_RETRY_MS = 50
_POPUP_MARGIN_PX = 8
_POPUP_CURSOR_OFFSET = (4, 10)
_POPUP_LIST_WIDTH = 288
_POPUP_LIST_MAX_HEIGHT = 360
_POPUP_LIST_ROW_HEIGHT = 22
_POPUP_LIST_MIN_HEIGHT = 120
_INNER_FONT_NAME = "font1"
_SEPARATOR_LABEL = "_separator_"
_cached_parm_stylesheet: str | None = None
_cached_font_menu: list[str] | None = None
_menu_read_failed = False
_menu_warmup_scheduled = False


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


def _node_valid(node: hou.Node | None) -> bool:
    if not isinstance(node, hou.Node):
        return False
    try:
        path = node.path()
    except hou.Error:
        return False
    if not path:
        return False
    try:
        return hou.node(path) is not None
    except hou.Error:
        return False


def _session_get(key: str, default: object = None) -> object:
    try:
        return getattr(hou.session, key)
    except AttributeError:
        return default


def _session_set(key: str, value: object) -> None:
    try:
        setattr(hou.session, key, value)
    except AttributeError:
        pass


def _state() -> dict:
    try:
        st = hou.session.kb_fontex_state
        if isinstance(st, dict):
            return st
    except (AttributeError, hou.Error):
        pass
    st: dict[str, object] = {
        "popup": None,
        "popup_reuse": None,
        "original_font": {},
        "menu_cache": None,
    }
    try:
        hou.session.kb_fontex_state = st
    except Exception:
        pass
    return st


def popup_is_open() -> bool:
    return _state().get("popup") is not None


def note_fontex_node(node: hou.Node | None) -> None:
    if _node_valid(node):
        _session_set(_PHM_NODE_KEY, node.path())


def _original_fonts() -> dict:
    originals = _state().setdefault("original_font", {})
    if not isinstance(originals, dict):
        originals = {}
        _state()["original_font"] = originals
    return originals


def _remember_original_font(node: hou.Node, parm: hou.Parm | None) -> None:
    if parm is None:
        return
    _original_fonts().setdefault(node.path(), parm.evalAsString())


def _take_original_font(node: hou.Node) -> str | None:
    return _original_fonts().pop(node.path(), None)


def emergency_stop() -> None:
    _popup_clear()


# ---------------------------------------------------------------------------
# Font menu data
# ---------------------------------------------------------------------------


def _flat_token_label_pairs(parm: hou.Parm) -> list[str]:
    flat: list[str] = []
    for label, token in zip(parm.menuLabels(), parm.menuItems()):
        if label == _SEPARATOR_LABEL:
            continue
        flat.append(token if token else label)
        flat.append(label)
    return flat


def _entries_from_flat(flat: list[str]) -> list[tuple[str, str]]:
    if len(flat) < 2 or len(flat) % 2:
        return []
    return [(flat[i + 1], flat[i]) for i in range(0, len(flat), 2)]


def _inner_font(node: hou.Node) -> hou.Node | None:
    return node.node(_INNER_FONT_NAME)


def _resolve_fontex_node(node: hou.Node | None = None) -> hou.Node | None:
    if _node_valid(node):
        return node
    path = _session_get(_PHM_NODE_KEY)
    if isinstance(path, str) and path:
        found = hou.node(path)
        if _node_valid(found):
            return found
    return None


def _read_font_menu_from_inner(node: hou.Node | None = None) -> list[str]:
    """Native Font SOP ``file`` menu on the HDA's locked inner ``font1``."""
    fx = _resolve_fontex_node(node)
    if fx is None:
        return []
    inner = _inner_font(fx)
    if inner is None:
        return []
    parm = inner.parm("file")
    if parm is None:
        return []
    try:
        return _flat_token_label_pairs(parm)
    except hou.Error:
        return []


def _valid_menu(flat: object) -> bool:
    return isinstance(flat, list) and bool(flat) and len(flat) % 2 == 0


def _ensure_menu_cache(node: hou.Node | None = None) -> list[str]:
    global _cached_font_menu, _menu_read_failed
    if _valid_menu(_cached_font_menu):
        return list(_cached_font_menu)
    st = _state()
    cache = st.get("menu_cache")
    if _valid_menu(cache):
        _cached_font_menu = list(cache)
        return list(cache)
    st["menu_cache"] = None

    flat = _read_font_menu_from_inner(node)
    if not _valid_menu(flat):
        _menu_read_failed = True
        return []
    _menu_read_failed = False
    _cached_font_menu = list(flat)
    st["menu_cache"] = list(flat)
    return list(flat)


def _font_entries(node: hou.Node) -> list[tuple[str, str]]:
    """Font list for Qt popup — inner Font SOP only (never outer ``file_menu_script``)."""
    entries = _entries_from_flat(_ensure_menu_cache(node))
    if entries:
        return entries
    return _entries_from_flat(_read_font_menu_from_inner(node))


def file_menu_script(kwargs=None) -> list[str]:
    """``file`` item-generator menu: Qt font list only (no native menu entries)."""
    kwargs = kwargs if isinstance(kwargs, dict) else {}
    node = kwargs.get("node")
    if _node_valid(node) and hou.isUIAvailable():
        note_fontex_node(node)
        if kwargs.get("sync"):
            open_list(node)
        else:
            _schedule_popup_for_node(node)
        return []
    return _ensure_menu_cache(node)


# ---------------------------------------------------------------------------
# Cook / set font on HDA parm only (inner font1 uses ch references)
# ---------------------------------------------------------------------------


def _cook_display(node: hou.Node) -> None:
    inner = _inner_font(node)
    if inner is not None:
        inner.cook(force=True)
    node.cook(force=True)
    parent = node.parent()
    if isinstance(parent, hou.Node):
        try:
            parent.cook(force=True)
        except hou.Error:
            pass
    try:
        hou.ui.triggerUpdate()
        for tab in hou.ui.paneTabs():
            if tab.type() == hou.paneTabType.SceneViewer:
                try:
                    tab.curViewport().draw()
                except (AttributeError, hou.Error, TypeError):
                    pass
                break
    except (hou.Error, AttributeError, TypeError):
        pass


def _set_font(node: hou.Node, font_name: str) -> None:
    if not _node_valid(node):
        return
    parm = node.parm("file")
    if parm is None or not font_name:
        return
    if parm.evalAsString() == font_name:
        _cook_display(node)
        return
    parm.set(font_name)
    _cook_display(node)


@contextlib.contextmanager
def _auto_update_mode() -> Iterator[None]:
    prev = hou.updateModeSetting()
    try:
        if prev != hou.updateMode.AutoUpdate:
            hou.setUpdateMode(hou.updateMode.AutoUpdate)
        yield
    finally:
        try:
            hou.setUpdateMode(prev)
        except hou.Error:
            pass


def _pump_qt_events() -> None:
    app = QtWidgets.QApplication.instance()
    if app is not None:
        app.processEvents()


def preview_font(node: hou.Node, font_name: str) -> None:
    """Hover preview: viewport update without undo."""
    if not _node_valid(node) or not font_name:
        return
    parm = node.parm("file")
    if parm is None:
        return
    _remember_original_font(node, parm)
    if parm.evalAsString() == font_name:
        _cook_display(node)
        return
    with _auto_update_mode():
        with hou.undos.disabler():
            _set_font(node, font_name)
    _pump_qt_events()


def commit_font(node: hou.Node, font_name: str) -> None:
    """Single undo step from pre-list font to picked font."""
    if not _node_valid(node):
        return
    font_name = str(font_name)
    if not font_name:
        return
    original = _take_original_font(node)
    parm = node.parm("file")
    if parm is None:
        return
    current = parm.evalAsString()
    if original is None:
        original = current
    if font_name == original:
        if current != original:
            with hou.undos.disabler():
                _set_font(node, original)
        return
    with _auto_update_mode():
        if current == font_name and current != original:
            with hou.undos.disabler():
                _set_font(node, original)
        _set_font(node, font_name)
    _pump_qt_events()


def restore_if_uncommitted(node: hou.Node) -> None:
    if not _node_valid(node):
        return
    original = _take_original_font(node)
    if original is None:
        return
    parm = node.parm("file")
    if parm is None or parm.evalAsString() == original:
        return
    with hou.undos.disabler():
        try:
            _set_font(node, original)
        except hou.Error:
            pass


# ---------------------------------------------------------------------------
# Qt popup list
# ---------------------------------------------------------------------------


def _main_window() -> QtWidgets.QWidget | None:
    try:
        import hou.qt as hqt

        return hqt.mainWindow()
    except Exception:
        pass
    app = QtWidgets.QApplication.instance()
    return app.activeWindow() if app else None


def _popup_clear() -> None:
    st = _state()
    popup = st.get("popup")
    st["popup"] = None
    if popup is not None:
        try:
            popup._closing = True
            if not popup._committed:
                restore_if_uncommitted(popup._node)
            popup.close()
        except RuntimeError:
            pass


def _schedule_popup_for_node(node: hou.Node) -> None:
    if popup_is_open():
        return
    node_path = node.path()

    def _open() -> None:
        n = hou.node(node_path)
        if n is None or popup_is_open():
            return
        open_list(n)

    QtCore.QTimer.singleShot(1, _open)


def _qt_app_palette() -> QtGui.QPalette:
    app = QtWidgets.QApplication.instance()
    if app is not None:
        return app.palette()
    pal = QtGui.QPalette()
    pal.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor("#353535"))
    pal.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor("#e8e8e8"))
    pal.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor("#f09300"))
    pal.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor("#000000"))
    pal.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor("#202020"))
    return pal


def _build_parm_menu_stylesheet() -> str:
    pal = _qt_app_palette()
    bg = pal.color(QtGui.QPalette.ColorRole.Base).name()
    fg = pal.color(QtGui.QPalette.ColorRole.Text).name()
    hi = pal.color(QtGui.QPalette.ColorRole.Highlight).name()
    hit = pal.color(QtGui.QPalette.ColorRole.HighlightedText).name()
    return (
        "QFrame#kb_fontex_menu { background-color: %s; border: 1px solid #565656; }"
        " QListWidget { background-color: %s; color: %s; border: none; outline: none;"
        " padding: 0px; }"
        " QListWidget::item { padding: 3px 10px; min-height: 18px; }"
        " QListWidget::item:hover, QListWidget::item:selected {"
        " background-color: %s; color: %s; }"
        " QScrollBar:vertical { width: 10px; background: %s; margin: 0; }"
        " QScrollBar::handle:vertical { background: #686868; min-height: 24px;"
        " border-radius: 2px; }"
        " QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        % (bg, bg, fg, hi, hit, bg)
    )


def _parm_menu_stylesheet() -> str:
    global _cached_parm_stylesheet
    if _cached_parm_stylesheet is None:
        _cached_parm_stylesheet = _build_parm_menu_stylesheet()
    return _cached_parm_stylesheet


def _menu_popup_size(row_count: int) -> tuple[int, int]:
    height = min(
        _POPUP_LIST_MAX_HEIGHT,
        max(_POPUP_LIST_MIN_HEIGHT, row_count * _POPUP_LIST_ROW_HEIGHT + 4),
    )
    return _POPUP_LIST_WIDTH, height


def _menu_anchor() -> tuple[QtCore.QPoint, str]:
    return QtGui.QCursor.pos(), "cursor"


def _place_popup_near_anchor(
    popup: QtWidgets.QWidget,
    anchor: QtCore.QPoint,
    parent: QtWidgets.QWidget | None,
) -> None:
    w, h = popup.width(), popup.height()
    x = anchor.x() + _POPUP_CURSOR_OFFSET[0]
    y = anchor.y() + _POPUP_CURSOR_OFFSET[1]
    app = QtWidgets.QApplication.instance()
    screen = None
    if app is not None:
        screen = app.screenAt(anchor) or app.primaryScreen()
    if screen is not None:
        g = screen.availableGeometry()
        m = _POPUP_MARGIN_PX
        x = max(g.left() + m, min(x, g.right() - w - m))
        y = max(g.top() + m, min(y, g.bottom() - h - m))
    elif parent is not None:
        g = parent.geometry()
        m = _POPUP_MARGIN_PX
        x = max(g.left() + m, min(x, g.right() - w - m))
        y = max(g.top() + m, min(y, g.bottom() - h - m))
    popup.move(x, y)


_popup_construct_count = 0


def popup_construct_count() -> int:
    return _popup_construct_count


def reset_popup_construct_count() -> None:
    global _popup_construct_count
    _popup_construct_count = 0


def _sync_list_selection(list_w: QtWidgets.QListWidget, current: str) -> None:
    if not current:
        return
    for row in range(list_w.count()):
        item = list_w.item(row)
        if item is None:
            continue
        token = str(item.data(QtCore.Qt.ItemDataRole.UserRole) or item.text())
        if token == current or item.text() == current:
            list_w.setCurrentItem(item)
            break


def _clear_list_hover(list_w: QtWidgets.QListWidget) -> None:
    """Reused list keeps the last hovered row; reset it or reopen shows a stale
    highlight and ``itemEntered`` skips that row on the first hover."""
    viewport = list_w.viewport()
    outside = QtCore.QPointF(-1, -1)
    hover_leave = QtGui.QHoverEvent(QtCore.QEvent.Type.HoverLeave, outside, outside)
    QtWidgets.QApplication.sendEvent(viewport, hover_leave)
    QtWidgets.QApplication.sendEvent(viewport, QtCore.QEvent(QtCore.QEvent.Type.Leave))
    viewport.update()


class _FontListPopup(QtWidgets.QFrame):
    def __init__(
        self,
        node: hou.Node,
        anchor: QtCore.QPoint,
        parent: QtWidgets.QWidget | None,
    ) -> None:
        global _popup_construct_count
        _popup_construct_count += 1
        flags = QtCore.Qt.WindowType.Popup | QtCore.Qt.WindowType.FramelessWindowHint
        if parent is not None:
            flags |= QtCore.Qt.WindowType.NoDropShadowWindowHint
        super().__init__(parent, flags)
        self._node = node
        self._anchor_point = QtCore.QPoint(anchor)
        self._anchor_source = "cursor"
        self._committed = False
        self._closing = False
        self._last_token: str | None = None
        self.setObjectName("kb_fontex_menu")
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

        entries = _font_entries(node)
        pw, ph = _menu_popup_size(len(entries))
        self.setFixedSize(pw, ph)

        parm = node.parm("file")
        current = parm.evalAsString() if parm is not None else ""

        self._list = QtWidgets.QListWidget(self)
        self._list.setAlternatingRowColors(False)
        self._list.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._list.setVerticalScrollMode(
            QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self._list.setMouseTracking(True)
        self._list.setSpacing(0)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            self._list.setFont(app.font())
        self.setStyleSheet(_parm_menu_stylesheet())
        for label, token in entries:
            item = QtWidgets.QListWidgetItem(label)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, token)
            self._list.addItem(item)

        _sync_list_selection(self._list, current)
        self._list.itemEntered.connect(self._on_hover)
        self._list.itemClicked.connect(self._on_pick)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._list, 1)

        _place_popup_near_anchor(self, anchor, parent or _main_window())
        _remember_original_font(node, parm)

    def prepare_show(
        self,
        node: hou.Node,
        anchor: QtCore.QPoint,
        parent: QtWidgets.QWidget | None,
        *,
        anchor_source: str = "cursor",
    ) -> None:
        self._node = node
        self._anchor_point = QtCore.QPoint(anchor)
        self._anchor_source = anchor_source
        self._committed = False
        self._closing = False
        self._last_token = None
        parm = node.parm("file")
        current = parm.evalAsString() if parm is not None else ""
        _clear_list_hover(self._list)
        _sync_list_selection(self._list, current)
        _place_popup_near_anchor(self, anchor, parent or _main_window())
        _remember_original_font(node, parm)

    def _on_hover(self, item: QtWidgets.QListWidgetItem) -> None:
        if not _node_valid(self._node):
            self.close()
            return
        self._list.setCurrentItem(item)
        token = str(item.data(QtCore.Qt.ItemDataRole.UserRole) or item.text())
        if not token or token == self._last_token:
            return
        self._last_token = token
        preview_font(self._node, token)

    def _on_pick(self, item: QtWidgets.QListWidgetItem) -> None:
        if not _node_valid(self._node):
            self.close()
            return
        token = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if token:
            self._committed = True
            commit_font(self._node, str(token))
        self.close()

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        if self._closing:
            super().closeEvent(event)
            return
        if not self._committed and _node_valid(self._node):
            restore_if_uncommitted(self._node)
        st = _state()
        if st.get("popup") is self:
            st["popup"] = None
        st["popup_reuse"] = self
        super().closeEvent(event)


def _acquire_popup(
    node: hou.Node,
    anchor: QtCore.QPoint,
    parent: QtWidgets.QWidget | None,
    anchor_source: str,
) -> _FontListPopup:
    st = _state()
    _take_original_font(node)
    reuse = st.get("popup_reuse")
    if isinstance(reuse, _FontListPopup):
        try:
            reuse.prepare_show(node, anchor, parent, anchor_source=anchor_source)
            return reuse
        except RuntimeError:
            st["popup_reuse"] = None
    popup = _FontListPopup(node, anchor, parent)
    popup._anchor_source = anchor_source
    st["popup_reuse"] = popup
    return popup


def open_list(node: hou.Node | None = None) -> bool:
    if node is None:
        path = _session_get(_PHM_NODE_KEY)
        node = hou.node(path) if isinstance(path, str) else None
    if not _node_valid(node) or not hou.isUIAvailable():
        return False

    note_fontex_node(node)
    parent = _main_window()
    anchor, anchor_source = _menu_anchor()

    existing = _state().get("popup")
    if popup_is_open() and isinstance(existing, _FontListPopup):
        try:
            if existing._node.path() == node.path():
                existing.prepare_show(node, anchor, parent, anchor_source=anchor_source)
                existing.show()
                existing.raise_()
                return True
        except (RuntimeError, AttributeError):
            pass
        emergency_stop()

    popup = _acquire_popup(node, anchor, parent, anchor_source)
    _state()["popup"] = popup
    popup.show()
    popup.raise_()
    return True


def open_font_picker(node: hou.Node | None = None) -> bool:
    return open_list(node)


def _run_menu_warmup() -> None:
    global _menu_warmup_scheduled
    if hou.hipFile.isLoadingHipFile():
        QtCore.QTimer.singleShot(_MENU_WARMUP_RETRY_MS, _run_menu_warmup)
        return
    _menu_warmup_scheduled = False
    if _menu_read_failed:
        return
    try:
        _ensure_menu_cache()
    except hou.Error:
        pass


def _schedule_menu_warmup() -> None:
    """Runs once after the HIP finishes loading, however many instances it has."""
    global _menu_warmup_scheduled
    if _menu_warmup_scheduled or not hou.isUIAvailable():
        return
    _menu_warmup_scheduled = True
    QtCore.QTimer.singleShot(0, _run_menu_warmup)


def on_loaded(**kwargs) -> None:
    node = kwargs.get("node")
    note_fontex_node(node)
    _schedule_menu_warmup()
