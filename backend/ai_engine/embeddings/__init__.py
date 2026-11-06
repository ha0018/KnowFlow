import hashlib
import math
import re

import jieba
from django.conf import settings

STOPWORDS = {
    "的",
    "了",
    "吗",
    "呢",
    "是",
    "在",
    "我",
    "你",
    "和",
    "与",
    "请",
    "什么",
    "怎么",
    "如何",
    "可以",
    "一个",
}


def tokenize(text):
    return [
        token.lower()
        for token in jieba.cut_for_search(text)
        if re.search(r"[\w\u3400-\u9fff]", token) and token.lower() not in STOPWORDS
    ]


def demo_embed(text):
    vector = [0.0] * settings.EMBEDDING_DIMENSIONS
    for token in tokenize(text):
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % len(vector)
        vector[index] += 1.0 if digest[4] % 2 else -1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1
    return [v / norm for v in vector]


def validate_vectors(vectors, count):
    if len(vectors) != count:
        raise ValueError("EMBEDDING_COUNT_MISMATCH")
    for vector in vectors:
        if len(vector) != settings.EMBEDDING_DIMENSIONS or not all(
            type(v) in {int, float} and math.isfinite(v) and abs(v) <= 3.402823e38 for v in vector
        ):
            raise ValueError("INVALID_EMBEDDING")
        if not any(vector):
            raise ValueError("ZERO_EMBEDDING")
    return vectors
