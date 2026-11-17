"""Chunking, shared by the providers that cap request size."""

CHUNK_TARGET = 3500  # characters per TTS request


def _hard_split(s, target):
    """Cut one over-long string into <= target pieces, preferring a space.

    A paragraph with no sentence break (a run-on, a wall of dialogue, a block
    copied out of a chat log) is one "sentence" to the sentence splitter and
    used to go out as a single request of any size. A 206k-character chapter
    that failed repeatedly on one chapter on 2026-11-17 is the reason this
    exists: the cap has to hold on the request, not on the average request.
    """
    out = []
    s = s or ""
    while len(s) > target:
        cut = s.rfind(" ", 0, target)
        if cut < target // 2:
            cut = target
        out.append(s[:cut])
        s = s[cut:].lstrip()
    if s:
        out.append(s)
    return out


def chunk_text(text, target=CHUNK_TARGET):
    """Split on sentence/paragraph edges so each request stays under the cap."""
    import re
    parts, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > target and cur:
            parts.append(cur.strip())
            cur = ""
        if len(para) > target:
            sents = re.split(r"(?<=[.!?])\s+", para)
            for s in sents:
                for piece in _hard_split(s, target):
                    if len(cur) + len(piece) + 1 > target and cur:
                        parts.append(cur.strip())
                        cur = ""
                    cur += piece + " "
        else:
            cur += para + "\n\n"
    if cur.strip():
        parts.append(cur.strip())
    # An empty chapter still answers with one (empty) chunk, which is the
    # contract every caller reads; the splitter never hands back a blank.
    parts = [p for p in parts if p]
    return parts or [text]


def has_speech(text):
    """True when a chunk carries something an engine can actually say.

    A scene break ("~*~*~*~*~*~", "***", "* * *") is punctuation and symbols
    only. A strict speech server (Kokoro) answers 400 "input contains no
    speakable text" instead of returning silence, and one 400 used to fail a
    whole chapter - a 42k-char chapter of a real book sat unvoiced because of an
    eleven-character line between scenes.
    """
    return any(ch.isalnum() for ch in (text or ""))


def parts_for_request(text, target=CHUNK_TARGET):
    """What one synth() should actually send, and nothing else.

    The host already splits a chapter into request-sized chunks, so text at or
    under the cap is one request, exactly as handed over. Re-splitting it can
    only move the host's boundary: a pronunciation rule that lengthens a chunk
    by two characters was enough to push a trailing scene break ("~*~*~*~*~*~")
    into a request of its own, and a strict server answers that with 400 input
    contains no speakable text - which failed the whole chapter.

    Over the cap, split as before. Either way, pieces with nothing to say are
    not requests.
    """
    parts = chunk_text(text, target) if len(text or "") > target else [text]
    return [p for p in parts if has_speech(p)]
