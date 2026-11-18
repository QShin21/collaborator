"""The safety gate: look at what a model wrote before it reaches the user. Pure functions.

The supervising model reads a conversation that may contain anything — including text
that tries to steer whoever reads it. What it writes back (a finding, the question to
take to the main model) is read here against what was already in the conversation:

  drop     the answer is discarded — an address that came from nowhere, sending
           credentials somewhere, download-and-run, "ignore your instructions"
  caution  it is shown with a warning — asking for elevated rights, or something
           destructive with no preview / copy / backup / way back

`check(text, "", conversation)`: the first argument is what the model wrote, the last
what it was given. The word lists are data.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Tuple
from urllib.parse import unquote

OK, CAUTION, DROP = "ok", "caution", "drop"

_REST = r"[^\s\"'<>，。；）)」]*"
_TLDS = r"(?:com|net|org|io|cn|dev|app|xyz|top|info|ru|me|co|ai|sh|site|link)"
_ADDRESS = re.compile(
    r"(?i)\b(?:https?|ftp)://" + _REST +
    r"|\bwww\." + _REST +
    r"|\b[\w.+\-%]+@[\w\-]+(?:\.[\w\-]+)+" + _REST +
    r"|\b(?:\d{1,3}\.){3}\d{1,3}\b(?::\d+)?(?:[/?#]" + _REST + r")?"
    # A bare domain. The WHOLE dotted run is taken — "known.com.other.example" is one
    # host, and it is not "known.com".
    r"|\b(?:[a-z0-9\-]+\.)+" + _TLDS + r"\b(?:\.[a-z0-9\-]+)*\.?(?::\d+)?(?:[/?#@\\]" + _REST + r")?")
# Inside a path or query: something that would send the reader on to another place.
_EMBEDDED = re.compile(r"(?i)(?:https?|ftp):/{0,2}[^\s/?#&=]+|(?<![:/\w])//[^\s/?#&=]+|[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_EMBEDDED_BARE = re.compile(r"(?i)(?<![\w.\-])(?:[a-z0-9\-]+\.)+" + _TLDS + r"\b(?:\.[a-z0-9\-]+)*")
_DEFAULT_PORT = {"http": 80, "https": 443, "ftp": 21}
_HOST = re.compile(r"[a-z0-9\-]+(?:\.[a-z0-9\-]+)*|\[[0-9a-f:.]+\]")
_SEND = re.compile(r"(?i)上传|发送到|发送给|发到|提交到|传到|同步到|post\b|upload|send (?:it |them )?to|exfiltrat|webhook")
_SECRET = re.compile(r"(?i)密钥|令牌|密码|口令|cookie|\.env\b|环境变量|私钥|凭据|api[ _-]?key|token|password|credential|ssh key|id_rsa")
_PIPE_EXEC = re.compile(
    r"(?i)(?:curl|wget|iwr|invoke-webrequest|irm|invoke-restmethod)\b[^\n|]*\|\s*(?:sh|bash|zsh|python\d?|iex|invoke-expression|powershell|pwsh)\b"
    r"|base64\s+(?:-d|--decode)[^\n|]*\|\s*(?:sh|bash)"
    r"|下载[^。\n]{0,20}(?:并|后)?(?:直接)?(?:运行|执行)")
_OVERRIDE = re.compile(
    r"(?i)忽略(?:掉)?(?:之前|以上|上面|先前|所有|全部)[^。\n]{0,6}(?:指令|要求|规则|限制|提示)"
    r"|无视(?:之前|以上|所有)[^。\n]{0,6}(?:指令|要求|规则|限制)"
    r"|ignore (?:all |any |the )?(?:previous|prior|above|earlier)[^.\n]{0,20}(?:instructions|rules|prompts)"
    r"|disregard (?:all |the )?(?:previous|prior|above)")
_PRIVILEGE = re.compile(
    r"(?i)\bsudo\b|管理员(?:权限|身份)|注册表|regedit|关闭(?:杀毒|防火墙|安全|防护|defender)|禁用(?:杀毒|防火墙|安全|防护)"
    r"|--dangerously|--yolo|--no-verify|跳过(?:确认|沙箱|审批|校验)|绕过(?:确认|沙箱|限制|审批)|bypass|disable (?:the )?(?:sandbox|antivirus|firewall)")
_DESTRUCTIVE = re.compile(
    r"(?i)删除|删掉|清空|格式化|覆盖|强制推送|rm\s+-rf|\bdel\s+/|drop\s+table|reset\s+--hard|force[- ]push|\bdelete\b|\boverwrite\b|\bwipe\b")
_SAFEGUARD = re.compile(
    r"(?i)预览|副本|备份|恢复|撤销|先列出|清单|确认|dry[- ]?run|preview|backup|copy first|restore|undo|list (?:them|the files) first")


@dataclass(frozen=True)
class Finding:
    verdict: str = OK
    codes: Tuple[str, ...] = ()


def _unquote(text):
    """Percent-decoding, repeated: "%252F" hides a "%2F" hides a "/"."""
    for _ in range(3):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return text


def destination(token):
    """Where an address really leads: (kind, host, port, onward).

    kind    "mail" | "net" | None — None means it could not be read with certainty,
            and such an address is never taken as one that was already there
    host    lower-case, no trailing dot; for mail the whole address
    port    the explicit port, or the scheme's default; None for an address written
            without a scheme
    onward  addresses found inside its path, query or fragment (after decoding):
            where it would send the reader next

    Deliberately strict. Anything a browser and a careful reader could understand
    differently — a user-info part ("known.com@other"), an encoded or non-ASCII host,
    a backslash, a number in place of a name — is unreadable, not "probably fine"."""
    text = unicodedata.normalize("NFKC", token or "").strip().rstrip(".,;:!?")
    scheme, rest = "", text
    match = re.match(r"(?i)(https?|ftp)://", text)
    if match:
        scheme, rest = match.group(1).lower(), text[match.end():]
    cut = re.search(r"[/?#]", rest)
    authority, tail = (rest[:cut.start()], rest[cut.start():]) if cut else (rest, "")
    if not scheme and "@" in authority and "\\" not in authority and "%" not in authority:
        # A mail address — the same one only if it is the same in full.
        return ("mail", authority.casefold() + tail.casefold(), None, ())
    onward = _onward(tail)
    if not authority or "@" in authority or "\\" in text or "%" in authority or not authority.isascii():
        return (None, authority.casefold(), None, onward)
    host, port = authority.casefold(), None
    if host.startswith("["):
        host, _, after = host.partition("]")
        host, colon, digits = host + "]", after[:1], after[1:]
        if after and colon != ":":
            return (None, authority.casefold(), None, onward)
    else:
        host, colon, digits = host.partition(":")
    if colon:
        if not digits.isdigit() or not 0 < int(digits) < 65536:
            return (None, authority.casefold(), None, onward)
        port = int(digits)
    host = host.rstrip(".")
    if not _HOST.fullmatch(host):
        return (None, authority.casefold(), None, onward)
    labels = host.split(".")
    numeric = all(re.fullmatch(r"\d+|0x[0-9a-f]+", label) for label in labels)
    if numeric and not (len(labels) == 4 and all(label.isdigit() and str(int(label)) == label and int(label) < 256
                                                 for label in labels)):
        return (None, host, None, onward)           # 2130706433, 0x7f.1, 010.0.0.1: an address in disguise
    if port is None and scheme:
        port = _DEFAULT_PORT[scheme]
    return ("net", host, port, onward)


def _onward(tail):
    if not tail:
        return ()
    decoded = _unquote(tail).replace("\\", "/")
    found = [match.group(0) for match in _EMBEDDED.finditer(decoded)]
    cut = re.search(r"[?#]", decoded)
    if cut:                                          # a bare domain counts in the query, not in a file path
        found += [match.group(0) for match in _EMBEDDED_BARE.finditer(decoded[cut.start():])]
    return tuple(found)


def _same_place(address, known):
    """Is `address` (from the model) the destination of one the user already had?
    The host must be equal — not contained in, not ending with. The port must be
    equal too, except that an address written without a scheme stands for the
    ordinary web ports."""
    kind, host, port, _ = address
    if kind is None:
        return False
    for other_kind, other_host, other_port, _ in known:
        if other_kind != kind or other_host != host:
            continue
        if kind == "mail" or port == other_port:
            return True
        if port is None and other_port in (80, 443) or other_port is None and port in (80, 443):
            return True
    return False


def _addresses(text):
    return [destination(match.group(0)) for match in _ADDRESS.finditer(text or "")]


def _foreign(token, known, depth=0):
    address = destination(token)
    if not _same_place(address, known):
        return True
    # The place is known — but does it pass the reader on to somewhere that is not?
    return depth >= 3 or any(_foreign(inner.lstrip("/"), known, depth + 1) for inner in address[3])


def check(tail, draft="", chat=""):
    """Judge the model-written part of one route."""
    tail = tail or ""
    drops, cautions = [], []
    known = _addresses(draft) + _addresses(chat)
    if any(_foreign(match.group(0), known) for match in _ADDRESS.finditer(tail)):
        drops.append("foreign_address")
    for sentence in re.split(r"[。！？!?\n；;]", tail):
        if _SEND.search(sentence) and _SECRET.search(sentence):
            drops.append("exfiltration")
            break
    if _PIPE_EXEC.search(tail):
        drops.append("pipe_exec")
    if _OVERRIDE.search(tail):
        drops.append("override")
    if _PRIVILEGE.search(tail):
        cautions.append("privilege")
    if _DESTRUCTIVE.search(tail) and not _SAFEGUARD.search(tail):
        cautions.append("destructive")
    if drops:
        return Finding(DROP, tuple(drops))
    if cautions:
        return Finding(CAUTION, tuple(cautions))
    return Finding()
