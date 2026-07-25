"""Tests for narration.py - plan() is pure and deterministic, no server or
audio involved."""
import pytest

import narration


def _words(text: str) -> str:
    """Strip all whitespace so segment concatenation can be compared against
    source text regardless of where lines got merged or re-strung."""
    return "".join(text.split())


class TestNoRewrite:
    """The narrator may re-time and re-group text; it must never change it."""

    PASSAGES = [
        "First one here. Second one here. Third one here.",
        "A heading\n\nSome body text, with a clause, that runs on for a while.",
        "- item one\n- item two\n- item three",
        "He said, \"this is quoted material,\" and moved on.",
        "Para one line one.\nPara one line two.\n\nPara two line one.",
        "alpha, " * 30,
        "Chapter One: Introduction\n\nThis chapter covers the basics; it is short.",
    ]

    @pytest.mark.parametrize("text", PASSAGES)
    @pytest.mark.parametrize("study", [False, True])
    def test_concatenation_matches_source(self, text, study):
        segments = narration.plan(text, budget=40, base_speed=1.0, study=study)
        rebuilt = "".join(_words(t) for t, _, _ in segments)
        assert rebuilt == _words(text)

    @pytest.mark.parametrize("text", PASSAGES)
    def test_zero_budget_matches_source(self, text):
        segments = narration.plan(text, budget=0, base_speed=1.0)
        rebuilt = "".join(_words(t) for t, _, _ in segments)
        assert rebuilt == _words(text)


class TestBudget:
    @pytest.mark.parametrize("study", [False, True])
    def test_no_segment_exceeds_budget(self, study):
        text = "alpha, " * 30 + "This is a much longer sentence that keeps going and going. " * 3
        segments = narration.plan(text, budget=25, base_speed=1.0, study=study)
        assert all(len(t) <= 25 for t, _, _ in segments)

    def test_budget_zero_returns_whole_text_as_one_segment(self):
        text = "One. Two. Three."
        segments = narration.plan(text, budget=0, base_speed=1.0)
        assert len(segments) == 1
        assert segments[0][0] == text
        assert segments[0][1] == 0.0

    def test_budget_zero_ignores_study_flag_too(self):
        text = "One. Two. Three."
        segments = narration.plan(text, budget=0, base_speed=1.0, study=True)
        assert len(segments) == 1


class TestEmptyInput:
    @pytest.mark.parametrize("text", ["", "   ", "\n\n  \n", None])
    @pytest.mark.parametrize("study", [False, True])
    def test_empty_or_whitespace_returns_empty_list(self, text, study):
        assert narration.plan(text, budget=40, base_speed=1.0, study=study) == []


class TestFlatModeParity:
    """study=False must reproduce tts._chunk_text's behaviour exactly."""

    def test_splits_on_sentences_within_budget(self):
        text = "First one here. Second one here. Third one here."
        segments = narration.plan(text, budget=40, base_speed=1.0)
        assert [t for t, _, _ in segments] == [
            "First one here. Second one here.", "Third one here."]

    def test_last_segment_pause_is_zero(self):
        text = "First one here. Second one here. Third one here."
        segments = narration.plan(text, budget=40, base_speed=1.0)
        assert segments[-1][1] == 0.0

    def test_sentence_boundary_gets_bigger_pause_than_clause(self):
        text = "alpha, " * 30
        segments = narration.plan(text, budget=20, base_speed=1.0)
        # None of these pieces end a sentence, so every pause is the clause gap.
        assert all(p in (narration._GAP_CLAUSE_S, 0.0) for _, p, _ in segments)

    def test_speed_is_flat_across_segments(self):
        text = "First one here. Second one here. Third one here."
        segments = narration.plan(text, budget=10, base_speed=0.8)
        assert all(speed == 0.8 for _, _, speed in segments)

    def test_short_text_stays_one_chunk(self):
        segments = narration.plan("Just this.", budget=120, base_speed=1.0)
        assert [t for t, _, _ in segments] == ["Just this."]


class TestLongSentenceHardSplit:
    def test_run_on_sentence_is_broken_up(self):
        text = "alpha, " * 30
        segments = narration.plan(text, budget=60, base_speed=1.0)
        assert all(len(t) <= 60 for t, _, _ in segments)
        assert len(segments) > 1


class TestStudyVsFlatPauses:
    def test_study_mode_pauses_at_commas_flat_mode_does_not(self):
        text = "First we cover the basics, then we move to the details, and finish with a summary."
        flat = narration.plan(text, budget=200, base_speed=1.0, study=False)
        study = narration.plan(text, budget=200, base_speed=1.0, study=True)
        assert len(flat) == 1  # fits comfortably inside budget, no reason to split
        assert len(study) > 1  # study mode splits at every comma regardless of budget

    def test_study_mode_sentence_end_pause(self):
        text = "Short sentence one. Short sentence two."
        segments = narration.plan(text, budget=200, base_speed=1.0, study=True)
        assert segments[0][1] == pytest.approx(narration._PAUSE_SENTENCE)

    def test_study_mode_comma_pause(self):
        text = "First part, second part."
        segments = narration.plan(text, budget=200, base_speed=1.0, study=True)
        assert segments[0][0].endswith(",")
        assert segments[0][1] == pytest.approx(narration._PAUSE_COMMA)

    def test_pause_scale_multiplies_every_pause(self):
        text = "First part, second part. Another sentence follows here."
        base = narration.plan(text, budget=200, base_speed=1.0, study=True)
        scaled = narration.plan(text, budget=200, base_speed=1.0, study=True, pause_scale=2.0)
        for (_, p1, _), (_, p2, _) in zip(base, scaled):
            assert p2 == pytest.approx(p1 * 2.0)


class TestHeadingDetection:
    def test_short_line_before_blank_line_is_a_heading(self):
        text = "Introduction\n\nThis is the body text that follows the heading here today."
        segments = narration.plan(text, budget=200, base_speed=1.0, study=True)
        assert segments[0][0] == "Introduction"
        assert segments[0][1] == pytest.approx(narration._PAUSE_HEADING_AFTER)
        assert segments[0][2] == pytest.approx(narration._RATE_HEADING)

    def test_heading_before_pause_lands_on_preceding_segment(self):
        text = ("Some earlier paragraph that ends here.\n\n"
                "Chapter Two\n\nMore body text follows the second heading now.")
        segments = narration.plan(text, budget=200, base_speed=1.0, study=True)
        heading_idx = next(i for i, (t, _, _) in enumerate(segments) if t == "Chapter Two")
        preceding_pause = segments[heading_idx - 1][1]
        assert preceding_pause >= narration._PAUSE_HEADING_BEFORE

    def test_line_ending_in_terminal_punctuation_is_not_a_heading(self):
        text = "Short line.\n\nMore body text that follows right after this short one."
        segments = narration.plan(text, budget=200, base_speed=1.0, study=True)
        # Not read at the heading rate; the paragraph-first-sentence rate still applies.
        assert segments[0][2] == pytest.approx(narration._RATE_PARAGRAPH_FIRST_SENTENCE)
        assert segments[0][1] != pytest.approx(narration._PAUSE_HEADING_AFTER)


class TestListDetection:
    def test_dash_list_items_get_list_pause(self):
        text = "- first item\n- second item\n- third item"
        segments = narration.plan(text, budget=200, base_speed=1.0, study=True)
        texts = [t for t, _, _ in segments]
        assert texts == ["- first item", "- second item", "- third item"]
        assert segments[0][1] == pytest.approx(narration._PAUSE_LIST_ITEM)

    def test_extra_pause_before_first_item_lands_on_preceding_segment(self):
        text = "Intro sentence before the list starts here.\n- first item\n- second item"
        segments = narration.plan(text, budget=200, base_speed=1.0, study=True)
        intro_pause = segments[0][1]
        assert intro_pause >= narration._PAUSE_LIST_FIRST_EXTRA

    def test_numbered_list_items_are_detected(self):
        text = "1. first item\n2. second item"
        segments = narration.plan(text, budget=200, base_speed=1.0, study=True)
        assert [t for t, _, _ in segments] == ["1. first item", "2. second item"]

    def test_only_the_segment_before_the_first_item_gets_the_extra_pause(self):
        text = "- first item\n- second item\n- third item"
        segments = narration.plan(text, budget=200, base_speed=1.0, study=True)
        # No text precedes the first item, so nothing here carries the extra;
        # every item's own after-pause is the plain list pause.
        assert segments[0][1] == pytest.approx(narration._PAUSE_LIST_ITEM)
        assert segments[1][1] == pytest.approx(narration._PAUSE_LIST_ITEM)


class TestParagraphBreaks:
    def test_blank_line_gets_bigger_pause_than_single_newline(self):
        text = "Line one here.\nLine two here.\n\nNew paragraph line one here."
        segments = narration.plan(text, budget=200, base_speed=1.0, study=True)
        line_break_pause = next(p for t, p, _ in segments if t.rstrip(".") == "Line one here")
        para_break_pause = next(p for t, p, _ in segments if t.rstrip(".") == "Line two here")
        assert para_break_pause > line_break_pause
        assert para_break_pause == pytest.approx(narration._PAUSE_PARAGRAPH)


class TestSpeedClamping:
    def test_speed_clamped_low(self):
        segments = narration.plan("One. Two.", budget=200, base_speed=0.1)
        assert all(speed == narration._SPEED_MIN for _, _, speed in segments)

    def test_speed_clamped_high(self):
        segments = narration.plan("One. Two.", budget=200, base_speed=5.0)
        assert all(speed == narration._SPEED_MAX for _, _, speed in segments)

    def test_study_mode_heading_rate_still_clamped_low(self):
        text = "Introduction\n\nBody text that follows the short heading line right here."
        segments = narration.plan(text, budget=200, base_speed=0.51, study=True)
        # 0.51 * 0.95 would fall under the floor without clamping.
        assert segments[0][2] >= narration._SPEED_MIN
