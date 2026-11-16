"""Conservative prose selection and word-preserving target presentation."""
import re


def prose_shape(text: str) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    if re.search(r'https?://|www\.|\b\w+://|\b(?:mailto|data|file):|```|Traceback|\b\w*(?:Error|Exception):', text):
        return False
    if re.search(r'(?:[/\\]\w|\w[/\\])|\b[\w-]+\.[A-Za-z]{1,8}\b|(?:^|\s)\.[A-Za-z][\w.-]*', text):
        return False
    if re.search(r'^\s*[*\-#>]', text, re.M) or re.search(r'^\s*File ".*", line \d+', text, re.M):
        return False
    if any(ord(c) < 32 and c not in '\n\r\t' for c in text):
        return False
    return (sum(c.isalpha() or c.isspace() for c in text) / len(text) >= .85
            and sum(c.isascii() for c in text) / len(text) >= .95)


def is_prose(text: str) -> bool:
    return prose_shape(text) and 3 <= len(text.split()) <= 60


def tidy_target(text: str) -> str:
    text = ' '.join(text.split())
    text = re.sub(r"\bi(?=\b|['’](?:m|ve|ll|d)\b)", 'I', text)
    text = re.sub(r'''(^["'“‘(\[]*|[.!?]["'”’)]*\s+["'“‘(\[]*)([a-z])''',
                  lambda m: m[1] + m[2].upper(), text)
    if text and text[-1] not in '.?!':
        text += '.'
    return text
