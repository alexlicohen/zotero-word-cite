"""Tests for zoterocite.textpatterns — shared superset regex constants."""

import pytest
from zoterocite.textpatterns import (
    PVALUE_RE,
    EFFECT_SIZE_RE,
    DISPERSION_RE,
    SIG_DEF_RE,
    FIG_LABEL_RE,
    TABLE_LABEL_RE,
    FIG_REF_RE,
    TABLE_REF_RE,
    FIG_LEGEND_LINE_RE,
    TABLE_LEGEND_LINE_RE,
    SUPPL_FIG_REF_RE,
    SUPPL_TABLE_REF_RE,
    SUPPL_FIG_LEGEND_LINE_RE,
    SUPPL_TABLE_LEGEND_LINE_RE,
    extract_fig_refs,
    extract_table_refs,
)


# ---------------------------------------------------------------------------
# PVALUE_RE — must be the union of structure and claimcheck variants
# ---------------------------------------------------------------------------

class TestPvalueRE:
    # structure.py forms (≤≥ and en-dash)
    def test_less_than_decimal(self):
        assert PVALUE_RE.search("p < 0.05")

    def test_equals_form(self):
        assert PVALUE_RE.search("p = 0.03")

    def test_leq_unicode(self):
        # claimcheck lacked ≤ — this is a union fix
        assert PVALUE_RE.search("p ≤ .05")

    def test_geq_unicode(self):
        assert PVALUE_RE.search("p ≥ 0.01")

    def test_leading_dot_form(self):
        # APA style: p < .05 (no leading 0)
        assert PVALUE_RE.search("p < .05")

    def test_en_dash_pvalue(self):
        # structure.py: bp[-–] — en-dash variant
        assert PVALUE_RE.search("p–value was significant")

    def test_hyphen_pvalue(self):
        assert PVALUE_RE.search("p-value < 0.05")

    def test_pvalue_space(self):
        # claimcheck.py: bp[\s-]*values?
        assert PVALUE_RE.search("p value")

    def test_pvalues_plural(self):
        assert PVALUE_RE.search("p-values were corrected")

    def test_no_match_plain_p(self):
        assert not PVALUE_RE.search("p is for pterodactyl")

    def test_greater_than(self):
        assert PVALUE_RE.search("p > 0.05")


# ---------------------------------------------------------------------------
# EFFECT_SIZE_RE — union of structure._EFFECT_SIZE_RE + claimcheck._RATIO_RE
# ---------------------------------------------------------------------------

class TestEffectSizeRE:
    # From structure
    def test_95ci(self):
        assert EFFECT_SIZE_RE.search("95% CI")

    def test_effect_size_prose(self):
        assert EFFECT_SIZE_RE.search("effect size was large")

    def test_cohen(self):
        assert EFFECT_SIZE_RE.search("Cohen d = 0.8")

    def test_or_equals(self):
        assert EFFECT_SIZE_RE.search("OR = 1.8")

    def test_hr_equals(self):
        assert EFFECT_SIZE_RE.search("HR = 2.3")

    def test_beta_equals(self):
        assert EFFECT_SIZE_RE.search("β = 0.45")

    def test_r_equals(self):
        assert EFFECT_SIZE_RE.search("r = -0.6")

    def test_hedges(self):
        assert EFFECT_SIZE_RE.search("Hedges g")

    def test_confidence_interval(self):
        assert EFFECT_SIZE_RE.search("confidence interval")

    # From claimcheck._RATIO_RE (additions to superset)
    def test_rr_equals_digit(self):
        assert EFFECT_SIZE_RE.search("RR = 1.4")

    def test_or_of_form(self):
        # "OR of 2" form from claimcheck
        assert EFFECT_SIZE_RE.search("OR of 2.1")

    def test_odds_ratio_prose(self):
        # "odds ratio was 2.1" — the critical acceptance-criteria case
        assert EFFECT_SIZE_RE.search("odds ratio was 2.1")

    def test_hazard_ratio_prose(self):
        assert EFFECT_SIZE_RE.search("hazard ratio of 1.3")

    def test_risk_ratio_prose(self):
        assert EFFECT_SIZE_RE.search("risk ratio 0.8")

    def test_no_match_random(self):
        assert not EFFECT_SIZE_RE.search("the sample size was large")


# ---------------------------------------------------------------------------
# DISPERSION_RE — from structure._M8_ERROR_DEF_RE
# ---------------------------------------------------------------------------

class TestDispersionRE:
    def test_sd(self):
        assert DISPERSION_RE.search("mean ± SD")

    def test_standard_deviation(self):
        assert DISPERSION_RE.search("standard deviation")

    def test_sem(self):
        assert DISPERSION_RE.search("SEM")

    def test_standard_error(self):
        assert DISPERSION_RE.search("standard error of the mean")

    def test_ci_bare(self):
        assert DISPERSION_RE.search("CI")

    def test_confidence_interval(self):
        assert DISPERSION_RE.search("confidence interval")


# ---------------------------------------------------------------------------
# SIG_DEF_RE — from structure._M8_SIG_DEF_RE
# ---------------------------------------------------------------------------

class TestSigDefRE:
    def test_asterisk_p(self):
        assert SIG_DEF_RE.search("* p < 0.05")

    def test_p_less_than(self):
        assert SIG_DEF_RE.search("p < 0.05")

    def test_p_leq(self):
        assert SIG_DEF_RE.search("p ≤ 0.01")

    def test_p_dash_value(self):
        assert SIG_DEF_RE.search("p-value")

    def test_significant(self):
        assert SIG_DEF_RE.search("significant difference")

    def test_significance(self):
        assert SIG_DEF_RE.search("statistical significance")


# ---------------------------------------------------------------------------
# Figure / table label patterns
# ---------------------------------------------------------------------------

class TestFigLabelRE:
    def test_figure_n(self):
        assert FIG_LABEL_RE.search("Figure 1.")

    def test_fig_dot_n(self):
        assert FIG_LABEL_RE.search("Fig. 3 shows")

    def test_fig_n_space(self):
        assert FIG_LABEL_RE.search("Fig 2 ")

    def test_case_insensitive(self):
        assert FIG_LABEL_RE.search("FIGURE 4.")

    def test_no_match_suppl(self):
        # Supplementary label — handled by SUPPL_* patterns
        assert not FIG_LABEL_RE.search("Figure S1.")


class TestTableLabelRE:
    def test_table_n(self):
        assert TABLE_LABEL_RE.search("Table 1.")

    def test_table_n_space(self):
        assert TABLE_LABEL_RE.search("Table 2 ")

    def test_no_match_suppl(self):
        assert not TABLE_LABEL_RE.search("Table S2.")


# ---------------------------------------------------------------------------
# Figure / table in-text references
# ---------------------------------------------------------------------------

class TestFigRefRE:
    def test_figure_ref(self):
        assert FIG_REF_RE.search("(Figure 2)")

    def test_fig_dot_ref(self):
        assert FIG_REF_RE.search("(Fig. 1)")

    def test_figs_plural(self):
        # claimcheck _FIG_TABLE_RE also matched Figs.
        assert FIG_REF_RE.search("Figs. 2-3")

    def test_figures_plural(self):
        assert FIG_REF_RE.search("Figures 2 and 3")

    def test_no_match_suppl(self):
        assert not FIG_REF_RE.search("Figure S1")


class TestTableRefRE:
    def test_table_ref(self):
        assert TABLE_REF_RE.search("Table 1")

    def test_tables_plural(self):
        assert TABLE_REF_RE.search("Tables 1-2")

    def test_no_match_suppl(self):
        assert not TABLE_REF_RE.search("Table S1")


# ---------------------------------------------------------------------------
# Legend line patterns
# ---------------------------------------------------------------------------

class TestFigLegendLineRE:
    def test_figure_legend(self):
        assert FIG_LEGEND_LINE_RE.match("Figure 1. Caption text.")

    def test_fig_dot_legend(self):
        assert FIG_LEGEND_LINE_RE.match("Fig. 2. Caption.")

    def test_leading_space(self):
        assert FIG_LEGEND_LINE_RE.match("  Figure 3. Caption.")

    def test_no_suppl(self):
        assert not FIG_LEGEND_LINE_RE.match("Figure S1.")


class TestTableLegendLineRE:
    def test_table_legend(self):
        assert TABLE_LEGEND_LINE_RE.match("Table 1. Demographics.")

    def test_no_suppl(self):
        assert not TABLE_LEGEND_LINE_RE.match("Table S2.")


# ---------------------------------------------------------------------------
# Supplementary reference patterns
# ---------------------------------------------------------------------------

class TestSupplFigRefRE:
    def test_figure_s1(self):
        assert SUPPL_FIG_REF_RE.search("Figure S1")

    def test_fig_s2(self):
        assert SUPPL_FIG_REF_RE.search("Fig. S2")

    def test_supplementary_figure(self):
        assert SUPPL_FIG_REF_RE.search("Supplementary Figure 3")

    def test_suppl_dot_figure(self):
        assert SUPPL_FIG_REF_RE.search("Suppl. Figure 1")


class TestSupplTableRefRE:
    def test_table_s1(self):
        assert SUPPL_TABLE_REF_RE.search("Table S1")

    def test_supplementary_table(self):
        assert SUPPL_TABLE_REF_RE.search("Supplementary Table 2")

    def test_suppl_dot_table(self):
        assert SUPPL_TABLE_REF_RE.search("Suppl. Table 3")


class TestSupplFigLegendLineRE:
    def test_figure_s1_legend(self):
        assert SUPPL_FIG_LEGEND_LINE_RE.match("Figure S1.")

    def test_supplementary_figure_legend(self):
        assert SUPPL_FIG_LEGEND_LINE_RE.match("Supplementary Figure 2.")

    def test_suppl_dot_figure_legend(self):
        assert SUPPL_FIG_LEGEND_LINE_RE.match("Suppl. Figure 1.")


class TestSupplTableLegendLineRE:
    def test_table_s1_legend(self):
        assert SUPPL_TABLE_LEGEND_LINE_RE.match("Table S1.")

    def test_supplementary_table_legend(self):
        assert SUPPL_TABLE_LEGEND_LINE_RE.match("Supplementary Table 3.")

    def test_suppl_dot_table_legend(self):
        assert SUPPL_TABLE_LEGEND_LINE_RE.match("Suppl. Table 2.")


# ---------------------------------------------------------------------------
# B3-st regression: extract_fig_refs / extract_table_refs — multi-number expansion
# ---------------------------------------------------------------------------

class TestExtractFigRefs:
    """B3-st: 'Figures 1 and 2' must yield {1,2}, 'Figs 1-3' must yield {1,2,3},
    single 'Figure 4' must yield {4}.
    Previously FIG_REF_RE only captured the first integer, causing false
    figure-orphan findings and false-clean on genuinely missing panels.
    """

    def test_single_figure(self):
        assert extract_fig_refs("Figure 4") == {4}

    def test_figures_and(self):
        assert extract_fig_refs("Figures 1 and 2") == {1, 2}

    def test_figs_range(self):
        assert extract_fig_refs("Figs 1-3") == {1, 2, 3}

    def test_figures_comma_list(self):
        assert extract_fig_refs("Figures 1, 2 and 3") == {1, 2, 3}

    def test_fig_dot_single(self):
        assert extract_fig_refs("(Fig. 5)") == {5}

    def test_no_suppl(self):
        # Supplementary refs must not bleed into extract_fig_refs
        assert extract_fig_refs("Figure S1") == set()

    def test_multiple_refs_in_sentence(self):
        # "see Figures 1 and 2 and Table 1" — only figure numbers
        result = extract_fig_refs("see Figures 1 and 2 and Table 1")
        assert 1 in result and 2 in result

    def test_en_dash_range(self):
        # En-dash range
        assert extract_fig_refs("Figures 2–4") == {2, 3, 4}

    # R5-2 regression: a continuation number that is really a prose QUANTITY
    # (immediately followed by a noun) must not leak into the figure set.
    def test_quantity_after_and_not_a_figure(self):
        assert extract_fig_refs("Figure 1 and 23 patients") == {1}

    def test_quantity_after_comma_not_a_figure(self):
        assert extract_fig_refs("Figure 1, 100 cells") == {1}

    def test_quantity_after_range_dash_not_a_figure(self):
        # "-12 mice" is a quantity, not a range end-point.
        assert extract_fig_refs("Figure 3-12 mice were imaged") == {3}

    def test_connective_after_continuation_keeps_figure(self):
        # A continuation number followed by a CONNECTIVE (not a noun) is still a
        # figure number: "Figures 1 and 2 and Table 1" keeps 2.
        result = extract_fig_refs("see Figures 1 and 2 and Table 1")
        assert result == {1, 2}, result


class TestExtractTableRefs:
    """B3-st: equivalent coverage for table references."""

    def test_single_table(self):
        assert extract_table_refs("Table 1") == {1}

    def test_tables_and(self):
        assert extract_table_refs("Tables 1 and 2") == {1, 2}

    def test_tables_range(self):
        assert extract_table_refs("Tables 1-3") == {1, 2, 3}

    def test_tables_comma_list(self):
        assert extract_table_refs("Tables 1, 2 and 3") == {1, 2, 3}

    def test_no_suppl(self):
        assert extract_table_refs("Table S1") == set()
