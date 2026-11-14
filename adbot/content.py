"""Lossless Telegram content snapshots, strict input parsing and one-shot delivery.

All entity offsets are Telegram UTF-16 code units. Never recompute offsets from
Python string indices, nor retry an ambiguous network failure here.
"""
from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity
from telegram.error import BadRequest, TelegramError

_STYLES = {"p": "primary", "r": "danger", "g": "success"}
_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}\Z")
_EMOJI_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
# Source: https://core.telegram.org/bots/api#html-style (official 👍 example).
_OFFICIAL_EMOJI_EXAMPLE = "5368324170671202286"
_ICON_EMOJI = {
    "menu": ("🏠", "☰", "📋"), "chats": ("💬", "👥", "📣", "🤝", "📞"),
    "content": ("📝", "📄", "✏️", "✍️"), "tasks": ("📅", "⏰", "✅", "⏱"),
    "stats": ("📊", "📈", "📉"), "settings": ("⚙️", "🔧", "🛠️", "🔓"),
    "help": ("❓", "💡", "ℹ️"), "admin": ("👑", "🛡️", "👨‍💻", "👨‍🎓"),
    "broadcast": ("📢", "📣", "🔊", "👋"),
}


def _error(line: int, detail: str) -> ValueError:
    return ValueError(f"第{line}行：{detail}")


def _text(value: str, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(1, f"{what}不能为空")
    return value.strip()


def _bounded_decimal(value: str, maximum: int, detail: str) -> int:
    # Validate by length before int(): hostile input must not hit Python's
    # 4300-digit conversion limit and lose the user-visible source line.
    value = value.lstrip("0") or "0"
    bound = str(maximum)
    if len(value) > len(bound) or (len(value) == len(bound) and value > bound):
        raise _error(1, detail)
    return int(value)


def parse_interval(value: str) -> int:
    """Parse integer minutes or H:MM, returning seconds (60..INT32_MAX)."""
    value = _text(value, "间隔")
    if re.fullmatch(r"[0-9]+", value):
        seconds = _bounded_decimal(value, 2_147_483_647 // 60, "间隔超出允许范围") * 60
    elif match := re.fullmatch(r"([0-9]+):([0-5][0-9])", value):
        hours = _bounded_decimal(match[1], 2_147_483_647 // 3600, "间隔超出允许范围")
        seconds = (hours * 60 + int(match[2])) * 60
    else:
        raise _error(1, "间隔须为整数分钟或 H:MM（分钟00至59）")
    if not 60 <= seconds <= 2_147_483_647:
        raise _error(1, "间隔必须至少60秒且不超过2147483647秒")
    return seconds


def _telegram_path(value: str) -> list[str]:
    candidate = value if "://" in value else "https://" + value
    try:
        parsed = urlsplit(candidate)
        if (parsed.scheme not in {"http", "https"}
                or parsed.netloc.lower() not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}
                or any(c.isspace() or ord(c) < 32 for c in value)):
            raise ValueError
    except ValueError:
        raise _error(1, "须使用合法的 t.me 链接") from None
    return parsed.path.strip("/").split("/")


def parse_chat_ref(value: str) -> int | str:
    value = _text(value, "目标")
    if re.fullmatch(r"-?[0-9]+", value):
        magnitude = _bounded_decimal(value.lstrip("-"), 2**52, "目标数字ID无效")
        if magnitude == 0:
            raise _error(1, "目标数字ID无效")
        return -magnitude if value.startswith("-") else magnitude
    if value.startswith("@"):
        username = value[1:]
    elif "/" in value:
        path = _telegram_path(value)
        if len(path) != 1:
            raise _error(1, "目标不能使用邀请链接或消息链接")
        username = path[0]
    else:
        raise _error(1, "目标须为数字ID、@用户名或 t.me/用户名")
    if not _USERNAME.fullmatch(username) or username.lower() in {"joinchat", "c", "s", "addstickers", "addemoji", "share", "proxy"}:
        raise _error(1, "目标用户名无效，不接受邀请链接")
    return "@" + username


def parse_source_link(value: str) -> tuple[int | str, int]:
    path = _telegram_path(_text(value, "来源链接"))
    if path[0] == "s":
        path = path[1:]
    if path and path[0] == "c":
        if len(path) not in {3, 4} or not re.fullmatch(r"[1-9][0-9]*", path[1]):
            raise _error(1, "私密来源链接须为 t.me/c/频道ID/消息ID")
        chat: int | str = -_bounded_decimal("100" + path[1], 2**52, "来源ID超出 Telegram 范围")
        message_parts = path[2:]
    else:
        if len(path) not in {2, 3} or not _USERNAME.fullmatch(path[0]) or path[0].lower() in {"joinchat", "addemoji", "addstickers", "share"}:
            raise _error(1, "公开来源链接须为 t.me/用户名/消息ID")
        chat = "@" + path[0]
        message_parts = path[1:]
    if any(not re.fullmatch(r"[1-9][0-9]*", part) for part in message_parts):
        raise _error(1, "来源消息ID或话题ID须为正整数")
    parsed_parts = [_bounded_decimal(part, 2_147_483_647, "来源ID超出 Telegram 范围") for part in message_parts]
    message_id = parsed_parts[-1]
    return chat, message_id


def _validate_url(url: str, line: int) -> str:
    if (not isinstance(url, str) or not url
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url)
            or "\\" in url or re.search(r"%(?![0-9A-Fa-f]{2})", url)):
        raise _error(line, "URL无效：不得为空或包含空白、控制字符、反斜杠及错误转义")
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https", "tg"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError
        _ = parsed.port  # raises for malformed/out-of-range ports
        host = parsed.hostname.encode("idna").decode("ascii")
        if parsed.scheme.lower() == "tg":
            if not re.fullmatch(r"[A-Za-z_]+", host):
                raise ValueError
        elif ":" not in host and any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label) for label in host.rstrip(".").split(".")):
            raise ValueError
    except (ValueError, UnicodeError):
        raise _error(line, "URL无效：仅支持完整的 http://、https:// 或 tg:// 链接") from None
    return url


def _icon(value: object, line: int) -> str:
    if not isinstance(value, str) or not _EMOJI_ID.fullmatch(value):
        raise _error(line, "custom emoji ID须为正整数字符串")
    return value


def _entity_dicts(entities: Sequence | None) -> list[dict]:
    return [copy.deepcopy(entity.to_dict() if hasattr(entity, "to_dict") else dict(entity)) for entity in entities or ()]


def _validated_entities(text: str, entities: Sequence | None) -> list[dict]:
    result = _entity_dicts(entities)
    boundaries = {0}
    units = 0
    for char in text:
        units += len(char.encode("utf-16-le")) // 2
        boundaries.add(units)
    for entity in result:
        offset, length = entity.get("offset"), entity.get("length")
        if (type(offset) is not int or type(length) is not int or length <= 0
                or offset not in boundaries or offset + length not in boundaries):
            raise _error(1, "实体UTF-16范围无效或截断了代理对")
        if entity.get("type") == "custom_emoji":
            _icon(entity.get("custom_emoji_id"), 1)
    return result


def _validated_rows(rows: Sequence | None) -> list[list[dict]]:
    if rows is None:
        return []
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise _error(1, "按钮须为二维列表")
    result: list[list[dict]] = []
    total = 0
    for line, row in enumerate(rows, 1):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or not 1 <= len(row) <= 10:
            raise _error(line, "每行按钮须为1至10个")
        target = []
        for raw in row:
            button = raw.to_dict() if hasattr(raw, "to_dict") else raw
            if not isinstance(button, Mapping) or set(button) - {"text", "url", "style", "icon_custom_emoji_id"}:
                raise _error(line, "广告只允许URL按钮，不可继承callback或其他动作")
            label = button.get("text")
            if not isinstance(label, str) or not label.strip() or len(label) > 64 or any(ord(c) < 32 for c in label):
                raise _error(line, "按钮文字须为1至64个字符且无控制字符")
            clean = {"text": label, "url": _validate_url(button.get("url"), line)}
            if button.get("style") is not None:
                if button["style"] not in _STYLES.values():
                    raise _error(line, "按钮style只允许primary、danger、success")
                clean["style"] = button["style"]
            if button.get("icon_custom_emoji_id") is not None:
                clean["icon_custom_emoji_id"] = _icon(button["icon_custom_emoji_id"], line)
            target.append(clean)
        total += len(target)
        if total > 100:
            raise _error(line, "按钮总数不得超过100")
        result.append(target)
    return result


def parse_buttons(text: str, entities: list[dict] | None = None) -> list[list[dict]]:
    """Parse '#p Label - URL && #g Label - URL'; spaces around && are optional.

    An unspaced && inside a URL query is literal; use ' && ' between buttons
    when a URL itself contains &&. Custom emoji entities must be wholly inside
    the label; their fallback glyph remains visible in the label.
    """
    if not isinstance(text, str):
        raise _error(1, "按钮输入须为文字")
    if not text.strip():
        return []
    entities = _validated_entities(text, entities)
    custom = [e for e in entities if e.get("type") == "custom_emoji"]
    used: set[int] = set()
    rows = []
    position = 0
    for line_no, full_line in enumerate(text.splitlines(keepends=True), 1):
        line = full_line.rstrip("\r\n")
        if not line.strip():
            position += len(full_line)
            continue
        # A separator is && followed by another label - URL. Query && remains
        # literal when it is not followed by a valid second-button shape.
        separators = list(re.finditer(r"\s*&&\s*(?=(?:#[prg]\s+)?(?:(?!&&)[^\r\n])*?\s+-\s+(?:https?://|tg://))", line))
        starts = [0] + [m.end() for m in separators]
        ends = [m.start() for m in separators] + [len(line)]
        row = []
        for start, end in zip(starts, ends):
            piece = line[start:end]
            match = re.fullmatch(r"\s*(?:(#[prg])\s+)?(.+?)\s+-\s+(\S+)\s*", piece)
            if not match:
                raise _error(line_no, "按钮格式须为 [#p/#r/#g] 文字 - URL，同行用 && 分隔")
            label = match[2]
            label_start = position + start + match.start(2)
            label_end = position + start + match.end(2)
            utf_start = len(text[:label_start].encode("utf-16-le")) // 2
            utf_end = len(text[:label_end].encode("utf-16-le")) // 2
            ids = []
            for index, entity in enumerate(custom):
                a, b = entity["offset"], entity["offset"] + entity["length"]
                if a < utf_end and b > utf_start:
                    if a < utf_start or b > utf_end:
                        raise _error(line_no, "custom emoji实体跨越按钮文字边界")
                    ids.append(entity["custom_emoji_id"])
                    used.add(index)
            explicit = re.findall(r"\{emoji:([^{}]*)\}", label)
            for emoji_id in explicit:
                ids.append(_icon(emoji_id, line_no))
            label = re.sub(r"\{emoji:[^{}]*\}", "", label).strip()
            if "{emoji:" in label:
                raise _error(line_no, "显式表情格式须为 {emoji:ID}")
            if len(ids) > 1:
                raise _error(line_no, "每个按钮最多一个自定义表情图标")
            button = {"text": label, "url": _validate_url(match[3], line_no)}
            if match[1]:
                button["style"] = _STYLES[match[1][1]]
            if ids:
                button["icon_custom_emoji_id"] = ids[0]
            row.append(button)
        # Validate here so blank lines don't change the reported source line.
        try:
            _validated_rows([row])
        except ValueError as exc:
            raise _error(line_no, str(exc).split("：", 1)[-1]) from None
        rows.append(row)
        if sum(map(len, rows)) > 100:
            raise _error(line_no, "按钮总数不得超过100")
        position += len(full_line)
    if len(used) != len(custom):
        unused = next(e for i, e in enumerate(custom) if i not in used)
        prefix = text.encode("utf-16-le")[:unused["offset"] * 2].decode("utf-16-le")
        raise _error(prefix.count("\n") + 1, "custom emoji须位于按钮文字中")
    return rows


def build_markup(rows) -> InlineKeyboardMarkup | None:
    validated = _validated_rows(rows)
    if not validated:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(**button) for button in row] for row in validated])


def menu_button(text, callback_data=None, url=None, style=None, icon_id=None) -> InlineKeyboardButton:
    if not isinstance(text, str) or not text.strip() or len(text) > 64:
        raise _error(1, "菜单按钮文字须为1至64个字符")
    if (callback_data is None) == (url is None):
        raise _error(1, "菜单按钮须且只能指定callback_data或URL")
    if callback_data is not None and (not isinstance(callback_data, str) or not 1 <= len(callback_data.encode()) <= 64):
        raise _error(1, "callback_data须为1至64字节")
    if url is not None:
        _validate_url(url, 1)
    if style is not None and style not in _STYLES.values():
        raise _error(1, "按钮style只允许primary、danger、success")
    return InlineKeyboardButton(text, callback_data=callback_data, url=url, style=style,
                                icon_custom_emoji_id=_icon(icon_id, 1) if icon_id is not None else None)


def capture_message(message: Message) -> dict:
    kind = "text"
    file_id = None
    # animation is also exposed as document by Telegram; check it first.
    for candidate in ("animation", "photo", "video", "document"):
        media = getattr(message, candidate, None)
        if media:
            kind = candidate
            file_id = media[-1].file_id if candidate == "photo" else media.file_id
            break
    if kind == "text":
        if message.text is None:
            raise _error(1, "仅支持文字、图片、视频、动画和文档")
        text, entities = message.text, message.entities
    else:
        text, entities = message.caption or "", message.caption_entities
    markup = message.reply_markup
    return {"type": kind, "text": text, "entities": _validated_entities(text, entities),
            "file_id": file_id,
            "buttons": _validated_rows(markup.inline_keyboard if markup else []),
            "protect_content": bool(message.has_protected_content)}


def strip_custom_emoji(payload: dict) -> dict:
    """Deep-copy, removing only custom-emoji formatting, retaining fallback text."""
    result = copy.deepcopy(payload)
    for key in ("entities", "caption_entities"):
        if key in result:
            result[key] = [entity for entity in result[key] if entity.get("type") != "custom_emoji"]
    for row in result.get("buttons") or []:
        for button in row:
            button.pop("icon_custom_emoji_id", None)
    mode = result.get("parse_mode")
    for key in ("text", "caption"):
        if not isinstance(result.get(key), str):
            continue
        if mode == "HTML":
            result[key] = re.sub(r"</?tg-emoji\b[^>]*>", "", result[key], flags=re.IGNORECASE)
        elif mode == "MarkdownV2":
            result[key] = re.sub(r"!\[((?:\\.|[^\]\\])*)\]\(tg://emoji\?id=[0-9]+\)", lambda m: m[1], result[key])
    return result


def _emoji_qualification_error(exc: BadRequest) -> bool:
    # Do not include malformed-ID/entity errors or generic ENTITY_BOUNDS_INVALID.
    error = re.sub(r"[\s_-]+", " ", str(exc).lower()).strip()
    return error in {
        "custom emoji usage is not allowed", "custom emoji are not allowed",
        "custom emoji is not allowed", "custom emoji not allowed",
        "not enough rights to use custom emoji", "premium account required",
        "premium subscription required", "bot must have a premium subscription",
        "custom emoji can be used only by bots that purchased additional usernames on fragment",
    } or bool(re.fullmatch(r"(?:the )?bot (?:is not allowed|has no rights) to (?:use|send) custom emoji", error))


def _has_custom_emoji(payload: dict) -> bool:
    return strip_custom_emoji(payload) != payload


def _message_link(message, chat_id: int | str, message_id: int) -> str | None:
    if isinstance(message, Message):
        return message.link
    if isinstance(chat_id, str) and chat_id.startswith("@"):
        return f"https://t.me/{chat_id[1:]}/{message_id}"
    if isinstance(chat_id, int) and str(chat_id).startswith("-100"):
        return f"https://t.me/c/{str(chat_id)[4:]}/{message_id}"
    return None


async def _send(bot: Bot, chat_id: int, payload: dict):
    kind = payload.get("type")
    common = {"chat_id": chat_id, "protect_content": bool(payload.get("protect_content", False))}
    if kind in {"copy", "forward"}:
        source = payload.get("source_chat_id")
        message_id = payload.get("source_message_id")
        if (not isinstance(source, (str, int)) or isinstance(source, bool)
                or type(message_id) is not int or message_id <= 0):
            raise _error(1, "来源须指定source_chat_id及正整数source_message_id")
        if kind == "forward" and payload.get("buttons"):
            raise _error(1, "forward模式不支持自定义按钮，请明确选择copy")
        common.update(from_chat_id=source, message_id=message_id)
        if kind == "copy":
            common["reply_markup"] = build_markup(payload.get("buttons"))
            return await bot.copy_message(**common)
        return await bot.forward_message(**common)
    if kind not in {"text", "photo", "video", "animation", "document"}:
        raise _error(1, "不支持的内容type")
    text = payload.get("text", "")
    if not isinstance(text, str) or (kind == "text" and not text):
        raise _error(1, "正文须为文字且纯文字内容不得为空")
    common["reply_markup"] = build_markup(payload.get("buttons"))
    mode = payload.get("parse_mode")
    if mode not in {None, "", "HTML", "MarkdownV2"}:
        raise _error(1, "parse_mode只支持HTML或MarkdownV2")
    if mode and payload.get("entities"):
        raise _error(1, "parse_mode与entities不可同时使用，请明确选择原文或格式模式")
    if mode:
        common["parse_mode"] = mode
    else:
        common["entities" if kind == "text" else "caption_entities"] = [
            MessageEntity.de_json(entity, bot) for entity in _validated_entities(text, payload.get("entities"))
        ]
    if kind == "text":
        return await bot.send_message(text=text, **common)
    file_id = payload.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        raise _error(1, "媒体缺少file_id")
    return await getattr(bot, f"send_{kind}")(**{kind: file_id, "caption": text}, **common)


async def deliver(bot: Bot, chat_id: int, payload: dict, *, allow_fallback: bool = True) -> dict:
    degraded = False
    try:
        message = await _send(bot, chat_id, payload)
    except BadRequest as exc:
        # BadRequest is a definitive rejection; network errors are deliberately
        # not caught. Forward and a source copy's body cannot be transformed.
        if (not allow_fallback or not _emoji_qualification_error(exc)
                or payload.get("type") == "forward" or not _has_custom_emoji(payload)):
            raise
        stripped = strip_custom_emoji(payload)
        message = await _send(bot, chat_id, stripped)
        degraded = True
    message_id = message.message_id
    return {"message_id": message_id, "degraded": degraded,
            "link": _message_link(message, chat_id, message_id)}


async def discover_icons(bot) -> dict[str, str]:
    """Discover from Telegram's documented example, never guessed sticker IDs.

    Resolve the verified example's set, then independently verify its emoji IDs
    with getCustomEmojiStickers (max 200/request). Unavailable categories are
    absent; callers must retain their ordinary Unicode icon for those keys.
    This function makes no requests until explicitly called with a real bot.
    """
    try:
        seed = await bot.get_custom_emoji_stickers(custom_emoji_ids=[_OFFICIAL_EMOJI_EXAMPLE])
        seed = [s for s in seed if s.custom_emoji_id == _OFFICIAL_EMOJI_EXAMPLE and s.type == "custom_emoji"]
        if not seed:
            return {}
        verified = list(seed)
        set_names = {s.set_name for s in seed if s.set_name}
        for name in sorted(set_names):
            sticker_set = await bot.get_sticker_set(name=name)
            if sticker_set.sticker_type != "custom_emoji":
                continue
            ids = list(dict.fromkeys(s.custom_emoji_id for s in sticker_set.stickers if s.custom_emoji_id))
            for start in range(0, len(ids), 200):
                requested = ids[start:start + 200]
                checked = await bot.get_custom_emoji_stickers(custom_emoji_ids=requested)
                verified.extend(s for s in checked if s.type == "custom_emoji" and s.custom_emoji_id in requested)
        result = {}
        for key, candidates in _ICON_EMOJI.items():
            normalized = {emoji.replace("\ufe0f", "") for emoji in candidates}
            found = next((s for s in verified if s.emoji and s.emoji.replace("\ufe0f", "") in normalized), None)
            if found:
                result[key] = found.custom_emoji_id
        return result
    except TelegramError:
        # Icon discovery is optional startup enrichment, never prevents startup.
        return {}
