"""Regression tests for transcript post-processing, built from REAL dictation failures.

Source: history.json (2026-06-23 full-day usage). Every string below is something the
speaker actually said that came out wrong. These tests encode the *target* behaviour.

TDD note for the implementer: several of these tests FAIL against the current code on
purpose — they define the fixes for batch-1 items #1, #2, #3 (and #5). Implement until
green. Do NOT weaken a test to make it pass; if a target is genuinely wrong, flag it.

Run:  .venv\\Scripts\\python -m pytest tests/test_postprocess_real.py -v
"""

import transcribe


def pp(text: str) -> str:
    """Post-process with fillers/corrections disabled, to isolate the mechanical rules."""
    return transcribe._postprocess(text, filler_words=[], profile_rules={}).strip()


# ---------------------------------------------------------------------------
# Item #1 — acronym-collapse must NOT mangle contractions ("it's a" -> "it'SA")
# ---------------------------------------------------------------------------

class TestContractionMangling:
    def test_its_a_not_mangled(self):
        assert "it'SA" not in pp("it's a massive app")
        assert "it's a" in pp("it's a massive app").lower()

    def test_im_a_not_mangled(self):
        assert "I'MA" not in pp("I'm a product owner")
        assert "i'm a" in pp("I'm a product owner").lower()

    def test_theres_a_not_mangled(self):
        assert "there'SA" not in pp("there's a lot of AI training")

    def test_thats_a_not_mangled(self):
        assert "that'SA" not in pp("that's a worst case scenario")

    def test_real_acronyms_still_collapse(self):
        # Genuine spelled-out acronyms (capitalised) should still join up.
        assert "AWS" in pp("deploy to A W S today")
        assert "IBM" in pp("an I.B.M. machine")


# ---------------------------------------------------------------------------
# Item #2 — pronoun "one"/"two" must stay words, not become digits
# ---------------------------------------------------------------------------

class TestPronounNumbers:
    def test_no_one(self):
        assert "no 1" not in pp("no one does that at the moment")
        assert "no one" in pp("no one does that at the moment").lower()

    def test_the_one_who(self):
        assert "the 1 who" not in pp("I'm the one who did it")

    def test_next_one(self):
        assert "next 1" not in pp("let's go to the next one, next page")

    def test_one_of_my(self):
        assert "1 of my" not in pp("one of my colleagues sent it through")

    def test_real_quantities_still_convert(self):
        # Multi-word / clear quantities should still digit-ise.
        assert "25" in pp("it has twenty five workflows")
        assert "80" in pp("eighty percent reduction")


# ---------------------------------------------------------------------------
# Item #3 — decimal & apostrophe spacing
# ---------------------------------------------------------------------------

class TestSpacing:
    def test_decimal_not_split(self):
        assert "4. 8" not in pp("using Opus 4.8 right now")
        assert "4.8" in pp("using Opus 4.8 right now")

    def test_no_space_before_apostrophe_s(self):
        assert "that 's" not in pp("that 's going to come out")
        assert "there 's" not in pp("there 's more insight")
        assert "it 's" not in pp("it 's not so AI")


# ---------------------------------------------------------------------------
# Item #5 — NZ "uptalk": don't append '?' to declarative statements
# (Lighter-touch test: a clearly declarative sentence should not end in '?'.)
# ---------------------------------------------------------------------------

class TestUptalkQuestionMarks:
    def test_declarative_not_question(self):
        out = pp("we should just still continue")
        assert not out.endswith("?")

    def test_real_question_kept(self):
        out = pp("can you hear me")
        # A structurally interrogative sentence may keep/gain a '?'. Don't force it,
        # but ensure the guard didn't strip a legitimate one if present.
        assert out  # placeholder; refine once #5 lands
