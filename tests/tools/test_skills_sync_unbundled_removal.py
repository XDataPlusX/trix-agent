"""Скилл, который перестали поставлять, обязан исчезнуть и с машины клиента.

До 2026-09-07 `sync_skills` при исключении скилла из поставки удалял
только запись в манифесте — каталог оставался навсегда. Свежие установки
получали выверенный набор, а уже работающие машины продолжали держать
всё, платить за строку в индексе скиллов КАЖДЫМ ходом и получать от него
подсказки.

Граница ровно та же, что у миграции песочницы: удаляем только СВОЮ
нетронутую копию. Всё, что правил пользователь или агент, остаётся.
"""

from pathlib import Path

import pytest


@pytest.fixture
def sync(tmp_path, monkeypatch):
    import tools.skills_sync as s

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(s, "SKILLS_DIR", skills_dir)
    return s, skills_dir


def _make_skill(root: Path, category: str, name: str, body: str = "x") -> Path:
    d = root / category / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: \"t.\"\n---\n\n# {name}\n\n{body}\n"
    )
    return d


class TestRemovesOnlyOurUntouchedCopy:
    def test_removes_an_unmodified_copy(self, sync):
        s, root = sync
        d = _make_skill(root, "mlops", "gone-skill")
        shipped = s._dir_hash(d)

        assert s._remove_unbundled_copy("gone-skill", shipped) is not None
        assert not d.exists()

    def test_keeps_a_copy_the_user_edited(self, sync):
        """Правки пользователя — его работа, а не наш мусор."""
        s, root = sync
        d = _make_skill(root, "mlops", "edited-skill")
        shipped = s._dir_hash(d)
        (d / "SKILL.md").write_text("---\nname: edited-skill\n---\n\nмоя правка\n")

        assert s._remove_unbundled_copy("edited-skill", shipped) is None
        assert d.exists()

    def test_keeps_a_skill_with_an_extra_file(self, sync):
        """Агент дописал в скилл свой скрипт — это тоже правка."""
        s, root = sync
        d = _make_skill(root, "research", "grown-skill")
        shipped = s._dir_hash(d)
        (d / "notes.md").write_text("накопленное")

        assert s._remove_unbundled_copy("grown-skill", shipped) is None
        assert (d / "notes.md").exists()

    def test_silent_when_nothing_installed(self, sync):
        s, _ = sync
        assert s._remove_unbundled_copy("never-existed", "deadbeef") is None

    def test_touches_only_the_named_skill(self, sync):
        s, root = sync
        victim = _make_skill(root, "mlops", "target")
        bystander = _make_skill(root, "productivity", "keeper")
        s._remove_unbundled_copy("target", s._dir_hash(victim))

        assert not victim.exists()
        assert bystander.exists()


class TestResultShape:
    def test_key_present_even_on_the_opt_out_path(self, tmp_path, monkeypatch):
        """Потребитель читает result["unbundled_removed"]. Ранний выход,
        который его не кладёт, роняет вызывающего по KeyError — а проверять
        это надо ВЫЗОВОМ, а не чтением исходника (чтение текста программы
        в тестах в этом проекте запрещено)."""
        import tools.skills_sync as s

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / ".no-bundled-skills").write_text("")
        monkeypatch.setattr(s, "SKILLS_DIR", skills_dir)

        result = s.sync_skills(quiet=True)

        assert "unbundled_removed" in result
        assert result["unbundled_removed"] == []
