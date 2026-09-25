"""Общие фикстуры тестов."""

import pytest

import generator


@pytest.fixture(scope="session")
def synthetic():
    """Синтетические данные (seed 42): (transactions, ground_truth).

    Генерируются один раз на весь прогон. Тесты не должны их менять.
    """
    return generator.generate(42, 200)
