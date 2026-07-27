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


# ---------------------------------------------------------------------------
# 2026-07-27 corpus pass — 100 real dictations reviewed. Every string below is
# something Balu actually said that came out wrong; the target is what he meant.
# ---------------------------------------------------------------------------

class TestDottedNamesNotSplit:
    """The "space after punctuation" rule was splitting every domain, filename
    and package name he dictated."""

    def test_domain_survives(self):
        assert "make.powerapps.com" in pp("go to make.powerapps.com and import it")

    def test_short_domain_survives(self):
        assert "kove.nz" in pp("show them kove.nz in the browser")

    def test_package_and_file_names_survive(self):
        out = pp("use anime.js and the coderisk.zip in Downloads")
        assert "anime.js" in out and "coderisk.zip" in out

    def test_sentence_break_still_gets_its_space(self):
        assert "done. Next" in pp("that one is done.Next we look at the flows")


class TestThousandsSeparator:
    def test_comma_inside_a_number_is_not_split(self):
        assert "2,600" in pp("do the enrichment of the 2,600 emails")


class TestRepetitionLoop:
    """One real dictation came back with the same sentence twelve times: the
    decoder looping, not the speaker."""

    def test_long_loop_collapses(self):
        sentence = "Shouldn't we be building everything from the Power Platform directory? "
        out = pp("So how do we do that. " + sentence * 12)
        assert out.count("Shouldn't we be building") <= 2

    def test_saying_it_twice_is_left_alone(self):
        out = pp("Do it now. Do it now.")
        assert out.count("Do it now") == 2


class TestNonSpeechTags:
    def test_music_tag_dropped(self):
        assert pp("*sad music*") == ""

    def test_blank_audio_tag_dropped(self):
        assert "BLANK_AUDIO" not in pp("The plan is good. [BLANK_AUDIO] Next step.")

    def test_real_parenthetical_kept(self):
        assert "(the risk app)" in pp("port the canvas app (the risk app) across")


class TestSpeakerTurnDashes:
    def test_leading_dash_dropped(self):
        assert pp("- So I'm in the local app now.").startswith("So I'm in")

    def test_mid_dictation_turn_dash_dropped(self):
        assert " - Would" not in pp("Get it all done. - Would it be faster with agents?")

    def test_spoken_dash_still_works(self):
        assert "-" in pp("put a dash between them")


class TestSplitHyphens:
    def test_hyphen_split_words_rejoin(self):
        out = pp("fix the front -end and the co- authoring pop -ups")
        assert "front-end" in out and "co-authoring" in out and "pop-ups" in out

    def test_spaced_dash_is_left_as_punctuation(self):
        assert "this - it" in pp("take a look at this - it is fine")


class TestBareOneStaysAWord:
    """"one" digitised into "1" six times across the corpus, and read as a typo
    every time. Inside a real number it must still convert."""

    def test_phrases_keep_the_word(self):
        out = pp("put it in one solution, a good one, do one about SharePoint")
        assert "1" not in out

    def test_compound_numbers_still_digitise(self):
        assert "21" in pp("give me twenty one items")
        assert "100" in pp("give me one hundred ideas")


class TestVocabularySplitRepair:
    """Whisper breaks unfamiliar compounds mid-word. The configured vocabulary
    is the source of truth for putting them back together."""

    VOCAB = ["SharePoint", "Dataverse", "MyFitnessPal", "Waikato", "Copilot",
             "VoiceDictate", "Christchurch"]

    def pv(self, text: str) -> str:
        return transcribe._postprocess(text, [], {}, self.VOCAB).strip()

    def test_share_point_rejoined(self):
        assert "SharePoint" in self.pv("at work, Share Point is a data source")

    def test_data_verse_rejoined(self):
        assert "Dataverse" in self.pv("a full write up of all the data verse connections")

    def test_mid_word_split_rejoined(self):
        assert "MyFitnessPal" in self.pv("inputting things into the local MyF itnessPal")
        assert "Waikato" in self.pv("in Wai kato migration at the moment")
        assert "Copilot" in self.pv("get my GitHub Cop ilot to export")

    def test_casing_canonicalised(self):
        out = self.pv("the sharepoint list and the dataverse table")
        assert "SharePoint" in out and "Dataverse" in out

    def test_unrelated_words_untouched(self):
        assert self.pv("the data is in the source") == "The data is in the source"

    def test_no_vocabulary_is_a_no_op(self):
        assert "Share Point" in transcribe._postprocess("Share Point list", [], {}, None)


class TestSilenceHallucinations:
    """Whisper fills silence with its training data's sign-offs."""

    def test_thank_you_alone_is_dropped(self):
        assert pp("Thank you.") == ""
        assert pp("Thanks for watching!") == ""

    def test_same_words_mid_dictation_are_kept(self):
        out = pp("Thank you for the update, now push it to origin.")
        assert "Thank you" in out
