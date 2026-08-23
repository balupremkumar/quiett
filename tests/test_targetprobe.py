"""Tests for tp.py - the "can text actually land here" probe.

Headless by construction: every verdict test drives _classify_uia with a fake
property dict, so nothing here touches live UI Automation, COM, or a real
window. The one test that exercises the worker thread stubs COM init out.

The load-bearing test in this file is
test_blind_shape_in_untrusted_process_is_unknown: a Tk Text widget, which is
editable, reports the identical property shape to a Chrome pane with nothing
focused. If that ever starts returning NOT_EDITABLE, dictation breaks in every
app without an accessibility layer.
"""
import time

import pytest

import targetprobe as tp


def _props(**over):
    """A property dict shaped like _uia_props output, overridable per test."""
    base = dict(
        control_type=tp._CT_EDIT,
        name="",
        app="TestApp",
        rect=(10, 20, 300, 60),
        pid=1234,
        exe="testapp.exe",
        classes=["TestWindowClass"],
        trusted=False,
        text_pattern=False,
        value_pattern=False,
        readonly=True,
        enabled=True,
        focusable=False,
        is_password=False,
    )
    base.update(over)
    return base


# The exact shape measured on this machine for Chrome focused on a non-input
# pane, and identically for a Tkinter Text widget.
_BLIND_SHAPE = dict(
    control_type=tp._CT_PANE,
    focusable=False,
    text_pattern=False,
    value_pattern=False,
    readonly=True,
)


class TestClassifyEditable:
    def test_edit_with_writable_value_pattern(self):
        t = tp._classify_uia(_props(control_type=tp._CT_EDIT, value_pattern=True,
                                    readonly=False, focusable=True))
        assert t.verdict == tp.EDITABLE
        assert t.kind == "uia"
        assert t.source == "uia:pattern"
        assert t.rect == (10, 20, 300, 60)

    def test_document_with_text_pattern_and_no_value_pattern(self):
        # Word: TextPattern only, so ValueIsReadOnly's default True must not veto.
        t = tp._classify_uia(_props(control_type=tp._CT_DOCUMENT, text_pattern=True,
                                    focusable=True, readonly=True))
        assert t.verdict == tp.EDITABLE

    def test_editable_combobox(self):
        t = tp._classify_uia(_props(control_type=tp._CT_COMBOBOX, value_pattern=True,
                                    readonly=False, focusable=True))
        assert t.verdict == tp.EDITABLE

    def test_editable_wins_even_in_untrusted_process(self):
        # A positive needs no trust: the patterns are the evidence.
        t = tp._classify_uia(_props(value_pattern=True, readonly=False, trusted=False))
        assert t.verdict == tp.EDITABLE

    def test_label_is_app_plus_element_name(self):
        t = tp._classify_uia(_props(app="Claude", name="message box",
                                    value_pattern=True, readonly=False))
        assert t.label == "Claude - message box"


class TestClassifyNotEditable:
    def test_password_field_is_never_a_target(self):
        t = tp._classify_uia(_props(is_password=True, value_pattern=True,
                                    readonly=False, trusted=False))
        assert t.verdict == tp.NOT_EDITABLE
        assert t.label.endswith("password field")
        assert t.source == "uia:password"

    def test_chrome_pane_with_nothing_focused(self):
        t = tp._classify_uia(_props(exe="chrome.exe", trusted=True, **_BLIND_SHAPE))
        assert t.verdict == tp.NOT_EDITABLE
        assert t.source == "uia:blind-shape"

    def test_blind_shape_in_untrusted_process_is_unknown(self):
        # Tkinter Text: same shape, but editable. Must never be a hard negative.
        t = tp._classify_uia(_props(exe="python.exe", trusted=False, **_BLIND_SHAPE))
        assert t.verdict == tp.UNKNOWN
        assert t.source == "uia:untrusted-provider"

    def test_focused_button_in_trusted_process(self):
        t = tp._classify_uia(_props(control_type=tp._CT_BUTTON, focusable=True,
                                    trusted=True))
        assert t.verdict == tp.NOT_EDITABLE
        assert t.source == "uia:non-text-control"

    def test_focused_button_in_untrusted_process_is_unknown(self):
        t = tp._classify_uia(_props(control_type=tp._CT_BUTTON, focusable=True,
                                    trusted=False))
        assert t.verdict == tp.UNKNOWN

    def test_read_only_edit_in_trusted_process(self):
        t = tp._classify_uia(_props(control_type=tp._CT_EDIT, value_pattern=True,
                                    readonly=True, focusable=True, trusted=True))
        assert t.verdict == tp.NOT_EDITABLE
        assert t.source == "uia:readonly"

    def test_read_only_edit_in_untrusted_process_is_unknown(self):
        t = tp._classify_uia(_props(control_type=tp._CT_EDIT, value_pattern=True,
                                    readonly=True, focusable=True, trusted=False))
        assert t.verdict == tp.UNKNOWN

    def test_disabled_edit_in_trusted_process(self):
        t = tp._classify_uia(_props(control_type=tp._CT_EDIT, value_pattern=True,
                                    readonly=False, enabled=False, trusted=True))
        assert t.verdict == tp.NOT_EDITABLE
        assert t.source == "uia:disabled"

    def test_rect_and_label_survive_an_unknown_verdict(self):
        # The ring and the badge still need somewhere to draw.
        t = tp._classify_uia(_props(app="Cursor", name="editor", control_type=tp._CT_PANE,
                                    focusable=True, trusted=False))
        assert t.verdict == tp.UNKNOWN
        assert t.rect == (10, 20, 300, 60)
        assert t.label == "Cursor - editor"


class TestTrustedProvider:
    @pytest.mark.parametrize("exe", ["chrome.exe", "msedge.exe", "code.exe",
                                     "cursor.exe", "claude.exe", "explorer.exe",
                                     "notepad.exe", "winword.exe", "outlook.exe",
                                     "teams.exe", "slack.exe"])
    def test_allow_listed_exes(self, exe):
        assert tp._is_trusted_provider(exe, []) is True

    def test_case_insensitive(self):
        assert tp._is_trusted_provider("Chrome.EXE", []) is True

    @pytest.mark.parametrize("exe", ["python.exe", "pythonw.exe", "javaw.exe",
                                     "mstsc.exe", "someGame.exe", ""])
    def test_everything_else_is_untrusted(self, exe):
        assert tp._is_trusted_provider(exe, []) is False

    def test_chromium_host_class(self):
        assert tp._is_trusted_provider("randomapp.exe", ["Chrome_WidgetWin_1"]) is True

    def test_winui_class_prefixes(self):
        assert tp._is_trusted_provider("x.exe", ["Windows.UI.Core.CoreWindow"]) is True
        assert tp._is_trusted_provider("x.exe", ["Microsoft.UI.Content.DesktopChildSiteBridge"]) is True

    def test_unrelated_class(self):
        assert tp._is_trusted_provider("x.exe", ["TkTopLevel", "UnrealWindow"]) is False

    def test_bad_input_never_raises(self):
        assert tp._is_trusted_provider(None, None) is False


class TestLabel:
    def test_app_only_when_nothing_else_known(self):
        assert tp._label("Notepad") == "Notepad"

    def test_falls_back_to_control_type_name(self):
        assert tp._label("Chrome", "", tp._CT_EDIT) == "Chrome - text box"

    def test_long_names_are_trimmed(self):
        out = tp._label("Chrome", "x" * 200, tp._CT_EDIT)
        assert len(out) <= 50 and out.endswith("...")

    def test_element_name_equal_to_app_name_is_not_repeated(self):
        assert tp._label("Slack", "Slack", tp._CT_PANE) == "Slack - pane"

    def test_newlines_do_not_break_a_single_line_badge(self):
        assert "\n" not in tp._label("App", "line one\nline two")


class TestProbeLayering:
    @pytest.fixture(autouse=True)
    def _stub_win32(self, monkeypatch):
        monkeypatch.setattr(tp, "_is_window", lambda hwnd: True)
        monkeypatch.setattr(tp, "_caret_target", lambda hwnd: None)
        monkeypatch.setattr(tp, "_uia_target", lambda hwnd, budget: None)
        monkeypatch.setattr(tp, "_class_name", lambda hwnd: "SomeClass")
        monkeypatch.setattr(tp, "_exe_for_hwnd", lambda hwnd: "someapp.exe")
        monkeypatch.setattr(tp, "_window_rect", lambda hwnd: (0, 0, 800, 600))

    def test_invalid_hwnd_short_circuits(self, monkeypatch):
        monkeypatch.setattr(tp, "_is_window", lambda hwnd: False)
        monkeypatch.setattr(tp, "_uia_target", lambda *a: pytest.fail("UIA must not run"))
        t = tp.probe(0)
        assert t.verdict == tp.UNKNOWN and t.source == "no-window"

    def test_caret_layer_wins(self, monkeypatch):
        caret = tp.Target(tp.EDITABLE, (1, 2, 3, 20), "Notepad - text box", "caret", "caret")
        monkeypatch.setattr(tp, "_caret_target", lambda hwnd: caret)
        monkeypatch.setattr(tp, "_uia_target", lambda *a: pytest.fail("UIA must not run"))
        assert tp.probe(1234) is caret

    def test_confident_uia_answer_wins(self, monkeypatch):
        verdict = tp.Target(tp.NOT_EDITABLE, None, "Chrome - pane", "uia", "uia:blind-shape")
        monkeypatch.setattr(tp, "_uia_target", lambda hwnd, budget: verdict)
        monkeypatch.setattr(tp, "_class_name", lambda hwnd: "CASCADIA_HOSTING_WINDOW_CLASS")
        assert tp.probe(1234) is verdict

    def test_terminal_beats_an_inconclusive_uia_answer(self, monkeypatch):
        monkeypatch.setattr(tp, "_class_name", lambda hwnd: "CASCADIA_HOSTING_WINDOW_CLASS")
        monkeypatch.setattr(tp, "_exe_for_hwnd", lambda hwnd: "windowsterminal.exe")
        monkeypatch.setattr(tp, "_uia_target",
                            lambda hwnd, budget: tp.Target(tp.UNKNOWN, None, "x", "uia", "uia:inconclusive"))
        t = tp.probe(1234)
        assert t.verdict == tp.EDITABLE
        assert t.kind == "terminal"
        assert t.rect == (0, 0, 800, 600)
        assert t.label == "Terminal"   # "Terminal - terminal" collapses to one word

    def test_rdp_is_an_honest_unknown(self, monkeypatch):
        monkeypatch.setattr(tp, "_class_name", lambda hwnd: "TscShellContainerClass")
        t = tp.probe(1234)
        assert t.verdict == tp.UNKNOWN
        assert t.kind == "rdp"
        assert t.label == "Remote session - Quiett cannot see the field"

    def test_rdp_by_exe_name(self, monkeypatch):
        monkeypatch.setattr(tp, "_exe_for_hwnd", lambda hwnd: "msrdc.exe")
        assert tp.probe(1234).kind == "rdp"

    def test_unknown_uia_result_is_the_fallback(self, monkeypatch):
        inconclusive = tp.Target(tp.UNKNOWN, (5, 5, 50, 50), "App - pane", "uia", "uia:inconclusive")
        monkeypatch.setattr(tp, "_uia_target", lambda hwnd, budget: inconclusive)
        assert tp.probe(1234) is inconclusive

    def test_nothing_known_at_all(self):
        t = tp.probe(1234)
        assert t.verdict == tp.UNKNOWN
        assert t.kind == "none"
        assert t.source == "no-signal"

    def test_zero_budget_skips_the_com_layer(self, monkeypatch):
        monkeypatch.setattr(tp, "_uia_target", lambda *a: pytest.fail("no budget left"))
        assert tp.probe(1234, budget_ms=0).verdict == tp.UNKNOWN

    def test_probe_never_raises(self, monkeypatch):
        def _boom(*a, **kw):
            raise OSError("window died mid-probe")
        monkeypatch.setattr(tp, "_caret_target", _boom)
        t = tp.probe(1234)
        assert t.verdict == tp.UNKNOWN and t.source == "error"


class TestDeadline:
    @pytest.fixture(autouse=True)
    def _stub_com(self, monkeypatch):
        monkeypatch.setattr(tp, "_com_begin", lambda: "fake-com-token")
        monkeypatch.setattr(tp, "_com_end", lambda token: None)

    def test_result_returned_when_inside_the_budget(self):
        assert tp._run_with_deadline(lambda: "quick", 1.0) == "quick"

    def test_hung_call_returns_none_at_the_deadline(self):
        t0 = time.perf_counter()
        out = tp._run_with_deadline(lambda: time.sleep(2.0), 0.05)
        elapsed = (time.perf_counter() - t0) * 1000
        assert out is None
        assert elapsed < 500, f"probe hung for {elapsed:.0f}ms"

    def test_worker_exception_is_swallowed(self):
        def _boom():
            raise RuntimeError("UIA fell over")
        assert tp._run_with_deadline(_boom, 1.0) is None

    def test_inflight_cap_stops_leaking_threads(self, monkeypatch):
        monkeypatch.setattr(tp, "_inflight", tp._MAX_INFLIGHT)
        assert tp._run_with_deadline(lambda: pytest.fail("must not run"), 1.0) is None


class TestVerify:
    @pytest.fixture(autouse=True)
    def _stub_win32(self, monkeypatch):
        monkeypatch.setattr(tp, "_is_window", lambda hwnd: True)

    def _after(self, monkeypatch, signal):
        monkeypatch.setattr(tp, "_run_with_deadline", lambda fn, budget: signal)

    def test_growth_is_a_confirmed_insert(self, monkeypatch):
        before = tp.TargetSignal("value", 10, (1, 2), 99)
        self._after(monkeypatch, tp.TargetSignal("value", 27, (1, 2), 99))
        assert tp.verify_landed(99, before) is True

    def test_no_change_is_a_miss(self, monkeypatch):
        before = tp.TargetSignal("value", 10, (1, 2), 99)
        self._after(monkeypatch, tp.TargetSignal("value", 10, (1, 2), 99))
        assert tp.verify_landed(99, before) is False

    def test_shrink_proves_nothing(self, monkeypatch):
        before = tp.TargetSignal("value", 10, (1, 2), 99)
        self._after(monkeypatch, tp.TargetSignal("value", 4, (1, 2), 99))
        assert tp.verify_landed(99, before) is None

    def test_focus_moved_means_no_opinion(self, monkeypatch):
        before = tp.TargetSignal("value", 10, (1, 2), 99)
        self._after(monkeypatch, tp.TargetSignal("value", 40, (9, 9), 99))
        assert tp.verify_landed(99, before) is None

    def test_signal_kind_change_means_no_opinion(self, monkeypatch):
        before = tp.TargetSignal("value", 10, (1, 2), 99)
        self._after(monkeypatch, tp.TargetSignal("text", 40, (1, 2), 99))
        assert tp.verify_landed(99, before) is None

    def test_no_signal_after_means_no_opinion(self, monkeypatch):
        before = tp.TargetSignal("value", 10, (1, 2), 99)
        self._after(monkeypatch, None)
        assert tp.verify_landed(99, before) is None

    def test_no_token_means_no_opinion(self, monkeypatch):
        self._after(monkeypatch, tp.TargetSignal("value", 99, (1, 2), 99))
        assert tp.verify_landed(99, None) is None
        assert tp.verify_landed(99, "not a signal") is None

    def test_dead_window_means_no_opinion(self, monkeypatch):
        monkeypatch.setattr(tp, "_is_window", lambda hwnd: False)
        assert tp.verify_landed(99, tp.TargetSignal("value", 1, (1,), 99)) is None
        assert tp.verify_token(99) is None

    def test_verify_landed_never_raises(self, monkeypatch):
        def _boom(*a, **kw):
            raise OSError("COM gone")
        monkeypatch.setattr(tp, "_run_with_deadline", _boom)
        assert tp.verify_landed(99, tp.TargetSignal("value", 1, (1,), 99)) is None
        assert tp.verify_token(99) is None


class TestMirroredFromInject:
    """inject.py is the source of truth for both of these; catch drift early."""

    @staticmethod
    def _inject_source() -> str:
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(tp.__file__)), "inject.py")
        return open(path, encoding="utf-8").read()

    def test_terminal_classes_match_inject(self):
        import re
        src = self._inject_source()
        block = re.search(r"_TERMINAL_CLASSES = \{(.*?)\}", src, re.S).group(1)
        theirs = set(re.findall(r'"([^"]+)"', block))
        assert theirs == tp._TERMINAL_CLASSES

    def test_guithreadinfo_struct_matches_inject(self):
        import ctypes
        import re
        src = self._inject_source()
        block = re.search(r"class _GUITHREADINFO.*?\]", src, re.S).group(0)
        theirs = re.findall(r'\("(\w+)",\s*(?:wintypes\.)?\w+\)', block)
        mine = [name for name, _typ in tp._GUITHREADINFO._fields_]
        assert theirs == mine
        assert ctypes.sizeof(tp._GUITHREADINFO) > 0


class TestVerifyLandedZeroSignal:
    """THE DOUBLED-DICTATION REGRESSION (2026-08-23).

    verify_landed graded "same length before and after" as a confident NO.
    A WebView2 host (Tauri, Electron) hands UIA an element whose value reads
    "" whichever way the paste went, so every insert into Flightdeck came back
    0 == 0 and was reported as failed. That fired the recovery panel, Balu
    placed the text again, and the dictation arrived twice. app.log 12:08 to
    13:34 shows six of them in a row.

    No possible delta means no signal, not a negative verdict, which is the
    rule _read_signal already applies to a capped text read.
    """

    def _sig(self, value, kind="value", rid=(1, 2)):
        return tp.TargetSignal(kind, value, rid, 1)

    def test_zero_both_sides_claims_nothing(self, monkeypatch):
        monkeypatch.setattr(tp, "_is_window", lambda h: True)
        monkeypatch.setattr(tp, "_run_with_deadline",
                            lambda fn, budget: self._sig(0))
        assert tp.verify_landed(1, self._sig(0)) is None

    def test_a_real_unchanged_length_is_still_a_no(self, monkeypatch):
        """A field that genuinely held 40 characters before and holds 40 now
        did not receive the insert. That verdict must survive."""
        monkeypatch.setattr(tp, "_is_window", lambda h: True)
        monkeypatch.setattr(tp, "_run_with_deadline",
                            lambda fn, budget: self._sig(40))
        assert tp.verify_landed(1, self._sig(40)) is False

    def test_growth_from_zero_is_still_a_yes(self, monkeypatch):
        monkeypatch.setattr(tp, "_is_window", lambda h: True)
        monkeypatch.setattr(tp, "_run_with_deadline",
                            lambda fn, budget: self._sig(138))
        assert tp.verify_landed(1, self._sig(0)) is True

    def test_a_caret_that_never_moved_off_zero_claims_nothing(self, monkeypatch):
        """The text-pattern signal is a caret offset, so 0 -> 0 means the caret
        is at the start of a document UIA cannot see into."""
        monkeypatch.setattr(tp, "_is_window", lambda h: True)
        monkeypatch.setattr(tp, "_run_with_deadline",
                            lambda fn, budget: self._sig(0, kind="text"))
        assert tp.verify_landed(1, self._sig(0, kind="text")) is None
