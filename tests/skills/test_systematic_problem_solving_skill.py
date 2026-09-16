"""Скилл общего решения задач — контракт, а не снимок текста.

Заведён 2026-09-07 по решению владельца: методички разработки убраны из
поставки клиента, но сам метод «сначала причина, потом действие» бизнесу
нужен. Проверяется то, что делает скилл пригодным, а не его формулировки.
"""

import re
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[2] / "skills" / "productivity" / \
    "systematic-problem-solving" / "SKILL.md"


@pytest.fixture(scope="module")
def text():
    return SKILL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "frontmatter отсутствует"
    return m.group(1)


class TestAuthoringStandards:
    def test_description_fits_the_hard_limit(self, frontmatter):
        """Длинные описания раздувают индекс скиллов в промпте КАЖДОГО хода."""
        d = re.search(r"^description: (.*)$", frontmatter, re.M).group(1).strip('"')
        assert len(d) <= 60, f"{len(d)} символов: {d}"
        assert d.endswith("."), d

    def test_upstream_attribution_is_kept(self, frontmatter):
        """Метод заимствован — источник должен оставаться названным."""
        author = re.search(r"^author: (.*)$", frontmatter, re.M).group(1)
        assert "obra/superpowers" in author

    def test_declares_every_platform(self, frontmatter):
        """Ничего платформенно-зависимого внутри нет — гейт был бы ложным."""
        assert "[linux, macos, windows]" in frontmatter


class TestItDoesNotDuplicateTheDebuggingSkill:
    def test_sends_technical_faults_to_systematic_debugging(self, text):
        """Две похожие методички в поставке — повод выбрать не ту."""
        assert "systematic-debugging" in text
        section = text.split("## When to Use", 1)[1].split("##", 1)[0]
        assert "systematic-debugging" in section, (
            "разграничение обязано стоять в 'When to Use', а не в примечании"
        )

    def test_carries_no_developer_vocabulary(self, text):
        """Ради этого скилл и заведён: бизнес-клиенту не нужен регистр разработки."""
        body = text.split("---", 2)[2].lower()
        for word in ("red-green", "refactor", "test suite", "stack trace",
                     "pull request", "commit"):
            assert word not in body, f"осталось слово из разработки: {word}"


class TestTheMethodSurvives:
    def test_requires_a_signal_that_tells_fixed_from_coincidence(self, text):
        """Сердце метода: без признака «прошло» починку не отличить от совпадения."""
        assert "how would we know it stopped" in text.lower()

    def test_demands_more_than_one_candidate_cause(self, text):
        assert re.search(r"at least three", text, re.I)

    def test_requires_ruling_out_by_evidence_not_taste(self, text):
        assert re.search(r"with evidence, not with plausibility", text, re.I)

    def test_forces_unverified_claims_to_be_labelled(self, text):
        """Иначе агент выдаёт догадку с уверенным лицом — главный риск метода."""
        assert "unverified" in text.lower()

    def test_leaves_the_decision_to_the_user(self, text):
        assert "The decision is the user's." in text

    def test_names_only_real_hermes_tools(self, text):
        """Скилл, зовущий несуществующий инструмент, учит модель галлюцинировать."""
        import toolsets

        known = set()
        for name in toolsets.TOOLSETS:
            known.update(toolsets.resolve_toolset(name, include_registry=False) or [])
        for tool in re.findall(r"`([a-z_]+)`", text):
            if tool in {"todo", "read_file", "terminal", "web_search", "clarify"}:
                assert tool in known, f"инструмента {tool} не существует"


class TestRequiredSections:
    @pytest.mark.parametrize("section", [
        "## When to Use", "## Prerequisites", "## How to Run",
        "## Quick Reference", "## Procedure", "## Pitfalls", "## Verification",
    ])
    def test_section_present(self, text, section):
        assert section in text
