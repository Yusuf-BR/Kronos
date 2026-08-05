import re

STOPWORDS = {"of", "the", "and", "for", "in", "on", "a", "an", "to", "with"}


def extract_parenthetical(name: str) -> tuple[str, str | None]:
    """
    'Machine Learning (ML)' -> ('Machine Learning', 'ML')
    'Machine Learning' -> ('Machine Learning', None)

    A parenthetical abbreviation in the source text is a DECLARED alias —
    ground truth from the document itself, not an inference. It should be
    committed immediately, not left to embedding/fuzzy/referee matching.
    """
    m = re.match(r'^(.*?)\s*\(([A-Za-z0-9][A-Za-z0-9.\-]{0,7})\)\s*$', name.strip())
    if m:
        base = m.group(1).strip()
        abbrev = m.group(2).strip()
        if base:
            return base, abbrev
    return name.strip(), None


def initials(name: str) -> str:
    """'Machine Learning' -> 'ML'. Skips stopwords so 'Bureau of Labor
    Statistics' -> 'BLS', not 'BOLS'."""
    words = [w for w in re.split(r'[\s\-]+', name.strip()) if w]
    letters = []
    for w in words:
        clean = re.sub(r'[^A-Za-z]', '', w)
        if not clean:
            continue
        if clean.lower() in STOPWORDS:
            continue
        letters.append(clean[0].upper())
    return "".join(letters)


def is_acronym_token(s: str) -> bool:
    s = s.strip().replace(".", "")
    return 2 <= len(s) <= 6 and s.isalpha() and s.isupper()


def is_acronym_match(a: str, b: str) -> bool:
    """
    True if one name is plausibly an acronym/abbreviation of the other —
    structurally independent of embedding cosine similarity or edit
    distance, both of which are unreliable for short-acronym-vs-full-
    phrase pairs. 'ML' vs 'Machine Learning' has LOW cosine similarity
    (general embedding models aren't trained on acronym expansion) AND
    HIGH edit distance (totally different string lengths) despite
    meaning exactly the same thing — neither existing check can catch it,
    by construction, no matter how thresholds are tuned.
    """
    a_base, a_paren = extract_parenthetical(a)
    b_base, b_paren = extract_parenthetical(b)

    if a_paren and a_paren.lower() == b.strip().lower():
        return True
    if b_paren and b_paren.lower() == a.strip().lower():
        return True
    if a_paren and a_paren.lower() == b_base.lower():
        return True
    if b_paren and b_paren.lower() == a_base.lower():
        return True

    if a_base.lower() == b_base.lower():
        return True

    if is_acronym_token(a) and initials(b_base) == a.replace(".", "").upper():
        return True
    if is_acronym_token(b) and initials(a_base) == b.replace(".", "").upper():
        return True

    return False