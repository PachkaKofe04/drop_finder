"""Общие фикстуры тестов."""

import pytest

import generator


@pytest.fixture(scope="session")
def synthetic():
    """Синтетические данные (seed 42): (transactions, ground_truth).

    Генерируются один раз на весь прогон. Тесты не должны их менять.
    """
    return generator.generate(42, 200)


@pytest.fixture(scope="session")
def synthetic_hard_labeled():
    """Сложный режим (seed 42) с разметкой честных людей: (transactions, ground_truth, lookalikes)."""
    return generator.generate_labeled(42, 200, generator.HARD)


@pytest.fixture(scope="session")
def synthetic_hard(synthetic_hard_labeled):
    """То же для сложного режима (seed 42): (transactions, ground_truth)."""
    return synthetic_hard_labeled[:2]
