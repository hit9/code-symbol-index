from code_symbol_index import CodeIndex, IndexNotFoundError, Inspection, Position, Range, Reference, Repository, Symbol, index, main, search


def test_public_api_imports() -> None:
    assert CodeIndex
    assert IndexNotFoundError
    assert Inspection
    assert Position
    assert Range
    assert Reference
    assert Repository
    assert Symbol
    assert index
    assert main
    assert search
