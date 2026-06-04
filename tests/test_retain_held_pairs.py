"""
Testes da retenção de pares com posição aberta no rescore (_retain_held_pairs).

Bug: o rescore horário rotacionava quase todo o universo e, ao remover um par,
FORÇAVA o fechamento da posição aberta nele ("Par removido da lista") — cortando
o trade no meio, independente do SL/TP. Fix: um par com posição aberta nunca é
rotacionado para fora; segue gerido até fechar no próprio alvo.
"""
from __future__ import annotations

from trading_bot.core.bot import _retain_held_pairs


def test_no_held_symbols_keeps_selection():
    final, retained = _retain_held_pairs(["AGT", "ETH", "G"], set())
    assert final == ["AGT", "ETH", "G"]
    assert retained == []


def test_held_already_in_selection_is_not_duplicated():
    final, retained = _retain_held_pairs(["AGT", "ETH", "G"], {"ETH"})
    assert final == ["AGT", "ETH", "G"]  # ETH já está → nada a reter
    assert retained == []


def test_held_pair_that_fell_off_is_retained_and_appended():
    # BCH tem posição aberta mas caiu do top score → mantém na lista
    final, retained = _retain_held_pairs(["AGT", "ETH", "G"], {"BCH"})
    assert "BCH" in final
    assert final == ["AGT", "ETH", "G", "BCH"]  # anexado ao fim
    assert retained == ["BCH"]


def test_multiple_held_pairs_retained_sorted():
    final, retained = _retain_held_pairs(["ETH"], {"BCH", "ZRO", "ETH"})
    assert retained == ["BCH", "ZRO"]  # ETH já está; ordenado
    assert final == ["ETH", "BCH", "ZRO"]


def test_selection_order_is_preserved():
    final, _ = _retain_held_pairs(["G", "AGT", "ETH"], {"DOGE"})
    assert final[:3] == ["G", "AGT", "ETH"]  # ordem por score preservada
    assert final[-1] == "DOGE"


def test_does_not_mutate_input_list():
    original = ["AGT", "ETH"]
    final, _ = _retain_held_pairs(original, {"BCH"})
    assert original == ["AGT", "ETH"]  # entrada intacta
    assert final == ["AGT", "ETH", "BCH"]
