from app.services import Messenger


def test_long_whatsapp_reply_is_split_without_losing_text() -> None:
    body = ("goal line\n" * 400).strip()
    chunks = Messenger._chunks(body)
    assert len(chunks) > 1
    assert all(len(chunk) <= Messenger.MAX_WHATSAPP_BODY for chunk in chunks)
    assert "".join(chunk.replace("\n", "") for chunk in chunks) == body.replace("\n", "")
