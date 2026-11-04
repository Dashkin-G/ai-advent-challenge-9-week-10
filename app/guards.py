"""Проверки шлюза. Input Guard ищет секреты в запросе, Output Guard — опасное в ответе модели."""
import base64
import binascii
import re
from urllib.parse import urlsplit

from app.defense import INVISIBLE

NAMES = {"API_KEY": "API-ключ", "SECRET": "пароль или секрет", "CARD": "номер карты", "EMAIL": "почта",
         "PHONE": "телефон", "URL": "подозрительная ссылка", "COMMAND": "опасная команда",
         "PROMPT": "системный промпт"}

# Форма секрета. В SECRET важно значение (группа 1): имя переменной оставляем, а без цифр это заглушка вроде your_key
SECRETS = {
    "API_KEY": re.compile(r"(?<![A-Za-z0-9])(?:sk-[\w-]{6,}|ghp_\w{6,})|(?<![A-Z0-9])AKIA[A-Z0-9]{12,}"),
    "SECRET": re.compile(r"(?:api[_-]?key|secret(?:[_-]?(?:access[_-]?)?key)?|token|токен|passw(?:or)?d|парол[ья])"
                         r"[\"']?\s*[:=]\s*[\"']?((?=[^\s\"',;]*\d)[^\s\"',;]{6,})", re.I),
    "CARD": re.compile(r"(?<!\d)(?:(?:\d{4}[ -]?){3}\d{4}|\d{4}[ -]?\d{6}[ -]?\d{5})(?!\d)"),
    "EMAIL": re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    "PHONE": re.compile(r"(?<![\d+])(?:\+\d{1,3}|[78])[ \-(]*\d{3}[ \-)]*\d{3}[ -]*\d{2}[ -]*\d{2}(?!\d)"),
}


def _unbase64(token):
    try:
        raw = token.replace("-", "+").replace("_", "/")
        return base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return ""


# Обёртки, в которые прячут секрет: разворачиваем и ищем внутри
WRAPPERS = [("Base64", re.compile(r"(?<![\w+/=-])[\w+/-]{16,}={0,2}(?![\w+/=-])", re.A), _unbase64),
            ("склейка", re.compile(r"""(?<![^\s"'+()\[\],;])["']?[^\s"'+()\[\],;]+["']?"""
                                   r"""(?:\s*\+\s*["']?[^\s"'+()\[\],;]+["']?)+"""),
             lambda chain: re.sub(r"[\s\"'+]", "", chain))]


def clean(text):
    """Без невидимых символов: иначе они разрывают ключ и форма не совпадает."""
    return INVISIBLE.sub("", text)


def _preview(secret):
    """Как записать находку в журнал: сам секрет туда писать нельзя."""
    return f"{secret[:3]}…{secret[-2:]}" if len(secret) > 10 else "…"


def _hit(kind, via, start, end, preview):
    return {"kind": kind, "name": NAMES[kind], "via": via, "start": start, "end": end, "preview": preview}


def _luhn(number):
    """Контрольная сумма карты: отличает номер карты от других 16 цифр."""
    digits = [int(d) for d in number if d.isdigit()][::-1]
    return sum(d if i % 2 == 0 else d * 2 - 9 * (d > 4) for i, d in enumerate(digits)) % 10 == 0


def _apart(hits):
    """Находки без пересечений, по порядку в тексте. Важнее та, что раньше в списке."""
    kept = []
    for h in hits:
        if all(h["end"] <= k["start"] or h["start"] >= k["end"] for k in kept):
            kept.append(h)
    return sorted(kept, key=lambda h: h["start"])


def find(text):
    """Секреты в тексте: сначала по форме, потом то же внутри обёрток."""
    hits = []
    for kind, rx in SECRETS.items():
        for m in rx.finditer(text):
            a, b = m.span(m.lastindex or 0)
            if kind != "CARD" or _luhn(text[a:b]):
                hits.append(_hit(kind, "regex", a, b, _preview(text[a:b])))
    for via, rx, unwrap in WRAPPERS:
        for m in rx.finditer(text):
            if inner := find(unwrap(m.group())):
                hits.append(_hit(inner[0]["kind"], via, *m.span(), inner[0]["preview"]))
    return _apart(hits)


def mask(text, hits):
    """sk-proj-abc123 → [REDACTED_API_KEY]."""
    for h in reversed(hits):
        text = f"{text[:h['start']]}[REDACTED_{h['kind']}]{text[h['end']:]}"
    return text


# Output Guard: системный промпт, ссылки, команды
LEAK_WORDS = 5  # столько слов подряд из системного промпта — уже утечка
HIDDEN = "⚠ Ответ скрыт: в нём были служебные инструкции."
URL = re.compile(r"https?://[^\s<>\"'`)\]]+", re.I)
IMAGE = re.compile(r"!\[[^\]]*\]\(\s*(https?://[^\s)]+\?[^\s)]+)")  # браузер сам отправит данные из адреса картинки
SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "clck.ru", "cutt.ly", "is.gd"}
PROGRAMS = (".exe", ".msi", ".bat", ".cmd", ".ps1", ".sh", ".scr", ".apk")
COMMAND = re.compile("|".join([r"\brm\s+-\w*(?:r\w*f|f\w*r)\w*",  # удалить всё
                               r"\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b",  # выполнить скачанное, не глядя
                               r"\|\s*iex\b|\binvoke-expression\b",  # то же в PowerShell
                               r"\bpowershell\b[^\n]*\s-e(?:nc|ncodedcommand)?\b",  # скрытая команда
                               r"\bmkfs\b|\bdd\s+if=|\bformat\s+c:",  # затереть диск
                               r"\bchmod\s+(?:-r\s+)?777\b",  # открыть всем
                               r"\bdrop\s+(?:table|database)\b",  # снести базу
                               r":\(\)\s*\{\s*:\|:&\s*\};:"]), re.I)  # fork-бомба: плодит процессы до зависания


def _words(text):
    """Слова без номеров: «1. Релизы… 2. Код-ревью…» — те же правила, только списком."""
    return [w for w in re.findall(r"\w+", text.lower()) if not w.isdigit()]


def _leaked(answer, system):
    said = f" {' '.join(_words(answer))} "
    words = _words(system)
    return any(f" {' '.join(words[i:i + LEAK_WORDS])} " in said for i in range(len(words) - LEAK_WORDS + 1))


def _commands(text):
    """Опасная команда скрывается целиком: до края строки или обратной кавычки."""
    hits = []
    for m in COMMAND.finditer(text):
        a = max(text.rfind("\n", 0, m.start()), text.rfind("`", 0, m.start())) + 1
        b = min([i for i in (text.find("\n", m.end()), text.find("`", m.end())) if i >= 0], default=len(text))
        line = text[a:b]
        hits.append(_hit("COMMAND", m.group().strip(), a, b, mask(line, find(line)).strip()[:60]))
    return hits


def _url_problem(url):
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return "битый адрес"
    if re.fullmatch(r"[\d.]+", host) and not host.startswith("127."):
        return "IP вместо домена"
    if host in SHORTENERS:
        return "сокращатель"
    return "скачивание программы" if parts.path.lower().endswith(PROGRAMS) else None


def _urls(text):
    hits = [_hit("URL", "данные в адресе картинки", *m.span(1), m.group(1).split("?")[0][:60])
            for m in IMAGE.finditer(text)]
    for m in URL.finditer(text):
        if why := _url_problem(m.group()):
            hits.append(_hit("URL", why, *m.span(), m.group().split("?")[0][:60]))
    return hits


def check_output(answer, system="", canary=""):
    """Что покажем пользователю и что поймали. Утёк промпт — скрываем весь ответ, остальное маскируем на месте."""
    answer = clean(answer)
    how = "метка" if canary and canary in answer else f"{LEAK_WORDS} слов подряд" if _leaked(answer, system) else None
    if how:
        return HIDDEN, [_hit("PROMPT", how, 0, len(answer), "")]
    hits = _apart(_commands(answer) + find(answer) + _urls(answer))
    return mask(answer, hits), hits
