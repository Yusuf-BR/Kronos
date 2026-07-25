# tests/test_resolution_pipeline.py
"""
Regression tests for KRONOS entity resolution, confidence engine, and alias memory.

Run with: pytest tests/test_resolution_pipeline.py -v
"""

import json
import tempfile
from pathlib import Path

import pytest

# ── Real imports from your codebase ──
from agents.codex import (
    is_negation_pair,
    has_numeric_conflict,
    is_cross_domain_mismatch,
    is_disqualified_match,
    combine_confidence,
    validate_entity,
    validate_relationship,
    NEW_ENTITY_RESOLUTION_CONFIDENCE,
    REJECTED_REFEREE_RESOLUTION_CONFIDENCE,
    MIN_FUZZY_MATCH_LENGTH,
    FUZZY_MATCH_THRESHOLD,
)
from agents.extractor import (
    EXTRACTION_CONFIDENCE_BY_TIER,
    compute_extraction_confidence,
    FALLBACK_CONFIDENCE_PENALTY,
)
from agents.evidence_engine import (
    REINFORCEMENT_STEP,
    MAX_CONFIDENCE,
    apply_reinforcement,
    merge_evidence,
    build_evidence_record,
)
from agents.alias_memory import (
    AliasMemory,
    ALIAS_COMMIT_THRESHOLD,
    REUSE_REINFORCEMENT,
)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Disqualifier Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestNegationPairs:
    """Tests for is_negation_pair() — hard block on semantic opposites."""

    NEGATION_CASES = [
        ("supervised learning", "unsupervised learning"),
        ("supervised", "unsupervised"),
        ("valid", "invalid"),
        ("possible", "impossible"),
        ("regular", "irregular"),
        ("legal", "illegal"),
        ("agree", "disagree"),
        ("matter", "antimatter"),
        ("code", "decode"),
        ("understand", "misunderstand"),
        ("nuclear", "nonnuclear"),
        ("nuclear", "non-nuclear"),
        ("alignment", "nonalignment"),
    ]

    NON_NEGATION_CASES = [
        ("happy", "happiness"),
        ("create", "creative"),
        ("Table 4.1", "Table 4.2"),
        ("fast", "faster"),
        ("good", "goods"),
        ("use", "user"),
        ("inform", "information"),
        ("unified", "unified"),  # same word
        ("nonprofit", "nonprofit"),  # same word
    ]

    @pytest.mark.parametrize("a, b", NEGATION_CASES)
    def test_negation_pair_detected(self, a, b):
        assert is_negation_pair(a, b) is True, f"Expected negation pair: '{a}' vs '{b}'"

    @pytest.mark.parametrize("a, b", NON_NEGATION_CASES)
    def test_non_negation_pair_passes(self, a, b):
        assert is_negation_pair(a, b) is False, f"Expected NOT negation pair: '{a}' vs '{b}'"

    def test_negation_is_symmetric(self):
        assert is_negation_pair("supervised", "unsupervised") == is_negation_pair("unsupervised", "supervised")


class TestNumericConflicts:
    """Tests for has_numeric_conflict() — hard block on differing numbers."""

    CONFLICT_CASES = [
        ("Table 4.1", "Table 4.2"),
        ("Section 2", "Section 3"),
        ("Chapter 1", "Chapter 10"),
        ("Python 3", "Python 2.7"),
        ("Figure 5", "Figure 6"),
        ("Eq. 3.14", "Eq. 3.15"),
        ("v1.0", "v2.0"),
        ("Version 2", "Version 2.0"),   # "2" != "2.0" structurally
        ("Item 1", "Item 01"),           # "1" != "01" structurally
    ]

    NO_CONFLICT_CASES = [
        ("Table 4.1", "Table 4.1"),
        ("Section 2", "Section 2"),
        ("Python 3", "Python 3"),
        ("No numbers here", "Also none"),
    ]

    @pytest.mark.parametrize("a, b", CONFLICT_CASES)
    def test_numeric_conflict_detected(self, a, b):
        assert has_numeric_conflict(a, b) is True

    @pytest.mark.parametrize("a, b", NO_CONFLICT_CASES)
    def test_no_numeric_conflict(self, a, b):
        assert has_numeric_conflict(a, b) is False


class TestCrossDomainMismatch:
    """Tests for is_cross_domain_mismatch() and is_disqualified_match() — Component 10 guard."""

    def test_same_domain_is_allowed(self):
        assert is_cross_domain_mismatch("MachineLearning", "MachineLearning") is False

    def test_different_domains_is_mismatch(self):
        assert is_cross_domain_mismatch("MachineLearning", "Biology") is True

    def test_none_domain_is_permissive(self):
        """Legacy entities (pre-Component 10) without domain must not be blocked."""
        assert is_cross_domain_mismatch(None, "MachineLearning") is False
        assert is_cross_domain_mismatch("MachineLearning", None) is False
        assert is_cross_domain_mismatch(None, None) is False

    def test_whitespace_normalized_to_same_domain(self):
        """Whitespace and case differences must collapse to the same domain."""
        assert is_cross_domain_mismatch(" MachineLearning ", "machinelearning") is False

    def test_whitespace_different_domains_still_mismatch(self):
        """Different domains must mismatch even with whitespace."""
        assert is_cross_domain_mismatch(" MachineLearning ", " Biology ") is True

    def test_disqualified_match_includes_domain(self):
        """is_disqualified_match must return domain reason when domains differ."""
        result = is_disqualified_match("Neural Network", "Neural Network", "ML", "Biology")
        assert result is not None
        assert "different domains" in result

    def test_disqualified_match_none_domain_is_none(self):
        """Missing domain must not trigger disqualification."""
        result = is_disqualified_match("Python", "Python", None, "Software")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Confidence Engine Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfidenceCalculation:
    """Tests for combine_confidence() and compute_extraction_confidence()."""

    def test_multiplicative_confidence(self):
        """Final confidence = extraction × resolution × ontology."""
        extraction = 0.95
        resolution = 0.90
        ontology = 0.95
        result = combine_confidence(extraction, resolution, ontology)
        expected = round(0.95 * 0.90 * 0.95, 4)
        assert result == expected
        assert result < extraction
        assert result < resolution
        assert result < ontology

    def test_confidence_never_defaults_to_one(self):
        """Brand-new entity (resolution=1.0) with perfect extraction and ontology."""
        result = combine_confidence(0.95, NEW_ENTITY_RESOLUTION_CONFIDENCE, 1.0)
        assert result == 0.95
        assert result != 1.0

    def test_rejected_referee_confidence(self):
        """When referee correctly keeps entities separate, confidence is 0.75."""
        result = combine_confidence(0.95, REJECTED_REFEREE_RESOLUTION_CONFIDENCE, 1.0)
        expected = round(0.95 * 0.75, 4)
        assert result == expected
        assert result > 0.5

    def test_tier_confidence_values(self):
        """EXTRACTION_CONFIDENCE_BY_TIER must have exactly tiers 1-3."""
        assert EXTRACTION_CONFIDENCE_BY_TIER[1] == 0.95
        assert EXTRACTION_CONFIDENCE_BY_TIER[2] == 0.85
        assert EXTRACTION_CONFIDENCE_BY_TIER[3] == 0.70

    def test_compute_extraction_confidence_tier1_perfect_quality(self):
        result = compute_extraction_confidence(tier=1, quality_score=1.0, used_fallback=False)
        assert result == 0.95

    def test_compute_extraction_confidence_tier3_low_quality(self):
        result = compute_extraction_confidence(tier=3, quality_score=0.5, used_fallback=False)
        expected = round(0.70 * 0.5, 4)
        assert result == expected

    def test_compute_extraction_confidence_with_fallback_penalty(self):
        result = compute_extraction_confidence(tier=1, quality_score=1.0, used_fallback=True)
        expected = round(0.95 * FALLBACK_CONFIDENCE_PENALTY, 4)
        assert result == expected

    def test_combine_confidence_with_none(self):
        """None values must be treated as 0.0, not crash."""
        result = combine_confidence(0.9, None, 0.8)
        assert result == 0.0

    def test_combine_confidence_clamps_negative(self):
        """Negative inputs must be clamped to 0.0."""
        result = combine_confidence(-0.5, 0.9, 1.0)
        assert result == 0.0

    def test_combine_confidence_clamps_above_one(self):
        """Inputs above 1.0 must be clamped."""
        result = combine_confidence(1.5, 0.9, 1.0)
        assert result == round(1.0 * 0.9 * 1.0, 4)


class TestConfidenceReinforcement:
    """Tests for apply_reinforcement() and REINFORCEMENT_STEP."""

    def test_reinforcement_bumps_fresh_confidence(self):
        base = 0.70
        reinforced = apply_reinforcement(base, REINFORCEMENT_STEP)
        assert reinforced == round(base + REINFORCEMENT_STEP, 4)

    def test_reinforcement_caps_at_max(self):
        base = 0.98
        reinforced = apply_reinforcement(base, REINFORCEMENT_STEP)
        assert reinforced == MAX_CONFIDENCE

    def test_reinforcement_does_not_compound_bug6(self):
        """
        Critical regression test for Bug #6.
        Reprocessing must not compound confidence endlessly.
        """
        stored_confidence = 0.75  # Previously stored (already reinforced once)
        fresh_base = 0.70  # Fresh calculation for this ingestion
        correct_result = apply_reinforcement(fresh_base, REINFORCEMENT_STEP)
        # Must NOT drift toward 1.0 purely from mention count
        assert correct_result < stored_confidence + REINFORCEMENT_STEP + 0.01

    def test_zero_bump_does_nothing(self):
        assert apply_reinforcement(0.80, 0.0) == 0.80


class TestEvidenceMerge:
    """Tests for merge_evidence() — dedup and reinforcement triggering."""

    def test_new_evidence_triggers_reinforcement(self):
        existing = None
        new = build_evidence_record("doc1.pdf", 1, "chunk123", "excerpt here", 0.9)
        updated_json, is_new, bump = merge_evidence(existing, new)
        assert is_new is True
        assert bump == REINFORCEMENT_STEP
        data = json.loads(updated_json)
        assert len(data) == 1
        assert data[0]["source_doc"] == "doc1.pdf"

    def test_duplicate_evidence_no_reinforcement(self):
        new = build_evidence_record("doc1.pdf", 1, "chunk123", "excerpt here", 0.9)
        existing_json, _, _ = merge_evidence(None, new)
        updated_json, is_new, bump = merge_evidence(existing_json, new)
        assert is_new is False
        assert bump == 0.0

    def test_different_chunk_same_doc_is_new_evidence(self):
        """Same source doc, different chunk_id = genuinely new evidence."""
        first = build_evidence_record("doc1.pdf", 1, "chunkA", "excerpt A", 0.9)
        existing_json, _, _ = merge_evidence(None, first)
        second = build_evidence_record("doc1.pdf", 1, "chunkB", "excerpt B", 0.8)
        updated_json, is_new, bump = merge_evidence(existing_json, second)
        assert is_new is True
        assert bump == REINFORCEMENT_STEP
        assert len(json.loads(updated_json)) == 2

    def test_evidence_cap_at_50(self):
        """Evidence list must not grow unbounded."""
        existing = None
        for i in range(55):
            rec = build_evidence_record(f"doc{i}.pdf", 1, f"chunk{i}", "text", 0.5)
            existing, _, _ = merge_evidence(existing, rec)
        data = json.loads(existing)
        assert len(data) == 50


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Alias Memory Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestAliasMemoryCommitThreshold:
    """Tests for ALIAS_COMMIT_THRESHOLD = 0.93."""

    @pytest.fixture
    def temp_alias_memory(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write('{}')
            temp_path = f.name
        mem = AliasMemory(memory_file=temp_path)
        yield mem
        Path(temp_path).unlink(missing_ok=True)

    def test_below_threshold_not_committed(self, temp_alias_memory):
        """Confidence 0.92 must NOT be written to alias_memory.json."""
        temp_alias_memory.record(
            name="GPU",
            canonical="Graphics Processing Unit",
            confidence=0.92,
            source="embedding"
        )
        assert "gpu" not in temp_alias_memory.memory

    def test_at_threshold_is_committed(self, temp_alias_memory):
        """Confidence exactly 0.93 MUST be written."""
        temp_alias_memory.record(
            name="GPU",
            canonical="Graphics Processing Unit",
            confidence=0.93,
            source="embedding"
        )
        assert "gpu" in temp_alias_memory.memory
        assert temp_alias_memory.memory["gpu"]["canonical"] == "Graphics Processing Unit"

    def test_above_threshold_is_committed(self, temp_alias_memory):
        temp_alias_memory.record(
            name="ML",
            canonical="Machine Learning",
            confidence=0.95,
            source="referee"
        )
        assert "ml" in temp_alias_memory.memory

    def test_reuse_reinforces(self, temp_alias_memory):
        """Each hit on get() must increment times_seen and nudge confidence."""
        temp_alias_memory.record(
            name="GPU",
            canonical="Graphics Processing Unit",
            confidence=0.95,
            source="embedding"
        )
        initial_times = temp_alias_memory.memory["gpu"]["times_seen"]
        initial_conf = temp_alias_memory.memory["gpu"]["confidence"]

        for _ in range(5):
            temp_alias_memory.get("GPU")

        entry = temp_alias_memory.memory["gpu"]
        assert entry["times_seen"] == initial_times + 5
        assert entry["confidence"] > initial_conf
        assert entry["confidence"] <= MAX_CONFIDENCE

    def test_conflict_higher_confidence_overwrites(self, temp_alias_memory):
        """Same alias pointing to different canonical: higher confidence wins."""
        temp_alias_memory.record(
            name="JS",
            canonical="JavaScript",
            confidence=0.94,
            source="fuzzy"
        )
        temp_alias_memory.record(
            name="JS",
            canonical="John Smith",
            confidence=0.96,
            source="referee"
        )
        assert temp_alias_memory.memory["js"]["canonical"] == "John Smith"
        assert temp_alias_memory.memory["js"]["confidence"] == 0.96

    def test_conflict_lower_confidence_ignored(self, temp_alias_memory):
        temp_alias_memory.record(
            name="JS",
            canonical="JavaScript",
            confidence=0.95,
            source="embedding"
        )
        temp_alias_memory.record(
            name="JS",
            canonical="Junk Science",
            confidence=0.80,
            source="fuzzy"
        )
        assert temp_alias_memory.memory["js"]["canonical"] == "JavaScript"

    def test_flag_bad_entry_removes_alias(self, temp_alias_memory):
        temp_alias_memory.record(
            name="BadAlias",
            canonical="WrongCanonical",
            confidence=0.95,
            source="typo"
        )
        assert "badalias" in temp_alias_memory.memory
        removed = temp_alias_memory.flag_bad_entry("BadAlias", reason="test cleanup")
        assert removed is True
        assert "badalias" not in temp_alias_memory.memory

    def test_get_unknown_returns_none(self, temp_alias_memory):
        canonical, confidence = temp_alias_memory.get("UNKNOWN_ENTITY")
        assert canonical is None
        assert confidence == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Validation Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestEntityValidation:
    """Tests for validate_entity() — Bug #10 guard."""

    def test_valid_entity_passes(self):
        entity = {"name": "Python", "type": "TECHNOLOGY", "description": "A language"}
        assert validate_entity(entity) is None

    def test_missing_name_fails(self):
        entity = {"type": "TECHNOLOGY"}
        assert validate_entity(entity) is not None
        assert "name" in validate_entity(entity)

    def test_missing_type_fails(self):
        entity = {"name": "Python"}
        assert validate_entity(entity) is not None
        assert "type" in validate_entity(entity)

    def test_empty_name_fails(self):
        entity = {"name": "", "type": "TECHNOLOGY"}
        assert validate_entity(entity) is not None

    def test_none_name_fails(self):
        entity = {"name": None, "type": "TECHNOLOGY"}
        assert validate_entity(entity) is not None


class TestRelationshipValidation:
    """Tests for validate_relationship() — Bug #10 guard."""

    def test_valid_relationship_passes(self):
        rel = {
            "from": "Python", "from_type": "TECHNOLOGY",
            "to": "FastAPI", "to_type": "TECHNOLOGY",
            "relation": "USES"
        }
        assert validate_relationship(rel) is None

    def test_missing_from_fails(self):
        rel = {"from_type": "TECH", "to": "B", "to_type": "TECH", "relation": "USES"}
        assert validate_relationship(rel) is not None

    def test_missing_relation_fails(self):
        rel = {"from": "A", "from_type": "TECH", "to": "B", "to_type": "TECH"}
        assert validate_relationship(rel) is not None

    def test_empty_relation_fails(self):
        rel = {"from": "A", "from_type": "TECH", "to": "B", "to_type": "TECH", "relation": ""}
        assert validate_relationship(rel) is not None


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Known Bug Regression Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestKnownBugRegressions:
    """Verify specific bugs from project docs (Section 5) never recur."""

    def test_bug_7_typo_not_auto_committed(self):
        """
        Bug #7: "unsupervised learning" was merged into "Supervised learning"
        purely because they're a 2-character edit apart.
        Fix: typo candidates require LLM referee confirmation before caching.
        """
        # Negation guard catches this anyway
        assert is_negation_pair("supervised learning", "unsupervised learning") is True
        # Even without negation, a typo candidate must not be auto-committed
        # to alias memory without referee confirmation. The commit threshold
        # (0.93) and the referee path in codex.py enforce this.

    def test_bug_8_table_numbers_not_cross_contaminated(self):
        """Bug #8: Table 4.2 must not merge with Table 4.1."""
        assert has_numeric_conflict("Table 4.2", "Table 4.1") is True
        assert has_numeric_conflict("Table 2.1", "Table 2.2") is True
        # Same number should NOT conflict
        assert has_numeric_conflict("Table 4.1", "Table 4.1") is False

    def test_bug_6_confidence_not_compounded(self):
        """Bug #6: Repeated mentions must not drift confidence to 1.0."""
        # See TestConfidenceReinforcement.test_reinforcement_does_not_compound_bug6
        base = 0.70
        reinforced_once = apply_reinforcement(base, REINFORCEMENT_STEP)
        # Simulating old bug: reinforcing the already-reinforced value
        wrong_compound = apply_reinforcement(reinforced_once, REINFORCEMENT_STEP)
        # Correct behavior: always reinforce from fresh base
        correct = apply_reinforcement(base, REINFORCEMENT_STEP)
        assert wrong_compound > correct  # Old bug would be higher
        assert correct == 0.75

    def test_bug_10_malformed_entity_skipped(self):
        """Bug #10: Malformed entities must be caught before processing."""
        bad_entity = {"description": "missing name and type"}
        assert validate_entity(bad_entity) is not None

    def test_bug_5_narrow_relation_enum(self):
        """
        Bug #5: ~35% of valid relationships were discarded by narrow enum.
        Fix: Ontology Resolver now maps unknown relations to RELATED_TO instead
        of dropping them. This is architectural, tested via the resolver.
        """
        # Placeholder: if you expose OntologyResolver without LLM mocking,
        # add a test here that "HAS_PHASE" maps to "PART_OF" via synonym map.
        pass