from riki_memory.schemas import Extraction

from app.memory import lenient

ITEM = '{"content": "Likes chai.", "kind": "fact", "importance": 1, "semantic": false}'


def test_a_list_the_model_sent_as_a_json_string_is_read_as_the_list():
    # Bug seen in real use: kimi-k3 answered {"memories": "[{...}]"} and the whole turn's memories were lost.
    for reply in (f'{{"memories": [{ITEM}]}}', f'{{"memories": "[{ITEM.replace(chr(34), chr(92) + chr(34))}]"}}'):
        got = lenient(Extraction).model_validate_json(reply)
        assert [m.content for m in got.memories] == ["Likes chai."], reply
    assert lenient(Extraction).model_validate_json('{"memories": "[]"}').memories == []
    assert isinstance(lenient(Extraction).model_validate_json('{"memories": []}'), Extraction)
