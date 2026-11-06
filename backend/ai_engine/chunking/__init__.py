import hashlib
import re

TOKEN = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s]", re.UNICODE)


def split_blocks(blocks, size=600, overlap=80):
    """Deterministic conservative token proxy; locators refer to the parsed block.

    This is not a provider tokenizer. Provider limits need an additional safety margin.
    """
    if not 0 <= overlap < size:
        raise ValueError("overlap must be smaller than size")
    result = []
    for block_number, block in enumerate(blocks):
        text = block["text"]
        tokens = list(TOKEN.finditer(text))
        for start in range(0, len(tokens), size - overlap):
            end = min(start + size, len(tokens))
            left, right = tokens[start].start(), tokens[end - 1].end()
            content = text[left:right]
            result.append(
                {
                    "ordinal": len(result),
                    "content": content,
                    "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                    "token_count": end - start,
                    "locator": {
                        **block["locator"],
                        "block": block_number,
                        "char_start": left,
                        "char_end": right,
                    },
                }
            )
            if end == len(tokens):
                break
    return result
