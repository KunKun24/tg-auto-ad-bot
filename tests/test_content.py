"""Content boundary tests; all Telegram calls are offline and mocked."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Message, MessageEntity
from telegram.error import BadRequest, NetworkError, TimedOut

from adbot.content import (
    build_markup, capture_message, deliver, discover_icons, menu_button,
    parse_buttons, parse_chat_ref, parse_interval, parse_source_link, strip_custom_emoji,
)


def run(awaitable):
    return asyncio.run(awaitable)


def message(**values):
    return Message.de_json({"message_id": 9, "date": 1,
                            "chat": {"id": -100123, "type": "channel", "title": "Test"},
                            **values}, None)


def u16(text):
    return len(text.encode("utf-16-le")) // 2


def emoji_entity(text, glyph, emoji_id="5368324170671202286"):
    start = text.index(glyph)
    return {"type": "custom_emoji", "offset": u16(text[:start]),
            "length": u16(glyph), "custom_emoji_id": emoji_id}


def fake_bot(result=None, error=None):
    bot = SimpleNamespace()
    for method in ("send_message", "send_photo", "send_video", "send_animation", "send_document", "copy_message", "forward_message"):
        setattr(bot, method, AsyncMock(return_value=result or message(text="sent"), side_effect=error))
    return bot


@pytest.mark.parametrize(("value", "expected"), [("50", 3000), ("1", 60), (" 1:30 ", 5400), ("0:01", 60), ("00:59", 3540), ("24:00", 86400)])
def test_interval(value, expected):
    assert parse_interval(value) == expected


@pytest.mark.parametrize("value", ["", "0", "0:00", "-1", "1.5", "1:60", "1:2", "1h", "1:20:30", "1000000000000000000000", "1\n2"])
def test_invalid_interval(value):
    with pytest.raises(ValueError, match="第1行"):
        parse_interval(value)


@pytest.mark.parametrize(("value", "expected"), [("-10012345", -10012345), ("123", 123), ("@news", "@news"), ("https://t.me/news/?x=1", "@news"), ("t.me/MyNews", "@MyNews"), ("https://telegram.me/news", "@news")])
def test_chat_ref(value, expected):
    assert parse_chat_ref(value) == expected


@pytest.mark.parametrize("value", ["0", "@", "news", "https://evil.test/news", "https://t.me/+invite", "t.me/joinchat/invite", "https://t.me/news/123", "https://t.me/c/123/4", "https://t.me@evil.test/news", "https://t.me:443/news", "https://t.me/a b", "@joinchat", "9007199254740992"])
def test_invalid_chat_ref(value):
    with pytest.raises(ValueError, match="第1行"):
        parse_chat_ref(value)


@pytest.mark.parametrize(("value", "expected"), [("https://t.me/news/12?single#x", ("@news", 12)), ("t.me/c/12345/9?thread=8", (-10012345, 9)), ("https://t.me/s/news/123", ("@news", 123)), ("t.me/news/40/41", ("@news", 41)), ("t.me/c/123/40/41", (-100123, 41))])
def test_source_links(value, expected):
    assert parse_source_link(value) == expected


@pytest.mark.parametrize("value", ["t.me/news", "t.me/+invite", "t.me/news/0", "t.me/c/123/no", "https://evil.test/news/1", "t.me/news/1/a", "t.me/joinchat/123", "t.me/news/2147483648"])
def test_invalid_source(value):
    with pytest.raises(ValueError, match="第1行"):
        parse_source_link(value)


def test_buttons_styles_urls_and_splits():
    rows = parse_buttons("#p 购-买 - https://example.org/a-b?x=1&&y=2&&#r 删除 - tg://resolve?domain=Test\n# g - http://example.org")
    assert [len(row) for row in rows] == [2, 1]
    assert rows[0][0] == {"text": "购-买", "url": "https://example.org/a-b?x=1&&y=2", "style": "primary"}
    assert rows[0][1]["style"] == "danger"
    assert rows[1][0]["text"] == "# g"
    assert parse_buttons("#g 好 - https://example.org")[0][0]["style"] == "success"
    assert parse_buttons("") == []
    assert build_markup([]) is None


def test_buttons_utf16_multiple_rows_crlf():
    glyph1, glyph2 = "👩🏽‍💻", "🇨🇳"
    text = f"#p {glyph1} 编程 - https://example.org && {glyph2} 国旗 - https://example.org/2\r\n❤️ 帮助 - tg://resolve?domain=help"
    entities = [emoji_entity(text, glyph1), emoji_entity(text, glyph2, "123"), emoji_entity(text, "❤️", "456")]
    rows = parse_buttons(text, entities)
    assert rows[0][0]["icon_custom_emoji_id"] == "5368324170671202286"
    assert rows[0][0]["text"] == f"{glyph1} 编程"
    assert rows[0][1]["icon_custom_emoji_id"] == "123"
    assert rows[1][0]["icon_custom_emoji_id"] == "456"
    assert build_markup(rows).to_dict()["inline_keyboard"] == rows


def test_explicit_icon():
    assert parse_buttons("{emoji:123456} 帮助 - https://example.org")[0][0] == {
        "text": "帮助", "url": "https://example.org", "icon_custom_emoji_id": "123456"}


@pytest.mark.parametrize("line", ["No URL", "x - javascript:alert(1)", "x - https://", "x - https://evil.test/%zz", "x - https://user:password@evil.test", "x - https://evil.test:99999", "x - https://evil.test\\x", "x - https://evil..test", "x - ftp://example.org", "{emoji:no} x - https://example.org", "{emoji:1 x - https://example.org", "{emoji:1}{emoji:2} x - https://example.org", " - https://example.org", "a" * 65 + " - https://example.org"])
def test_button_line_errors(line):
    with pytest.raises(ValueError, match="第3行"):
        parse_buttons("ok - https://example.org\n\n" + line)


def test_keyboard_limits():
    with pytest.raises(ValueError, match="第1行.*10"):
        parse_buttons(" && ".join(["x - https://example.org"] * 11))
    with pytest.raises(ValueError, match="第11行.*100"):
        parse_buttons("\n".join([" && ".join(["x - https://example.org"] * 10)] * 11))
    assert sum(map(len, parse_buttons("\n".join([" && ".join(["x - https://example.org"] * 10)] * 10)))) == 100


def test_entities_may_not_split_surrogate_pairs_or_leave_labels():
    text = "👍 ok - https://example.org"
    with pytest.raises(ValueError, match="UTF-16"):
        parse_buttons(text, [{"type": "custom_emoji", "offset": 1, "length": 1, "custom_emoji_id": "123"}])
    with pytest.raises(ValueError, match="第1行.*按钮文字"):
        parse_buttons(text, [{"type": "custom_emoji", "offset": u16("👍 ok - "), "length": 1, "custom_emoji_id": "123"}])


@pytest.mark.parametrize("kind", ["text", "photo", "video", "animation", "document"])
def test_capture_and_delivery_media_entities(kind):
    text = "A👨‍👩‍👧‍👦B🇨🇳❤️"
    entities = [{"type": "bold", "offset": 0, "length": u16(text)}, emoji_entity(text, "👨‍👩‍👧‍👦")]
    raw = {"has_protected_content": True, "reply_markup": {"inline_keyboard": [[{
        "text": "去看看", "url": "https://example.org", "style": "success", "icon_custom_emoji_id": "123"}]]}}
    if kind == "text":
        raw.update(text=text, entities=entities)
    else:
        raw.update(caption=text, caption_entities=entities)
        if kind == "photo":
            raw[kind] = [{"file_id": "small", "file_unique_id": "s", "width": 10, "height": 10},
                         {"file_id": "large", "file_unique_id": "l", "width": 100, "height": 100}]
        else:
            raw[kind] = {"file_id": "media", "file_unique_id": "m", "width": 100, "height": 100, "duration": 2}
        if kind == "animation":
            raw["document"] = {"file_id": "also_document", "file_unique_id": "d"}
    payload = capture_message(message(**raw))
    assert payload["type"] == kind
    assert payload["text"] == text and payload["entities"] == entities
    assert payload["protect_content"] is True
    assert payload["buttons"][0][0]["style"] == "success"
    bot = fake_bot()
    result = run(deliver(bot, -100123, payload))
    kwargs = getattr(bot, f"send_{'message' if kind == 'text' else kind}").call_args.kwargs
    assert kwargs["protect_content"] is True
    assert [e.to_dict() for e in kwargs["entities" if kind == "text" else "caption_entities"]] == entities
    assert "parse_mode" not in kwargs
    if kind != "text":
        assert kwargs[kind] == ("large" if kind == "photo" else "media")
        assert kwargs["caption"] == text
    assert result == {"message_id": 9, "degraded": False, "link": "https://t.me/c/123/9"}


@pytest.mark.parametrize("unsafe", [{"text": "X", "callback_data": "bad"}, {"text": "X", "url": "https://example.org", "style": "purple"}, {"text": "X", "url": "javascript:alert(1)"}, {"text": "X", "url": "https://example.org", "pay": True}, {"text": "X", "url": "https://example.org", "icon_custom_emoji_id": "bad"}])
def test_capture_rejects_unsafe_markup(unsafe):
    with pytest.raises(ValueError, match="第1行"):
        capture_message(message(text="正文", reply_markup={"inline_keyboard": [[unsafe]]}))


def test_capture_unsupported():
    with pytest.raises(ValueError, match="仅支持"):
        capture_message(message(sticker={"file_id": "s", "file_unique_id": "s", "type": "regular", "width": 100, "height": 100, "is_animated": False, "is_video": False}))


@pytest.mark.parametrize("kind", ["text", "photo", "video", "animation", "document"])
@pytest.mark.parametrize("mode", ["HTML", "MarkdownV2"])
def test_parse_mode_exclusive(kind, mode):
    payload = {"type": kind, "text": "<b>test</b>" if mode == "HTML" else "*test*", "parse_mode": mode, "file_id": "file"}
    bot = fake_bot()
    run(deliver(bot, 1, payload))
    kwargs = getattr(bot, f"send_{'message' if kind == 'text' else kind}").call_args.kwargs
    assert kwargs["parse_mode"] == mode
    assert "entities" not in kwargs and "caption_entities" not in kwargs
    payload["entities"] = [{"type": "bold", "offset": 0, "length": 1}]
    with pytest.raises(ValueError, match="不可同时"):
        run(deliver(bot, 1, payload))


def custom_payload(kind="text"):
    return {"type": kind, "text": "👍 hello", "file_id": "file", "entities": [
        {"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "123"},
        {"type": "bold", "offset": 3, "length": 5}],
        "buttons": [[{"text": "👍 button", "url": "https://example.org", "style": "primary", "icon_custom_emoji_id": "123"}]], "protect_content": True}


def test_strip_is_lossless_and_does_not_mutate():
    payload = custom_payload()
    original = deepcopy(payload)
    stripped = strip_custom_emoji(payload)
    assert payload == original
    assert stripped["text"] == payload["text"]
    assert stripped["entities"] == [payload["entities"][1]]
    assert stripped["buttons"][0][0] == {"text": "👍 button", "url": "https://example.org", "style": "primary"}
    assert stripped["protect_content"] is True


@pytest.mark.parametrize(("mode", "text", "expected"), [("HTML", '<b><tg-emoji emoji-id="123">👍</tg-emoji> hi</b>', "<b>👍 hi</b>"), ("MarkdownV2", "*![👍](tg://emoji?id=123) hi*", "*👍 hi*")])
def test_strip_parse_mode(mode, text, expected):
    assert strip_custom_emoji({"text": text, "parse_mode": mode}) == {"text": expected, "parse_mode": mode}


@pytest.mark.parametrize("kind", ["text", "photo", "video", "animation", "document"])
@pytest.mark.parametrize("error", ["CUSTOM_EMOJI_NOT_ALLOWED", "Custom emoji usage is not allowed", "Premium account required", "Not enough rights to use custom emoji"])
def test_only_explicit_qualification_error_retries_once(kind, error):
    payload = custom_payload(kind)
    before = deepcopy(payload)
    bot = fake_bot()
    method = getattr(bot, f"send_{'message' if kind == 'text' else kind}")
    method.side_effect = [BadRequest(error), message(text="ok")]
    assert run(deliver(bot, -100123, payload))["degraded"] is True
    assert method.await_count == 2
    second = method.call_args_list[1].kwargs
    assert all(e.type != "custom_emoji" for e in second["entities" if kind == "text" else "caption_entities"])
    assert "icon_custom_emoji_id" not in second["reply_markup"].to_dict()["inline_keyboard"][0][0]
    assert second["protect_content"] is True
    assert payload == before


@pytest.mark.parametrize("error", [TimedOut(), NetworkError("connection reset"), BadRequest("CUSTOM_EMOJI_ID_INVALID"), BadRequest("Can't parse entities"), BadRequest("CHAT_WRITE_FORBIDDEN"), BadRequest("message is too long"), BadRequest("not enough rights to send messages"), BadRequest("custom emoji offset invalid"), BadRequest("premium account required for another feature")])
def test_uncertain_or_unrelated_errors_never_retry(error):
    bot = fake_bot(error=error)
    with pytest.raises(type(error)):
        run(deliver(bot, 123, custom_payload()))
    assert bot.send_message.await_count == 1


def test_fallback_disabled_or_no_emoji_never_retries():
    for payload, allowed in [(custom_payload(), False), ({"type": "text", "text": "plain"}, True)]:
        bot = fake_bot(error=BadRequest("CUSTOM_EMOJI_NOT_ALLOWED"))
        with pytest.raises(BadRequest):
            run(deliver(bot, 1, payload, allow_fallback=allowed))
        assert bot.send_message.await_count == 1


def test_second_error_does_not_trigger_third_send():
    bot = fake_bot(error=BadRequest("CUSTOM_EMOJI_NOT_ALLOWED"))
    with pytest.raises(BadRequest):
        run(deliver(bot, 1, custom_payload()))
    assert bot.send_message.await_count == 2


@pytest.mark.parametrize("kind", ["copy", "forward"])
def test_source_modes_exactly_preserved(kind):
    bot = fake_bot(result=SimpleNamespace(message_id=80))
    payload = {"type": kind, "source_chat_id": "@source", "source_message_id": 55, "protect_content": True}
    if kind == "copy":
        payload["buttons"] = [[{"text": "X", "url": "https://example.org"}]]
    result = run(deliver(bot, -100333, payload))
    expected = {"chat_id": -100333, "from_chat_id": "@source", "message_id": 55, "protect_content": True}
    if kind == "copy":
        expected["reply_markup"] = build_markup(payload["buttons"])
    getattr(bot, f"{kind}_message").assert_awaited_once_with(**expected)
    assert result == {"message_id": 80, "degraded": False, "link": "https://t.me/c/333/80"}
    for other in ("copy_message", "forward_message", "send_message"):
        if other != f"{kind}_message":
            getattr(bot, other).assert_not_awaited()


def test_forward_does_not_accept_buttons_or_switch_to_copy():
    bot = fake_bot()
    payload = {"type": "forward", "source_chat_id": -100123, "source_message_id": 1, "buttons": custom_payload()["buttons"]}
    with pytest.raises(ValueError, match="forward模式"):
        run(deliver(bot, 1, payload))
    bot.forward_message.assert_not_awaited()
    bot.copy_message.assert_not_awaited()
    payload.pop("buttons")
    bot.forward_message.side_effect = BadRequest("CUSTOM_EMOJI_NOT_ALLOWED")
    with pytest.raises(BadRequest):
        run(deliver(bot, 1, payload))
    assert bot.forward_message.await_count == 1
    bot.copy_message.assert_not_awaited()


def test_copy_icon_fallback_stays_copy():
    bot = fake_bot()
    bot.copy_message.side_effect = [BadRequest("CUSTOM_EMOJI_NOT_ALLOWED"), SimpleNamespace(message_id=20)]
    payload = {"type": "copy", "source_chat_id": -100123, "source_message_id": 1, "buttons": custom_payload()["buttons"]}
    assert run(deliver(bot, 1, payload))["degraded"] is True
    assert bot.copy_message.await_count == 2
    bot.send_message.assert_not_awaited()
    bot.forward_message.assert_not_awaited()


def test_menu_button_native_fields_and_validation():
    button = menu_button("菜单", callback_data="menu", style="primary", icon_id="123")
    assert button.to_dict() == {"text": "菜单", "callback_data": "menu", "style": "primary", "icon_custom_emoji_id": "123"}
    for kwargs in ({}, {"callback_data": "x", "url": "https://example.org"}, {"callback_data": "中" * 22}, {"url": "javascript:bad"}, {"callback_data": "x", "icon_id": "fake"}):
        with pytest.raises(ValueError, match="第1行"):
            menu_button("菜单", **kwargs)


def sticker(emoji_id, emoji, name="OfficialSet"):
    return SimpleNamespace(custom_emoji_id=emoji_id, emoji=emoji, set_name=name, type="custom_emoji")


def test_icon_discovery_only_returns_verified_ids():
    seed = sticker("5368324170671202286", "👍")
    settings, chats, fake = sticker("100", "⚙️"), sticker("101", "💬"), sticker("999", "📊")
    bot = SimpleNamespace(
        get_custom_emoji_stickers=AsyncMock(side_effect=[[seed], [settings, chats]]),
        get_sticker_set=AsyncMock(return_value=SimpleNamespace(sticker_type="custom_emoji", stickers=[seed, settings, chats, fake])))
    result = run(discover_icons(bot))
    assert result == {"chats": "101", "settings": "100"}
    assert "999" not in result.values()
    assert bot.get_custom_emoji_stickers.call_args_list[0].kwargs == {"custom_emoji_ids": ["5368324170671202286"]}
    bot.get_sticker_set.assert_awaited_once_with(name="OfficialSet")


def test_icon_discovery_unavailable_and_wrong_seed():
    bot = SimpleNamespace(get_custom_emoji_stickers=AsyncMock(side_effect=NetworkError("offline")))
    assert run(discover_icons(bot)) == {}
    bot.get_custom_emoji_stickers = AsyncMock(return_value=[sticker("999", "⚙️")])
    assert run(discover_icons(bot)) == {}


def test_long_numeric_input_has_a_friendly_line_error():
    digits = "9" * 5000
    for parse, value in ((parse_interval, digits), (parse_interval, digits + ":30"),
                         (parse_chat_ref, digits), (parse_source_link, "t.me/c/" + digits + "/1"),
                         (parse_source_link, "t.me/news/" + digits),
                         (parse_source_link, "t.me/news/2147483648/1")):
        with pytest.raises(ValueError, match="第1行"):
            parse(value)
    assert parse_interval("0" * 5000 + "1") == 60
    assert parse_chat_ref("-" + "0" * 5000 + "123") == -123


def test_button_separator_accepts_single_ampersand_in_label():
    rows = parse_buttons("A - https://example.org?x=1&&y=2 && #g R&D - https://example.org/2")
    assert rows[0][0]["url"] == "https://example.org?x=1&&y=2"
    assert rows[0][1]["text"] == "R&D"


def test_icon_discovery_does_not_match_partial_zwj_emoji():
    seed = sticker("5368324170671202286", "👍")
    person = sticker("123", "👨")
    bot = SimpleNamespace(
        get_custom_emoji_stickers=AsyncMock(side_effect=[[seed], [person]]),
        get_sticker_set=AsyncMock(return_value=SimpleNamespace(sticker_type="custom_emoji", stickers=[person])))
    assert run(discover_icons(bot)) == {}
